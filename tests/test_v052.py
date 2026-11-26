"""v0.5.2: the hook fails closed, every check has a time limit, files on disk are private and
redacted, and parallel hooks don't lose counts. No network."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

from vexorgate import claude_hook, config, gate
from vexorgate.gate import EndpointError, SystemOneClient

PROD = {"command": "kubectl rollout restart deploy/web -n production"}


def run_hook(event):
    """Run the hook the way Claude Code does: JSON on stdin, JSON (or nothing) on stdout."""
    out = io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(json.dumps(event) if not isinstance(event, str) else event)), \
            contextlib.redirect_stdout(out):
        rc = claude_hook.main()
    text = out.getvalue().strip()
    return rc, (json.loads(text) if text else None)


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"VEXORGATE_HOME": self.tmp.name, "VEXORGATE_RULES_ONLY": "1"})
        self.env.start()
        for var in ("VEXORGATE_MODE", "VEXORGATE_KEY", "VEXORGATE_URL"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_config(self, text):
        with open(os.path.join(self.tmp.name, "config.json"), "w") as f:
            f.write(text)

    def prompt(self, text, session="s1"):
        return run_hook({"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": text})

    def call(self, tool_input, tool="Bash", session="s1"):
        return run_hook({"hook_event_name": "PreToolUse", "session_id": session, "tool_name": tool,
                         "tool_input": tool_input})


class FailClosedTests(HookCase):
    def test_damaged_session_file_asks_instead_of_silence(self):
        self.write_config('{"mode": "enforce"}')
        self.prompt("Restart staging")
        claude_hook._session_file("s1").write_text('{"prompts": ["Restart staging"], "ledger": {')
        rc, out = self.call({"command": "kubectl rollout restart deploy/web -n staging"})
        self.assertEqual(rc, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("session was damaged", out["hookSpecificOutput"]["permissionDecisionReason"])
        damaged = [f for f in os.listdir(os.path.join(self.tmp.name, "sessions")) if ".damaged-" in f]
        self.assertEqual(len(damaged), 1)
        rc, out = self.call({"command": "kubectl rollout restart deploy/web -n staging"})
        self.assertIsNone(out)                    # the next call works normally again

    def test_damaged_session_file_still_blocks_a_real_mismatch(self):
        self.write_config('{"mode": "enforce"}')
        self.prompt("Restart staging")
        claude_hook._session_file("s1").write_text("{not json")
        rc, out = self.call(PROD)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")   # request lost with the file

    def test_broken_config_still_fails_closed(self):
        self.write_config('{"mode": "enforce", "policy": {')          # half-written config
        rc, out = self.call(PROD)
        self.assertEqual(rc, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("internal error", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_mode_from_environment_when_config_is_broken(self):
        self.write_config("{oops")
        os.environ["VEXORGATE_MODE"] = "enforce"
        rc, out = self.call(PROD)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_unexpected_error_inside_the_check_asks(self):
        self.write_config('{"mode": "enforce"}')
        with mock.patch.object(gate.Gate, "check", side_effect=RuntimeError("boom")):
            rc, out = self.call({"command": "ls"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_bad_input_asks(self):
        self.write_config('{"mode": "enforce"}')
        rc, out = run_hook("this is not json")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_shadow_mode_stays_silent_on_errors(self):
        self.write_config('{"mode": "shadow"}')
        with mock.patch.object(gate.Gate, "check", side_effect=RuntimeError("boom")):
            rc, out = self.call({"command": "ls"})
        self.assertIsNone(out)

    def test_other_events_never_get_a_permission_answer(self):
        self.write_config('{"mode": "enforce"}')
        with mock.patch.object(claude_hook, "remember_prompt", side_effect=RuntimeError("boom")):
            rc, out = self.prompt("hello")
        self.assertIsNone(out)

    def test_busy_lock_asks(self):
        self.write_config('{"mode": "enforce"}')
        with mock.patch.object(claude_hook, "LOCK_WAIT", 0.2), claude_hook._session_lock("s1"):
            done = {}
            t = threading.Thread(target=lambda: done.setdefault("r", self.call({"command": "ls"})))
            t.start()
            t.join(5)
        self.assertEqual(done["r"][1]["hookSpecificOutput"]["permissionDecision"], "ask")


class PrivateFilesTests(HookCase):
    def test_prompts_are_redacted_before_saving(self):
        self.prompt("deploy it, the token=abc123SECRETVALUE and key sk-or-v1-1234567890abcdef1234")
        text = claude_hook._session_file("s1").read_text()
        self.assertNotIn("abc123SECRETVALUE", text)
        self.assertNotIn("sk-or-v1-1234567890abcdef1234", text)

    def test_ledger_does_not_keep_raw_secrets(self):
        self.prompt("Refund order 1182 ($40)")
        self.call({"charge": "ch_1182", "amount": 4000, "api_key": "sk-live-1234567890abcdefXYZ"},
                  tool="stripe.refunds.create")
        self.assertNotIn("sk-live-1234567890abcdefXYZ", claude_hook._session_file("s1").read_text())

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_owner_only_permissions(self):
        self.prompt("Restart staging")
        self.call({"command": "ls"})
        home = self.tmp.name
        self.assertEqual(os.stat(os.path.join(home, "sessions")).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(claude_hook._session_file("s1")).st_mode & 0o777, 0o600)
        log = os.listdir(os.path.join(home, "logs"))[0]
        self.assertEqual(os.stat(os.path.join(home, "logs", log)).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(home).st_mode & 0o777, 0o700)


class ParallelTests(HookCase):
    def test_parallel_calls_keep_every_count(self):
        self.prompt("Refund the damaged orders from this week")
        errors = []

        def one(i):
            try:
                claude_hook.handle({"hook_event_name": "PreToolUse", "session_id": "s1",
                                    "tool_name": "stripe.refunds.create",
                                    "tool_input": {"charge": f"ch_{9000 + i}", "amount": 1000}},
                                   cfg=dict(config.load(), mode="shadow"))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        data = json.loads(claude_hook._session_file("s1").read_text())
        entries = [e for task in data["ledger"]["sessions"]["s1"]["tasks"].values() for e in task["entries"]]
        self.assertEqual(len(entries), 12)


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.sleeps = []
        patches = [mock.patch.object(gate.time, "monotonic", lambda: self.now[0]),
                   mock.patch.object(gate.time, "sleep", self.sleep)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def sleep(self, s):
        self.sleeps.append(s)
        self.now[0] += s

    def client(self, responses, **kw):
        c = SystemOneClient("https://x/v1/systemone", "k", "m", **kw)
        calls = iter(responses)

        def post(data, headers):
            self.timeouts.append(c._attempt_timeout)
            self.now[0] += 2.0                    # each attempt takes 2 s
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        self.timeouts = []
        c._post = post
        return c

    def test_long_retry_after_stops_at_the_time_limit(self):
        busy = urllib.error.HTTPError("u", 429, "busy", {"Retry-After": "30"}, io.BytesIO(b""))
        c = self.client([busy] * 4, deadline=15)
        with self.assertRaises(EndpointError) as e:
            c.decide("s", {})
        self.assertIn("time limit of 15 s", str(e.exception))
        self.assertEqual(self.sleeps, [])                       # did not wait 30 s
        self.assertLessEqual(self.now[0] - 100.0, 15)

    def test_attempt_timeout_shrinks_to_what_is_left(self):
        busy = urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b""))
        c = self.client([busy, busy, {"answers": {}}], timeout=8, deadline=10)
        c.decide("s", {})
        self.assertEqual(self.timeouts[0], 8)
        self.assertLess(self.timeouts[-1], 8)

    def test_error_from_deadline_never_allows(self):
        busy = urllib.error.HTTPError("u", 429, "busy", {"Retry-After": "30"}, io.BytesIO(b""))
        v = gate.Gate(self.client([busy] * 4, deadline=15), rules=False).check("x", "Bash", {"command": "ls"})
        self.assertEqual(v.decision, "error")

    def test_config_sets_the_deadline(self):
        g = gate.gate_from_config(dict(config.DEFAULTS, key="k", deadline=12))
        self.assertEqual(g.client.deadline, 12.0)
        self.assertEqual(config.DEFAULTS["deadline"], 15)


if __name__ == "__main__":
    unittest.main()
