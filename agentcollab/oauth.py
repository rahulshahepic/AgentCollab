"""Persistent owner-approved grants for the MCP SDK's OAuth server."""

import json
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hmac import compare_digest, digest as hmac_digest
from secrets import token_bytes, token_urlsafe
from time import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .staging import STAGING_WORKSPACE
from .store import StoreError

SCOPE = "message"
ACCESS_SECONDS = 15 * 60
GRANT_SECONDS = 30 * 24 * 60 * 60
CHATGPT_REDIRECT = re.compile(
    r"https://chatgpt\.com/(?:connector_platform_oauth_redirect|connector/oauth/[A-Za-z0-9_-]{1,128})"
)
EXACT_REDIRECT_PROVIDERS = {
    "https://agent.meta.ai/api/hatch/oauth/callback": "muse",
    "https://claude.ai/api/mcp/auth_callback": "claude",
}


def redirect_provider(uri):
    if CHATGPT_REDIRECT.fullmatch(uri):
        return "chatgpt"
    return EXACT_REDIRECT_PROVIDERS.get(uri)


class AgentRefresh(RefreshToken):
    agent_id: str


class PendingConsent(BaseModel):
    client_id: str
    params: AuthorizationParams


class StagingOAuth:
    def __init__(self, store, resource):
        self.store = store
        self.resource = resource
        with store.session(write=True) as db:
            db.execute("INSERT OR IGNORE INTO oauth_issuer (id, signing_key) VALUES (1, ?)", (token_bytes(32),))
            self.signing_key = db.execute("SELECT signing_key FROM oauth_issuer WHERE id = 1").fetchone()[0]

    def sign(self, purpose, value):
        message = f"{self.resource} {purpose} {value}".encode()
        return urlsafe_b64encode(hmac_digest(self.signing_key, message, "sha256")).decode().rstrip("=")

    async def get_client(self, client_id):
        return self.registered_client(client_id)

    def registered_client(self, client_id):
        if len(client_id) > 1024:
            return None
        payload, _, signature = client_id.partition(".")
        if not signature.isascii() or not compare_digest(signature, self.sign("client", payload)):
            return None
        redirects, method, issued_at, _ = json.loads(urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return OAuthClientInformationFull(
            client_id=client_id, client_id_issued_at=issued_at, redirect_uris=redirects,
            token_endpoint_auth_method=method,
            client_secret=None if method == "none" else self.sign("secret", client_id),
            grant_types=["authorization_code", "refresh_token"], response_types=["code"], scope=SCOPE,
        )

    async def register_client(self, client):
        redirects = [str(uri) for uri in client.redirect_uris or []]
        providers = {redirect_provider(uri) for uri in redirects}
        if not redirects or len(redirects) > 2 or None in providers or len(providers) != 1:
            raise RegistrationError("invalid_redirect_uri", "Use a supported callback for one provider.")
        method = client.token_endpoint_auth_method
        if method not in {"none", "client_secret_post", "client_secret_basic"}:
            raise RegistrationError("invalid_client_metadata", "Unsupported client authentication method.")

        # Registration has no durable cost. Only the owner's authorization can create a grant.
        payload = json.dumps([redirects, method, client.client_id_issued_at, token_urlsafe(16)], separators=(",", ":"))
        encoded = urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        client.client_id = f"{encoded}.{self.sign('client', encoded)}"
        if method != "none":
            client.client_secret = self.sign("secret", client.client_id)

    async def authorize(self, client, params):
        if (params.resource != self.resource or params.scopes not in (None, [SCOPE])
                or not params.redirect_uri_provided_explicitly
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge)):
            raise AuthorizeError("invalid_request", "Request the message scope with S256 for this MCP endpoint.")
        pending = PendingConsent(client_id=client.client_id, params=params)
        handle = token_urlsafe(32)
        await run_in_threadpool(self.save_pending, handle, pending)
        return f"/oauth/consent?request={handle}"

    def save_pending(self, handle, pending):
        now = time()
        with self.store.session(write=True) as db:
            db.execute("DELETE FROM oauth_requests WHERE expires <= ?", (now,))
            db.execute("INSERT INTO oauth_requests VALUES (?, ?, ?)",
                       (self.store.hash_token(handle), pending.model_dump_json(), now + 300))

    @staticmethod
    def unexpired(db, table, digest):
        # Table names are fixed at each internal call site.
        return db.execute(f"SELECT data FROM {table} WHERE digest = ? AND expires > ?", (digest, time())).fetchone()

    def pending(self, handle):
        with self.store.session() as db:
            row = self.unexpired(db, "oauth_requests", self.store.hash_token(handle))
        return PendingConsent.model_validate_json(row["data"]) if row else None

    def compatible_agent(self, db, agent, provider):
        if agent["provider"] not in ("", "other", provider):
            return False
        grants = db.execute(
            "SELECT client_id FROM oauth_grants WHERE agent_id = ? AND grant_expires > ?",
            (agent["id"], time()),
        )
        for grant in grants:
            client = self.registered_client(grant["client_id"])
            if client is None or {redirect_provider(str(uri)) for uri in client.redirect_uris} != {provider}:
                return False
        return True

    def compatible_agents(self, workspace, provider):
        agents = self.store.list_agents(workspace)
        with self.store.session() as db:
            return [agent for agent in agents if self.compatible_agent(db, agent, provider)]

    def consent(self, handle, agent_id, approved, name=""):
        digest = self.store.hash_token(handle)
        with self.store.session(write=True) as db:
            row = self.unexpired(db, "oauth_requests", digest)
            if not row:
                raise StoreError("This consent request has expired. Reconnect from your agent provider.")
            pending = PendingConsent.model_validate_json(row["data"])
            params = pending.params
            db.execute("DELETE FROM oauth_requests WHERE digest = ?", (digest,))
            if not approved:
                return construct_redirect_uri(str(params.redirect_uri), state=params.state, error="access_denied")
            provider = redirect_provider(str(params.redirect_uri))
            if provider is None:
                raise StoreError("This OAuth callback is unavailable.")
            if agent_id and name.strip():
                raise StoreError("Choose an existing agent or name a new one.")
            if agent_id:
                agent = self.store.active_agent(db, STAGING_WORKSPACE, agent_id)
                if not self.compatible_agent(db, agent, provider):
                    raise StoreError("Choose an agent for this provider.")
                db.execute("UPDATE agents SET provider = ? WHERE id = ?", (provider, agent_id))
            else:
                agent_id = self.store.insert_agent(db, STAGING_WORKSPACE, name, provider)
            code = token_urlsafe(32)
            now = time()
            record = AuthorizationCode(
                code="", client_id=pending.client_id, code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=True,
                resource=self.resource, scopes=[SCOPE], expires_at=now + 90, subject=agent_id,
            )
            db.execute("DELETE FROM oauth_codes WHERE expires <= ?", (now,))
            db.execute("INSERT INTO oauth_codes VALUES (?, ?, ?)",
                       (self.store.hash_token(code), record.model_dump_json(), record.expires_at))
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, code):
        def read():
            with self.store.session() as db:
                row = self.unexpired(db, "oauth_codes", self.store.hash_token(code))
            if row:
                record = AuthorizationCode.model_validate_json(row["data"])
                if record.client_id == client.client_id:
                    return record.model_copy(update={"code": code})
            return None
        return await run_in_threadpool(read)

    async def exchange_authorization_code(self, client, authorization_code):
        def exchange():
            digest = self.store.hash_token(authorization_code.code)
            now = int(time())
            try:
                with self.store.session(write=True) as db:
                    if not self.unexpired(db, "oauth_codes", digest):
                        raise StoreError("Authorization code is no longer valid.")
                    agent = self.store.active_agent(db, STAGING_WORKSPACE, authorization_code.subject)
                    provider = redirect_provider(str(authorization_code.redirect_uri))
                    if provider is None or not self.compatible_agent(db, agent, provider):
                        raise StoreError("Agent is no longer available for this provider.")
                    db.execute("DELETE FROM oauth_codes WHERE digest = ?", (digest,))
                    db.execute("DELETE FROM oauth_grants WHERE grant_expires <= ?", (now,))
                    if agent["provider"] != provider:
                        db.execute("UPDATE agents SET provider = ? WHERE id = ?", (provider, agent["id"]))
                    return self.issue(db, client.client_id, agent["id"], now, now + GRANT_SECONDS)
            except StoreError as exc:
                raise TokenError("invalid_grant", str(exc)) from exc
        return await run_in_threadpool(exchange)

    def issue(self, db, client_id, agent_id, now, grant_expires):
        db.execute("DELETE FROM event_subscriptions WHERE client_id = ? AND agent_id != ?", (client_id, agent_id))
        access = token_urlsafe(32)
        refresh = token_urlsafe(32)
        lifetime = min(ACCESS_SECONDS, grant_expires - now)
        db.execute("""
            INSERT INTO oauth_grants VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                agent_id = excluded.agent_id, access_digest = excluded.access_digest,
                refresh_digest = excluded.refresh_digest, access_expires = excluded.access_expires,
                grant_expires = excluded.grant_expires
        """, (client_id, agent_id, self.store.hash_token(access), self.store.hash_token(refresh),
              now + lifetime, grant_expires))
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=lifetime, scope=SCOPE)

    def grant(self, db, digest, *, refresh=False):
        column = "refresh_digest" if refresh else "access_digest"
        expiry = "grant_expires" if refresh else "access_expires"
        return db.execute(f"""
            SELECT g.* FROM oauth_grants g JOIN agents a ON a.id = g.agent_id
            WHERE g.{column} = ? AND g.{expiry} > ? AND a.workspace = ? AND a.disconnected = 0
        """, (digest, time(), STAGING_WORKSPACE)).fetchone()

    async def load_access_token(self, token):
        def read():
            with self.store.session() as db:
                row = self.grant(db, self.store.hash_token(token))
            if row:
                return AccessToken(
                    token=token, client_id=row["client_id"], subject=STAGING_WORKSPACE, resource=self.resource,
                    scopes=[SCOPE], expires_at=row["access_expires"], claims={"agent_id": row["agent_id"]},
                )
            # The private pilot still accepts its separate agent-scoped smoke test credentials.
            identity = self.store.identity(token)
            if identity is not None and identity[1] == STAGING_WORKSPACE:
                return AccessToken(token=token, client_id=identity[0], subject=STAGING_WORKSPACE,
                                   resource=self.resource, scopes=[SCOPE])
            return None
        return await run_in_threadpool(read)

    async def load_refresh_token(self, client, refresh_token):
        def read():
            with self.store.session() as db:
                row = self.grant(db, self.store.hash_token(refresh_token), refresh=True)
            if row and row["client_id"] == client.client_id:
                return AgentRefresh(
                    token=refresh_token, client_id=row["client_id"], subject=STAGING_WORKSPACE,
                    resource=self.resource, scopes=[SCOPE], expires_at=row["grant_expires"], agent_id=row["agent_id"],
                )
            return None
        return await run_in_threadpool(read)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        def rotate():
            now = int(time())
            with self.store.session(write=True) as db:
                row = self.grant(db, self.store.hash_token(refresh_token.token), refresh=True)
                if not row or row["client_id"] != client.client_id:
                    raise TokenError("invalid_grant", "Refresh token is no longer valid.")
                if scopes != [SCOPE]:
                    raise TokenError("invalid_scope", "Request the message scope.")
                return self.issue(db, client.client_id, row["agent_id"], now, row["grant_expires"])
        return await run_in_threadpool(rotate)

    async def revoke_token(self, token):
        def revoke():
            digest = self.store.hash_token(token.token)
            with self.store.session(write=True) as db:
                db.execute("""DELETE FROM oauth_grants WHERE client_id = ?
                             AND (access_digest = ? OR refresh_digest = ?)""", (token.client_id, digest, digest))
        await run_in_threadpool(revoke)
