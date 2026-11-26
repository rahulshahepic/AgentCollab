import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from agentcollab.app import LOCAL_WORKSPACE, create_app
from agentcollab.store import Store


class MessagingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "test.sqlite3"
        self.store = Store(self.path)
        self.base = "http://127.0.0.1:8000"
        self.client = self.enterContext(TestClient(create_app(self.path), base_url=self.base))
        self.alice, self.alice_token = self.register("Researcher")
        self.bob, self.bob_token = self.register("Reviewer")

    def register(self, name, workspace=LOCAL_WORKSPACE):
        agent_id = self.store.add_agent(workspace, name)
        return agent_id, self.store.issue_token(workspace, name)

    def rpc(self, token, method, params):
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post("/mcp", headers=headers, json={
            "id": 1, "jsonrpc": "2.0", "method": method, "params": params,
        })

    def tool(self, token, name, **arguments):
        response = self.rpc(token, "tools/call", {"name": name, "arguments": arguments})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def result(self, token, name, **arguments):
        result = self.tool(token, name, **arguments)
        self.assertFalse(result.get("isError"), result)
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def send(self):
        return self.result(self.alice_token, "send_message", to_agent_id=self.bob,
                           text="Check the <draft>.", request_id="draft-1")

    def test_pair_round_trip_and_duplicate_requests(self):
        tools = self.rpc(self.alice_token, "tools/list", {}).json()["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools},
                         {"list_agents", "send_message", "read_inbox", "reply_message"})
        listing = self.result(self.alice_token, "list_agents")
        self.assertEqual(listing["identity"]["name"], "Researcher")
        self.assertEqual([agent["id"] for agent in listing["agents"]], [self.bob])
        sent = self.send()
        self.assertEqual(self.send(), sent)
        self.assertTrue(self.tool(self.alice_token, "send_message", to_agent_id=self.bob,
                                  text="Changed request", request_id="draft-1")["isError"])
        request_id = sent["message_id"]
        request = self.result(self.bob_token, "read_inbox", message_id=request_id)["messages"][0]
        self.assertEqual(request["type"], "request")
        self.assertIn("Fetched", self.client.get("/").text)
        reply = self.result(self.bob_token, "reply_message", message_id=request_id, text="Looks good.")
        self.assertEqual(reply, self.result(self.bob_token, "reply_message",
                                            message_id=request_id, text="Looks good."))
        self.assertTrue(self.tool(self.bob_token, "reply_message",
                                  message_id=request_id, text="Changed reply")["isError"])
        inbox = self.result(self.alice_token, "read_inbox")
        self.assertEqual([(item["id"], item["type"], item["reply_to"]) for item in inbox["messages"]],
                         [(reply["message_id"], "reply", request_id)])
        self.assertEqual(Store(self.path).activity(LOCAL_WORKSPACE)[0]["reply"], "Looks good.")
        activity = self.client.get("/").text
        self.assertIn("Check the &lt;draft&gt;.", activity)
        self.assertIn("Looks good.", activity)

    def test_workspace_isolation_and_revocation(self):
        outsider, token = self.register("Outsider", workspace="elsewhere")
        self.assertEqual(self.result(token, "list_agents")["agents"], [])
        message_id = self.send()["message_id"]
        self.assertTrue(self.tool(token, "read_inbox", message_id=message_id)["isError"])
        self.assertTrue(self.tool(self.alice_token, "send_message", to_agent_id=outsider,
                                  text="Blocked", request_id="cross-workspace")["isError"])
        self.assertEqual(self.rpc(None, "tools/list", {}).status_code, 401)
        self.store.disconnect(LOCAL_WORKSPACE, self.bob)
        self.assertEqual(self.rpc(self.bob_token, "tools/list", {}).status_code, 401)
        self.assertEqual(self.result(self.alice_token, "list_agents")["agents"], [])

    def test_browser_registration_and_limit(self):
        self.assertEqual(self.client.post("/agents", data={"name": "Blocked"}).status_code, 403)
        headers = {"Origin": self.base}
        for index in range(8):
            response = self.client.post("/agents", headers=headers, data={"name": f"Agent {index}"})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.store.list_agents(LOCAL_WORKSPACE)), 10)
        error = self.client.post("/agents", headers=headers, data={"name": "Extra"})
        self.assertEqual(error.status_code, 400)
        self.assertIn("10 active agents", error.text)
        self.client.post(f"/agents/{self.alice}/rename", headers=headers, data={"name": "Cosworth"})
        self.assertEqual(self.result(self.alice_token, "list_agents")["identity"]["name"], "Cosworth")
