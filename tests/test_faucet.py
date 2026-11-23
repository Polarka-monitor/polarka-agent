"""The faucet: work before a coin, one coin an address a day, nothing beyond the wallet's limits. Against the
small model of a wallet server, without a node."""
import collections
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polarka_agent import faucet, keys
from polarka_agent.wallet import Refused, Wallet
from tests.test_wallet_rules import Model, WORDS

N = keys.UNIT


def address(n):
    return keys.Account.from_words(keys.new_words(12), "regtest").address(0, n)


class FaucetTest(unittest.TestCase):
    def setUp(self):
        self.m = Model()
        self.now = [1_800_000_000]
        self.folder = os.path.join(tempfile.mkdtemp(prefix="polarka-faucet-"), "w")
        Wallet.create(self.folder, "regtest", self.m.fake.url, WORDS)
        with open(os.path.join(self.folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "2.1", "per_day_north": "6.5"}, f)
        self.w = Wallet(self.folder, clock=lambda: self.now[0])
        for i in range(8):
            self.m.coin(self.w.account.address(0, i), 10 * N, "%02x" % (i + 1) * 32)
        self.f = faucet.Faucet(self.w, 2 * N, base_bits=8, clock=lambda: self.now[0])

    def tearDown(self):
        self.w.server.close()
        self.m.fake.stop()

    def claim(self, to, source="1.2.3.4"):
        c = self.f.challenge(source, to)
        return self.f.claim(source, to, c["challenge"], faucet.solve(c["challenge"], c["bits"]))

    def refused(self, kind, call, *a, **k):
        with self.assertRaises(Refused) as e:
            call(*a, **k)
        self.assertEqual(e.exception.kind, kind)

    def test_work_then_a_coin_and_once_a_day(self):
        to = address(0)
        got = self.claim(to)
        self.assertEqual(got["paid"], "2.00000000")
        self.assertIn(got["transaction_id"], self.m.waiting)
        self.refused("ALREADY", self.f.challenge, "1.2.3.4", to)
        self.now[0] += faucet.PERIOD + 1
        self.assertEqual(self.claim(to)["paid"], "2.00000000")
        book = [json.loads(x) for x in open(os.path.join(self.folder, "faucet.jsonl"))]
        self.assertEqual([(e["address"], e["state"]) for e in book], [(to, "handed")] * 2)
        self.assertNotIn("1.2.3.4", open(os.path.join(self.folder, "faucet.jsonl")).read())

    def test_no_coin_without_the_work_or_with_another_ones_challenge(self):
        to, other = address(0), address(1)
        c = self.f.challenge("s", to)
        answer = faucet.solve(c["challenge"], c["bits"])
        wrong = next("%x" % i for i in range(1000) if not faucet.work_done(c["challenge"], "%x" % i, c["bits"]))
        self.refused("WORK", self.f.claim, "s", to, c["challenge"], wrong)
        self.refused("WORK", self.f.claim, "s", to, c["challenge"], "zz")
        self.refused("CHALLENGE", self.f.claim, "s", other, c["challenge"], answer)
        self.refused("CHALLENGE", self.f.claim, "s", to, "8.9999999999.abcd.ffff", answer)
        self.refused("CHALLENGE", self.f.claim, "s", to, "nonsense", answer)
        self.refused("ADDRESS", self.f.challenge, "s", "north1znotanaddress")
        self.refused("ADDRESS", self.f.challenge, "s", None)
        self.now[0] += faucet.CHALLENGE_LIFE + 1
        self.refused("CHALLENGE", self.f.claim, "s", to, c["challenge"], answer)
        self.assertEqual(self.m.waiting, {})

    def test_a_challenge_pays_once(self):
        to = address(0)
        c = self.f.challenge("s", to)
        answer = faucet.solve(c["challenge"], c["bits"])
        self.f.claim("s", to, c["challenge"], answer)
        self.refused("ALREADY", self.f.claim, "s", to, c["challenge"], answer)
        self.assertEqual(len(self.m.waiting), 1)

    def test_the_wallets_limits_are_the_faucets_purse(self):
        for i in range(3):
            self.assertEqual(self.claim(address(i), "s%d" % i)["paid"], "2.00000000")
        self.refused("EMPTY", self.claim, address(9), "s9")                 # 6.5 a day: a fourth would pass it
        self.assertEqual(len(self.m.waiting), 3)
        self.now[0] += faucet.PERIOD + 1
        self.assertEqual(self.claim(address(9), "s9")["paid"], "2.00000000")

    def test_more_asking_means_more_work_and_one_source_cannot_ask_without_end(self):
        self.assertEqual(self.f.bits(), 8)
        self.f.recent = collections.deque([self.now[0]] * 8)
        self.assertEqual(self.f.bits(), 9)
        self.f.recent = collections.deque([self.now[0]] * 64)
        self.assertEqual(self.f.bits(), 12)
        self.f.recent = collections.deque([self.now[0]] * 10 ** 6)
        self.assertEqual(self.f.bits(), faucet.ASK_BITS)                    # never more than this program's own asker does unasked
        self.f.base_bits = 20
        self.assertEqual(self.f.bits(), faucet.ASK_BITS)
        self.f.base_bits = 28
        self.assertEqual(self.f.bits(), 28)                                 # unless the owner set the quiet work higher
        self.f.base_bits = 8
        self.now[0] += 3601
        self.assertEqual(self.f.bits(), 8)
        for i in range(20):
            self.refused("ADDRESS", self.f.challenge, "one", "x")
        self.refused("TOO_OFTEN", self.f.challenge, "one", address(0))
        self.assertIn("challenge", self.f.challenge("another", address(0)))

    def test_an_amount_that_the_limits_would_never_let_through_does_not_start(self):
        with open(os.path.join(self.folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "2", "per_day_north": "500"}, f)       # 2 and its fee is over 2
        self.refused("LIMITS", faucet.serve, self.w, 2 * N, "127.0.0.1:0")
        self.refused("LIMITS", faucet.serve, self.w, 0, "127.0.0.1:0")
        os.remove(os.path.join(self.folder, "limits.json"))
        self.refused("NO_LIMITS", faucet.serve, self.w, 2 * N, "127.0.0.1:0")

    def test_an_agent_asks_through_the_mcp_server_and_does_no_work_without_bound(self):
        from polarka_agent import mcp
        asker_folder = os.path.join(os.path.dirname(self.folder), "asker")
        Wallet.create(asker_folder, "regtest", self.m.fake.url, None)
        asker = Wallet(asker_folder)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), faucet.make_handler(self.f, False))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            got = mcp.call(asker, "north_faucet", {"url": url}, allow_local=True)
            self.assertEqual(got["transaction_id"], self.m.handed[-1])
            self.assertEqual(asker.account.address(0, 0), got["asked"])          # to a fresh address of the asking wallet
            # a faucet that asks for a quarter of an hour of work gets none from an agent's tool
            srv.RequestHandlerClass = faucet.make_handler(faucet.Faucet(self.w, 2 * N, base_bits=28, clock=lambda: self.now[0]), False)
            self.refused("FAUCET", mcp.call, asker, "north_faucet", {"url": url}, allow_local=True)
            self.refused("FAUCET", mcp.call, asker, "north_faucet", {"url": "http://127.0.0.1.faucet.example"}, allow_local=True)
            self.assertEqual(len(self.m.handed), 1)
        finally:
            srv.shutdown()
            srv.server_close()
            asker.server.close()

    def test_with_trust_proxy_it_listens_on_loopback_only(self):
        """Human review of 23 November, L-5: with --trust-proxy the asker's address is the web server's word."""
        for listen in ("0.0.0.0:0", "10.0.0.5:0", "localhost:0", "[::]:0"):
            self.refused("LISTEN", faucet.serve, self.w, 2 * N, listen, True)
        # on loopback it gets as far as the limits (here: an amount that the limits never let through)
        self.refused("LIMITS", faucet.serve, self.w, 0, "127.0.0.1:0", True)
        self.refused("LIMITS", faucet.serve, self.w, 0, "[::1]:0", True)

    def test_a_server_that_fails_after_signing_does_not_open_the_challenge_again(self):
        """Human review, L-5: the challenge goes back only when the wallet failed before it signed anything."""
        from polarka_agent.server import ServerError

        def silent(*a, **k):
            raise ServerError(0, "no answer")
        self.w.server.validate = silent
        self.w.server.broadcast = silent
        self.w.server.known = silent
        to = address(0)
        c = self.f.challenge("s", to)
        answer = faucet.solve(c["challenge"], c["bits"])
        self.refused("NOT_NOW", self.f.claim, "s", to, c["challenge"], answer)     # signed, outcome not known
        self.refused("ALREADY", self.f.claim, "s", to, c["challenge"], answer)     # the challenge stays used
        self.assertIn(to, self.f.paid)                                              # the address stays closed today

    def test_the_asker_works_no_longer_than_a_challenge_lives_and_no_harder_than_it_says(self):
        """Human review, L-5: faucet.ask() bounds what a faucet says with its own numbers."""
        import inspect
        self.assertEqual(inspect.signature(faucet.ask).parameters["most_bits"].default, faucet.ASK_BITS)
        seen = {}
        real = faucet.solve

        def solve(challenge, bits, most=None, until=None):
            seen["until"] = until
            return None                                     # no answer: the claim is never made
        faucet.solve = solve
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), faucet.make_handler(self.f, False))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = "http://127.0.0.1:%d" % srv.server_address[1]
            ten_days = self.now[0] + 10 * 24 * 3600
            original = self.f.challenge
            self.f.challenge = lambda source, address: dict(original(source, address), expires=ten_days)
            started = int(time.time())
            self.refused("FAUCET", faucet.ask, url, address(1))
            self.assertLessEqual(seen["until"], started + faucet.CHALLENGE_LIFE + 5)
        finally:
            faucet.solve = real
            srv.shutdown()
            srv.server_close()

    def test_over_http_from_asking_to_a_coin(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), faucet.make_handler(self.f, False))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d" % srv.server_address[1]
            to = address(0)
            got = faucet.ask(url, to)
            self.assertEqual((got["asked"], got["work_bits"]), (to, 8))
            self.assertIn(got["transaction_id"], self.m.waiting)
            self.refused("FAUCET", faucet.ask, url, to)                     # the same address again today
            self.refused("FAUCET", faucet.ask, "http://example.org", to)    # not over plain http to somewhere else
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
