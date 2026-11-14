import json
import os
import tempfile
import unittest

from vexorgate import audit
from vexorgate.gate import CHECKS, Gate

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class ScriptedClient:
    """Blocks with 'beyond' when the state contains a trigger word."""

    def __init__(self, triggers=("production", "ch_1183", "vanguard")):
        self.triggers, self.states = triggers, []

    def decide(self, state, questions):
        self.states.append(state)
        hit = any(t in state for t in self.triggers)
        answers = {"gate": {"choice": "allow", "probabilities": {"block": 0.1}}, "injected": {"noul": 0.0}}
        for name in CHECKS:
            answers[name] = {"noul": 0.95 if hit and name == "beyond" else 0.02}
        return {"answers": answers, "usage": {"cost": 0.000003}}


class ParseTests(unittest.TestCase):
    def test_example_file_all_formats(self):
        actions, problems = audit.load_actions(os.path.join(ROOT, "benchmarks", "example_traces.jsonl"))
        self.assertEqual(problems, [])
        self.assertEqual(len(actions), 2 + 3 + 1)
        msg = actions[-1]
        self.assertEqual(msg["tool"], "gmail_send")
        self.assertEqual(msg["request"], "Send the October usage CSV to Northstar Logistics.")
        self.assertEqual(msg["input"]["to"], "billing@northstar-logistics.com")

    def test_benchmark_format(self):
        actions, problems = audit.load_actions(os.path.join(ROOT, "benchmarks", "dev_fresh.jsonl"))
        self.assertEqual((len(actions), problems), (28, []))
        d17 = [a for a in actions if a["case_id"] == "d17"][0]
        self.assertIn("$3,200", d17["request"])
        self.assertTrue(d17["action_text"].startswith("Call `mercury.payments.create`"))

    def test_bad_lines_reported_not_fatal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write('{"request": "x", "tool": "Bash", "input": {"command": "ls"}}\nnot json\n{"foo": 1}\n')
        try:
            actions, problems = audit.load_actions(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(problems), 2)

    def test_messages_use_latest_user_message(self):
        obj = {"messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "tool_calls": [{"function": {"name": "a", "arguments": "{}"}}]},
            {"role": "user", "content": [{"type": "text", "text": "second"}]},
            {"role": "assistant", "tool_calls": [{"function": {"name": "b", "arguments": "not json"}}]}]}
        a, b = audit.parse_line(obj, 1)
        self.assertEqual((a["request"], b["request"], b["input"]), ("first", "second", "not json"))


class RunTests(unittest.TestCase):
    def test_audit_report_with_scope_violations(self):
        actions, _ = audit.load_actions(os.path.join(ROOT, "benchmarks", "example_traces.jsonl"))
        records = audit.run(Gate(ScriptedClient(), org="acme.io"), actions, workers=2)
        decisions = [r["decision"] for r in records]
        self.assertEqual(decisions.count("block"), 3)
        text = audit.report(records, "example_traces.jsonl")
        self.assertIn("# VexorGate trace audit: example_traces.jsonl", text)
        self.assertIn("blocked | 3 (50%)", text)
        self.assertIn("-n production", text)

    def test_accuracy_against_gold(self):
        actions, _ = audit.load_actions(os.path.join(ROOT, "benchmarks", "dev_fresh.jsonl"))
        client = ScriptedClient(triggers=("production",))
        records = audit.run(Gate(client), actions, workers=4)
        text = audit.report(records, "dev_fresh.jsonl")
        # "production" appears in d21 (risky) and in d05's request (safe)
        self.assertIn("Correct: 14/28", text)
        self.assertIn("risky actions stopped: 1/14", text)
        self.assertIn("safe actions allowed: 13/14", text)
        self.assertNotIn("on ``", text)
        self.assertTrue(all(s.startswith("User asked:") for s in client.states))

    def test_benchmark_state_matches_frozen_format(self):
        gate = Gate(ScriptedClient(), org="acme.io")
        a = audit.parse_line({"id": "x", "gold": "block", "text": "User asked: do A. Agent action: did B"}, 1)[0]
        self.assertEqual(gate.state(a["request"], a["tool"], a["input"], a["action_text"]),
                         "User organization domain: acme.io. User asked: do A. Agent action: did B")

    def test_skipped_tools_not_sent(self):
        client = ScriptedClient()
        actions = [{"line": 1, "session": "s", "case_id": None, "gold": None, "request": "r",
                    "tool": "Read", "input": {"file_path": "a"}, "action_text": None}]
        records = audit.run(Gate(client), actions, skip_tools=["Read"])
        self.assertEqual((records[0]["decision"], client.states), ("skipped", []))


if __name__ == "__main__":
    unittest.main()
