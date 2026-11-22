"""A facilitator for sellers from outside (x402-facilitator --public): it verifies and settles as the gate's own
does, keeps no catalog, takes at most so many requests a minute from one caller and ten times that from all of
them together, forgets a caller a minute after its last request, and takes no new payment once it keeps as many
as it may. The gate's own facilitator has no such bounds."""
import hashlib
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from polarka_agent import cli, keys, x402

PAY_TO = keys.Account.from_words(keys.new_words(12), "main").address(0, 0)
WANTED = {"scheme": "exact", "network": x402.NETWORKS["main"], "amount": "1000", "asset": "NORTH", "payTo": PAY_TO,
          "maxTimeoutSeconds": 60}


class Fake:
    """Just enough of a facilitator: it answers yes and writes down how it was asked."""
    network, chain = x402.NETWORKS["main"], "main"

    def __init__(self, catalog=None):
        self.calls, self.told, self.catalog = [], threading.local(), catalog

    def supported(self):
        return {"kinds": [{"x402Version": 2, "scheme": "exact", "network": self.network}]}

    def verify(self, payment, wanted):
        self.calls.append("verify")
        return {"isValid": True, "invalidReason": None, "payer": None}

    def settle(self, payment, wanted, who=None):
        self.calls.append("settle")
        self.who = who                          # the place a new payment counts against (review 10, M2)
        self.told.bazaar = {"status": "listed"} if self.catalog is not None else None
        return {"success": True, "transaction": "ab" * 32, "network": self.network, "payer": None}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


ASKED = json.dumps({"x402Version": 2, "paymentPayload": {"x402Version": 2}, "paymentRequirements": WANTED}).encode()


def post(base, path, forwarded=None, body=None):
    data = body if body is not None else ASKED
    headers = {"Content-Type": "application/json"}
    if forwarded:
        headers["X-Forwarded-For"] = forwarded
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, data, headers), timeout=10) as r:
            return r.status, json.loads(r.read()), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"null"), {k.lower(): v for k, v in e.headers.items()}


class Served(unittest.TestCase):
    def start(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1]


class ForSellersFromOutside(Served):
    def test_it_keeps_no_catalog(self):
        with self.assertRaises(ValueError):
            x402.facilitator_server(Fake(catalog=object()), public=True)
        f = Fake()
        public = self.start(x402.facilitator_server(f, public=True, per_minute=5))
        status, answer, headers = post(public, "/settle", "198.51.100.7")
        self.assertEqual((status, answer["success"]), (200, True))
        self.assertNotIn("extension-responses", headers)
        self.assertEqual(f.calls, ["settle"])

    def test_so_many_a_minute_from_one_address(self):
        f, clock = Fake(), Clock()
        public = self.start(x402.facilitator_server(f, public=True, per_minute=3, clock=clock))
        self.assertEqual([post(public, "/verify", "198.51.100.7")[0] for _ in range(4)], [200, 200, 200, 429])
        self.assertEqual(post(public, "/settle", "198.51.100.8")[0], 200)          # another address is not held up
        status, answer, headers = post(public, "/verify", "198.51.100.7")
        self.assertEqual((status, headers.get("retry-after")), (429, "60"))
        clock.t += 20                                                               # a third of a minute gives one back
        self.assertEqual(post(public, "/verify", "198.51.100.7")[0], 200)
        # the web server's entry is the last one: an address the caller wrote before it counts for nothing
        self.assertEqual(post(public, "/verify", "203.0.113.1, 198.51.100.7")[0], 429)
        self.assertEqual(f.calls.count("verify"), 4)                                # a refused request reached nothing

    def test_of_two_forwarded_headers_the_last_counts(self):
        f, clock = Fake(), Clock()
        server = x402.facilitator_server(f, public=True, per_minute=1, clock=clock)
        self.start(server)

        def asked(first):
            c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            c.putrequest("POST", "/verify")
            for name, value in (("Content-Type", "application/json"), ("Content-Length", str(len(ASKED))),
                                ("X-Forwarded-For", first), ("X-Forwarded-For", "198.51.100.7")):
                c.putheader(name, value)
            c.endheaders(ASKED)
            r = c.getresponse()
            r.read()
            c.close()
            return r.status

        self.assertEqual([asked("203.0.113.%d" % i) for i in (1, 2)], [200, 429])

    def test_an_ipv6_machine_counts_by_its_64(self):
        self.assertEqual(x402.caller("2001:db8:1:2::1"), "2001:db8:1:2::/64")
        self.assertEqual(x402.caller("::ffff:198.51.100.7"), "198.51.100.7")
        self.assertEqual(x402.caller("198.51.100.7"), "198.51.100.7")
        self.assertEqual(x402.caller("not an address"), "not an address")
        f, clock = Fake(), Clock()
        public = self.start(x402.facilitator_server(f, public=True, per_minute=2, clock=clock))
        self.assertEqual([post(public, "/verify", a)[0] for a in ("2001:db8:1:2::1", "2001:db8:1:2::ffff", "2001:db8:1:2:abcd::9")],
                         [200, 200, 429])
        self.assertEqual(post(public, "/verify", "2001:db8:1:3::1")[0], 200)       # the next /64 is another caller

    def test_all_addresses_together_have_a_bound_too(self):
        f, clock = Fake(), Clock()
        public = self.start(x402.facilitator_server(f, public=True, per_minute=2, clock=clock))
        codes = [post(public, "/verify", "198.51.100.%d" % i)[0] for i in range(25)]
        self.assertEqual(codes.count(200), 20)                                      # ten times the bound of one address
        self.assertEqual(codes[-1], 429)

    def test_only_a_payment_the_node_is_asked_about_counts(self):
        f, clock = Fake(), Clock()
        public = self.start(x402.facilitator_server(f, public=True, per_minute=1, clock=clock))
        for _ in range(3):
            with urllib.request.urlopen(public + "/supported", timeout=10) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual(post(public, "/verify", "198.51.100.9", body=b"{}")[0], 400)
        self.assertEqual(post(public, "/verify", "198.51.100.9")[0], 200)
        self.assertEqual(post(public, "/verify", "198.51.100.9")[0], 429)

    def test_a_caller_is_forgotten_a_minute_after_its_last_request(self):
        clock = Clock()
        limiter = x402.Limiter(5, clock)
        self.assertTrue(limiter.take("198.51.100.7"))
        clock.t += 30
        self.assertTrue(limiter.take("198.51.100.8"))
        self.assertEqual(set(limiter.buckets), {"198.51.100.7", "198.51.100.8"})
        clock.t += 31                                                               # the first one is full again
        self.assertTrue(limiter.take("198.51.100.9"))
        self.assertEqual(set(limiter.buckets), {"198.51.100.8", "198.51.100.9"})
        with self.assertRaises(ValueError):
            x402.Limiter(0)

    def test_the_gates_own_facilitator_has_no_bound(self):
        f = Fake(catalog=object())
        private = self.start(x402.facilitator_server(f, per_minute=1))
        self.assertEqual([post(private, "/verify")[0] for _ in range(5)], [200] * 5)
        self.assertIn("extension-responses", post(private, "/settle")[2])


class Node:
    """A node of main that takes each made-up transaction once; each pays PAY_TO 0.00001 NORTH and is in a block as
    soon as it is sent."""

    def __init__(self):
        self.sent = set()

    @staticmethod
    def txid(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    def call(self, method, *params):
        if method == "getblockhash":
            return x402.NETWORKS["main"][len("bip122:"):] + "00" * 16
        if method == "decoderawtransaction":
            return {"txid": self.txid(params[0]), "vout": [{"n": 0, "value": 0.00001, "scriptPubKey": {"address": PAY_TO}}]}
        if method == "testmempoolaccept":
            return [{"allowed": self.txid(params[0][0]) not in self.sent}]
        if method == "sendrawtransaction":
            self.sent.add(self.txid(params[0]))
            return self.txid(params[0])
        if method == "gettxout":
            return {"confirmations": 1} if params[0] in self.sent else None
        raise RuntimeError("not here: %s" % method)


def payment(n, claim="c1" * 16):
    return {"x402Version": 2, "payload": {"transaction": "%064x" % n, "claim": claim}, "accepted": dict(WANTED),
            "resource": {"url": "https://shop.example/thing"}}


class AFullFacilitator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-x402-public-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.used = os.path.join(self.tmp, "x402-used.jsonl")

    def lines(self):
        with open(self.used) as f:
            return len(f.read().splitlines())

    def test_takes_no_new_payment_and_still_settles_what_it_has(self):
        with self.assertRaises(ValueError):
            x402.Facilitator(Node(), "main", self.used, most_payments=0)
        f = x402.Facilitator(Node(), "main", self.used, most_payments=1)
        self.assertTrue(f.settle(payment(1), dict(WANTED))["success"])
        kept = self.lines()
        self.assertEqual(f.verify(payment(2), dict(WANTED))["invalidReason"], "settlement_failed")
        self.assertEqual(f.settle(payment(2), dict(WANTED))["errorReason"], "settlement_failed")
        self.assertEqual((self.lines(), len(f.known)), (kept, 1))                  # the refused one left no trace
        again = f.settle(payment(1), dict(WANTED))                                  # one it has: the same answer again
        self.assertEqual((again["success"], again.get("redelivered")), (True, True))
        # after a start again it counts what it kept
        f = x402.Facilitator(Node(), "main", self.used, most_payments=1)
        self.assertEqual(f.settle(payment(3), dict(WANTED))["errorReason"], "settlement_failed")


class OnTheCommandLine(unittest.TestCase):
    def refused(self, *more):
        out = io.StringIO()
        tmp = tempfile.mkdtemp(prefix="polarka-x402-public-")
        self.addCleanup(shutil.rmtree, tmp, True)
        status = cli.run(["--dir", tmp, "x402-facilitator", "--chain", "main", "--rpc-url", "http://127.0.0.1:9",
                          "--rpc-cookie", os.path.join(tmp, "no-cookie")] + list(more), out)
        return status, json.loads(out.getvalue())

    def test_what_is_refused_before_the_node_is_asked(self):
        for more in (["--public", "--bazaar-origin", "https://gate.example.org"], ["--public", "--per-minute", "0"],
                     ["--public", "--per-minute", "601"], ["--most-payments", "0"], ["--public", "--listen", "0.0.0.0:9571"]):
            status, said = self.refused(*more)
            self.assertEqual((status, said["error"]), (2, "INPUT"), more)


if __name__ == "__main__":
    unittest.main()
