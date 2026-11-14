"""Start and verify the production Compose stack on an isolated CI runner."""

import http.cookiejar
import json
import secrets
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


def verify(ca_file, password):
    context = ssl.create_default_context(cafile=ca_file)
    cookies = http.cookiejar.CookieJar()
    opener = build_opener(HTTPSHandler(context=context), HTTPCookieProcessor(cookies))

    def call(path, method="GET", value=None, origin="https://localhost"):
        headers = {"Origin": origin, "Content-Type": "application/json"}
        body = json.dumps(value).encode() if value is not None else None
        request = Request("https://localhost" + path, data=body, headers=headers, method=method)
        try:
            with opener.open(request, timeout=10) as reply:
                return reply.status, json.load(reply)
        except HTTPError as exc:
            with exc:
                return exc.code, None

    for attempt in range(12):
        try:
            code, _ = call("/api/products")
            if code == 401:
                break
        except OSError:
            if attempt == 11:
                raise
        time.sleep(1)
    else:
        raise RuntimeError("HTTPS endpoint did not become ready")
    assert call("/health/ready")[0] == 404
    credentials = {"email": "owner@example.test", "password": password}
    assert call("/api/auth/login", "POST", credentials, "https://other.example.test")[0] == 403
    assert call("/api/auth/login", "POST", credentials)[0] == 200
    assert any(c.secure and c.has_nonstandard_attr("HttpOnly") for c in cookies)
    assert call("/api/products") == (200, [])
    code, product = call("/api/products", "POST", {"sku": "CI-1", "name": "CI sample"})
    assert code == 201 and product["revision"] == 1
    assert call("/api/products/" + product["id"])[1]["sku"] == "CI-1"
    for _ in range(12):
        code, status = call("/api/operations")
        assert code == 200 and status["mode"] == "production"
        if status["worker"]["healthy"]:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Worker did not record a heartbeat")
    assert call("/api/auth/logout", "POST")[0] == 200
    assert call("/api/products")[0] == 401
    print(
        "Production Compose smoke passed: HTTPS, secure login, PostgreSQL writes, worker heartbeat."
    )


def main():
    with tempfile.TemporaryDirectory(prefix="launch-studio-ci-") as temp:
        path = Path(temp)
        env_file = path / "studio.env"
        db_password = secrets.token_hex(24)
        owner_password = secrets.token_hex(24)
        environment = {
            "STUDIO_DOMAIN": "localhost",
            "PUBLIC_ORIGIN": "https://localhost",
            "ALLOWED_HOSTS": "localhost,127.0.0.1",
            "POSTGRES_PASSWORD": secrets.token_hex(24),
            "STUDIO_DATABASE_PASSWORD": db_password,
            "DATABASE_URL": f"postgresql://studio:{db_password}@postgres:5432/studio",
            "BOOTSTRAP_EMAIL": "owner@example.test",
            "BOOTSTRAP_PASSWORD": owner_password,
            "LLM_BASE_URL": "https://unused-provider.example.test/v1",
            "LLM_API_KEY": "ci-test-placeholder",
        }
        env_file.write_text(
            "\n".join(f"{key}={value}" for key, value in environment.items()), encoding="utf-8"
        )

        def compose(*args, **kwargs):
            return subprocess.run(
                ["docker", "compose", "--env-file", str(env_file), "-p", "launch-studio-ci", *args],
                cwd=ROOT,
                check=True,
                **kwargs,
            )

        try:
            compose("config", "--quiet")
            compose("up", "-d", "--build", "--wait", "--wait-timeout", "120")
            ca_file = path / "ca.crt"
            with ca_file.open("wb") as output:
                compose(
                    "exec",
                    "-T",
                    "proxy",
                    "cat",
                    "/data/caddy/pki/authorities/local/root.crt",
                    stdout=output,
                )
            verify(ca_file, owner_password)
        except Exception:
            compose("logs", "--tail=70")
            raise
        finally:
            compose("down", "-v")


if __name__ == "__main__":
    main()
