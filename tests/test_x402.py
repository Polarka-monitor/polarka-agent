"""x402 in NORTH on regtest: a resource that answers 402, a buyer's wallet that pays within its limits, the
seller's node that takes the payment, and what must not go through: the same payment twice, too little, too much,
another chain, requirements changed on the way."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from polarka_agent import walletd, x402
from polarka_agent.wallet import Refused, Wallet, units

from . import bench

SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"       # key 0/0 of the public test phrase
PRICE = units("0.5")


@unittest.skipUnless(bench.find_bin(), "truenorthd and truenorth-cli are needed (TN_BIN names their folder)")
class X402OnRegtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(bench.find_bin())
        cls.standin = bench.StandIn(cls.node)
        cls.tmp = tempfile.mkdtemp(prefix="polarka-x402-")
        cls.buyer = cls.make("buyer", {"per_payment_north": "2", "per_day_north": "20"})
        cls.seller = cls.make("seller")
        cls.node.mine(3, cls.buyer.fresh_address())
        cls.node.mine(100, SINK)
        cls.rpc = walletd.Node("http://127.0.0.1:%d" % cls.node.port, cls.node.cookie)
        cls.network = x402.network_of(cls.rpc)
        cls.pay_to = cls.seller.account.address(0, 0)
        cls.facilitator = x402.Facilitator(cls.rpc, "regtest", os.path.join(cls.tmp, "used.txt"))
        cls.server = x402.paywall(cls.facilitator, PRICE, cls.pay_to, b"the answer is 42", description="a test resource")
        cls.url = "http://127.0.0.1:%d/answer" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def make(cls, name, limits=None):
        folder = os.path.join(cls.tmp, name)
        Wallet.create(folder, "regtest", cls.standin.url, None)
        if limits:
            with open(os.path.join(folder, "limits.json"), "w") as f:
                json.dump(limits, f)
        return Wallet(folder)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.standin.stop()
        cls.node.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ask(self, payment=None):
        headers = {} if payment is None else {"PAYMENT-SIGNATURE": x402.b64json(payment)}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, headers=headers), timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def offer(self):
        status, headers, _ = self.ask()
        self.assertEqual(status, 402)
        needed = x402.from_b64json(headers.get("PAYMENT-REQUIRED"))
        self.assertEqual(needed["x402Version"], 2)
        return needed, needed["accepts"][0]

    def test_unpaid_is_told_the_price(self):
        needed, offer = self.offer()
        self.assertEqual(offer, {"scheme": "exact", "network": self.network, "amount": str(PRICE), "asset": "NORTH",
                                 "payTo": self.pay_to, "maxTimeoutSeconds": 60})
        self.assertTrue(self.network.startswith("bip122:") and len(self.network) == 39)
        self.assertEqual(self.facilitator.supported()["kinds"][0]["network"], self.network)

    def test_paid_once_and_only_once(self):
        done = x402.fetch(self.url, self.buyer, units("1"), network=self.network)
        self.assertEqual((done["status"], done["body"], done["paid"]), (200, b"the answer is 42", "0.50000000"))
        self.assertEqual(done["settlement"], {"success": True, "transaction": done["txid"], "network": self.network, "payer": None})
        self.assertIn(done["txid"], self.node.cli("getrawmempool"))
        # the same payment shown again buys nothing
        raw = self.node.cli("getrawtransaction", done["txid"])
        needed, offer = self.offer()
        status, headers, _ = self.ask({"x402Version": 2, "accepted": offer, "payload": {"transaction": raw}})
        self.assertEqual(status, 402)
        self.assertEqual(x402.from_b64json(headers.get("PAYMENT-RESPONSE"))["errorReason"], "transaction_already_used")
        # and not after the seller starts again either
        again = x402.Facilitator(self.rpc, "regtest", os.path.join(self.tmp, "used.txt"))
        self.assertEqual(again.verify({"x402Version": 2, "accepted": offer, "payload": {"transaction": raw}}, offer)["invalidReason"],
                         "transaction_already_used")
        self.node.mine(1, SINK)
        self.assertGreaterEqual(self.seller.balance()["spendable"] + self.seller.balance()["unripe"], 0)
        self.assertIn(done["txid"], [line["txid"] for line in self.seller.history()["lines"]])

    def test_too_little_and_requirements_changed_on_the_way(self):
        needed, offer = self.offer()
        less = self.buyer.pay([(self.pay_to, PRICE - 1)], "x402 test, too little", hand=False)
        status, headers, _ = self.ask({"x402Version": 2, "accepted": offer, "payload": {"transaction": less["raw"]}})
        self.assertEqual((status, x402.from_b64json(headers.get("PAYMENT-RESPONSE"))["errorReason"]), (402, "insufficient_funds"))
        self.assertNotIn(less["id"], self.node.cli("getrawmempool"))
        cheaper = dict(offer, amount=str(PRICE - 1))           # the buyer claims another price
        status, headers, _ = self.ask({"x402Version": 2, "accepted": cheaper, "payload": {"transaction": less["raw"]}})
        self.assertEqual(x402.from_b64json(headers.get("PAYMENT-RESPONSE"))["errorReason"], "invalid_payment_requirements")
        status, _, _ = self.ask({"x402Version": 2})
        self.assertEqual(status, 402)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(self.url, headers={"PAYMENT-SIGNATURE": "not base64!"}), timeout=30)
        self.assertEqual(caught.exception.code, 400)
        # the payment that was refused stays open in the buyer's wallet; the next one stands against it
        done = x402.fetch(self.url, self.buyer, units("1"), network=self.network)
        self.assertEqual(done["status"], 200)

    def test_from_the_command_line_and_as_a_tool(self):
        import io
        from unittest import mock
        from polarka_agent import cli, mcp
        with mock.patch.dict(x402.NETWORKS, {"regtest": self.network}):
            out = io.StringIO()
            status = cli.run(["--dir", self.buyer.folder, "fetch", self.url, "--most", "1"], out=out)
            got = json.loads(out.getvalue())
            self.assertEqual((status, got["status"], got["body"], got["settlement"]["success"]), (0, 200, "the answer is 42", True))
            out = io.StringIO()
            status = cli.run(["--dir", self.buyer.folder, "fetch", self.url, "--most", "0.1"], out=out)
            self.assertEqual((status, json.loads(out.getvalue())["error"]), (2, "X402_TOO_MUCH"))
            got = mcp.call(self.buyer, "north_fetch", {"url": self.url, "most": "1"})
            self.assertEqual((got["status"], got["body"], got["paid"]), (200, "the answer is 42", "0.50000000"))

    def test_what_the_buyer_does_not_pay(self):
        before = len(self.buyer.payments())
        with self.assertRaises(Refused) as caught:
            x402.fetch(self.url, self.buyer, units("0.4"), network=self.network)
        self.assertEqual(caught.exception.kind, "X402_TOO_MUCH")
        with self.assertRaises(Refused) as caught:
            x402.fetch(self.url, self.buyer, units("1"), network="bip122:" + "00" * 16)
        self.assertEqual(caught.exception.kind, "X402")
        with self.assertRaises(Refused) as caught:
            x402.fetch(self.url, self.buyer, units("1"))            # regtest has no name of its own here
        self.assertEqual(caught.exception.kind, "X402")
        poor = self.make("poor", {"per_payment_north": "0.1", "per_day_north": "1"})
        with self.assertRaises(Refused) as caught:
            x402.fetch(self.url, poor, units("1"), network=self.network)
        self.assertIn(caught.exception.kind, ("OVER_LIMIT", "NOT_ENOUGH", "NOTHING_TO_SPEND"))
        self.assertEqual(len(self.buyer.payments()), before)
        with self.assertRaises(Refused):
            x402.fetch("http://example.com/x", self.buyer, units("1"), network=self.network)


if __name__ == "__main__":
    unittest.main()
