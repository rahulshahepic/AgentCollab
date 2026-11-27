# Isolated Railway staging fixture

Use this service for synthetic owner and agent tests. The shared staging service and its owner enrollment are separate.

1. In the AgentCollab project, select the `staging` environment and create the `AgentCollab Fixture` service in that environment only. Use the reviewed `staging` branch as its source and verify the GitHub `test` job passed for the commit you deploy. Enable Wait for CI on the source before relying on future automatic deployments.
2. Give the fixture a generated HTTPS domain and its own private SQLite volume mounted at `/data`. Keep one replica. The repository's `railway.toml` runs `python -m agentcollab serve-staging` and checks `/health`.
3. On the fixture service only, set `AGENTCOLLAB_LOCAL_ACCOUNTS=1`. Generate a unique 32-byte URL-safe owner setup code and a short Unix expiry. Set `AGENTCOLLAB_OWNER_SETUP_CODE` and `AGENTCOLLAB_OWNER_SETUP_EXPIRES_AT` in the fixture service's variables. Keep them out of commands that log arguments, GitHub, and fixtures.
4. Verify the deployment's commit SHA and HTTPS `/health` response. In a fresh browser session, confirm that a protected browser route redirects to sign-in and that an anonymous MCP request cannot access the endpoint. On the fixture domain, open `/setup`, enroll a disposable owner, sign out, and sign back in at `/login`. Remove the one-time setup values from fixture service variables after enrollment. Keep local accounts enabled.

To rehearse persistence, restart the fixture deployment with its existing volume still mounted at `/data`. Confirm that HTTPS health returns, the synthetic owner can sign in again, and any fixture thread recorded before the restart is still visible. Record the deployed commit, Railway deployment, fixture URL, and sanitized results on issue #9. Keep credentials in the fixture's authorized secret storage.
