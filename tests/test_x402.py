"""x402 in NORTH on regtest: a resource that answers 402, a buyer's wallet that pays within its limits, the
seller's node that takes the payment, and what must not go through: the same payment twice, too little, too much,
another chain, requirements changed on the way. And a seller that waits for a block: a payment copied from the
network takes nothing, the buyer asks again and pays once, a facilitator as a service, and a gate of postage that
sells its lot over x402."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

from polarka_agent import postage, walletd, x402
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
        cls.facilitator = x402.Facilitator(cls.rpc, "regtest", os.path.join(cls.tmp, "used.txt"), confirmations=0)
        cls.server = x402.paywall(cls.facilitator, PRICE, cls.pay_to, b"the answer is 42", description="a test resource")
        cls.url = "http://127.0.0.1:%d/answer" % cls.server.server_address[1]
        cls.skew = 0
        # a seller that waits for one block
        cls.patient = x402.Facilitator(cls.rpc, "regtest", os.path.join(cls.tmp, "used-deep.jsonl"), confirmations=1,
                                       clock=lambda: time.time() + cls.skew)
        cls.deep = x402.paywall(cls.patient, PRICE, cls.seller.account.address(0, 1), b"deep answer")
        cls.deep_url = "http://127.0.0.1:%d/deep" % cls.deep.server_address[1]
        cls.servers = [cls.server, cls.deep]
        for s in cls.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()

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
        for s in cls.servers:
            s.shutdown()
        cls.standin.stop()
        cls.node.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def start(cls, server):
        cls.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % server.server_address[1]

    def ask(self, payment=None, url=None, header=None):
        headers = {} if payment is None else {"PAYMENT-SIGNATURE": x402.b64json(payment)}
        if header is not None:
            headers = {"PAYMENT-SIGNATURE": header}
        try:
            with urllib.request.urlopen(urllib.request.Request(url or self.url, headers=headers), timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def offer(self, url=None):
        status, headers, _ = self.ask(url=url)
        self.assertEqual(status, 402)
        needed = x402.from_b64json(headers.get("PAYMENT-REQUIRED"))
        self.assertEqual(needed["x402Version"], 2)
        return needed, needed["accepts"][0]

    def test_unpaid_is_told_the_price(self):
        needed, offer = self.offer()
        self.assertEqual(offer, {"scheme": "exact", "network": self.network, "amount": str(PRICE), "asset": "NORTH",
                                 "payTo": self.pay_to, "maxTimeoutSeconds": 60, "extra": {"confirmations": 0}})
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


class X402Deep(X402OnRegtest):
    """The same chain, a seller that waits for a block."""

    def setUp(self):
        type(self).skew = 0

    def book(self, wallet=None):
        return x402._book(wallet or self.buyer)

    def test_unpaid_is_told_the_price(self):
        _, offer = self.offer(self.deep_url)
        self.assertEqual(offer["extra"], {"confirmations": 1})
        self.assertEqual(self.patient.supported()["kinds"][0]["extra"], {"confirmations": 1, "instantBelow": "0"})

    def test_waits_for_a_block_and_a_copy_takes_nothing(self):
        done = x402.fetch(self.deep_url, self.buyer, units("1"), network=self.network, wait=0)
        made = len(self.buyer.payments())
        self.assertEqual((done["status"], done["pending"], done["settlement"]["errorReason"]), (202, True, "awaiting_confirmation"))
        self.assertEqual((done["settlement"]["confirmations"], done["settlement"]["required"]), (0, 1))
        self.assertIn(done["txid"], self.node.cli("getrawmempool"))
        header = self.book()["pending"][self.deep_url]["header"]
        # whoever copies the payment from the network has no claim
        raw = self.node.cli("getrawtransaction", done["txid"])
        _, offer = self.offer(self.deep_url)
        for payload in ({"transaction": raw}, {"transaction": raw, "claim": "ab" * 16}):
            status, headers, _ = self.ask({"x402Version": 2, "accepted": offer, "payload": payload}, url=self.deep_url)
            self.assertEqual((status, x402.from_b64json(headers.get("PAYMENT-RESPONSE"))["errorReason"]), (402, "transaction_already_used"))
        # asked again while it waits: nothing more is paid
        again = x402.fetch(self.deep_url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((again["status"], again["txid"]), (202, done["txid"]))
        self.assertEqual(len(self.buyer.payments()), made)
        self.node.mine(1, SINK)
        got = x402.fetch(self.deep_url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["status"], got["body"], got["txid"], got["settlement"]["success"]), (200, b"deep answer", done["txid"], True))
        self.assertIn("nothing more was paid", got["note"])
        self.assertEqual(len(self.buyer.payments()), made)
        self.assertEqual(self.book()["pending"], {})
        # an answer lost on the way is given again for a while, then not
        self.assertEqual(self.ask(url=self.deep_url, header=header)[0], 200)
        type(self).skew = 601
        status, headers, _ = self.ask(url=self.deep_url, header=header)
        self.assertEqual((status, x402.from_b64json(headers.get("PAYMENT-RESPONSE"))["errorReason"]), (402, "transaction_already_used"))
        # after a start again too
        again = x402.Facilitator(self.rpc, "regtest", os.path.join(self.tmp, "used-deep.jsonl"))
        self.assertEqual(again.known[done["txid"]]["state"], "delivered")

    def test_a_payment_without_a_claim_is_not_taken(self):
        _, offer = self.offer(self.deep_url)
        made = self.buyer.pay([(offer["payTo"], PRICE)], "x402 test, no claim", hand=False)
        status, headers, _ = self.ask({"x402Version": 2, "accepted": offer, "payload": {"transaction": made["raw"]}}, url=self.deep_url)
        self.assertEqual((status, x402.from_b64json(headers.get("PAYMENT-RESPONSE"))["errorReason"]), (402, "claim_required"))
        self.assertNotIn(made["id"], self.node.cli("getrawmempool"))
        self.assertFalse(self.patient.verify({"x402Version": 2, "accepted": offer, "payload": {"transaction": made["raw"]}}, offer)["isValid"])

    def test_a_facilitator_as_a_service(self):
        base = self.start(x402.facilitator_server(self.patient))
        remote = x402.RemoteFacilitator(base)
        self.assertEqual((remote.network, remote.confirmations, remote.instant_below), (self.network, 1, 0))
        url = self.start(x402.paywall(remote, PRICE, self.seller.account.address(0, 2), b"served by a stranger")) + "/x"
        first = x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual(first["status"], 202)
        self.node.mine(1, SINK)
        got = x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["status"], got["body"]), (200, b"served by a stranger"))

        def post(path, body):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(base + path, data, {"Content-Type": "application/json"}), timeout=30) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
        self.assertEqual(post("/settle", b"not json")[0], 400)
        _, offer = self.offer(url)
        status, answer = post("/verify", {"x402Version": 2, "paymentPayload": {}, "paymentRequirements": dict(offer, network="bip122:" + "0" * 32)})
        self.assertEqual((status, answer["invalidReason"]), (200, "invalid_network"))
        status, answer = post("/settle", {"x402Version": 2, "paymentPayload": {}, "paymentRequirements": dict(offer, payTo="north1zbad")})
        self.assertEqual(answer["errorReason"], "invalid_payment_requirements")
        status, answer = post("/settle", {"x402Version": 2, "paymentPayload": {"x402Version": 2}, "paymentRequirements": offer})
        self.assertEqual(answer["errorReason"], "invalid_payload")

    def test_a_gate_of_postage_sells_its_lot_over_x402(self):
        class Up(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b"upstream ok")
        up = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Up))
        gate_wallet = self.make("gate")
        gate = postage.Gate(gate_wallet, PRICE, 3, x402=self.patient)
        url = self.start(ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(gate, up.split("//")[1], False))) + "/v1/unit"
        status, headers, body = self.ask(url=url)
        self.assertEqual(status, 402)
        self.assertEqual(json.loads(body)["x402"]["confirmations"], 1)
        self.assertEqual(x402.from_b64json(headers.get("PAYMENT-REQUIRED"))["accepts"][0]["payTo"], gate.state["x402_address"])
        made = len(self.buyer.payments())
        self.assertEqual(x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)["status"], 202)
        self.node.mine(1, SINK)
        got = x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["status"], got["body"], got["calls_left"]), (200, b"upstream ok", 2))
        got = x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["status"], got["paid"], got["calls_left"]), (200, None, 1))
        got = x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["status"], got["paid"]), (200, None))
        self.assertEqual(self.book()["lots"], {})
        self.assertEqual(len(self.buyer.payments()), made + 1)
        # the lot is used up: the next call pays a new one
        self.assertEqual(x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)["status"], 202)
        self.assertEqual(len(self.buyer.payments()), made + 2)
        self.node.mine(1, SINK)
        self.assertEqual(x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)["status"], 200)

    # the tests of the seller that takes a payment at once are not repeated here
    test_paid_once_and_only_once = test_too_little_and_requirements_changed_on_the_way = None
    test_from_the_command_line_and_as_a_tool = test_what_the_buyer_does_not_pay = None
