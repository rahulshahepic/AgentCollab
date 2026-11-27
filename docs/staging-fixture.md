# Isolated Railway staging fixture

Use this service for synthetic owner and agent tests. The shared staging service and its owner enrollment are separate.

1. In the AgentCollab project, select the `staging` environment and create the `AgentCollab Fixture` service in that environment only. Use the reviewed `staging` branch as its source and verify the GitHub `test` job passed for the commit you deploy. Enable Wait for CI on the source before relying on future automatic deployments.
2. Give the fixture a generated HTTPS domain and its own private SQLite volume mounted at `/data`. Keep one replica. The repository's `railway.toml` runs `python -m agentcollab serve-staging` and checks `/health`.
3. On the fixture service only, set `AGENTCOLLAB_LOCAL_ACCOUNTS=1`. Generate a unique 32-byte URL-safe owner setup code and a short Unix expiry. Set `AGENTCOLLAB_OWNER_SETUP_CODE` and `AGENTCOLLAB_OWNER_SETUP_EXPIRES_AT` in the fixture service's variables. Keep them out of commands that log arguments, GitHub, and fixtures.
4. Verify the deployment's commit SHA and HTTPS `/health` response. In a fresh browser session, confirm that a protected browser route redirects to sign-in and that an anonymous MCP request cannot access the endpoint. On the fixture domain, open `/setup`, enroll a disposable owner, sign out, and sign back in at `/login`. Remove the one-time setup values from fixture service variables after enrollment. Keep local accounts enabled.

To rehearse persistence, restart the fixture deployment with its existing volume still mounted at `/data`. Confirm that HTTPS health returns, the synthetic owner can sign in again, and any fixture thread recorded before the restart is still visible. Record the deployed commit, Railway deployment, fixture URL, and sanitized results on issue #9. Keep credentials in the fixture's authorized secret storage.

## Repeatable hosted smoke

Run from the reviewed repository revision with the requirements installed.
Supply `AGENTCOLLAB_FIXTURE_TEST_USERNAME` and
`AGENTCOLLAB_FIXTURE_TEST_PASSWORD` from the fixture service's authorized
secret storage. Set `AGENTCOLLAB_FIXTURE_ORIGIN` to
`https://agentcollab-fixture-staging.up.railway.app`.

```sh
python -m scripts.staging_smoke
```

The runner signs in as the pre-enrolled disposable owner and creates two
synthetic agents with unique names. It uses the Python MCP SDK's Streamable
HTTP client with an OAuth access token for one agent and an independently
issued credential for the other. The OAuth registration uses a synthetic
ChatGPT callback. The client captures the redirect locally and never visits
the callback. It tests request, read, reply, read, Activity attribution, and
access and refresh revocation after disconnect. The runner refuses any other
origin, exits nonzero on failure, and reports only sanitized status and a
synthetic run ID.

Each run disconnects its temporary agents and retains the thread. If interrupted,
sign into the fixture's Agents page and disconnect the leftover agents with
the `Smoke credential` or `Smoke OAuth` names from that attempt. Run the
command again to create a fresh pair.

After a fixture-only Railway restart, verify the retained thread with the
same fixture credentials and the run ID printed by the successful smoke:

```sh
python -m scripts.staging_smoke --verify-activity RUN_ID
```
