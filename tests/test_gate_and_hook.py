import json
import os
import tempfile
import unittest

from vexorgate import claude_hook, config, report
from vexorgate.gate import CHECKS, Gate
from vexorgate.redact import redact


class FakeClient:
    """Returns fixed scores; records the last request."""

    def __init__(self, choice="allow", risks=None, injected=0.0, fail=False):
        self.choice, self.risks, self.injected, self.fail = choice, risks or {}, injected, fail
        self.last = None

    def decide(self, state, questions):
        if self.fail:
            raise TimeoutError("endpoint down")
        self.last = (state, questions)
        answers = {"gate": {"choice": self.choice, "probabilities": {"block": 0.9 if self.choice == "block" else 0.1}}}
        for name in CHECKS:
            answers[name] = {"noul": self.risks.get(name, 0.05)}
        answers["injected"] = {"noul": self.injected}
        return {"answers": answers, "usage": {"cost": 0.000003}}


class GateTests(unittest.TestCase):
    def test_allow(self):
        v = Gate(FakeClient()).check("rename the doc", "Edit", {"file": "a.md"})
        self.assertEqual(v.decision, "allow")

    def test_block_with_reason(self):
        v = Gate(FakeClient(risks={"destroy": 0.95})).check("clean temp", "Bash", {"command": "rm -rf /"})
        self.assertEqual((v.decision, v.reasons), ("block", ["destroy"]))

    def test_gate_block_without_check(self):
        v = Gate(FakeClient(choice="block")).check("x", "Bash", {})
        self.assertEqual((v.decision, v.reasons), ("block", ["gate"]))

    def test_injection_asks(self):
        v = Gate(FakeClient(injected=0.9)).check("summarize page", "WebFetch", {"url": "x"})
        self.assertEqual((v.decision, v.reasons), ("ask", ["injected"]))

    def test_risk_beats_injection(self):
        v = Gate(FakeClient(risks={"external": 0.9}, injected=0.9)).check("x", "Send", {})
        self.assertEqual(v.decision, "block")

    def test_error_never_raises(self):
        v = Gate(FakeClient(fail=True)).check("x", "Bash", {})
        self.assertEqual(v.decision, "error")

    def test_one_call_with_all_questions_and_locked_options(self):
        fake = FakeClient()
        Gate(fake, org="acme.io").check("x", "Bash", {"command": "ls"})
        state, qs = fake.last
        self.assertIn("User organization domain: acme.io.", state)
        self.assertEqual(set(qs), {"gate", "injected", *CHECKS})
        self.assertEqual(list(qs["gate"]["criteria"]), ["allow", "block"])

    def test_secrets_redacted_before_sending(self):
        fake = FakeClient()
        Gate(fake).check("deploy", "Bash", {"command": "export OPENAI=sk-abcdefghijklmnopqrstuvwx password=hunter2"})
        self.assertNotIn("sk-abcdefghijklmnop", fake.last[0])
        self.assertNotIn("hunter2", fake.last[0])


class RedactTests(unittest.TestCase):
    def test_patterns(self):
        text = "ghp_" + "a" * 36 + " AKIA" + "B" * 16 + " Bearer abcdefghijklmnopqrstuv api_key=xyz123"
        out = redact(text)
        for secret in ("ghp_aaaa", "AKIABBBB", "abcdefghijklmnopqrstuv", "xyz123"):
            self.assertNotIn(secret, out)


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["VEXORGATE_HOME"] = self.tmp.name
        self.cfg = dict(config.DEFAULTS)

    def tearDown(self):
        os.environ.pop("VEXORGATE_HOME", None)
        self.tmp.cleanup()

    def logs(self):
        d = config.home() / "logs"
        return [json.loads(l) for f in d.glob("*.jsonl") for l in f.read_text().splitlines()]

    def test_shadow_mode_logs_and_never_interferes(self):
        claude_hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                            "prompt": "refund order 1182"}, cfg=self.cfg)
        out = claude_hook.handle({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                                  "tool_input": {"command": "refund --all"}},
                                 gate=Gate(FakeClient(risks={"beyond": 0.97})), cfg=self.cfg)
        self.assertIsNone(out)
        rec = self.logs()[-1]
        self.assertEqual((rec["decision"], rec["request"]), ("block", "refund order 1182"))

    def test_enforce_mode_denies_and_asks(self):
        cfg = dict(self.cfg, mode="enforce")
        deny = claude_hook.handle({"hook_event_name": "PreToolUse", "session_id": "s2", "tool_name": "Bash",
                                   "tool_input": {}}, gate=Gate(FakeClient(risks={"destroy": 0.9})), cfg=cfg)
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        ask = claude_hook.handle({"hook_event_name": "PreToolUse", "session_id": "s2", "tool_name": "Bash",
                                  "tool_input": {}}, gate=Gate(FakeClient(fail=True)), cfg=cfg)
        self.assertEqual(ask["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_read_only_tools_skipped(self):
        out = claude_hook.handle({"hook_event_name": "PreToolUse", "session_id": "s3", "tool_name": "Read",
                                  "tool_input": {"file_path": "a"}}, gate=Gate(FakeClient(fail=True)), cfg=self.cfg)
        self.assertIsNone(out)
        self.assertEqual(self.logs()[-1]["decision"], "skipped")

    def test_transcript_fallback(self):
        path = os.path.join(self.tmp.name, "t.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "fix the lint errors"}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}) + "\n")
        self.assertEqual(claude_hook.prompt_from_transcript(path), "fix the lint errors")

    def test_report(self):
        claude_hook.handle({"hook_event_name": "PreToolUse", "session_id": "s4", "tool_name": "Bash",
                            "tool_input": {"command": "git push --force origin main"}},
                           gate=Gate(FakeClient(risks={"destroy": 0.9})), cfg=self.cfg)
        text = report.build(days=1)
        self.assertIn("blocked | 1 (100%)", text)
        self.assertIn("destroy", text)


if __name__ == "__main__":
    unittest.main()
