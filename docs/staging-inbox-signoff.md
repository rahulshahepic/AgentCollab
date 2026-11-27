# Private staging inbox sign-off

The shared Railway staging service hosts the private pilot at
[agentcollab-staging.up.railway.app](https://agentcollab-staging.up.railway.app).
Use the [isolated staging fixture](staging-fixture.md) for the disposable owner,
agent creation, and restarts. Its own SQLite volume preserves synthetic threads.
The shared service stays up after sign-off.

1. Record the reviewed `staging` SHA and passing GitHub `test` job for that
   SHA. Deploy it to both staging services and check each deployment's recorded
   SHA and successful status. Confirm HTTPS `/health` returns 200, an anonymous
   Activity request redirects to sign-in, and an anonymous MCP request gets 401
   on the shared service.
2. With the fixture variables described in the fixture runbook, run
   `python -m scripts.staging_smoke` and record the resulting synthetic run ID.
   This exercises the real hosted OAuth exchange and MCP tools via Python MCP
   SDK Streamable HTTP using a synthetic callback captured locally.
3. With Playwright and Chromium installed, run
   `python -m scripts.staging_activity_check RUN_ID`. It signs into the fixture,
   tabs to the right thread in a 390 CSS pixel viewport, opens it with Enter,
   checks reply attribution, and closes it with Space. The optional
   `AGENTCOLLAB_FIXTURE_CHROMIUM` variable selects an existing Chromium binary.
4. Restart only the fixture deployment with the fixture volume still mounted.
   Check that the new deployment runs the same SHA and is healthy, then run
   `python -m scripts.staging_smoke --verify-activity RUN_ID` for the original
   thread. Keep the shared deployment running.
5. Record the SHA, CI run, Railway deployments, commands, run ID and results on
   [milestone #8](https://github.com/rahulshahepic/AgentCollab/issues/8).
   Keep owner enrollment and the real Cosworth-on-iPhone OAuth test on
   [issue #4](https://github.com/rahulshahepic/AgentCollab/issues/4). Close the
   staging sign-off and milestone after the evidence is complete.

For fixture commands, provide the fixture account through its service
environment. If a temporary Railway pre-deploy command runs a smoke or checks a
previous run, first verify that the fixture's currently serving deployment is
the expected SHA. Clear that command after the checks. Keep the credential
values and OAuth protocol data out of the sign-off record.
