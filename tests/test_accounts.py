import base64
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

from agentcollab.accounts import SESSION_COOKIE, return_path
from agentcollab.app import create_app
from agentcollab.staging import STAGING_WORKSPACE, StagingConfig
from agentcollab.store import Store


class OwnerAccountTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.origin = "https://agentcollab-staging.up.railway.app"
        self.code = "A" * 43
        self.password = "a-synthetic-test-password"
        self.config = StagingConfig(
            self.origin, 3000, Path(directory) / "staging.sqlite3", "", self.code, int(time()) + 100, True,
        )
        self.client = self.enterContext(TestClient(self.app(), base_url=self.origin, follow_redirects=False))
        self.headers = {"Origin": self.origin}

    def app(self):
        return create_app(self.config.database, 3000, staging=self.config)

    def setup_account(self, **changes):
        return self.client.post("/setup", headers=self.headers, data={
            "code": self.code, "username": "pilot-owner", "password": self.password,
        } | changes)

    def test_setup_login_logout_and_restart(self):
        self.assertEqual(self.client.get("/").headers["location"], "/login?next=%2F")
        self.assertEqual(self.client.get("/setup").status_code, 200)
        self.assertEqual(self.setup_account(code="B" * 43).status_code, 401)
        self.assertEqual(self.client.post("/setup", data={"code": self.code}).status_code, 403)
        setup = self.setup_account(next="//elsewhere.example/")
        self.assertEqual(setup.status_code, 303)
        self.assertEqual(setup.headers["location"], "/")
        self.assertIn("HttpOnly", setup.headers["set-cookie"])
        self.assertIn("Secure", setup.headers["set-cookie"])
        self.assertIn("SameSite=lax", setup.headers["set-cookie"])
        session = self.client.cookies.get(SESSION_COOKIE)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.setup_account(username="imposter").status_code, 303)
        with TestClient(self.app(), base_url=self.origin, cookies={SESSION_COOKIE: session}) as restarted:
            self.assertEqual(restarted.get("/").status_code, 200)
        self.assertEqual(self.client.post("/logout").status_code, 403)
        self.assertEqual(self.client.post("/logout", headers=self.headers).status_code, 303)
        self.client.cookies.set(SESSION_COOKIE, session)
        self.assertEqual(self.client.get("/").status_code, 303)
        self.client.cookies.clear()
        wrong = self.client.post("/login", headers=self.headers, data={"username": "pilot-owner", "password": "wrong"})
        self.assertEqual(wrong.status_code, 401)
        login = self.client.post("/login", headers=self.headers, data={
            "username": "pilot-owner", "password": self.password, "next": "/agents",
        })
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/agents")
        self.assertEqual(self.client.get("/agents").status_code, 200)

    def test_account_login_preserves_oauth_authorize_request(self):
        registration = self.client.post("/register", json={
            "redirect_uris": ["https://chatgpt.com/connector/oauth/synthetic"], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        })
        self.assertEqual(registration.status_code, 201)
        verifier = "A" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        agent_id = Store(self.config.database).add_agent(STAGING_WORKSPACE, "Synthetic agent")
        params = {
            "client_id": registration.json()["client_id"], "response_type": "code", "scope": "message",
            "resource": self.origin + "/mcp", "redirect_uri": "https://chatgpt.com/connector/oauth/synthetic",
            "state": "synthetic-state", "code_challenge": challenge, "code_challenge_method": "S256",
        }
        auth = self.client.get("/authorize", params=params)
        target = parse_qs(urlsplit(auth.headers["location"]).query)["next"][0]
        self.assertEqual(urlsplit(target).path, "/authorize")
        self.assertIn("state=synthetic-state", target)
        self.assertEqual(self.setup_account(next=target).headers["location"], target)
        consent_uri = self.client.get(target).headers["location"]
        self.assertTrue(consent_uri.startswith("/oauth/consent?"))
        handle = parse_qs(urlsplit(consent_uri).query)["request"][0]
        approval = self.client.post("/oauth/consent", headers=self.headers, data={
            "request": handle, "agent": agent_id, "decision": "approve",
        })
        code = parse_qs(urlsplit(approval.headers["location"]).query)["code"][0]
        token = self.client.post("/token", data={
            "grant_type": "authorization_code", "client_id": params["client_id"], "code": code,
            "redirect_uri": params["redirect_uri"], "resource": params["resource"], "code_verifier": verifier,
        }).json()["access_token"]
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_agents"}}
        headers = {"Accept": "application/json, text/event-stream"}
        self.assertEqual(self.client.post("/mcp", headers=headers, json=rpc).status_code, 401)
        response = self.client.post("/mcp", headers=headers | {"Authorization": f"Bearer {token}"}, json=rpc)
        self.assertEqual(response.json()["result"]["structuredContent"]["identity"]["id"], agent_id)
        self.assertEqual(return_path("///authorize"), "/")
        self.assertEqual(return_path("/\\unauthorized.example"), "/")

    def test_sign_out_on_populated_activity(self):
        store = Store(self.config.database)
        sender = store.add_agent(STAGING_WORKSPACE, "Synthetic sender")
        recipient = store.add_agent(STAGING_WORKSPACE, "Synthetic receiver")
        store.issue_token_for_agent(STAGING_WORKSPACE, recipient)
        store.send(STAGING_WORKSPACE, sender, recipient, "Synthetic message", "test-request")
        self.assertEqual(self.setup_account().status_code, 303)
        activity = self.client.get("/")
        self.assertIn("Synthetic message", activity.text)
        self.assertIn('method="post" action="/logout"', activity.text)
        self.assertIn("Sign out", activity.text)
        session = self.client.cookies.get(SESSION_COOKIE)
        self.assertEqual(self.client.post("/logout", headers=self.headers).status_code, 303)
        self.client.cookies.set(SESSION_COOKIE, session)
        self.assertEqual(self.client.get("/").status_code, 303)
