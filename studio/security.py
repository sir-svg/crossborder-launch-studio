"""Password hashing, browser request checks, and session token digests."""

import base64
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

PASSWORD_ROUNDS = 600_000
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
    return "$".join(
        (
            "pbkdf2-sha256",
            str(PASSWORD_ROUNDS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password, encoded):
    try:
        scheme, rounds_value, salt_value, digest_value = encoded.split("$")
        rounds = int(rounds_value)
        if scheme != "pbkdf2-sha256" or not 100_000 <= rounds <= 2_000_000:
            return False
        salt = base64.b64decode(salt_value, validate=True)
        expected = base64.b64decode(digest_value, validate=True)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    except (UnicodeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalized_origin(raw):
    try:
        origin = urlsplit(raw)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            return None
        return (
            origin.scheme,
            origin.hostname.lower(),
            origin.port or (443 if origin.scheme == "https" else 80),
        )
    except ValueError:
        return None


class OriginGuardMiddleware:
    """Check Origin/Fetch Metadata for browser writes; permit same-origin and CLI requests."""

    def __init__(self, app, allowed_origins=()):
        self.app = app
        self.origins = {normalized_origin(value) for value in allowed_origins}
        if None in self.origins:
            raise ValueError("APP_ORIGINS must contain valid HTTP or HTTPS origins")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        site = headers.get("sec-fetch-site")
        supplied = headers.getlist("origin")
        origins = self.origins or {
            normalized_origin(scope["scheme"] + "://" + headers.get("host", ""))
        }
        if (
            site in {"same-site", "cross-site"}
            or len(supplied) > 1
            or supplied
            and (
                normalized_origin(supplied[0]) is None
                or normalized_origin(supplied[0]) not in origins
            )
        ):
            response = JSONResponse({"detail": "请求来源与工作台地址不一致。"}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
