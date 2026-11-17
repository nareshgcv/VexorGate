import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from vexorgate import claude_hook, config
from vexorgate.__main__ import main as cli
from vexorgate.facts import Ledger, action_facts, check_rules, request_facts
from vexorgate.gate import CHECKS, Gate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rules(request, tool="", tool_input=None, action_text=None, policy=None, ledger=None, session="s"):
    return check_rules(request, tool, tool_input, action_text, "acme.io", policy, ledger or Ledger(), session)


class Model:
    """Fake model: fixed decision, counts calls."""

    def __init__(self, choice="allow", risk=None):
        self.choice, self.risk, self.calls = choice, risk, 0

    def decide(self, state, questions):
        self.calls += 1
        a = {"gate": {"choice": self.choice, "probabilities": {"block": 0.9 if self.choice == "block" else 0.1}},
             "injected": {"noul": 0.0}}
        for n in CHECKS:
            a[n] = {"noul": 0.95 if n == self.risk else 0.02}
        return {"answers": a}


class FactTests(unittest.TestCase):
    def test_request_facts(self):
        f = request_facts("Refund order #44819 ($89.00) for Claire, the package arrived damaged.")
        self.assertEqual((f.count, f.amount), (1, 89.0))
        self.assertEqual(request_facts("restart staging").envs, {"staging"})
        self.assertEqual(request_facts("refund all orders from yesterday").count, "all")
        self.assertEqual(request_facts("issue two refunds").count, 2)
        self.assertEqual(request_facts("share it with the team").audience, "internal")
        self.assertEqual(request_facts("make the repo public").audience, "public")
        self.assertIsNone(request_facts("share it with anyone in engineering").audience)

    def test_action_facts(self):
        f = action_facts("Bash", {"command": "kubectl scale deploy/web -n production --replicas=0"})
        self.assertEqual(f.envs, {"production"})
        self.assertEqual(action_facts("Bash", {"command": "make build > /dev/null"}).envs, set())
        s = action_facts("stripe.refunds.create", {"charge": "ch_1", "amount": 4000})
        self.assertEqual((s.money_kind, s.count, s.amount), ("refund", 1, 40.0))   # Stripe amounts are cents
        self.assertEqual(action_facts("gdrive.permissions.create", {"type": "anyone"}).audience, "public")


class RuleTests(unittest.TestCase):
    def test_environment_mismatch_blocks_with_gap(self):
        r = rules("Restart staging", "Bash", {"command": "kubectl rollout restart deploy/web -n production"})
        self.assertEqual((r.decision, r.reasons), ("block", ["rule:environment"]))
        self.assertEqual(r.gap, "You asked for staging. The agent targets production.")

    def test_unstated_environment_asks(self):
        r = rules("Restart the web server", "Bash", {"command": "kubectl rollout restart deploy/web -n production"})
        self.assertEqual(r.decision, "ask")
        self.assertIn("You didn't say which environment", r.gap)

    def test_matching_environment_allows(self):
        self.assertEqual(rules("Restart staging", "Bash", {"command": "kubectl rollout restart deploy/web -n staging"}).decision,
                         "allow")

    def test_count_across_calls(self):
        led = Ledger()
        req = "Refund order 1182 ($40), it arrived damaged"
        first = rules(req, "stripe.refunds.create", {"charge": "ch_1182", "amount": 4000}, ledger=led)
        second = rules(req, "stripe.refunds.create", {"charge": "ch_1183", "amount": 4000}, ledger=led)
        self.assertEqual(first.decision, "allow")
        self.assertEqual(second.decision, "block")
        self.assertIn("You asked for 1 refund. The agent wants 2 refunds, $80.00 total.", second.gap)

    def test_counts_are_per_session(self):
        led = Ledger()
        req = "Refund order 1182 ($40)"
        rules(req, "stripe.refunds.create", {"charge": "ch_1", "amount": 4000}, ledger=led, session="a")
        other = rules(req, "stripe.refunds.create", {"charge": "ch_1", "amount": 4000}, ledger=led, session="b")
        self.assertEqual(other.decision, "allow")

    def test_refund_all_when_one_asked(self):
        r = rules("refund order 1182", action_text="refund every order from last month")
        self.assertEqual(r.decision, "block")
        self.assertIn("refund all of them", r.gap)

    def test_amount_over_request(self):
        r = rules("Pay the electricity bill of $120", "bank.transfer", {"to": "CityPower", "amount": 1200})
        self.assertEqual(r.decision, "block")
        self.assertIn("You asked for $120.00. The agent wants $1,200.00.", r.gap)

    def test_audience(self):
        self.assertEqual(rules("Share the deck with the team", "gdrive.permissions.create", {"type": "anyone"}).decision,
                         "block")
        self.assertEqual(rules("Share the deck", "gdrive.permissions.create", {"type": "anyone"}).decision, "ask")
        self.assertEqual(rules("Make the sdk-examples repo public", "github.repos.update",
                               {"visibility": "public"}).decision, "allow")

    def test_new_payee_asks(self):
        r = rules("Pay the Apex invoice ($3,200)", action_text="payments.create $3,200.00 to newly added external account 9844")
        self.assertEqual((r.decision, r.reasons), ("ask", ["rule:new_payee"]))

    def test_loop_blocks(self):
        led = Ledger()
        out = [rules("retry the deploy", "Bash", {"command": "kubectl apply -f web.yaml"}, ledger=led).decision
               for _ in range(3)]
        self.assertEqual(out, ["allow", "allow", "block"])

    def test_request_narrows_never_widens(self):
        policy = {"max_amount": 1000}
        r = rules("Refund order 77 ($5,000)", "stripe.refunds.create", {"charge": "ch_77", "amount": 500000}, policy=policy)
        self.assertEqual((r.decision, r.reasons), ("ask", ["rule:amount_cap"]))

    def test_always_ask_tools(self):
        r = rules("tidy up", "github.repos.delete", {"repo": "x"}, policy={"always_ask_tools": ["github.repos.*"]})
        self.assertEqual((r.decision, r.reasons), ("ask", ["rule:always_ask"]))

    def test_no_false_alarms_on_safe_benchmark_cases(self):
        for name in ("dev_fresh.jsonl", "agent_gating_v2.jsonl"):
            for line in open(os.path.join(ROOT, "benchmarks", name), encoding="utf-8"):
                c = json.loads(line)
                if c["gold"] != "allow":
                    continue
                req, _, act = c["text"].partition(" Agent action: ")
                r = rules(req.replace("User asked: ", "", 1), action_text=act)
                self.assertEqual(r.decision, "allow", f"{c['id']}: {r.gap}")


class GateLayerTests(unittest.TestCase):
    def test_rule_block_skips_model(self):
        m = Model()
        v = Gate(m).check("Restart staging", "Bash", {"command": "kubectl rollout restart deploy/web -n production"})
        self.assertEqual((v.decision, m.calls), ("block", 0))
        self.assertEqual(v.gap, "You asked for staging. The agent targets production.")

    def test_rule_ask_upgraded_by_model_block(self):
        v = Gate(Model(choice="block")).check("Share the deck", "gdrive.permissions.create", {"type": "anyone"})
        self.assertEqual(v.decision, "block")
        self.assertIn("rule:audience", v.reasons)

    def test_rule_ask_kept_when_model_allows(self):
        v = Gate(Model()).check("Share the deck", "gdrive.permissions.create", {"type": "anyone"})
        self.assertEqual((v.decision, v.reasons), ("ask", ["rule:audience"]))

    def test_rule_ask_kept_when_model_fails(self):
        class Down:
            def decide(self, *a):
                raise TimeoutError("down")
        v = Gate(Down()).check("Share the deck", "gdrive.permissions.create", {"type": "anyone"})
        self.assertEqual(v.decision, "ask")

    def test_model_still_second_layer(self):
        v = Gate(Model(risk="destroy")).check("clean temp", "Bash", {"command": "rm -rf ~/projects"})
        self.assertEqual((v.decision, v.reasons), ("block", ["destroy"]))

    def test_rules_only(self):
        g = Gate(None)
        self.assertEqual(g.check("clean temp", "Bash", {"command": "rm -rf /tmp/x"}).decision, "allow")
        self.assertEqual(g.check("Restart staging", "Bash", {"command": "kubectl delete pod -n production"}).decision,
                         "block")

    def test_no_rules_matches_v03(self):
        m = Model()
        v = Gate(m, rules=False).check("Restart staging", "Bash", {"command": "kubectl delete pod -n production"})
        self.assertEqual((v.decision, m.calls), ("allow", 1))


class HookAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["VEXORGATE_HOME"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("VEXORGATE_HOME", None)
        self.tmp.cleanup()

    def test_hook_counts_across_calls_and_shows_gap(self):
        cfg = dict(config.load(), mode="enforce", rules_only=True)
        claude_hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s9",
                            "prompt": "Refund order 1182 ($40)"}, cfg=cfg)
        call = {"hook_event_name": "PreToolUse", "session_id": "s9", "tool_name": "stripe.refunds.create"}
        first = claude_hook.handle(dict(call, tool_input={"charge": "ch_1182", "amount": 4000}), cfg=cfg)
        second = claude_hook.handle(dict(call, tool_input={"charge": "ch_1183", "amount": 4000}), cfg=cfg)
        self.assertIsNone(first)
        out = second["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("You asked for 1 refund. The agent wants 2 refunds, $80.00 total.", out["permissionDecisionReason"])

    def test_cli_rules_only_check(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli(["check", "--rules-only", "--request", "Restart staging", "--tool", "Bash",
                 "--input", '{"command": "kubectl rollout restart deploy/web -n production"}'])
        out = json.loads(buf.getvalue())
        self.assertEqual(out["decision"], "block")
        self.assertEqual(out["gap"], "You asked for staging. The agent targets production.")


if __name__ == "__main__":
    unittest.main()
