"""call/server.py: who may post to it, and that a text or a share always names its agent."""

import importlib.util
import unittest
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:  # the call's own packages are in the project's venv: run the suite with its python
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

spec = importlib.util.spec_from_file_location("call_server", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class Routes(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_post("/text", server.texted)
        app.router.add_post("/share/tell", server.share_tell)
        app.router.add_get("/chat/minions", server.chat_minions)
        app.router.add_post("/chat/minions/view", server.chat_minion_view)
        app.router.add_post("/chat/minions/decide", server.chat_minion_decide)
        return app

    def origin(self):
        return {"Origin": f"https://{self.server.host}:{self.server.port}"}

    async def test_another_sites_page_is_refused(self):
        answer = await self.client.post("/text", json={"text": "hi", "session": "claude-1"},
                                        headers={"Origin": "https://evil.example"})
        self.assertEqual(answer.status, 403)

    async def test_a_text_without_an_agent_is_refused(self):
        with mock.patch.object(server, "to_agent") as to_agent:
            answer = await self.client.post("/text", json={"text": "hi", "session": ""}, headers=self.origin())
            self.assertEqual(answer.status, 400)
            to_agent.assert_not_called()

    async def test_a_text_goes_to_the_agent_named_without_switching(self):
        with mock.patch.object(server, "to_agent", return_value=("Claude", "")) as to_agent:
            answer = await self.client.post("/text", json={"text": " hi  there ", "session": "claude-1"},
                                            headers=self.origin())
            self.assertEqual(await answer.json(), {"to": "Claude", "queued": False})
            to_agent.assert_called_once_with("hi there", "claude-1", server.Heard.TEXTED, False, False)

    async def test_a_share_off_a_call_without_an_agent_is_refused(self):
        with mock.patch.object(server, "in_call", return_value=False), mock.patch.object(server, "to_agent") as to_agent:
            answer = await self.client.post("/share/tell", json={"text": "a link", "session": ""}, headers=self.origin())
            self.assertEqual(answer.status, 400)
            to_agent.assert_not_called()

    async def test_files_that_never_arrived_are_refused(self):
        answer = await self.client.post("/share/tell", json={"files": ["nope.bin"], "session": "claude-1"},
                                         headers=self.origin())
        self.assertEqual(answer.status, 400)

    async def test_delegated_job_is_scoped_to_the_named_chat(self):
        shown = {"boss": "boss", "used": 1, "slots": 8, "minions": []}
        with mock.patch.object(server.chains, "view", return_value=shown) as view:
            answer = await self.client.get("/chat/minions?session=boss", headers=self.origin())
        self.assertEqual(await answer.json(), {"chain": shown})
        view.assert_called_once_with("boss")

        with mock.patch.object(server.chains, "note_phone_view") as note, \
                mock.patch.object(server.chains, "view", return_value=shown):
            answer = await self.client.post("/chat/minions/view", json={"session": "child"}, headers=self.origin())
        self.assertEqual(await answer.json(), {"chain": shown})
        note.assert_called_once_with("child")

    async def test_end_or_keep_requires_a_waiting_request(self):
        with mock.patch.object(server.chains, "decide_end", return_value=True) as decide:
            answer = await self.client.post("/chat/minions/decide", json={"session": "child", "decision": "keep"},
                                            headers=self.origin())
        self.assertEqual(await answer.json(), {"ok": True})
        decide.assert_called_once_with("child", "keep")

        with mock.patch.object(server.chains, "decide_end", return_value=True), \
                mock.patch.object(server.minions, "end_session", return_value=0) as ended:
            answer = await self.client.post("/chat/minions/decide", json={"session": "child", "decision": "end"},
                                            headers=self.origin())
        self.assertEqual(await answer.json(), {"ok": True})
        ended.assert_called_once_with("child", force=True)


class ToAgent(unittest.TestCase):
    def test_an_unreachable_agent_gets_nothing(self):
        with mock.patch.object(server, "recent_speakers", return_value=[]), \
                mock.patch.object(server.routing, "deliver") as deliver:
            got, why = server.to_agent("hi", "claude-1", server.Heard.TEXTED, False)
            self.assertEqual(got, "")
            self.assertIn("isn't reachable", why)
            deliver.assert_not_called()

    def test_typed_to_exactly_that_agent(self):
        target = mock.Mock(session="claude-1", spoken=lambda: "Claude")
        with mock.patch.object(server, "recent_speakers", return_value=[target]), \
                mock.patch.object(server.routing, "reachable", return_value=True), \
                mock.patch.object(server.routing, "switch_to") as switch, \
                mock.patch.object(server.routing, "deliver", return_value=True) as deliver:
            self.assertEqual(server.to_agent("hi", "claude-1", server.Heard.TEXTED, False), ("Claude", ""))
            deliver.assert_called_once_with("hi", server.Heard.TEXTED, to=target, urgent=False)
            switch.assert_not_called()


class Owns(unittest.TestCase):
    def test_a_missing_pid_does_not_own_the_live_pane(self):
        server.write_json(server.SPAWNED, {"panes": [{"pane": "%1", "pid": 9}]}, private=True)
        with mock.patch.object(server.tmux, "pane_pids", return_value={"%1": 9}):
            self.assertFalse(server.owns("%1", 0))
            self.assertFalse(server.owns("%1", 7))
            self.assertTrue(server.owns("%1", 9))

    def test_an_unanswered_tmux_does_not_own_a_saved_pid(self):
        server.write_json(server.SPAWNED, {"panes": [{"pane": "%1", "pid": 9}]}, private=True)
        with mock.patch.object(server.tmux, "pane_pids", return_value=None):
            self.assertFalse(server.owns("%1", 9))
        self.assertEqual(server.read_json(server.SPAWNED).get("panes"), [{"pane": "%1", "pid": 9}])

    def test_a_replaced_process_is_not_owned(self):
        server.write_json(server.SPAWNED, {"panes": [{"pane": "%1", "pid": 9}]}, private=True)
        with mock.patch.object(server.tmux, "pane_pids", return_value={"%1": 12}):
            self.assertFalse(server.owns("%1", 9))
            self.assertFalse(server.owns("%1", 12))


if __name__ == "__main__":
    unittest.main()
