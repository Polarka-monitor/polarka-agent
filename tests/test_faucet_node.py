"""The faucet against a TrueNorth Core node on regtest, through the stand-in for a Northbound wallet server:
a program without a coin asks over HTTP, does the work, and the coin is in a block on its address. Once a
day for an address, and never past the limits of the faucet's wallet."""
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
from polarka_agent import faucet, keys, postage
from polarka_agent.wallet import Refused, Wallet, units
from tests import bench

N = keys.UNIT
SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"       # key 0/0 of the public test phrase


@unittest.skipUnless(bench.find_bin(), "truenorthd and truenorth-cli are needed (TN_BIN names their folder)")
class FaucetOnNodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(bench.find_bin())
        cls.standin = bench.StandIn(cls.node)
        cls.tmp = tempfile.mkdtemp(prefix="polarka-agent-faucet-")
        cls.count = 0
        bank = cls.bank = cls.make({"per_payment_north": "100000", "per_day_north": "1000000"})
        cls.node.mine(5, bank.fresh_address())
        cls.node.mine(100, SINK)
        # the faucet's wallet: six coins of 3 NORTH, and limits its owner wrote: 2 and its fee a payment, 5 a day
        cls.tap = cls.make({"per_payment_north": "2.1", "per_day_north": "5"})
        for _ in range(6):
            done = bank.pay([(cls.tap.fresh_address(), 3 * N)])
            assert done["state"] == "handed", done
            cls.node.mine(1, SINK)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), faucet.make_handler(faucet.Faucet(cls.tap, 2 * N, base_bits=8), False))
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.standin.stop()
        cls.node.stop()

    @classmethod
    def make(cls, limits=None):
        cls.count += 1
        folder = os.path.join(cls.tmp, "w%d" % cls.count)
        Wallet.create(folder, "regtest", cls.standin.url, None)
        if limits is not None:
            with open(os.path.join(folder, "limits.json"), "w") as f:
                json.dump(limits, f)
        return Wallet(folder)

    def test_a_first_coin_once_a_day_and_never_past_the_limits(self):
        a, b, c = (self.make() for _ in range(3))                       # three programs without a coin and without limits
        to_a, to_b, to_c = a.fresh_address(), b.fresh_address(), c.fresh_address()
        got = faucet.ask(self.url, to_a)
        self.assertEqual(got["asked"], to_a)
        self.assertIn(got["transaction_id"], self.node.cli("getrawmempool"))
        with self.assertRaises(Refused) as again:                       # the same address a second time that day
            faucet.ask(self.url, to_a)
        self.assertIn("ALREADY", str(again.exception))
        faucet.ask(self.url, to_b)                                      # 4 NORTH given today
        with self.assertRaises(Refused):                                # a third would make 6, the owner said 5 a day
            faucet.ask(self.url, to_c)
        self.assertEqual(len(self.node.cli("getrawmempool")), 2)
        self.node.mine(1, SINK)
        self.assertEqual((a.received(to_a)["amount"], a.received(to_a)["confirmations"] >= 1), (2 * N, True))
        self.assertEqual(b.received(to_b)["amount"], 2 * N)
        self.assertEqual(c.received(to_c)["amount"], 0)
        self.assertEqual(sorted(p["state"] for p in self.tap.payments()), ["in_block", "in_block"])    # two payments, no third

    def test_the_whole_round_a_first_coin_then_postage_then_a_call(self):
        """A program with nothing gets a coin from the faucet, buys postage with it and is let through a gate."""
        class Behind(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"served": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        gate_wallet = self.make()                                       # the gate's wallet only receives: no limits
        behind = ThreadingHTTPServer(("127.0.0.1", 0), Behind)
        gate = ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(
            postage.Gate(gate_wallet, N // 100, 3, look_every=0), "127.0.0.1:%d" % behind.server_address[1], False))
        tap = self.make({"per_payment_north": "2.1", "per_day_north": "5"})    # a faucet of its own, so the day's limit of the other test stays out of it
        self.bank.pay([(tap.fresh_address(), 3 * N)])
        self.node.mine(1, SINK)
        well = ThreadingHTTPServer(("127.0.0.1", 0), faucet.make_handler(faucet.Faucet(tap, 2 * N, base_bits=8), False))
        for s in (behind, gate, well):
            s.daemon_threads = True
            threading.Thread(target=s.serve_forever, daemon=True).start()
        gate_url = "http://127.0.0.1:%d" % gate.server_address[1]

        def call(token=None):
            req = urllib.request.Request(gate_url + "/v1/thing", headers={"X-Postage": token} if token else {})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        try:
            program = self.make({"per_payment_north": "0.5", "per_day_north": "1"})
            self.assertEqual(call(), 402)                               # nothing without postage
            faucet.ask("http://127.0.0.1:%d" % well.server_address[1], program.fresh_address())
            self.node.mine(1, SINK)
            bought = postage.buy(program, gate_url, N // 10)
            self.assertEqual((bought["paid"], bought["state"]), ("0.01000000", "handed"))
            self.assertEqual(call(bought["token"]), 402)                # paid, but not in a block yet
            self.node.mine(1, SINK)
            self.assertEqual([call(bought["token"]) for _ in range(4)], [200, 200, 200, 402])     # three calls bought, three calls made
            self.assertEqual(gate_wallet.received(bought["pay_to"])["amount"], N // 100)
        finally:
            for s in (behind, gate, well):
                s.shutdown()
                s.server_close()


if __name__ == "__main__":
    unittest.main()
