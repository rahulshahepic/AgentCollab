"""Configuration and owner authentication for the private Railway pilot."""

import re
from base64 import b64decode
from binascii import Error
from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from cryptography.fernet import Fernet
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from .accounts import SESSION_COOKIE, TOKEN, return_path

STAGING_WORKSPACE = "railway-staging"
RAILWAY_HEALTH_HOST = "healthcheck.railway.app"
DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DNS_HOST = re.compile(rf"(?:{DNS_LABEL}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?")
PUBLIC_PATHS = {
    "/health", "/mcp", "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server", "/register", "/token", "/revoke",
}


@dataclass(frozen=True)
class StagingConfig:
    origin: str
    port: int
    database: Path
    owner_password_hash: str = field(repr=False)
    owner_setup_code: str = field(default="", repr=False)
    owner_setup_expires: int = 0
    local_accounts: bool = False
    event_key: str = field(default="", repr=False)
    event_discovery_only: bool = False
    trust_railway_http_edge: bool = False

    @property
    def host(self):
        return self.origin.removeprefix("https://")

    def browser_peer(self, request):
        peer = request.client.host if request.client and request.client.host else "unknown"
        if self.trust_railway_http_edge:
            values = request.headers.getlist("x-real-ip")
            if len(values) == 1:
                try:
                    return str(ip_address(values[0]))
                except ValueError:
                    pass
        return peer

    @classmethod
    def from_environment(cls, env):
        origin = env.get("AGENTCOLLAB_STAGING_ORIGIN")
        if origin is None:
            domain = env.get("RAILWAY_PUBLIC_DOMAIN", "")
            if not re.fullmatch(rf"{DNS_LABEL}\.up\.railway\.app", domain):
                raise ValueError("A generated Railway HTTPS domain is required.")
            origin = f"https://{domain}"
        else:
            host = origin.removeprefix("https://")
            if (not origin.startswith("https://") or len(host) > 253
                    or not DNS_HOST.fullmatch(host) or host == RAILWAY_HEALTH_HOST):
                raise ValueError("AGENTCOLLAB_STAGING_ORIGIN must be an HTTPS origin with an exact DNS hostname.")
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
        code = env.get("AGENTCOLLAB_OWNER_SETUP_CODE", "")
        expires = env.get("AGENTCOLLAB_OWNER_SETUP_EXPIRES_AT", "")
        if (code or expires) and (not TOKEN.fullmatch(code) or not re.fullmatch(r"[0-9]{1,12}", expires)):
            raise ValueError("Configure both the generated owner setup code and its Unix expiration.")
        local_accounts = env.get("AGENTCOLLAB_LOCAL_ACCOUNTS") == "1"
        if not local_accounts and not code and not re.fullmatch(r"[a-f0-9]{64}", password_hash):
            raise ValueError("Configure the SHA-256 hash of a randomly generated owner password.")
        event_key = env.get("AGENTCOLLAB_EVENT_ENCRYPTION_KEY", "")
        event_discovery_only = env.get("AGENTCOLLAB_EVENT_DISCOVERY_ONLY") == "1"
        if event_key:
            try:
                Fernet(event_key)
            except (ValueError, Error) as exc:
                raise ValueError("Configure a Fernet key for MCP Events signing secrets.") from exc
        return cls(origin, port, volume / "agentcollab-staging.sqlite3", password_hash,
                   code, int(expires or 0), local_accounts, event_key, event_discovery_only,
                   env.get("AGENTCOLLAB_TRUST_RAILWAY_HTTP_EDGE") == "1")

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

    def __init__(self, app, config, accounts):
        self.app = app
        self.config = config
        self.accounts = accounts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        headers = Headers(scope=scope)
        if headers.get("host", "").split(":")[0] == RAILWAY_HEALTH_HOST and path != "/health":
            response = PlainTextResponse("Not found", status_code=404)
        elif path == "/token" and scope["method"] == "POST":
            await self.exchange(scope, headers, receive, send)
            return
        elif path in PUBLIC_PATHS or (self.accounts and path in {"/login", "/setup", "/static/app.css"}):
            await self.app(scope, receive, send)
            return
        elif self.accounts:
            request = Request(scope)
            if await run_in_threadpool(self.accounts.valid, request.cookies.get(SESSION_COOKIE, "")):
                await self.app(scope, receive, send)
                return
            if scope["method"] in {"GET", "HEAD"}:
                destination = return_path(request.url.path + ("?" + request.url.query if request.url.query else ""))
                response = RedirectResponse("/login?" + urlencode({"next": destination}), status_code=303,
                                            headers={"Cache-Control": "no-store"})
            else:
                response = PlainTextResponse("Sign in first", status_code=401, headers={"Cache-Control": "no-store"})
        elif self.config.allows(headers.get("authorization", "")):
            await self.app(scope, receive, send)
            return
        else:
            response = PlainTextResponse("Owner sign-in required", status_code=401, headers={
                "WWW-Authenticate": 'Basic realm="AgentCollab staging"',
                "Cache-Control": "no-store",
            })
        await response(scope, receive, send)

    async def exchange(self, scope, headers, receive, send):
        # The current MCP SDK does not validate RFC 8707's token-request resource.
        content = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            content.extend(event.get("body", b""))
            if len(content) > 8192:
                response = PlainTextResponse("Request too large", status_code=413)
                await response(scope, receive, send)
                return
            if not event.get("more_body"):
                break
        try:
            fields = parse_qs(content.decode(), keep_blank_values=True, max_num_fields=16)
        except ValueError:
            fields = {}
        content_type = headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded" or fields.get("resource") != [
            f"{self.config.origin}/mcp"
        ]:
            response = JSONResponse({"error": "invalid_target"}, status_code=400, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return

        async def replay():
            return {"type": "http.request", "body": bytes(content), "more_body": False}

        await self.app(scope, replay, send)
