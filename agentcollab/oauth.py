"""Persistent owner-approved grants for the MCP SDK's OAuth server."""

import re
from secrets import token_urlsafe
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


class AgentRefresh(RefreshToken):
    agent_id: str


class PendingConsent(BaseModel):
    client_id: str
    params: AuthorizationParams


class StagingOAuth:
    def __init__(self, store, resource):
        self.store = store
        self.resource = resource
    async def get_client(self, client_id):
        def read():
            with self.store.session() as db:
                row = db.execute("SELECT metadata FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
                return OAuthClientInformationFull.model_validate_json(row["metadata"]) if row else None
        return await run_in_threadpool(read)

    async def register_client(self, client):
        redirects = [str(uri) for uri in client.redirect_uris or []]
        if not redirects or len(redirects) > 2 or any(not CHATGPT_REDIRECT.fullmatch(uri) for uri in redirects):
            raise RegistrationError("invalid_redirect_uri", "Use a ChatGPT OAuth callback.")
        if client.token_endpoint_auth_method not in {"none", "client_secret_post", "client_secret_basic"}:
            raise RegistrationError("invalid_client_metadata", "Unsupported client authentication method.")

        def register():
            now = int(time())
            with self.store.session(write=True) as db:
                db.execute("DELETE FROM oauth_grants WHERE grant_expires <= ?", (now,))
                db.execute("""DELETE FROM oauth_clients WHERE created <= ? AND client_id NOT IN
                              (SELECT client_id FROM oauth_grants)""", (now - GRANT_SECONDS,))
                if db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] >= 128:
                    raise RegistrationError("invalid_client_metadata", "The staging registration limit was reached.")
                db.execute("INSERT INTO oauth_clients VALUES (?, ?, ?)",
                           (client.client_id, client.model_dump_json(), now))
        await run_in_threadpool(register)

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

    def consent(self, handle, agent_id, approved):
        digest = self.store.hash_token(handle)
        with self.store.session(write=True) as db:
            row = self.unexpired(db, "oauth_requests", digest)
            if not row:
                raise StoreError("This consent request has expired. Reconnect from ChatGPT.")
            pending = PendingConsent.model_validate_json(row["data"])
            params = pending.params
            db.execute("DELETE FROM oauth_requests WHERE digest = ?", (digest,))
            if not approved:
                return construct_redirect_uri(str(params.redirect_uri), state=params.state, error="access_denied")
            self.store.active_agent(db, STAGING_WORKSPACE, agent_id)
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
            with self.store.session(write=True) as db:
                if not self.unexpired(db, "oauth_codes", digest):
                    raise TokenError("invalid_grant", "Authorization code is no longer valid.")
                db.execute("DELETE FROM oauth_codes WHERE digest = ?", (digest,))
                try:
                    self.store.active_agent(db, STAGING_WORKSPACE, authorization_code.subject)
                except StoreError as exc:
                    raise TokenError("invalid_grant", "Agent connection was revoked.") from exc
                return self.issue(db, client.client_id, authorization_code.subject, now, now + GRANT_SECONDS)
        return await run_in_threadpool(exchange)

    def issue(self, db, client_id, agent_id, now, grant_expires):
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
