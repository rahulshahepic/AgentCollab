import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

import httpx

from agentcollab.app import create_app
from agentcollab.staging import StagingConfig
from scripts.staging_smoke import FIXTURE_ORIGIN, SmokeFailure, run_smoke


class StagingSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_and_verify_after_restart(self):
        directory = self.enterContext(TemporaryDirectory())
        username = "fixture-owner"
        password = "synthetic-test-owner-password"
        config = StagingConfig(
            FIXTURE_ORIGIN, 3000, Path(directory) / "staging.sqlite3", "",
            "A" * 43, int(time()) + 100, True,
        )

        def new_app():
            return create_app(config.database, config.port, staging=config)

        app = new_app()
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(base_url=FIXTURE_ORIGIN, transport=transport) as web:
                response = await web.post("/setup", headers={"Origin": FIXTURE_ORIGIN}, data={
                    "code": config.owner_setup_code, "username": username, "password": password,
                })
                self.assertEqual(response.status_code, 303)
            await run_smoke(FIXTURE_ORIGIN, username, password, "0123456789abcdef", transport=transport)

        restarted = new_app()
        async with restarted.router.lifespan_context(restarted):
            await run_smoke(
                FIXTURE_ORIGIN, username, password, "0123456789abcdef",
                activity_only=True, transport=httpx.ASGITransport(app=restarted),
            )

    async def test_refuses_shared_staging(self):
        async def record_request(request):
            self.fail("Smoke tried to connect to a non-fixture origin")

        with self.assertRaisesRegex(SmokeFailure, "isolated"):
            await run_smoke(
                "https://agentcollab-staging.up.railway.app", "owner", "unused", "0123456789abcdef",
                transport=httpx.MockTransport(record_request),
            )
