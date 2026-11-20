"""The phone's "working…" line: whether a Claude or Codex session is in the middle of a turn, from its log."""

import json
import os
import time
import unittest
from pathlib import Path

import importlib.util

from tests import sandbox  # first: a throwaway HOME
from voice.state import Speaker

spec = importlib.util.spec_from_file_location("call_server_working", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

UUID = "11111111-2222-3333-4444-555555555555"


def claude_log(*entries: dict) -> Speaker:
    folder = Path.home() / ".claude/projects/-tmp-x"
    folder.mkdir(parents=True, exist_ok=True)
    big = {"type": "api-request-blob", "blob": "x" * 2_000_000}  # Claude's lines can be megabytes
    lines = [json.dumps(e) for e in entries[:-1]] + [json.dumps(big)] * 2 + [json.dumps(entries[-1])] \
        if len(entries) > 1 else [json.dumps(e) for e in entries]
    (folder / f"{UUID}.jsonl").write_text("\n".join(lines) + "\n")
    return Speaker(f"claude-{UUID}", "claude", "x")


def prompt(text: str = "do it") -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def tool_result() -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}


def assistant(stop: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "stop_reason": stop, "content": []}}


class Claude(unittest.TestCase):
    def test_prompt_then_tools_is_working(self):
        self.assertTrue(server.claude_working(claude_log(prompt(), assistant("tool_use"), tool_result())))

    def test_through_megabyte_lines(self):
        self.assertTrue(server.claude_working(claude_log(prompt(), tool_result())))

    def test_turn_end_is_idle(self):
        self.assertFalse(server.claude_working(claude_log(prompt(), assistant("end_turn"),
                                                          {"type": "system", "subtype": "turn_duration"})))

    def test_interrupted_is_idle(self):
        stopped = {"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}}
        self.assertFalse(server.claude_working(claude_log(prompt(), stopped)))

    def test_slash_command_is_not_a_turn(self):
        self.assertFalse(server.claude_working(claude_log(
            prompt(), {"type": "system", "subtype": "turn_duration"}, prompt("<command-name>/model</command-name>"))))

    def test_quiet_mid_turn_is_unknown(self):
        speaker = claude_log(prompt())
        old = time.time() - server.STALE - 60
        os.utime(Path.home() / f".claude/projects/-tmp-x/{UUID}.jsonl", (old, old))
        self.assertIsNone(server.claude_working(speaker))

    def test_quiet_after_the_turn_is_idle(self):
        speaker = claude_log(prompt(), {"type": "system", "subtype": "turn_duration"})
        old = time.time() - server.STALE - 60
        os.utime(Path.home() / f".claude/projects/-tmp-x/{UUID}.jsonl", (old, old))
        self.assertFalse(server.claude_working(speaker))

    def test_shell_mode_and_compaction_are_not_turns(self):
        done = {"type": "system", "subtype": "turn_duration"}
        self.assertFalse(server.claude_working(claude_log(prompt(), done, prompt("<bash-input>ls</bash-input>"))))
        summary = {**prompt("This session is being continued..."), "isCompactSummary": True}
        self.assertFalse(server.claude_working(claude_log(prompt(), done, summary)))

    def test_what_it_is_doing(self):
        self.assertEqual(server.claude_activity(claude_log(prompt(), assistant("tool_use"))), (True, "tool", ""))
        self.assertEqual(server.claude_activity(claude_log(prompt(), assistant("tool_use"), tool_result())),
                         (True, "thinking", ""))

    def test_tool_words(self):
        call = {"type": "tool_use", "name": "Edit", "input": {"file_path": "/home/u/secret/routing.py"}}
        reading = {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.md"}}
        busy = {"type": "assistant", "message": {"stop_reason": "tool_use", "content": [call, reading]}}
        self.assertEqual(server.claude_activity(claude_log(prompt(), busy)),
                         (True, "tool", "editing routing.py (+1 more)"))
        bash = {"name": "Bash", "input": {"command": "rm -rf x", "description": "Run tests"}}
        self.assertEqual(server.tool_label(bash), "Run tests")
        self.assertEqual(server.tool_label({"name": "Bash", "input": {"command": "ls"}}), "running a command")
        self.assertEqual(server.tool_label({"name": "mcp__x__y", "input": {}}), "using mcp__x__y")

    def test_unknown_session(self):
        self.assertIsNone(server.claude_working(Speaker("claude-_home_x", "claude", "x")))


def codex_log(*events: str) -> Speaker:
    folder = Path.home() / ".codex/sessions/2026/11/16"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "session_meta", "payload": {"cwd": "/home/u/proj"}}]
    lines += [{"type": "event_msg", "payload": {"type": e}} for e in events]
    (folder / "rollout-x.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return Speaker("codex-_home_u_proj", "codex", "proj")


class Codex(unittest.TestCase):
    def test_started_is_working(self):
        self.assertTrue(server.codex_working(codex_log("task_started", "agent_message")))

    def test_complete_or_aborted_is_idle(self):
        self.assertFalse(server.codex_working(codex_log("task_started", "task_complete")))
        self.assertFalse(server.codex_working(codex_log("task_started", "turn_aborted")))

    def test_other_folder_is_unknown(self):
        codex_log("task_started")
        self.assertIsNone(server.codex_working(Speaker("codex-_home_u_other", "codex", "other")))

    def test_only_explicit_commentary_becomes_a_thought(self):
        folder = Path.home() / ".codex/sessions/2026/11/20"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [
            {"timestamp": "2026-11-20T01:00:00.000Z", "type": "response_item",
             "payload": {"type": "reasoning", "encrypted_content": "secret"}},
            {"timestamp": "2026-11-20T01:00:01.000Z", "type": "event_msg",
             "payload": {"type": "agent_message", "phase": "commentary", "message": "Checking the tests."}},
            {"timestamp": "2026-11-20T01:00:01.000Z", "type": "response_item",
             "payload": {"type": "message", "phase": "commentary", "content": "duplicate"}},
            {"timestamp": "2026-11-20T01:00:02.000Z", "type": "event_msg",
             "payload": {"type": "agent_message", "phase": "final", "message": "Done."}},
        ]
        (folder / f"rollout-test-{UUID}.jsonl").write_text("\n".join(json.dumps(x) for x in entries) + "\n")
        speaker = Speaker(f"codex-{UUID}", "codex", "proj")
        self.assertEqual(server.codex_thoughts(speaker, "2026-11-20T01:00:00.500Z"), [
            {"at": "2026-11-20T01:00:01.000Z", "text": "Checking the tests."}])
        self.assertEqual(server.codex_thoughts(speaker, "2026-11-20T01:00:01.000Z"), [])


if __name__ == "__main__":
    unittest.main()
