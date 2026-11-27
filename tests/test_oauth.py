import base64
import hashlib
import hmac
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from agentcollab.app import LOCAL_WORKSPACE, create_app
from agentcollab.events import EVENT, InboxEvents
from agentcollab.oauth import StagingOAuth
from agentcollab.staging import STAGING_WORKSPACE, StagingConfig
from agentcollab.store import Store


class StagingOAuthTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.origin = "https://agentcollab-staging.up.railway.app"
        self.resource = f"{self.origin}/mcp"
        self.redirect = "https://chatgpt.com/connector/oauth/synthetic"
        password = "synthetic-owner-test-password-only"
        self.owner = ("owner", password)
        database = Path(temporary) / "stage.sqlite3"
        self.config = StagingConfig(self.origin, 3000, database, hashlib.sha256(password.encode()).hexdigest())
        self.store = Store(database)
        self.app = create_app(database, 3000, staging=self.config)
        self.client = self.enterContext(TestClient(self.app, base_url=self.origin, follow_redirects=False))
        self.agent = self.store.add_agent(STAGING_WORKSPACE, "Cosworth")
        self.other = self.store.add_agent(STAGING_WORKSPACE, "Other agent")
        self.other_token = self.store.issue_token_for_agent(STAGING_WORKSPACE, self.other)
        self.verifier = "a" * 43
        self.challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")

    def register(self, redirect=None):
        return self.client.post("/register", json={
            "redirect_uris": [redirect or self.redirect], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        })

    def authorize(self, client_id, **changes):
        return self.client.get("/authorize", auth=self.owner, params={
            "client_id": client_id, "response_type": "code", "scope": "message", "resource": self.resource,
            "redirect_uri": self.redirect, "state": "client-state", "code_challenge": self.challenge,
            "code_challenge_method": "S256",
        } | changes)

    def consent(self, location, *, agent=None, decision="approve", origin=None):
        handle = parse_qs(urlsplit(location).query)["request"][0]
        return self.client.post("/oauth/consent", auth=self.owner, headers={"Origin": origin or self.origin}, data={
            "request": handle, "agent": agent or self.agent, "decision": decision,
        })

    def exchange(self, client_id, code, **changes):
        return self.client.post("/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "redirect_uri": self.redirect,
            "code": code, "code_verifier": self.verifier, "resource": self.resource,
        } | changes)

    def refresh(self, client_id, token):
        return self.client.post("/token", data={
            "grant_type": "refresh_token", "client_id": client_id, "refresh_token": token, "resource": self.resource,
        })

    def rpc(self, token=None, name="list_agents", client=None, **arguments):
        headers = {"Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return (client or self.client).post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })

    def result(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def test_callback_discovery_logs_only_hostname_without_sending(self):
        oauth_client = self.register().json()["client_id"]
        approved = self.consent(self.authorize(oauth_client).headers["location"])
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        token = self.exchange(oauth_client, code).json()["access_token"]
        callback = "https://receiver.example.com/synthetic-path?private=value"
        old_subscription_id = "sub_" + hashlib.sha256(f"{oauth_client}\n{self.agent}\n{callback}".encode()).hexdigest()
        with self.store.session(write=True) as db:
            db.execute("""
                INSERT INTO event_subscriptions (id, client_id, agent_id, url, secret, expires)
                VALUES (?, ?, ?, ?, ?, unixepoch() + 300)
            """, (old_subscription_id, oauth_client, self.agent, callback, b"sealed synthetic secret"))
        self.store.send(STAGING_WORKSPACE, self.other, self.agent, "synthetic queued message", "discovery-unsubscribe")
        config = replace(self.config, event_discovery_only=True)
        app = create_app(config.database, 3000, staging=config)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}

        def call(client, method, params):
            return client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
            }).json()

        with patch.object(InboxEvents, "post") as outbound, patch.object(InboxEvents, "run") as worker:
            with TestClient(app, base_url=self.origin) as client:
                self.assertEqual(call(client, "events/list", {})["result"]["events"][0]["name"], EVENT)
                with self.assertLogs("agentcollab.events") as logs:
                    result = call(client, "events/subscribe", {
                        "name": EVENT, "arguments": {}, "delivery": {
                            "mode": "webhook", "url": callback,
                            "secret": "whsec_" + base64.b64encode(b"s" * 32).decode(),
                        },
                    })
                self.assertEqual(result["error"]["data"]["reason"], "staging_callback_discovery")
                self.assertEqual(logs.output, [
                    "WARNING:agentcollab.events:ChatGPT event callback discovery: receiver.example.com"
                ])
                self.assertEqual(call(client, "events/unsubscribe", {
                    "name": EVENT, "arguments": {}, "delivery": {"mode": "webhook", "url": callback},
                })["result"], {})
            outbound.assert_not_called()
            worker.assert_not_called()
        with self.store.session() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM event_subscriptions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM event_outbox").fetchone()[0], 0)
        resumed = InboxEvents(self.store, StagingOAuth(self.store, self.resource),
                              ("receiver.example.com",), Fernet.generate_key())
        self.assertFalse(resumed.dispatch_one())

    def test_chatgpt_events_are_agent_scoped_and_retry_with_the_same_id(self):
        signing_key = b"s" * 32
        callback_host = "receiver.example.com"
        callback = f"https://{callback_host}/mcp-events/main"
        key = Fernet.generate_key()
        deliveries = []
        outage = False

        def receive(url, headers, body):
            self.assertEqual(url, callback)
            event_id = headers["webhook-id"]
            signed = f"{event_id}.{headers['webhook-timestamp']}.".encode() + body
            expected = base64.b64encode(hmac.digest(signing_key, signed, "sha256")).decode()
            self.assertEqual(headers["webhook-signature"], f"v1,{expected}")
            event = json.loads(body)
            if event.get("type") == "verification":
                return 200, json.dumps({"challenge": event["challenge"]}).encode()
            deliveries.append(event)
            return (503 if outage or len(deliveries) == 1 else 204), b""

        async def idle(worker):
            pass

        with patch.object(InboxEvents, "post", side_effect=receive), patch.object(InboxEvents, "run", idle):
            config = replace(self.config, event_hosts=(callback_host,), event_key=key.decode())
            app = create_app(config.database, 3000, staging=config)
            with TestClient(app, base_url=self.origin, follow_redirects=False) as client:
                self.client = client
                oauth_client = self.register().json()["client_id"]
                approved = self.consent(self.authorize(oauth_client).headers["location"])
                code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
                grant = self.exchange(oauth_client, code).json()
                token = grant["access_token"]

                def call(method, params=None, bearer=token):
                    headers = {"Accept": "application/json, text/event-stream"}
                    if bearer:
                        headers["Authorization"] = f"Bearer {bearer}"
                    return client.post("/mcp", headers=headers, json={
                        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
                    })

                discover = call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                               "clientInfo": {"name": "Test", "version": "1"}})
                self.assertIn("events", discover.json()["result"]["capabilities"])
                self.assertEqual(call("events/list", bearer=None).status_code, 401)
                self.assertEqual(call("events/list", bearer=self.other_token).json()["error"]["code"], -32601)
                self.assertEqual(call("events/list").json()["result"]["events"][0]["name"], EVENT)
                options = {"name": EVENT, "arguments": {}, "delivery": {
                    "mode": "webhook", "url": callback,
                    "secret": "whsec_" + base64.b64encode(signing_key).decode(),
                }}
                wrong = options | {"delivery": options["delivery"] | {"url": "https://other.example.com/events"}}
                self.assertEqual(call("events/subscribe", wrong).json()["error"]["code"], -32015)
                sub = call("events/subscribe", options).json()["result"]["id"]
                self.assertEqual(call("events/subscribe", options).json()["result"]["id"], sub)
                self.assertTrue(self.result(self.rpc(token))["identity"]["push"])
                sent = self.result(self.rpc(self.other_token, "send_message", to_agent_id=self.agent,
                                            text="synthetic message", request_id="push-test"))
                dispatcher = InboxEvents(self.store, StagingOAuth(self.store, self.resource), (callback_host,), key,
                                         transport=receive)
                self.assertTrue(dispatcher.dispatch_one())
                self.assertFalse(dispatcher.dispatch_one())
                with self.store.session(write=True) as db:
                    db.execute("UPDATE event_outbox SET next_attempt = unixepoch()")
                self.assertTrue(dispatcher.dispatch_one())
                self.assertEqual(deliveries[0], deliveries[1])
                self.assertEqual(deliveries[0]["data"], {"message_id": sent["message_id"]})
                self.assertFalse(dispatcher.dispatch_one())

                outage = True
                failed = self.result(self.rpc(self.other_token, "send_message", to_agent_id=self.agent,
                                              text="never shown in the delivery log", request_id="outage-test"))
                for _ in range(5):
                    with self.store.session(write=True) as db:
                        db.execute("UPDATE event_outbox SET next_attempt = unixepoch()")
                    self.assertTrue(dispatcher.dispatch_one())
                restarted = Store(config.database)
                dispatcher = InboxEvents(restarted, StagingOAuth(restarted, self.resource),
                                         (callback_host,), key, transport=receive)
                self.assertFalse(dispatcher.dispatch_one())
                self.assertEqual(dispatcher.failures(STAGING_WORKSPACE)[0]["message_id"], failed["message_id"])
                self.assertEqual(dispatcher.failures(LOCAL_WORKSPACE), [])
                page = client.get("/agents", auth=self.owner)
                self.assertIn("HTTP 503", page.text)
                self.assertNotIn("never shown in the delivery log", page.text)
                retry = {"subscription": sub, "message": failed["message_id"]}
                self.assertEqual(client.post("/agents/retry-delivery", auth=self.owner, data=retry).status_code, 403)
                self.assertEqual(client.post("/agents/retry-delivery", auth=self.owner, data=retry,
                                             headers={"Origin": self.origin}).status_code, 303)
                outage = False
                old_event = deliveries[-1]
                self.assertTrue(dispatcher.dispatch_one())
                self.assertEqual(deliveries[-1], old_event)
                self.assertEqual(dispatcher.failures(STAGING_WORKSPACE), [])

                self.assertEqual(call("events/unsubscribe", options).json()["result"], {})
                self.assertEqual(call("events/unsubscribe", options).json()["result"], {})
                self.assertFalse(self.result(self.rpc(token))["identity"]["push"])
                call("events/subscribe", options)
                self.result(self.rpc(self.other_token, "send_message", to_agent_id=self.agent,
                                     text="second message", request_id="revoke-test"))
                with self.store.session(write=True) as db:
                    db.execute("DELETE FROM oauth_grants WHERE client_id = ?", (oauth_client,))
                    self.assertEqual(db.execute("SELECT count(*) FROM event_outbox").fetchone()[0], 0)
                self.assertFalse(dispatcher.dispatch_one())

    def test_muse_oauth_callback_requires_an_exact_match(self):
        self.redirect = "https://agent.meta.ai/api/hatch/oauth/callback"
        for other in (
            self.redirect + "?target=elsewhere",
            self.redirect + "/",
            self.redirect.replace("agent.meta.ai", "agent.meta.ai.example"),
            self.redirect.replace("https:", "http:"),
        ):
            with self.subTest(redirect=other):
                response = self.register(other)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_redirect_uri")

        registered = self.register()
        self.assertEqual(registered.status_code, 201)
        client_id = registered.json()["client_id"]
        authorize = self.authorize(client_id)
        self.assertEqual(authorize.status_code, 302)
        approved = self.consent(authorize.headers["location"])
        callback = urlsplit(approved.headers["location"])
        self.assertEqual(callback._replace(query="").geturl(), self.redirect)
        code = parse_qs(callback.query)["code"][0]
        token = self.exchange(client_id, code)
        self.assertEqual(token.status_code, 200)
        self.assertEqual(self.result(self.rpc(token.json()["access_token"]))["identity"]["name"], "Cosworth")

    def test_consent_can_create_or_update_an_agent_for_the_connecting_provider(self):
        self.redirect = "https://claude.ai/api/mcp/auth_callback"
        client_id = self.register().json()["client_id"]
        location = self.authorize(client_id).headers["location"]
        page = self.client.get(location, auth=self.owner)
        self.assertIn("Connect Claude", page.text)
        self.assertIn("New agent name", page.text)
        self.assertIn("Connect existing agent", page.text)
        handle = parse_qs(urlsplit(location).query)["request"][0]
        approved = self.client.post("/oauth/consent", auth=self.owner, headers={"Origin": self.origin}, data={
            "request": handle, "decision": "approve", "name": "Research Claude",
        })
        self.assertEqual(approved.status_code, 303)
        self.assertEqual(self.client.get(location, auth=self.owner).status_code, 400)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        authorized = self.exchange(client_id, code)
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(self.result(self.rpc(authorized.json()["access_token"]))["identity"]["name"], "Research Claude")

        reconnect = self.authorize(self.register().json()["client_id"]).headers["location"]
        self.assertEqual(self.consent(reconnect, agent=self.agent).status_code, 303)
        agents = {row["name"]: row for row in self.store.list_agents(STAGING_WORKSPACE)}
        self.assertEqual(agents["Cosworth"]["id"], self.agent)
        self.assertEqual(agents["Cosworth"]["provider"], "claude")
        self.assertEqual(agents["Research Claude"]["provider"], "claude")

        self.redirect = "https://chatgpt.com/connector/oauth/synthetic"
        chatgpt = self.authorize(self.register().json()["client_id"]).headers["location"]
        self.assertNotIn("Research Claude", self.client.get(chatgpt, auth=self.owner).text)
        self.assertEqual(self.consent(chatgpt, agent=self.agent).status_code, 400)

    def test_legacy_oauth_grant_blocks_rebinding_to_a_different_provider(self):
        client_id = self.register().json()["client_id"]
        consent = self.consent(self.authorize(client_id).headers["location"])
        code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
        token = self.exchange(client_id, code).json()["access_token"]
        with self.store.session(write=True) as db:
            db.execute("ALTER TABLE agents DROP COLUMN provider")
        Store(self.config.database)

        self.redirect = "https://claude.ai/api/mcp/auth_callback"
        rejected = self.authorize(self.register().json()["client_id"]).headers["location"]
        self.assertNotIn("Cosworth", self.client.get(rejected, auth=self.owner).text)
        self.assertEqual(self.consent(rejected, agent=self.agent).status_code, 400)
        self.assertEqual(self.result(self.rpc(token))["identity"]["name"], "Cosworth")

        self.redirect = "https://chatgpt.com/connector/oauth/synthetic"
        allowed = self.authorize(self.register().json()["client_id"]).headers["location"]
        self.assertIn("Cosworth", self.client.get(allowed, auth=self.owner).text)
        self.assertEqual(self.consent(allowed, agent=self.agent).status_code, 303)

    def test_legacy_oauth_codes_respect_provider_binding_after_upgrade(self):
        chatgpt_redirect = self.redirect
        legacy_codes = {}
        for agent in (self.agent, self.other):
            client_id = self.register().json()["client_id"]
            approved = self.consent(self.authorize(client_id).headers["location"], agent=agent)
            legacy_codes[agent] = (client_id, parse_qs(urlsplit(approved.headers["location"]).query)["code"][0])

        with self.store.session(write=True) as db:
            db.execute("ALTER TABLE agents DROP COLUMN provider")
        Store(self.config.database)

        self.redirect = "https://claude.ai/api/mcp/auth_callback"
        claude_id = self.register().json()["client_id"]
        approved = self.consent(self.authorize(claude_id).headers["location"])
        self.assertEqual(approved.status_code, 303)

        rejected = self.exchange(*legacy_codes[self.agent], redirect_uri=chatgpt_redirect)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["error"], "invalid_grant")
        claude_code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        claude_token = self.exchange(claude_id, claude_code).json()["access_token"]
        self.assertEqual(self.result(self.rpc(claude_token))["identity"]["name"], "Cosworth")

        accepted = self.exchange(*legacy_codes[self.other], redirect_uri=chatgpt_redirect)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(self.result(self.rpc(accepted.json()["access_token"]))["identity"]["name"], "Other agent")
        providers = {row["id"]: row["provider"] for row in self.store.list_agents(STAGING_WORKSPACE)}
        self.assertEqual(providers[self.agent], "claude")
        self.assertEqual(providers[self.other], "chatgpt")

    def test_registration_keeps_callbacks_on_one_provider(self):
        result = self.client.post("/register", json={
            "redirect_uris": [self.redirect, "https://claude.ai/api/mcp/auth_callback"],
            "token_endpoint_auth_method": "none",
        })
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()["error"], "invalid_redirect_uri")

    def test_owner_consent_produces_one_agent_and_rotating_tokens(self):
        anonymous = self.rpc()
        self.assertEqual(anonymous.status_code, 401)
        self.assertIn("resource_metadata=", anonymous.headers["www-authenticate"])
        resource = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(resource["resource"], self.resource)
        metadata = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertIn("S256", metadata["code_challenge_methods_supported"])
        self.assertEqual(self.client.get("/authorize").status_code, 401)
        self.assertEqual(self.register("https://chatgpt.com.evil.example/connector/oauth/synthetic").status_code, 400)
        self.assertEqual(self.register("https://chatgpt.com/connector/oauth/synthetic?next=elsewhere").status_code, 400)
        client_id = self.register().json()["client_id"]
        rejected_target = self.authorize(client_id, resource="https://elsewhere.example/mcp")
        self.assertIn("error=invalid_request", rejected_target.headers["location"])
        authorize = self.authorize(client_id)
        self.assertEqual(authorize.status_code, 302, authorize.text)
        location = authorize.headers["location"]
        self.assertEqual(self.client.get(location).status_code, 401)
        consent_page = self.client.get(location, auth=self.owner)
        self.assertIn("Cosworth", consent_page.text)
        self.assertEqual(self.consent(location, origin="https://elsewhere.example").status_code, 403)
        local = self.store.add_agent(LOCAL_WORKSPACE, "Local")
        self.assertEqual(self.consent(location, agent=local).status_code, 400)
        approved = self.consent(location)
        callback = urlsplit(approved.headers["location"])
        self.assertEqual(callback.netloc, "chatgpt.com")
        query = parse_qs(callback.query)
        self.assertEqual(query["state"], ["client-state"])
        self.assertEqual(self.consent(location).status_code, 400)
        code = query["code"][0]
        self.assertEqual(self.exchange(client_id, code, code_verifier="b" * 43).status_code, 400)
        self.assertEqual(self.exchange(client_id, code, resource="https://elsewhere.example/mcp").status_code, 400)
        self.assertEqual(self.exchange(client_id, code, resource="").status_code, 400)
        response = self.exchange(client_id, code)
        self.assertEqual(response.status_code, 200, response.text)
        tokens = response.json()
        self.assertEqual(self.exchange(client_id, code).status_code, 400)
        self.assertEqual(self.result(self.rpc(tokens["access_token"]))["identity"]["id"], self.agent)
        message = self.result(self.rpc(self.other_token, "send_message", to_agent_id=self.agent,
                                       text="Synthetic OAuth test", request_id="test-1"))
        with TestClient(create_app(self.config.database, 3000, staging=self.config), base_url=self.origin) as restarted:
            inbox = self.result(self.rpc(tokens["access_token"], "read_inbox", client=restarted,
                                         message_id=message["message_id"]))
            self.assertEqual(inbox["messages"][0]["id"], message["message_id"])
        refreshed = self.refresh(client_id, tokens["refresh_token"])
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        rotated = refreshed.json()
        self.assertEqual(self.refresh(client_id, tokens["refresh_token"]).status_code, 400)
        self.assertEqual(self.rpc(tokens["access_token"]).status_code, 401)
        self.assertEqual(self.result(self.rpc(rotated["access_token"]))["identity"]["id"], self.agent)
        revoke = self.client.post("/revoke", data={
            "client_id": client_id, "client_secret": "", "token": rotated["refresh_token"],
        })
        self.assertEqual(revoke.status_code, 200, revoke.text)
        self.assertEqual(self.rpc(rotated["access_token"]).status_code, 401)

    def test_deny_and_agent_disconnect(self):
        client_id = self.register().json()["client_id"]
        denied = self.consent(self.authorize(client_id).headers["location"], decision="deny")
        self.assertEqual(parse_qs(urlsplit(denied.headers["location"]).query), {
            "error": ["access_denied"], "state": ["client-state"],
        })
        callback = self.consent(self.authorize(client_id).headers["location"])
        code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
        tokens = self.exchange(client_id, code).json()
        self.store.disconnect(STAGING_WORKSPACE, self.agent)
        self.assertEqual(self.rpc(tokens["access_token"]).status_code, 401)
        self.assertEqual(self.refresh(client_id, tokens["refresh_token"]).status_code, 400)

    def test_unapproved_registration_cannot_crowd_out_owner_or_existing_grant(self):
        client_id = self.register().json()["client_id"]
        approved = self.consent(self.authorize(client_id).headers["location"])
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        current = self.exchange(client_id, code).json()["access_token"]

        for _ in range(140):
            self.assertEqual(self.register().status_code, 201)

        registration = self.register()
        self.assertEqual(registration.status_code, 201, registration.text)
        new_client = registration.json()["client_id"]
        self.assertNotEqual(new_client, client_id)
        self.assertEqual(self.authorize(new_client[:-1] + ("A" if new_client[-1] != "A" else "B")).status_code, 400)
        approved = self.consent(self.authorize(new_client).headers["location"], agent=self.other)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        token = self.exchange(new_client, code).json()["access_token"]
        self.assertEqual(self.result(self.rpc(token))["identity"]["id"], self.other)
        self.assertEqual(self.result(self.rpc(current))["identity"]["id"], self.agent)
