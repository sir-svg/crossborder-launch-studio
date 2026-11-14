"""Server-side client for compatible chat completion providers."""

import json
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_path: str = "data/studio.db"
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "deepseek-v4.1-flash-1017"


class ModelFailure(Exception):
    pass


class Model:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.llm_base_url.rstrip("/")

    @property
    def enabled(self):
        return bool(self.settings.llm_api_key.get_secret_value())

    async def generate(self, prompt, data, schema):
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ModelFailure("请检查模型地址，填入提供方的 HTTPS 兼容地址。")
        payload = {
            "model": self.settings.llm_model,
            "temperature": 0.2,
            "max_tokens": 2400,
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
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10)) as client:
                reply = await client.post(
                    self.base_url + "/chat/completions",
                    headers={
                        "Authorization": "Bearer " + self.settings.llm_api_key.get_secret_value()
                    },
                    json=payload,
                )
                reply.raise_for_status()
                raw = reply.json()["choices"][0]["message"]["content"]
                return schema.model_validate_json(raw)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise ModelFailure("模型密钥或权限有误，请检查后重试。") from None
            if code == 429:
                raise ModelFailure("模型请求过多，稍后可以重试。") from None
            raise ModelFailure("模型服务没有成功返回，请检查配置后重试。") from None
        except httpx.HTTPError:
            raise ModelFailure("模型连接失败或超时，可以重试。") from None
        except (ValidationError, ValueError, TypeError, KeyError, IndexError):
            raise ModelFailure("模型返回的草稿格式有误，可以重试。") from None
