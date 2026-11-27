import base64
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

from agentcollab.app import LOCAL_WORKSPACE, create_app
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
