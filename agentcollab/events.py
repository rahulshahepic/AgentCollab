"""Agent-scoped OpenAI MCP Events subscriptions and delivery."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import ssl
from datetime import UTC, datetime
from http.client import HTTPSConnection, HTTPException
from time import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.middleware.auth_context import get_access_token
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .oauth import CHATGPT_EVENT_CALLBACK_HOST, SCOPE, redirect_provider
from .store import StoreError

EVENT = "inbox.message_received"
METHODS = {"events/list", "events/subscribe", "events/unsubscribe"}
SOCKET_TIMEOUT = 3
MAX_BODY = 128 * 1024
SUBSCRIPTION_SECONDS = 7 * 86400
LOGGER = logging.getLogger(__name__)

DEFINITION = {
    "name": EVENT,
    "description": "A direct message or reply arrived for the connected agent. Fetch it with read_inbox.",
    "delivery": ["webhook"],
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "payloadSchema": {
        "type": "object",
        "properties": {"message_id": {"type": "string"}},
        "required": ["message_id"],
        "additionalProperties": False,
    },
}


class CallbackError(Exception):
    pass


class PublicHTTPSConnection(HTTPSConnection):
    def connect(self):
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        public = [
            (family, kind, protocol, address)
            for family, kind, protocol, _, address in addresses
            if (ip := ipaddress.ip_address(address[0])).is_global and not ip.is_multicast
        ]
        if not public or len(public) != len(addresses):
            raise CallbackError("non_public_address")
        family, kind, protocol, address = public[0]
        with socket.socket(family, kind, protocol) as connection:
            connection.settimeout(SOCKET_TIMEOUT)
            connection.connect(address)
            self.sock = ssl.create_default_context().wrap_socket(connection, server_hostname=self.host)


class InboxEvents:
    def __init__(self, store, oauth, encryption_key, *, transport=None, discovery_only=False):
        self.store = store
        self.oauth = oauth
        self.discovery_only = discovery_only
        self.encryption = None if discovery_only else Fernet(encryption_key)
        self.transport = self.post if transport is None else transport

    def permits(self, token):
        if not token or not token.claims or SCOPE not in token.scopes:
            return False
        client = self.oauth.registered_client(token.client_id)
        return client is not None and {redirect_provider(str(uri)) for uri in client.redirect_uris} == {"chatgpt"}

    @staticmethod
    def callback_url(url):
        if not url.isascii() or len(url) > 2048 or re.search(r"[\x00-\x20\x7f\\]", url):
            raise CallbackError("invalid_url")
        target = urlsplit(url)
        if (target.scheme != "https" or not target.hostname or target.netloc != target.hostname or "#" in url
                or not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]*",
                                    target.hostname)):
            raise CallbackError("invalid_url")
        return target

    def destination(self, url):
        target = self.callback_url(url)
        if target.hostname != CHATGPT_EVENT_CALLBACK_HOST:
            raise CallbackError("invalid_url")
        return target

    def post(self, url, headers, body):
        target = self.destination(url)
        conn = PublicHTTPSConnection(target.hostname, timeout=SOCKET_TIMEOUT)
        try:
            conn.request("POST", (target.path or "/") + ("?" + target.query if target.query else ""),
                         body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read(4097)
        except (OSError, HTTPException) as exc:
            raise CallbackError("unreachable") from exc
        finally:
            conn.close()

    def signed_post(self, url, key, subscription_id, event_id, payload):
        self.destination(url)
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time()))
        signature = base64.b64encode(hmac.digest(key, f"{event_id}.{timestamp}.".encode() + body, "sha256")).decode()
        return self.transport(url, {
            "Content-Type": "application/json",
            "webhook-id": event_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": f"v1,{signature}",
            "X-MCP-Subscription-Id": subscription_id,
        }, body)

    @staticmethod
    def subscription_id(token, url):
        identity = f"{token.client_id}\n{token.claims['agent_id']}\n{url}"
        return "sub_" + hashlib.sha256(identity.encode()).hexdigest()

    def subscribe(self, token, url, key):
        agent_id = token.claims["agent_id"]
        subscription_id = self.subscription_id(token, url)
        challenge = secrets.token_urlsafe(32)
        status, body = self.signed_post(url, key, subscription_id, "verify_" + secrets.token_hex(16),
                                        {"type": "verification", "challenge": challenge})
        try:
            received = json.loads(body).get("challenge") if 200 <= status < 300 else None
        except (ValueError, AttributeError):
            received = None
        if not isinstance(received, str) or not hmac.compare_digest(received.encode(), challenge.encode()):
            raise CallbackError("challenge_failed")
        now = int(time())
        expires = now + SUBSCRIPTION_SECONDS
        with self.store.session(write=True) as db:
            grant = self.oauth.grant(db, self.store.hash_token(token.token))
            if not grant or grant["agent_id"] != agent_id:
                raise CallbackError("grant_expired")
            expires = min(expires, grant["grant_expires"])
            db.execute("DELETE FROM event_subscriptions WHERE expires <= ?", (now,))
            if db.execute("""SELECT count(*) FROM event_subscriptions
                             WHERE agent_id = ? AND id != ?""", (agent_id, subscription_id)).fetchone()[0] >= 4:
                raise CallbackError("subscription_limit")
            db.execute("""
                INSERT INTO event_subscriptions (id, client_id, agent_id, url, secret, expires)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET secret = excluded.secret, expires = excluded.expires
            """, (subscription_id, token.client_id, agent_id, url, self.encryption.encrypt(key), expires))
        return {"id": subscription_id, "refreshBefore": datetime.fromtimestamp(expires, UTC).isoformat(),
                "cursor": None, "truncated": False}

    def has_subscription(self, agent_id):
        with self.store.session() as db:
            urls = db.execute("""
                SELECT s.url FROM event_subscriptions s
                JOIN oauth_grants g ON g.client_id = s.client_id AND g.agent_id = s.agent_id
                WHERE s.agent_id = ? AND s.expires > unixepoch() AND g.grant_expires > unixepoch()
            """, (agent_id,))
            for row in urls:
                if urlsplit(row["url"]).netloc == CHATGPT_EVENT_CALLBACK_HOST:
                    return True
        return False

    def unsubscribe(self, token, subscription_id):
        with self.store.session(write=True) as db:
            db.execute("DELETE FROM event_subscriptions WHERE id = ? AND client_id = ? AND agent_id = ?",
                       (subscription_id, token.client_id, token.claims["agent_id"]))
        return {}

    def failures(self, workspace):
        with self.store.session() as db:
            return db.execute("""
                SELECT j.message_id, j.subscription_id, j.last_error, a.name, m.created
                FROM event_outbox j
                JOIN event_subscriptions s ON s.id = j.subscription_id
                JOIN messages m ON m.id = j.message_id
                JOIN agents a ON a.id = s.agent_id
                JOIN oauth_grants g ON g.client_id = s.client_id AND g.agent_id = s.agent_id
                WHERE j.attempts >= 5 AND m.workspace = ? AND s.expires > unixepoch()
                AND g.grant_expires > unixepoch()
                AND m.created > strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
                ORDER BY m.created DESC, j.message_id LIMIT 50
            """, (workspace,)).fetchall()

    def retry(self, workspace, subscription_id, message_id):
        with self.store.session(write=True) as db:
            result = db.execute("""
                UPDATE event_outbox SET attempts = 0, last_error = '', next_attempt = unixepoch()
                WHERE subscription_id = ? AND message_id = ? AND attempts >= 5
                AND EXISTS (
                    SELECT 1 FROM event_subscriptions s
                    JOIN oauth_grants g ON g.client_id = s.client_id AND g.agent_id = s.agent_id
                    JOIN messages m ON m.id = event_outbox.message_id AND m.recipient = s.agent_id
                    WHERE s.id = event_outbox.subscription_id AND m.workspace = ?
                    AND s.expires > unixepoch() AND g.grant_expires > unixepoch()
                    AND m.created > strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
                )
            """, (subscription_id, message_id, workspace))
            if not result.rowcount:
                raise StoreError("This failed delivery is no longer available to retry.")

    def dispatch_one(self):
        now = int(time())
        with self.store.session(write=True) as db:
            db.execute("DELETE FROM event_subscriptions WHERE expires <= ?", (now,))
            job = db.execute("""
                SELECT j.*, s.url, s.secret, m.created FROM event_outbox j
                JOIN event_subscriptions s ON s.id = j.subscription_id
                JOIN oauth_grants g ON g.client_id = s.client_id AND g.agent_id = s.agent_id
                JOIN messages m ON m.id = j.message_id
                WHERE j.next_attempt <= ? AND g.grant_expires > ? AND j.attempts < 5
                AND m.created > strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
                ORDER BY j.next_attempt, j.message_id LIMIT 1
            """, (now, now)).fetchone()
            if job is None:
                return False
            key = (job["subscription_id"], job["message_id"])
            # Leave a lease so a restart or concurrent worker can retry the same event ID.
            db.execute("UPDATE event_outbox SET next_attempt = ? WHERE subscription_id = ? AND message_id = ?",
                       (now + 60, *key))
        event_id = "evt_" + hashlib.sha256(":".join(key).encode()).hexdigest()
        attempt = job["attempts"] + 1
        failure = ""
        try:
            status, _ = self.signed_post(job["url"], self.encryption.decrypt(job["secret"]), key[0], event_id,
                                         {"eventId": event_id, "name": EVENT, "timestamp": job["created"],
                                          "data": {"message_id": key[1]}, "cursor": None})
            if not 200 <= status < 300:
                failure = f"HTTP {status}"
        except CallbackError as exc:
            failure = str(exc)
        except InvalidToken:
            failure = "signing_key_unavailable"
        except ValueError:
            failure = "invalid_callback"
        with self.store.session(write=True) as db:
            if not failure:
                db.execute("DELETE FROM event_outbox WHERE subscription_id = ? AND message_id = ?", key)
            else:
                db.execute("""UPDATE event_outbox SET attempts = ?, next_attempt = ?, last_error = ?
                              WHERE subscription_id = ? AND message_id = ?""",
                           (attempt, int(time()) + 15 * 2 ** (attempt - 1), failure, *key))
        if failure and attempt >= 5:
            LOGGER.warning("Inbox event delivery exhausted its retries")
        return True

    async def run(self):
        while True:
            try:
                processed = await run_in_threadpool(self.dispatch_one)
            except Exception:
                LOGGER.exception("Inbox event worker failed")
                processed = False
            if not processed:
                await asyncio.sleep(2)

    async def handle(self, request, message, token):
        def respond(**values):
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], **values}, headers={"Cache-Control": "no-store"})

        def fail(code, message_text, **data):
            return respond(error={"code": code, "message": message_text, **({"data": data} if data else {})})

        if not self.permits(token):
            return fail(-32601, "Event method unavailable.")
        if request.headers.get("origin") not in (None, self.oauth.resource.removesuffix("/mcp")):
            return PlainTextResponse("Invalid Origin", status_code=403)
        params = message.get("params", {})
        if not isinstance(params, dict) or message.get("jsonrpc") != "2.0":
            return fail(-32602, "Invalid event parameters.")
        method = message["method"]
        if method == "events/list":
            return respond(result={"events": [DEFINITION]})
        delivery = params.get("delivery")
        if (params.get("name") != EVENT or params.get("arguments", {}) != {} or params.get("cursor") is not None
                or not isinstance(delivery, dict) or delivery.get("mode") != "webhook"):
            return fail(-32602, "Use the connected inbox event and webhook delivery.")
        url = delivery.get("url")
        if not isinstance(url, str):
            return fail(-32602, "A callback URL is required.")
        if method == "events/unsubscribe":
            return respond(result=await run_in_threadpool(self.unsubscribe, token, self.subscription_id(token, url)))
        secret = delivery.get("secret")
        if not isinstance(secret, str) or not secret.startswith("whsec_"):
            return fail(-32602, "A signing secret is required.")
        try:
            key = base64.b64decode(secret[6:], validate=True)
        except ValueError:
            key = b""
        if not 24 <= len(key) <= 64:
            return fail(-32602, "Invalid signing secret.")
        if self.discovery_only:
            try:
                hostname = self.callback_url(url).hostname
            except (CallbackError, ValueError):
                return fail(-32015, "Invalid callback URL.", reason="invalid_url")
            LOGGER.warning("ChatGPT event callback discovery: %s", hostname)
            return fail(-32015, "Callback recorded for staging review. No subscription was created.",
                        reason="staging_callback_discovery")
        try:
            result = await run_in_threadpool(self.subscribe, token, url, key)
        except (CallbackError, ValueError) as exc:
            return fail(-32015, "Callback subscription rejected.",
                        reason=str(exc) if isinstance(exc, CallbackError) else "invalid_url")
        return respond(result=result)

    def wrap(self, app):
        async def endpoint(scope, receive, send):
            token = get_access_token()
            if scope["method"] != "POST" or not token:
                await app(scope, receive, send)
                return
            request = Request(scope, receive)
            if request.headers.get("content-type", "").partition(";")[0] != "application/json":
                await app(scope, receive, send)
                return
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > MAX_BODY:
                    await PlainTextResponse("Request too large", status_code=413)(scope, receive, send)
                    return
            try:
                message = json.loads(content)
            except ValueError:
                message = None
            method = message.get("method") if isinstance(message, dict) else None

            async def replay():
                return {"type": "http.request", "body": bytes(content), "more_body": False}

            if isinstance(method, str) and method in METHODS and "id" in message:
                response = await self.handle(request, message, token)
                await response(scope, receive, send)
                return
            if method != "initialize" or not self.permits(token):
                await app(scope, replay, send)
                return

            captured = []

            async def collect(event):
                captured.append(event)

            await app(scope, replay, collect)
            if len(captured) == 2 and captured[0].get("status") == 200 and not captured[1].get("more_body"):
                try:
                    response = json.loads(captured[1]["body"])
                    response["result"]["capabilities"]["events"] = {}
                except (KeyError, TypeError, ValueError):
                    pass
                else:
                    await JSONResponse(response)(scope, receive, send)
                    return
            for event in captured:
                await send(event)

        return endpoint
