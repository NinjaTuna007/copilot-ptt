"""call/server.py: plan display and approval stay scoped to the chat's agent."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:  # the call's own packages are in the project's venv: run the suite with its python
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

spec = importlib.util.spec_from_file_location("call_server_plans", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class PlanRoutes(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_post("/chat/plan/build", server.chat_plan_build)
        return app

    def origin(self):
        return {"Origin": f"https://{self.server.host}:{self.server.port}"}

    async def test_build_uses_the_same_session_owned_plan_as_the_chat(self):
        project = Path("/home/me/henchline")
        target = mock.Mock(session="codex-00000000-0000-0000-0000-000000000001")
        plan = {"name": "Codex plan", "file": "/home/me/.codex/plans/mine.plan.md"}
        with mock.patch.object(server, "recent_speakers", return_value=[target]), \
                mock.patch.object(server, "project_of", return_value=project), \
                mock.patch.object(server.plans, "of_session", return_value=plan) as owned, \
                mock.patch.object(server.plans, "find") as project_wide, \
                mock.patch.object(server, "to_agent", return_value=("Codex", "")) as to_agent:
            answer = await self.client.post("/chat/plan/build", json={"session": target.session},
                                            headers=self.origin())

        self.assertEqual(await answer.json(), {"to": "Codex"})
        owned.assert_called_once_with(target.session, project)
        project_wide.assert_not_called()
        to_agent.assert_called_once_with(server.plans.approval(plan), target.session, server.Heard.TEXTED, False)


if __name__ == "__main__":
    unittest.main()
