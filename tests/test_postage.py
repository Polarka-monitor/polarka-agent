"""Postage: no call without it, a payment gives calls to the token it was quoted for and to no other, and an
address that was not paid serves the next asker. Against the small model of a wallet server, without a node."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polarka_agent import keys, postage
from polarka_agent.wallet import Refused, Wallet
from tests.test_wallet_rules import Model, WORDS

N = keys.UNIT


class Behind(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"served": self.path, "postage_header_seen": "X-Postage" in self.headers}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class PostageTest(unittest.TestCase):
    def setUp(self):
        self.m = Model()
        self.now = [1_800_000_000]
        self.folder = os.path.join(tempfile.mkdtemp(prefix="polarka-postage-"), "w")
        Wallet.create(self.folder, "regtest", self.m.fake.url, WORDS)
        self.w = Wallet(self.folder, clock=lambda: self.now[0])
        self.g = postage.Gate(self.w, N // 100, 100, clock=lambda: self.now[0], look_every=0)
        self.n = 0

    def tearDown(self):
        self.w.server.close()
        self.m.fake.stop()

    def pay(self, address, amount):
        self.n += 1
        self.m.coin(address, amount, "%02x" % self.n * 32)

    def test_no_call_without_postage_and_calls_for_a_payment(self):
        self.assertFalse(self.g.spend(None, "s"))
        self.assertFalse(self.g.spend("nonsense", "s"))
        q = self.g.quote()
        self.assertEqual((q["price"], q["calls"]), ("0.01000000", 100))
        self.assertFalse(self.g.spend(q["token"], "s"))                 # quoted, not paid
        self.assertEqual(self.g.status(q["token"]), {"paid": False, "calls_left": 0})
        self.pay(q["pay_to"], N // 100)
        self.assertEqual(self.g.status(q["token"]), {"paid": True, "calls_left": 100})
        for _ in range(100):
            self.assertTrue(self.g.spend(q["token"], "s"))
        self.assertFalse(self.g.spend(q["token"], "s"))                 # used up
        with self.assertRaises(Refused):
            self.g.status("another token")

    def test_too_little_gives_nothing_and_more_gives_more(self):
        a, b = self.g.quote(), self.g.quote()
        self.assertNotEqual(a["pay_to"], b["pay_to"])
        self.pay(a["pay_to"], N // 100 - 1)
        self.pay(b["pay_to"], 3 * N // 100 + 5)
        self.assertEqual(self.g.status(a["token"]), {"paid": False, "calls_left": 0})
        self.assertEqual(self.g.status(b["token"]), {"paid": True, "calls_left": 300})

    def test_a_payment_belongs_to_the_token_it_was_quoted_for(self):
        a = self.g.quote()
        self.now[0] += postage.QUOTE_LIFE + 601                         # a never paid, and its time is over
        b = self.g.quote()
        self.assertEqual(b["pay_to"], a["pay_to"])                      # the address serves the next asker
        self.pay(b["pay_to"], N // 100)
        self.assertEqual(self.g.status(a["token"]), {"paid": False, "calls_left": 0})
        self.assertEqual(self.g.status(b["token"]), {"paid": True, "calls_left": 100})
        c = self.g.quote()                                              # and again, with what was paid already counted
        self.assertEqual(c["pay_to"], a["pay_to"])
        self.assertEqual(self.g.status(c["token"]), {"paid": False, "calls_left": 0})
        self.pay(c["pay_to"], N // 100)
        self.assertEqual(self.g.status(c["token"]), {"paid": True, "calls_left": 100})

    def test_what_is_written_down_holds_no_token_and_comes_back(self):
        q = self.g.quote()
        self.pay(q["pay_to"], N // 100)
        self.assertTrue(self.g.spend(q["token"], "s"))
        self.assertNotIn(q["token"], open(os.path.join(self.folder, "postage.json")).read())
        again = postage.Gate(self.w, N // 100, 100, clock=lambda: self.now[0], look_every=0)
        self.assertEqual(again.status(q["token"]), {"paid": True, "calls_left": 99})

    def test_free_calls_a_day_for_one_source(self):
        g = postage.Gate(self.w, N // 100, 100, free=2, clock=lambda: self.now[0])
        self.assertEqual([g.spend(None, "one") for _ in range(3)], [True, True, False])
        self.assertTrue(g.spend(None, "another"))
        self.now[0] += 86400
        self.assertTrue(g.spend(None, "one"))

    def test_over_http_before_a_service(self):
        behind = ThreadingHTTPServer(("127.0.0.1", 0), Behind)
        gate = ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(self.g, "127.0.0.1:%d" % behind.server_address[1], False))
        for s in (behind, gate):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d" % gate.server_address[1]

        def ask(path, token=None, data=None):
            req = urllib.request.Request(url + path, data, {"X-Postage": token} if token else {})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        try:
            status, said = ask("/v1/thing")
            self.assertEqual((status, said["error"], said["price"]), (402, "POSTAGE", "0.01000000"))
            status, q = ask("/_postage/quote", data=b"")
            self.assertEqual(status, 200)
            self.assertEqual(ask("/v1/thing", q["token"])[0], 402)
            self.pay(q["pay_to"], N // 100)
            status, said = ask("/v1/thing?x=1", q["token"])
            self.assertEqual((status, said), (200, {"served": "/v1/thing?x=1", "postage_header_seen": False}))     # the token stays at the gate
            self.assertEqual(ask("/_postage/status", q["token"]), (200, {"paid": True, "calls_left": 99}))
        finally:
            for s in (behind, gate):
                s.shutdown()
                s.server_close()


if __name__ == "__main__":
    unittest.main()
