import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agentcollab.pilot_accounts import INVITE_SECONDS, PilotAccounts
from agentcollab.store import Store


class PilotAccountTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.store = Store(Path(directory) / "pilot.sqlite3")
        self.addresses = ("one@example.test", "two@example.test", "three@example.test")
        self.accounts = PilotAccounts(self.store, self.addresses)
        self.password = "synthetic-pilot-password"

    def test_allowlist_one_time_links_and_workspace_isolation(self):
        self.assertIsNone(self.accounts.issue_invite("outsider@example.test"))
        self.assertIsNone(self.accounts.issue_reset(self.addresses[0]))
        workspaces = []
        for email in self.addresses:
            token = self.accounts.issue_invite(email)
            self.assertFalse(self.accounts.reset(token, self.password))
            self.assertTrue(self.accounts.enroll(token, self.password))
            self.assertFalse(self.accounts.enroll(token, self.password))
            self.assertIsNone(self.accounts.issue_invite(email))
            workspaces.append(self.accounts.workspace(self.accounts.authenticate(email, self.password)))
        self.assertEqual(len(set(workspaces)), 3)
        self.assertTrue(all(workspace.startswith("pilot-") for workspace in workspaces))
        for i, workspace in enumerate(workspaces):
            self.store.add_agent(workspace, f"Synthetic agent {i}")
        for i, workspace in enumerate(workspaces):
            self.assertEqual([agent["name"] for agent in self.store.list_agents(workspace)],
                             [f"Synthetic agent {i}"])

    def test_reset_invalidates_sessions_and_preserves_workspace(self):
        email = self.addresses[0]
        self.assertTrue(self.accounts.enroll(self.accounts.issue_invite(email), self.password))
        original = self.accounts.authenticate(email, self.password)
        another = self.accounts.authenticate(email, self.password)
        workspace = self.accounts.workspace(original)
        reopened = PilotAccounts(self.store, self.addresses)
        self.assertEqual(reopened.workspace(another), workspace)
        replaced = reopened.issue_reset(email)
        reset = reopened.issue_reset(email)
        new_password = "replacement-pilot-password"
        self.assertFalse(reopened.reset(replaced, new_password))
        self.assertTrue(reopened.reset(reset, new_password))
        self.assertFalse(reopened.reset(reset, new_password))
        self.assertIsNone(reopened.workspace(original))
        self.assertIsNone(reopened.workspace(another))
        self.assertIsNone(reopened.authenticate(email, self.password))
        session = reopened.authenticate(email, new_password)
        self.assertEqual(reopened.workspace(session), workspace)
        self.assertIsNone(PilotAccounts(self.store, ()).workspace(session))
        reopened.logout(session)
        self.assertIsNone(reopened.workspace(session))

    def test_invite_expires(self):
        with patch("agentcollab.pilot_accounts.time", return_value=100):
            token = self.accounts.issue_invite(self.addresses[0])
        with patch("agentcollab.pilot_accounts.time", return_value=100 + INVITE_SECONDS):
            self.assertFalse(self.accounts.enroll(token, self.password))
