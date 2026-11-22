"""SystemOneClient: retries on rate limits, clear server messages on other errors. No network."""
import io
import json
import unittest
import urllib.error
from unittest import mock

from vexorgate import config, gate
from vexorgate.gate import EndpointError, Gate, SystemOneClient

OK = {"answers": {"gate": {"choice": "allow", "probabilities": {"block": 0.1}}, "injected": {"noul": 0.0},
                  **{n: {"noul": 0.02} for n in gate.CHECKS}}}


def http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError("https://x/v1/systemone", code, "Error", headers or {}, io.BytesIO(body))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        p = mock.patch.object(gate.time, "sleep", self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def client(self, responses, **kw):
        c = SystemOneClient("https://x/v1/systemone", "k", "m", **kw)
        calls = iter(responses)

        def post(data, headers):
            self.headers = headers
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        c._post = post
        return c

    def test_retries_rate_limit_then_succeeds(self):
        c = self.client([http_error(429), http_error(503), OK])
        self.assertEqual(c.decide("s", {}), OK)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_respects_retry_after(self):
        c = self.client([http_error(429, headers={"Retry-After": "5"}), OK])
        c.decide("s", {})
        self.assertEqual(self.sleeps, [5.0])

    def test_gives_up_after_retries(self):
        c = self.client([http_error(429)] * 4, retries=3)
        with self.assertRaises(EndpointError) as e:
            c.decide("s", {})
        self.assertIn("HTTP 429", str(e.exception))
        self.assertEqual(len(self.sleeps), 3)

    def test_404_not_retried_and_shows_server_message(self):
        body = json.dumps({"error": {"message": "No endpoints found for liquid/d1-20260930."}}).encode()
        c = self.client([http_error(404, body)])
        with self.assertRaises(EndpointError) as e:
            c.decide("s", {})
        self.assertEqual(str(e.exception), "HTTP 404: No endpoints found for liquid/d1-20260930.")
        self.assertEqual(self.sleeps, [])

    def test_empty_error_body_still_readable(self):
        c = self.client([http_error(404)])
        with self.assertRaises(EndpointError) as e:
            c.decide("s", {})
        self.assertEqual(str(e.exception), "HTTP 404: Error")

    def test_user_agent_reports_version(self):
        c = self.client([OK])
        c.decide("s", {})
        self.assertEqual(self.headers["User-Agent"], f"vexorgate/{gate._version()}")

    def test_failure_is_error_never_allow(self):
        c = self.client([http_error(429)] * 4)
        v = Gate(c, rules=False).check("Send the report to Sam", "email.send", {"to": "sam@acme.io"})
        self.assertEqual(v.decision, "error")
        self.assertIn("HTTP 429", v.reasons[0])


class DefaultTests(unittest.TestCase):
    def test_default_model_is_not_the_retired_one(self):
        self.assertEqual(config.DEFAULTS["model"], "liquid/d1.1-20261027")
        self.assertEqual(config.DEFAULTS["retries"], 3)

    def test_config_passes_retries(self):
        g = gate.gate_from_config(dict(config.DEFAULTS, key="k", retries=0))
        self.assertEqual(g.client.retries, 0)


if __name__ == "__main__":
    unittest.main()
