"""voice/chains.py: a boss may deploy, a child may not, and a report carries each correction once."""

import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # first: a throwaway HOME
from voice import chains
from voice.desktop import Tab
from voice.minions import deploy, end, report
from voice.state import CALL_PINNED, SPEAKERS, Speaker

assert sandbox.HOME


class Chains(unittest.TestCase):
    def setUp(self):
        if chains.CHAINS.is_dir():
            for path in chains.CHAINS.glob("*.jsonl"):
                path.unlink()
        if SPEAKERS.is_dir():
            for path in SPEAKERS.glob("*.json"):
                path.unlink()

    def test_a_depth_2_child_cannot_deploy(self):
        chain = chains.open("boss")
        self.assertEqual(chains.open("boss", max_depth=9, slots=99), chain)  # reopening does not raise knobs
        chains.append(chain, "launch", parent="boss", session="child", pane="%1", pid=1, depth=2, may_deploy=True)
        self.assertEqual(chains.parent_of("child"), "boss")
        self.assertEqual(chains.allow(chain, "boss", 1), "")
        self.assertEqual(chains.allow(chain, "child", 2), "only the boss may deploy")
        self.assertEqual(chains.allow(chain, "boss", 2), "too deep")
        self.assertEqual(chains.open("child", max_depth=9, slots=99), chain)
        self.assertEqual(chains.knobs(chain), {"max_depth": 2, "slots": 8})
        launch = chains.history(chain)[-1]
        self.assertFalse(launch["may_deploy"])
        self.assertEqual(launch["depth"], 2)
        child = Speaker("child", "claude", "job", Tab("tmux", "%1"))
        with mock.patch("voice.desktop.tmux.start") as start, mock.patch("voice.minions._server") as server:
            code = deploy(["--agent", "claude", "--folder", "/tmp/nope", "--ask", "go deeper"], lambda: child)
        self.assertEqual(code, 1)
        start.assert_not_called()
        server.assert_not_called()

    def test_the_ninth_start_is_refused(self):
        chain = chains.open("boss")
        for i in range(8):
            self.assertEqual(chains.allow(chain, "boss", 1), "", i)
            chains.append(chain, "launch", parent="boss", session=f"c{i}", pane=f"%{i}", pid=i + 1, depth=2)
        self.assertEqual(chains.allow(chain, "boss", 1), "no slots left")
        chains.append(chain, "ended", session="c0", pane="%0")  # a slot is a live child
        self.assertEqual(chains.allow(chain, "boss", 1), "")

    def test_a_correction_is_prepended_to_the_next_report_and_then_consumed(self):
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", pane="%1", pid=3, depth=2, agent="claude", where="job")
        chains.append(chain, "correction", session="child", text="use the lock")
        boss = Speaker("boss", "claude", "job", Tab("tmux", "%9"))
        boss.save(SPEAKERS / "boss.json")
        child = Speaker("child", "claude", "job", Tab("tmux", "%1"))
        CALL_PINNED.write_text("stay-with-boss")

        import routing
        with mock.patch("routing.deliver", return_value=False):
            self.assertEqual(report(["the pane is up"], lambda: child), 1)
        self.assertEqual(chains.corrections_since("child"), ["use the lock"])  # a failed delivery keeps it
        self.assertEqual(CALL_PINNED.read_text(), "stay-with-boss")

        seen = {}

        def fake(text, heard, to=None, urgent=False):
            seen["text"] = text
            seen["heard"] = heard
            seen["session"] = to.session
            return True

        with mock.patch("routing.deliver", side_effect=fake):
            self.assertEqual(report(["the pane is up"], lambda: child), 0)
        self.assertTrue(seen["text"].startswith(chains.REPORT))
        self.assertLess(seen["text"].find("use the lock"), seen["text"].find("the pane is up"))
        self.assertEqual(seen["heard"], routing.Heard.SCHEDULED)
        self.assertEqual(seen["session"], "boss")
        self.assertEqual(chains.corrections_since("child"), [])
        self.assertTrue(any(e.get("kind") == "report" and e.get("text") == "the pane is up"
                           for e in chains.history(chain)))
        self.assertEqual(CALL_PINNED.read_text(), "stay-with-boss")
        chains.append(chain, "correction", session="child", text="and the other file")
        self.assertEqual(chains.corrections_since("child"), ["and the other file"])

    def test_end_records_ended_and_a_sibling_keeps_the_boss(self):
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", session="child-a", pane="%1", pid=1, depth=2)
        chains.append(chain, "launch", parent="boss", session="child-b", pane="%2", pid=2, depth=2)
        for session, pane, pid in (("boss", "%9", 9), ("child-a", "%1", 1), ("child-b", "%2", 2)):
            Speaker(session, "claude", "job", Tab("tmux", pane), pid=pid).save(SPEAKERS / f"{session}.json")
        panes = [{"pane": "%1", "pid": 1}, {"pane": "%2", "pid": 2}]

        def spawned(change=None):
            nonlocal panes
            if change is not None:
                panes = change(list(panes))
            return [e["pane"] for e in panes]

        server = mock.Mock()
        server.spawned.side_effect = spawned
        server.owns.side_effect = lambda pane, pid=0: any(
            e["pane"] == pane and (not pid or e["pid"] == pid) for e in panes)
        with mock.patch("voice.desktop.tmux.end", return_value=True) as ended, \
                mock.patch("voice.desktop.tmux.resume") as resume, \
                mock.patch("voice.minions._server", return_value=server):
            self.assertEqual(end(["child-a"]), 0)
            self.assertEqual(end(["nobody"]), 1)
        self.assertTrue(any(e["kind"] == "ended" and e["session"] == "child-a" for e in chains.history(chain)))
        self.assertFalse(any(e.get("kind") == "ended" and e.get("session") == "child-b" for e in chains.history(chain)))
        self.assertEqual(chains.parent_of("child-b"), "boss")
        self.assertEqual(chains.parent_of("child-a"), "boss")
        self.assertEqual(panes, [{"pane": "%2", "pid": 2}])
        self.assertTrue((SPEAKERS / "child-b.json").is_file())
        self.assertFalse((SPEAKERS / "child-a.json").exists())
        self.assertEqual(ended.call_count, 1)
        self.assertEqual(ended.call_args.args[0].address, "%1")
        resume.assert_not_called()

    def test_a_user_message_is_a_correction_and_a_report_is_not(self):
        chains.open("boss")
        chain = chains.chain_of("boss")
        chains.append(chain, "launch", parent="boss", pane="%1", pid=5, depth=2, agent="claude", where="job")
        speaker = Speaker("child", "claude", "job", Tab("tmux", "%1"))
        import routing
        terms = mock.Mock()
        terms.agent_running.return_value = True
        terms.type_line.return_value = True
        desk = mock.Mock()
        desk.return_value.terminals = terms
        with mock.patch("routing.desktop", desk):
            self.assertTrue(routing.deliver_words("use the lock", routing.Heard.TEXTED, to=speaker))
            self.assertEqual(chains.parent_of("child"), "boss")
            self.assertEqual(chains.corrections_since("child"), ["use the lock"])
            self.assertEqual(terms.type_line.call_count, 1)
            self.assertIn("use the lock", terms.type_line.call_args.args[1])
            self.assertEqual(terms.type_line.call_args.args[0].address, "%1")
            self.assertTrue(routing.deliver_words(f"{chains.REPORT} all good", routing.Heard.SCHEDULED, to=speaker))
        self.assertEqual(chains.corrections_since("child"), ["use the lock"])
        self.assertEqual(terms.type_line.call_count, 2)
        self.assertTrue(terms.type_line.call_args.args[1].startswith(chains.REPORT))
        self.assertEqual(terms.type_line.call_args.args[0].address, "%1")

    def test_deploy_starts_a_new_pane_and_types_the_ask(self):
        folder = Path.home() / "workproj"
        folder.mkdir(exist_ok=True)
        terms = mock.Mock()
        terms.type_line.return_value = True
        desk = mock.Mock(terminals=terms)
        recorded = {}

        def spawned(change=None):
            if change is not None:
                recorded["alive"] = change([{"pane": "%old", "pid": 1}])
            return ["%7"]

        def typed(given):
            return folder if Path(given).expanduser().resolve() == folder else None

        server = mock.Mock()
        server.trusted_folders.return_value = []
        server.typed_folder.side_effect = typed
        server.spawned.side_effect = spawned
        boss = Speaker("boss", "claude", "job", Tab("tmux", "%3"))
        with mock.patch("voice.desktop.tmux.start", return_value=Tab("tmux", "%7")) as start, \
                mock.patch("voice.desktop.tmux.resume") as resume, \
                mock.patch("voice.desktop.tmux.pane_pid", return_value=42), \
                mock.patch("voice.desktop.tmux.accept_trust", return_value=True), \
                mock.patch("voice.desktop.current", return_value=desk), \
                mock.patch("voice.minions._server", return_value=server):
            code = deploy(["--agent", "claude", "--folder", str(folder), "--ask", "fix the parser"], lambda: boss)
        self.assertEqual(code, 0)
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0], "claude")
        self.assertEqual(start.call_args.args[1], folder)
        resume.assert_not_called()
        typed_text = terms.type_line.call_args.args[1]
        self.assertIn("handsfree report", typed_text)
        self.assertIn("fix the parser", typed_text)
        self.assertEqual(recorded["alive"][-1], {"pane": "%7", "pid": 42})
        launch = chains.history(chains.chain_of("boss"))[-1]
        self.assertEqual(launch["kind"], "launch")
        self.assertEqual(launch["pane"], "%7")
        self.assertEqual(launch["pid"], 42)
        self.assertEqual(launch["parent"], "boss")
        self.assertEqual(launch["depth"], 2)
        self.assertFalse(launch["may_deploy"])
        self.assertNotIn(str(folder), __import__("json").dumps(launch))
        for args, _kwargs in server.notice.call_args_list:
            blob = " ".join(str(part) for part in args)
            self.assertNotIn(str(folder), blob)
            self.assertIn(folder.name, blob)

    def test_deploy_refuses_a_folder_the_server_does_not(self):
        server = mock.Mock()
        server.trusted_folders.return_value = []
        server.typed_folder.return_value = None
        with mock.patch("voice.desktop.tmux.start") as start, \
                mock.patch("voice.minions._server", return_value=server):
            code = deploy(["--agent", "cursor", "--folder", "/tmp/not-yours", "--ask", "look"], lambda: Speaker("boss", "cursor"))
        self.assertEqual(code, 1)
        start.assert_not_called()

    def test_ending_a_session_frees_a_launch_that_never_named_it(self):
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", pane="%0", pid=7, depth=2, agent="claude", where="job")
        session = "claude-11111111-1111-1111-1111-111111111111"
        Speaker(session, "claude", "job", Tab("tmux", "%0"), pid=7).save(SPEAKERS / f"{session}.json")
        panes = [{"pane": "%0", "pid": 7}]

        def spawned(change=None):
            nonlocal panes
            if change is not None:
                panes = change(list(panes))
            return [e["pane"] for e in panes]

        server = mock.Mock()
        server.spawned.side_effect = spawned
        server.owns.side_effect = lambda pane, pid=0: any(
            e["pane"] == pane and (not pid or e["pid"] == pid) for e in panes)
        with mock.patch("voice.desktop.tmux.end", return_value=True), \
                mock.patch("voice.minions._server", return_value=server), \
                mock.patch("voice.minions._hand_up"):
            self.assertEqual(end([session]), 0)
        self.assertEqual(chains.allow(chain, "boss", 1), "")
        self.assertTrue(any(e["kind"] == "ended" and e["pane"] == "%0" for e in chains.history(chain)))

    def test_ending_one_pid_leaves_the_other_launch_on_that_pane(self):
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", pane="%1", pid=7, depth=2)
        chains.append(chain, "launch", parent="boss", pane="%1", pid=9, depth=2)
        chains.close_child("old", "%1", 7)
        self.assertEqual(chains.bind("still", "%1", 9), "boss")
        self.assertEqual(chains.bind("gone", "%1", 7), "")
        self.assertEqual(chains.allow(chain, "boss", 1), "")

    def test_an_ended_line_without_a_pid_leaves_a_known_pid(self):
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", pane="%1", pid=9, depth=2)
        for i in range(7):
            chains.append(chain, "launch", parent="boss", pane=f"%{i + 2}", pid=i + 1, depth=2)
        chains.append(chain, "ended", pane="%1")
        self.assertEqual(chains.allow(chain, "boss", 1), "no slots left")
        self.assertEqual(chains.bind("still", "%1", 9), "boss")
        chains.close_child("", "%1", 0)
        self.assertEqual(chains.bind("still-open", "%1", 9), "boss")

    def test_an_ended_pid_leaves_a_launch_that_recorded_none(self):
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", pane="%1", depth=2)
        for i in range(7):
            chains.append(chain, "launch", parent="boss", pane=f"%{i + 2}", pid=i + 1, depth=2)
        chains.append(chain, "ended", pane="%1", pid=9)
        self.assertEqual(chains.allow(chain, "boss", 1), "no slots left")
        self.assertEqual(chains.bind("still", "%1"), "boss")
        chains.close_child("", "%1", 9)
        self.assertEqual(chains.bind("still-open", "%1"), "boss")

    def test_a_missing_pid_does_not_end_someone_elses_launch(self):
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", session="live", pane="%1", pid=9, depth=2)
        chains.close_child("ender", "%1", 0)
        self.assertEqual(chains.bind("fresh", "%1", 9), "boss")
        self.assertFalse(any(e.get("kind") == "ended" for e in chains.history(chain)))
        self.assertEqual(chains.parent_of("ender"), "")

    def test_two_lines_with_no_pid_on_one_pane_do_close(self):
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", pane="%1", depth=2)
        chains.append(chain, "ended", pane="%1")
        self.assertEqual(chains.bind("gone", "%1"), "")

    def test_a_correction_under_the_folder_name_reaches_the_chat_id(self):
        from voice.state import renamed
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", pane="%1", pid=5, depth=2, agent="cursor", where="workproj")
        folder_key = "cursor-_home_shekharu_workproj"
        chat = "cursor-22222222-2222-2222-2222-222222222222"
        chains.append(chain, "correction", session=folder_key, text="use the lock", pane="%1", pid=5)
        renamed(folder_key, chat)
        self.assertEqual(chains.bind(chat, "%1", 5), "boss")
        self.assertEqual(chains.corrections_since(chat), ["use the lock"])
        self.assertEqual(chains.bind("cursor-33333333-3333-3333-3333-333333333333", "%1", 9), "")
        self.assertEqual(chains.parent_of(chat), "boss")

    def test_end_waits_while_the_user_is_in_the_chat(self):
        from voice.state import LAST
        chain = chains.open("boss")
        chains.append(chain, "launch", parent="boss", session="child", pane="%1", pid=1, depth=2)
        Speaker("child", "claude", "job", Tab("tmux", "%1"), pid=1).save(SPEAKERS / "child.json")
        Speaker("child", "claude", "job", Tab("tmux", "%1"), pid=1).save(LAST)
        server = mock.Mock()
        server.owns.return_value = True
        with mock.patch("voice.desktop.tmux.end") as ended, \
                mock.patch("voice.minions._server", return_value=server):
            self.assertEqual(end(["child"]), 1)
        ended.assert_not_called()
        self.assertFalse(any(e.get("kind") == "ended" for e in chains.history(chain)))

    def test_a_trust_question_that_stays_up_does_not_take_a_slot(self):
        folder = Path.home() / "workproj"
        folder.mkdir(exist_ok=True)
        server = mock.Mock()
        server.trusted_folders.return_value = []
        server.typed_folder.return_value = folder
        boss = Speaker("boss", "claude", "job", Tab("tmux", "%3"))
        with mock.patch("voice.desktop.tmux.start", return_value=Tab("tmux", "%7")), \
                mock.patch("voice.desktop.tmux.end", return_value=True) as ended, \
                mock.patch("voice.desktop.tmux.accept_trust", return_value=False), \
                mock.patch("voice.minions._server", return_value=server):
            code = deploy(["--agent", "claude", "--folder", str(folder), "--ask", "go"], lambda: boss)
        self.assertEqual(code, 1)
        ended.assert_called_once()
        self.assertFalse(any(e.get("kind") == "launch" for e in chains.history(chains.chain_of("boss"))))
        server.spawned.assert_not_called()

    def test_report_without_a_boss_fails(self):
        self.assertEqual(report(["done"], lambda: Speaker("lone", "claude")), 1)
        self.assertEqual(report([], lambda: Speaker("lone", "claude")), 1)


if __name__ == "__main__":
    unittest.main()
