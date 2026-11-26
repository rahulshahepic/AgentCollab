"""Configuration and owner authentication for the private Railway pilot."""

import re
from base64 import b64decode
from binascii import Error
from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

STAGING_WORKSPACE = "railway-staging"
RAILWAY_HEALTH_HOST = "healthcheck.railway.app"


@dataclass(frozen=True)
class StagingConfig:
    origin: str
    port: int
    database: Path
    owner_password_hash: str = field(repr=False)

    @property
    def host(self):
        return self.origin.removeprefix("https://")

    @classmethod
    def from_environment(cls, env):
        domain = env.get("RAILWAY_PUBLIC_DOMAIN", "")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.up\.railway\.app", domain):
            raise ValueError("A generated Railway HTTPS domain is required.")
        try:
            port = int(env.get("PORT", ""))
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            raise ValueError("Railway must provide a valid PORT.")
        volume = Path(env.get("RAILWAY_VOLUME_MOUNT_PATH", ""))
        if not volume.is_absolute() or volume == Path("/") or not volume.is_dir():
            raise ValueError("Attach a persistent Railway volume before starting staging.")
        if env.get("RAILWAY_ENVIRONMENT_NAME") != "staging":
            raise ValueError("This entry point requires the Railway staging environment.")
        password_hash = env.get("AGENTCOLLAB_OWNER_PASSWORD_SHA256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", password_hash):
            raise ValueError("Configure the SHA-256 hash of a randomly generated owner password.")
        return cls(f"https://{domain}", port, volume / "agentcollab-staging.sqlite3", password_hash)

    def allows(self, authorization):
        scheme, _, encoded = authorization.partition(" ")
        if scheme.lower() != "basic" or len(encoded) > 256:
            return False
        try:
            username, _, password = b64decode(encoded, validate=True).partition(b":")
        except (ValueError, Error):
            return False
        valid_password = compare_digest(sha256(password).hexdigest(), self.owner_password_hash)
        return username == b"owner" and len(password) >= 32 and valid_password


class StagingBoundary:
    """Require owner auth on web routes and leave MCP auth to the MCP SDK."""

    def __init__(self, app, config):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        headers = Headers(scope=scope)
        if headers.get("host", "").split(":")[0] == RAILWAY_HEALTH_HOST and path != "/health":
            response = PlainTextResponse("Not found", status_code=404)
        elif path in {"/health", "/mcp"}:
            await self.app(scope, receive, send)
            return
        elif self.config.allows(headers.get("authorization", "")):
            await self.app(scope, receive, send)
            return
        else:
            response = PlainTextResponse("Owner sign-in required", status_code=401, headers={
                "WWW-Authenticate": 'Basic realm="AgentCollab staging"',
                "Cache-Control": "no-store",
            })
        await response(scope, receive, send)
