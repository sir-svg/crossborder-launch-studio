"""Bounded provider calls and their per-task operational metadata."""

import asyncio
import json
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROMPT_VERSION = "listing-v2"
MAX_REQUEST_BYTES = 300_000
MAX_RESPONSE_BYTES = 256_000
RETRYABLE = {429, 502, 503, 504}
_TRACE: ContextVar[list | None] = ContextVar("model_trace", default=None)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_path: str = "data/studio.db"
    database_url: SecretStr = SecretStr("")
    auth_enabled: bool = False
    bootstrap_email: str = ""
    bootstrap_password: SecretStr = SecretStr("")
    bootstrap_workspace: str = "上新工作区"
    public_origin: str = ""
    allowed_hosts: str = "127.0.0.1,localhost,testserver"
    embedded_worker: bool = True
    app_environment: str = "development"
    worker_concurrency: int = Field(default=1, ge=1, le=8)
    allow_demo: bool = True
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "deepseek-v4.1-flash-1017"
    llm_timeout_seconds: float = Field(default=40, ge=3, le=60)
    llm_attempts: int = Field(default=2, ge=1, le=3)


class ModelFailure(Exception):
    def __init__(self, message: str, code: str = "invalid_reply"):
        self.code = code
        super().__init__(message)


def start_trace(calls):
    return _TRACE.set(calls)


def end_trace(token):
    _TRACE.reset(token)


class Model:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.base_url = settings.llm_base_url.strip().rstrip("/")
        self.transport = transport

    @property
    def enabled(self):
        return bool(self.settings.llm_api_key.get_secret_value().strip())

    def validate_configuration(self):
        if not self.enabled:
            raise ModelFailure("请配置模型密钥后再执行。", "missing_key")
        try:
            parts = urlsplit(self.base_url)
            valid = (
                parts.scheme == "https"
                and parts.hostname
                and not parts.username
                and not parts.password
                and not parts.query
                and not parts.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ModelFailure("请填写服务商提供的 HTTPS 兼容地址。", "invalid_url")
        if not self.settings.llm_model.strip():
            raise ModelFailure("请设置模型名称。", "missing_model")
        return parts

    async def generate(self, prompt, data, schema):
        parts = self.validate_configuration()
        payload = {
            "model": self.settings.llm_model,
            "temperature": 0.2,
            "max_tokens": 3000,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": prompt
                    + "\n只返回 JSON。结构如下："
                    + json.dumps(schema.model_json_schema(), ensure_ascii=False),
                },
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
        }
        if parts.hostname.endswith(".maas.aliyuncs.com"):
            payload["enable_thinking"] = False
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ModelFailure("本次模型输入过大，请减少资料。", "body_limit")

        trace = _TRACE.get()
        entry = {"purpose": schema.__name__, "status": "started", "attempts": 0}
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.settings.llm_timeout_seconds, connect=8),
                    transport=self.transport,
                ) as client:
                    answer = await self._request(client, body, schema, entry)
            entry["status"] = "ok"
            return answer
        except (TimeoutError, httpx.TimeoutException):
            entry["status"] = "timeout"
            raise ModelFailure("模型连接超时，可重新运行。", "timeout") from None
        except httpx.HTTPError:
            entry["status"] = "connection"
            raise ModelFailure("模型连接失败，可重新运行。", "connection") from None
        except ModelFailure as exc:
            entry["status"] = exc.code
            raise
        except (ValueError, KeyError, TypeError, IndexError):
            entry["status"] = "invalid_reply"
            raise ModelFailure("模型返回格式有误，可重新运行。") from None
        except asyncio.CancelledError:
            entry["status"] = "cancelled"
            raise
        finally:
            entry["duration_ms"] = round((time.monotonic() - started) * 1000)
            if trace is not None:
                trace.append(entry)

    async def _request(self, client, body, schema, entry):
        attempts = self.settings.llm_attempts
        headers = {
            "Authorization": "Bearer " + self.settings.llm_api_key.get_secret_value().strip(),
            "Content-Type": "application/json",
        }
        for attempt in range(1, attempts + 1):
            entry["attempts"] = attempt
            async with client.stream(
                "POST", self.base_url + "/chat/completions", headers=headers, content=body
            ) as response:
                code = response.status_code
                entry["provider_status"] = code
                entry.pop("request_id", None)
                request_id = response.headers.get("x-request-id", "")
                if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", request_id):
                    entry["request_id"] = request_id
                if code in RETRYABLE and attempt < attempts:
                    retry_after = response.headers.get("retry-after", "")
                else:
                    self._check_status(code)
                    result = await self._bounded_json(response)
                    self._record_usage(entry, result.get("usage"))
                    message = result["choices"][0]["message"]
                    return schema.model_validate_json(message["content"])
            delay = self._delay(retry_after, attempt)
            if delay is None:
                raise ModelFailure("模型服务要求稍后重试。", "provider_capacity")
            await asyncio.sleep(delay)
        raise ModelFailure("模型服务暂不可用。", "provider_capacity")

    @staticmethod
    async def _bounded_json(response):
        raw = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=16_384):
            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ModelFailure("模型回复超过大小限制。", "response_limit")
            raw.extend(chunk)
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ModelFailure("模型返回格式有误，可重新运行。")
        return result

    @staticmethod
    def _record_usage(entry, usage):
        if not isinstance(usage, dict):
            return
        counts = {
            key: usage[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(usage.get(key)) is int and 0 <= usage[key] <= 10_000_000
        }
        if counts:
            entry["usage"] = counts

    @staticmethod
    def _delay(retry_after, attempt):
        default = 0.5 * attempt
        if not retry_after:
            return default
        if re.fullmatch(r"\d{1,6}(?:\.\d{1,3})?", retry_after):
            delay = float(retry_after)
        else:
            try:
                retry_date = parsedate_to_datetime(retry_after)
                delay = (retry_date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        return max(default, delay) if delay <= 4 else None

    @staticmethod
    def _check_status(code):
        if 200 <= code < 300:
            return
        if code in (401, 403):
            raise ModelFailure("模型密钥或权限有误，请检查配置。", "provider_auth")
        if code == 429:
            raise ModelFailure("模型请求过多，稍后可以重试。", "provider_capacity")
        if code >= 500:
            raise ModelFailure("模型服务暂不可用，可重新运行。", "provider_unavailable")
        raise ModelFailure("模型服务拒绝请求，请检查地址和模型配置。", "provider_rejected")
