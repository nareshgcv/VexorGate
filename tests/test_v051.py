"""v0.5.1: field names across venues, the number of identical orders taken from the request,
gap wording for running totals, more ways to say "limit", and the coverage line. No network."""
import unittest

from vexorgate import audit
from vexorgate.facts import Ledger, check_rules, request_facts
from vexorgate.gate import Gate


def rules(request, tool, tool_input, ledger, policy=None, session="s"):
    return check_rules(request, tool, tool_input, None, "acme.com", policy, ledger, session)


HL = {"coin": "ETH", "is_buy": True, "sz": 0.1, "limit_px": 3000, "order_type": {"limit": {"tif": "Gtc"}}}
ALPACA = {"symbol": "MSFT", "side": "buy", "qty": 5, "type": "limit", "limit_price": 410}


class VenueFieldTests(unittest.TestCase):
    def test_hyperliquid_same_cloid_is_a_retry(self):
        g = Gate(None)
        g.check("Buy 0.1 ETH with a limit at 3000", "exchange.order", dict(HL, cloid="0xa1"))
        g.settle("default", "unknown")
        v = g.check("Buy 0.1 ETH with a limit at 3000", "exchange.order", dict(HL, cloid="0xa1"))
        self.assertEqual(v.decision, "allow")

    def test_hyperliquid_new_cloid_after_timeout_asks(self):
        g = Gate(None)
        g.check("Buy 0.1 ETH with a limit at 3000", "exchange.order", dict(HL, cloid="0xa1"))
        g.settle("default", "unknown")
        v = g.check("Buy 0.1 ETH with a limit at 3000", "exchange.order", dict(HL, cloid="0xb2"))
        self.assertEqual((v.decision, v.reasons), ("ask", ["rule:duplicate_unverified"]))

    def test_other_venues(self):
        for key in ("newClientOrderId", "clOrdID", "orderRef", "clientOid", "orderLinkId", "client-order-id"):
            led = Ledger()
            order = {"symbol": "BTCUSDT", "side": "BUY", "origQty": 1}
            rules("Buy 1 BTC", "binance.new_order", dict(order, **{key: "a"}), led)
            same = rules("Buy 1 BTC", "binance.new_order", dict(order, **{key: "a"}), led)
            new = rules("Buy 1 BTC", "binance.new_order", dict(order, **{key: "b"}), led)
            self.assertEqual(same.decision, "allow", key)
            self.assertEqual(new.decision, "ask", key)

    def test_your_own_field_names(self):
        policy = {"dedupe_keys": ["myTag"], "qty_keys": ["lot_count"]}
        led = Ledger()
        order = {"symbol": "MSFT", "side": "buy", "lot_count": 5}
        rules("Buy 5 shares of MSFT", "broker.submit", dict(order, myTag="t1"), led, policy)
        self.assertEqual(rules("Buy 5 shares of MSFT", "broker.submit", dict(order, myTag="t1"), led, policy).decision,
                         "allow")
        r = rules("Buy 5 shares of MSFT", "broker.submit", dict(order, myTag="t2"), led, policy)
        self.assertEqual(r.decision, "block")
        self.assertIn("You asked for 5 shares. This call is 5 shares, which would make 10 shares in total", r.gap)


class TrancheTests(unittest.TestCase):
    def test_split_is_read_from_the_request(self):
        self.assertEqual(request_facts("buy 10 MSFT in two orders of 5").splits, 2)
        self.assertEqual(request_facts("Buy 12 MSFT in 3 tranches").splits, 3)
        self.assertEqual(request_facts("split it into four orders").splits, 4)
        self.assertIsNone(request_facts("Buy 5 MSFT").splits)

    def test_two_orders_of_five(self):
        led = Ledger()
        req = "buy 10 MSFT in two orders of 5"
        self.assertEqual(rules(req, "alpaca.submit_order", dict(ALPACA, client_order_id="a"), led).decision, "allow")
        self.assertEqual(rules(req, "alpaca.submit_order", dict(ALPACA, client_order_id="b"), led).decision, "allow")
        third = rules(req, "alpaca.submit_order", dict(ALPACA, client_order_id="c"), led)
        self.assertEqual(third.decision, "block")
        self.assertIn("rule:count", third.reasons)

    def test_three_tranches(self):
        led = Ledger()
        req = "Buy 12 shares of MSFT in three orders of 4"
        order = dict(ALPACA, qty=4)
        for cid in "abc":
            self.assertEqual(rules(req, "alpaca.submit_order", dict(order, client_order_id=cid), led).decision, "allow")
        self.assertEqual(rules(req, "alpaca.submit_order", dict(order, client_order_id="d"), led).decision, "block")

    def test_single_order_still_flags_the_second(self):
        led = Ledger()
        rules("Buy MSFT for the long-term account", "alpaca.submit_order", dict(ALPACA, client_order_id="a"), led)
        r = rules("Buy MSFT for the long-term account", "alpaca.submit_order", dict(ALPACA, client_order_id="b"), led)
        self.assertEqual((r.decision, r.reasons), ("ask", ["rule:duplicate"]))


class WordingTests(unittest.TestCase):
    def test_call_size_and_running_total_are_separate(self):
        led = Ledger()
        req = "Buy 5 shares of MSFT"
        rules(req, "alpaca.submit_order", dict(ALPACA, client_order_id="a"), led)
        r = rules(req, "alpaca.submit_order", dict(ALPACA, client_order_id="b"), led)
        self.assertIn("This call is 5 shares, which would make 10 shares in total in this task.", r.gap)

    def test_single_call_keeps_the_short_wording(self):
        r = rules("Buy 5 shares of MSFT", "alpaca.submit_order", dict(ALPACA, qty=50), Ledger())
        self.assertIn("You asked for 5 shares. The agent wants 50 shares.", r.gap)

    def test_more_ways_to_say_limit(self):
        market = dict(ALPACA, type="market")
        for req in ("Buy 5 MSFT with a limit at 140", "Buy 5 MSFT, limit at 140", "Buy 5 MSFT using a limit",
                    "Buy 5 MSFT with a limit order at 140", "limit buy 5 MSFT at 140"):
            self.assertEqual(rules(req, "alpaca.submit_order", market, Ledger()).decision, "block", req)
        self.assertEqual(rules("Buy 5 MSFT at market, limit of $1,000 total", "alpaca.submit_order", market,
                               Ledger()).decision, "allow")
        self.assertEqual(rules("Buy 5 MSFT with a limit at 140", "alpaca.submit_order",
                               dict(ALPACA, order_type="market", type=None), Ledger()).decision, "block")


class CoverageTests(unittest.TestCase):
    def test_coverage_line(self):
        actions = [{"line": 1, "session": "s", "case_id": "a", "gold": None, "request": "Buy 5 MSFT",
                    "tool": "alpaca.submit_order", "input": ALPACA, "action_text": None, "verify": "log_ends"},
                   {"line": 2, "session": "t", "case_id": "b", "gold": None, "request": "list files",
                    "tool": "Read", "input": {"path": "."}, "action_text": None}]
        records = audit.run(Gate(None), actions, workers=1, skip_tools=("Read",))
        text = audit.report(records, "t", problems=["line 9: not JSON"])
        self.assertIn("**Coverage:** checked 1 of 2 tool calls found in these logs; 1 judged on their arguments only "
                      "(see Couldn't verify); 1 skipped as read-only; 1 unreadable line(s). This audit only covers "
                      "calls in these logs", text)
        self.assertTrue(text.splitlines()[2].startswith("**Coverage:**"))


if __name__ == "__main__":
    unittest.main()
