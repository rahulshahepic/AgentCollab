import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from agentcollab.app import LOCAL_WORKSPACE, create_app
from agentcollab.staging import RAILWAY_HEALTH_HOST, STAGING_WORKSPACE, StagingConfig
from agentcollab.store import Store

OWNER_PASSWORD = "synthetic-staging-password-for-tests-only"


class StagingTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.env = {
            "RAILWAY_PUBLIC_DOMAIN": "agentcollab-staging.up.railway.app",
            "RAILWAY_ENVIRONMENT_NAME": "staging",
            "RAILWAY_VOLUME_MOUNT_PATH": directory,
            "PORT": "3000",
            "AGENTCOLLAB_OWNER_PASSWORD_SHA256": sha256(OWNER_PASSWORD.encode()).hexdigest(),
        }
        self.config = StagingConfig.from_environment(self.env)
        self.store = Store(self.config.database)
        self.client = self.enterContext(TestClient(self.app(), base_url=self.config.origin))
        self.owner = ("owner", OWNER_PASSWORD)
        self.form = {"Origin": self.config.origin, "Accept": "application/json"}

    def app(self):
        return create_app(self.config.database, self.config.port, staging=self.config)

    def rpc(self, method, params, *, token=None, auth=None, origin=None, client=None):
        headers = {"Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        return (client or self.client).post("/mcp", headers=headers, auth=auth, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        })

    def tool(self, name, token, *, client=None, **arguments):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments}, token=token, client=client)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def credential(self, agent):
        response = self.client.post(f"/agents/{agent}/credential", auth=self.owner, headers=self.form)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["token"]

    def test_startup_requires_complete_staging_configuration(self):
        self.assertEqual(self.config.database, Path(self.env["RAILWAY_VOLUME_MOUNT_PATH"]) / "agentcollab-staging.sqlite3")
        for key, invalid in {
            "RAILWAY_PUBLIC_DOMAIN": "attacker.example/path",
            "RAILWAY_ENVIRONMENT_NAME": "production",
            "RAILWAY_VOLUME_MOUNT_PATH": "/",
            "PORT": "70000",
            "AGENTCOLLAB_OWNER_PASSWORD_SHA256": "password",
        }.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                StagingConfig.from_environment(self.env | {key: invalid})

    def test_private_web_origin_and_health_check(self):
        for path in ("/", "/agents", "/static/app.css"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get("/", auth=("owner", "incorrect")).status_code, 401)
        self.assertEqual(self.client.get("/", auth=self.owner).status_code, 200)
        self.assertEqual(self.client.get("/health", headers={"Host": RAILWAY_HEALTH_HOST}).status_code, 200)
        self.assertEqual(self.client.get("/agents", headers={"Host": RAILWAY_HEALTH_HOST}, auth=self.owner).status_code, 404)
        self.assertEqual(self.client.get("/", headers={"Host": "foreign.example"}, auth=self.owner).status_code, 400)
        self.assertEqual(self.client.post("/agents", auth=self.owner, headers={"Origin": "https://foreign.example"},
                                          data={"name": "Blocked"}).status_code, 403)
        self.assertEqual(self.client.post("/agents", auth=self.owner, headers=self.form, data={"name": "Synthetic"},
                                          follow_redirects=False).status_code, 303)
        self.assertIn("Synthetic", self.client.get("/agents", auth=self.owner).text)

    def test_custom_origin_sets_oauth_and_request_boundaries(self):
        old_origin = self.config.origin
        origin = "https://staging.agentcollab.example"
        self.config = StagingConfig.from_environment(self.env | {"AGENTCOLLAB_STAGING_ORIGIN": origin})
        with TestClient(self.app(), base_url=origin) as client:
            resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
            self.assertEqual(resource["resource"], f"{origin}/mcp")
            metadata = client.get("/.well-known/oauth-authorization-server").json()
            self.assertEqual(metadata["issuer"].rstrip("/"), origin)
            self.assertEqual(client.get("/health", headers={"Host": RAILWAY_HEALTH_HOST}).status_code, 200)
            self.assertEqual(client.get("/agents", headers={"Host": old_origin.removeprefix("https://")},
                                        auth=self.owner).status_code, 400)
            for form_origin, status in ((old_origin, 403), (origin, 303)):
                self.assertEqual(client.post("/agents", auth=self.owner, headers={"Origin": form_origin},
                                             data={"name": "Synthetic"}, follow_redirects=False).status_code, status)

    def test_custom_origin_rejects_malformed_values(self):
        for invalid in ("", "http://staging.example", "https://staging.example/",
                        "https://staging.example:443", "https://staging.example?x=1",
                        "https://user@staging.example", "https://127.0.0.1", f"https://{RAILWAY_HEALTH_HOST}"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                StagingConfig.from_environment(self.env | {"AGENTCOLLAB_STAGING_ORIGIN": invalid})

    def test_mcp_isolation_rotation_and_round_trip_across_restart(self):
        local_id = self.store.add_agent(LOCAL_WORKSPACE, "Local preview")
        local_token = self.store.issue_token_for_agent(LOCAL_WORKSPACE, local_id)
        self.assertNotIn("Local preview", self.client.get("/agents", auth=self.owner).text)
        self.assertEqual(self.client.post(f"/agents/{local_id}/credential", auth=self.owner,
                                          headers=self.form).status_code, 400)
        self.assertEqual(self.rpc("tools/list", {}).status_code, 401)
        self.assertEqual(self.rpc("tools/list", {}, auth=self.owner).status_code, 401)
        self.assertEqual(self.rpc("tools/list", {}, token=local_token).status_code, 401)
        sender = self.store.add_agent(STAGING_WORKSPACE, "Synthetic sender")
        receiver = self.store.add_agent(STAGING_WORKSPACE, "Synthetic receiver")
        original = self.credential(sender)
        sender_token = self.credential(sender)
        receiver_token = self.credential(receiver)
        self.assertEqual(self.rpc("tools/list", {}, token=original).status_code, 401)
        self.assertEqual(self.rpc("tools/list", {}, token=sender_token, origin="https://foreign.example").status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Authorization": f"Bearer {sender_token}"}).status_code, 401)
        self.assertEqual(self.tool("list_agents", sender_token)["identity"]["id"], sender)
        message = self.tool("send_message", sender_token, to_agent_id=receiver,
                            text="Synthetic staging request", request_id="staging-check")
        with TestClient(self.app(), base_url=self.config.origin) as restarted:
            inbox = self.tool("read_inbox", receiver_token, client=restarted, message_id=message["message_id"])
            self.assertEqual(inbox["messages"][0]["id"], message["message_id"])
            self.tool("reply_message", receiver_token, client=restarted, message_id=message["message_id"],
                      text="Synthetic staging reply")
        self.assertIn("Synthetic staging reply", self.client.get("/", auth=self.owner).text)
        response = self.client.post(f"/agents/{receiver}/disconnect", auth=self.owner,
                                    headers=self.form, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.rpc("tools/list", {}, token=receiver_token).status_code, 401)
