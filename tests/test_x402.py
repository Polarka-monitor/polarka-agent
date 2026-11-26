"""x402 in NORTH on regtest: a resource that answers 402, a buyer's wallet that pays within its limits, the
seller's node that takes the payment, and what must not go through: the same payment twice, too little, too much,
another chain, requirements changed on the way. And a seller that waits for a block: a payment copied from the
network takes nothing, the buyer asks again and pays once, a facilitator as a service, and a gate of postage that
sells its lot over x402."""
import copy
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.parse
import urllib.request

from polarka_agent import postage, walletd, x402
from polarka_agent.wallet import Refused, Wallet, units

from . import bench

SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"       # key 0/0 of the public test phrase
PRICE = units("0.5")


class Echo(BaseHTTPRequestHandler):
    """A service behind a gate: answers 200 with the path it was asked for."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        said = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(said)))
        self.end_headers()
        self.wfile.write(said)


class PathOnly(x402.Facilitator):
    """A facilitator that knows no extra.resource: it binds a payment to the resource of its payload only, as one
    written to x402 without the naming of this package would."""

    @staticmethod
    def _named(wanted):
        return False

    @staticmethod
    def _resource(payment, wanted=None):
        return x402.Facilitator._resource(payment)


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
        if payment is not None and "resource" not in payment:          # what a client copies from the 402
            payment = dict(payment, resource={"url": urllib.parse.urlsplit(url or self.url).path})
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
            got = mcp.call(self.buyer, "north_fetch", {"url": self.url, "most": "1"}, allow_local=True)
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


class X402Deep(X402OnRegtest):
    """The same chain, a seller that waits for a block."""

    def setUp(self):
        type(self).skew = 0

    def book(self, wallet=None):
        return x402._book(wallet or self.buyer)

    def gate_at(self, name, calls=3, facilitator=None):
        """A gate of postage in front of Echo; (the gate, its url)."""
        up = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Echo))
        gate = postage.Gate(self.make(name), PRICE, calls, x402=facilitator or self.patient)
        return gate, self.start(ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(gate, up.split("//")[1], False)))

    def binding_kit(self, n):
        """(wanted(name), payment(wanted, path)) for a seller at key 0/n that waits for one block."""
        pay_to = self.seller.account.address(0, n)

        def wanted(name=None):
            w = x402.requirements(self.network, PRICE, pay_to, confirmations=1)
            if name is not None:
                w["extra"]["resource"] = name
            return w

        def payment(w, path):
            made = self.buyer.pay([(pay_to, PRICE)], "x402 test of binding", hand=False)
            return {"x402Version": 2, "resource": {"url": path}, "accepted": copy.deepcopy(w),
                    "payload": {"transaction": made["raw"], "claim": os.urandom(16).hex()}}
        return wanted, payment

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
        self.assertNotIn(self.deep_url, self.book()["pending"])
        # an answer lost on the way is given again for a while, then not
        self.assertEqual(self.ask(url=self.deep_url, header=header)[0], 200)
        type(self).skew = 601
        status, headers, _ = self.ask(url=self.deep_url, header=header)
        self.assertEqual((status, x402.from_b64json(headers.get("PAYMENT-RESPONSE"))["errorReason"]), (402, "transaction_already_used"))
        # after a start again too
        again = x402.Facilitator(self.rpc, "regtest", os.path.join(self.tmp, "used-deep.jsonl"))
        self.assertEqual(again.known[done["txid"]]["state"], "delivered")

    def test_a_coin_the_seller_spent_before_the_buyer_came_back_still_counts(self):
        # human review, 22 November: the paying output is spent, so the node's set of coins no longer has it; the
        # payment is found in its block instead of being called lost
        seller = self.make("spender", {"per_payment_north": "1", "per_day_north": "5"})
        facilitator = x402.Facilitator(self.rpc, "regtest", os.path.join(self.tmp, "used-spent.jsonl"), confirmations=1)
        url = self.start(x402.paywall(facilitator, PRICE, seller.account.address(0, 0), b"spent answer")) + "/spent"
        done = x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual(done["status"], 202)
        self.node.mine(1, SINK)
        spent = seller.pay([(SINK, units("0.4"))], "the seller spends what it got before the buyer comes back")
        self.assertEqual(spent["state"], "handed", spent)
        got = x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["status"], got["body"], got["txid"]), (200, b"spent answer", done["txid"]))

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

    def test_letters_paid_with_postage_over_x402(self):
        """mail.send buys its lot of postage over x402 the first time, waits for the block like any buyer, and uses
        the lot for the letters after it; the inbox reads them at the free door."""
        from polarka_agent import mail
        office = mail.PostOffice(os.path.join(self.tmp, "office"))
        paid, free = mail.servers(office, "127.0.0.1:0", "127.0.0.1:0", per_minute=600)
        free_url = self.start(free)
        office.origin = mail.origin_of(free_url)
        self.start(paid)
        gate = postage.Gate(self.make("gate-mail"), PRICE, 3, x402=self.patient)
        front = self.start(ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(
            gate, "127.0.0.1:1", False, [("/v1/mail", "127.0.0.1:%d" % paid.server_address[1])])))
        reader = self.make("reader")
        to = mail.my_inbox(reader)["inbox"]
        made = len(self.buyer.payments())
        first = mail.send(self.buyer, to, "first letter", units("1"), front, wait=0, network=self.network)
        self.assertEqual((first["sent"], first["pending"]), (False, True))
        self.node.mine(1, SINK)
        sent = mail.send(self.buyer, to, "first letter", units("1"), front, wait=0, network=self.network)
        self.assertEqual((sent["sent"], sent["calls_left"]), (True, 2))         # the letter was the lot's first call (review 11, L5)
        self.assertEqual(mail.send(self.buyer, to, "second", units("1"), front, wait=0, network=self.network)["calls_left"], 1)
        self.assertEqual(len(self.buyer.payments()), made + 1)
        got = mail.fetch_letters(reader, free_url)
        me = mail.my_inbox(self.buyer)["inbox"]
        self.assertEqual([(x["from"], x["text"]) for x in got["letters"]], [(me, "first letter"), (me, "second")])

    def test_two_paths_of_one_gate_while_its_lot_waits_for_a_block(self):
        """0.2.8: a call to another path of the same gate, while the payment for a lot there waits for its block,
        pays nothing more and loses nothing. On 23 November an agent stamped two files 66 seconds apart and bought
        two lots, the first one's claim waiting under the first url only."""
        class Up(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                said = self.path.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(said)))
                self.end_headers()
                self.wfile.write(said)
        up = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Up))
        gate = postage.Gate(self.make("gate-two-paths"), PRICE, 3, x402=self.patient)
        base = self.start(ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(gate, up.split("//")[1], False)))
        a, b = base + "/v1/unit/a", base + "/v1/unit/b"
        made = len(self.buyer.payments())
        self.assertEqual(x402.fetch(a, self.buyer, units("1"), network=self.network, wait=0)["status"], 202)
        waiting = x402.fetch(b, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((waiting["status"], waiting.get("pending")), (202, True))
        self.assertEqual(len(self.buyer.payments()), made + 1)                  # nothing more paid
        self.node.mine(1, SINK)
        got = x402.fetch(b, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["status"], got["body"]), (200, b"/v1/unit/b"))
        got = x402.fetch(a, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["status"], got["body"]), (200, b"/v1/unit/a"))
        self.assertEqual(len(self.buyer.payments()), made + 1)
        self.assertEqual([k for k in self.book()["pending"] if k.startswith(base)], [])     # nothing left waiting at this gate

    def test_a_payment_is_bound_to_the_first_name_its_seller_gives(self):
        """0.2.8 (review M2): a payment is bound to the name its seller gives (extra.resource). One first seen without
        a name is bound to its payload's resource until a seller names the thing there, and from then on to that name
        only: a buyer who shows its payment first without a name buys no more than one thing with it."""
        self.addCleanup(self.node.mine, 1, SINK)
        fac = x402.Facilitator(self.rpc, "regtest", os.path.join(self.tmp, "used-bound.jsonl"), confirmations=1)
        wanted, payment = self.binding_kit(9)

        def why(p, w):
            return fac.settle(p, w).get("errorReason")
        # named apart: a payment for "article 1" does not buy "article 2"
        p = payment(wanted("article 1"), "/a1")
        self.assertEqual([why(p, wanted("article 1")), why(p, wanted("article 2")), why(p, wanted("article 1"))],
                         [x402.AWAITING, "invalid_payment_requirements", x402.AWAITING])
        self.node.mine(1, SINK)
        self.assertTrue(fac.settle(p, wanted("article 1"))["success"])
        # first seen without a name, then named where its payload points: bound to that name from then on
        q = payment(wanted(), "/own")
        self.assertEqual([why(q, wanted()), why(q, wanted("r1")), why(q, wanted("r2"))],
                         [x402.AWAITING, x402.AWAITING, "invalid_payment_requirements"])
        self.assertFalse(fac.verify(q, wanted("r2"))["isValid"])
        self.assertTrue(fac.verify(dict(q, resource={"url": "/elsewhere"}), wanted("r1"))["isValid"])
        self.assertEqual(why(dict(q, resource={"url": "/elsewhere"}), wanted()), "invalid_payment_requirements")
        self.node.mine(1, SINK)
        self.assertTrue(fac.settle(q, wanted("r1"))["success"])
        # bound to a path: on another path it is refused, whatever name the seller gives there
        r = payment(wanted(), "/x")
        self.assertEqual([why(r, wanted()), why(dict(r, resource={"url": "/y"}), wanted("lot"))],
                         [x402.AWAITING, "invalid_payment_requirements"])
        self.node.mine(1, SINK)
        self.assertTrue(fac.settle(r, wanted())["success"])
        # delivered without a name: the first name it is shown with binds it, then it is given again (R-M1)
        self.assertTrue(fac.settle(r, wanted("lot")).get("redelivered"))
        self.assertEqual(why(r, wanted("lot 2")), "invalid_payment_requirements")
        # the binding outlives a restart, and verify wrote nothing
        again = x402.Facilitator(self.rpc, "regtest", os.path.join(self.tmp, "used-bound.jsonl"), confirmations=1)
        self.assertEqual(again.settle(q, wanted("r2"))["errorReason"], "invalid_payment_requirements")
        self.assertTrue(again.settle(q, wanted("r1")).get("redelivered"))

    def test_a_record_from_before_0_2_8_passes_as_it_did(self):
        """0.2.8 (review M1): a record written before 0.2.8 does not say whether its seller named the thing. One bound
        to the seller's name passes with that name, as it did; one bound to its payload's path passes there, and a
        seller that names the thing from now on (a gate of 0.2.8) binds it to that name on that path."""
        self.addCleanup(self.node.mine, 1, SINK)
        path = os.path.join(self.tmp, "used-old.jsonl")
        fac = x402.Facilitator(self.rpc, "regtest", path, confirmations=1)
        wanted, payment = self.binding_kit(10)

        def as_before(txid):
            rec = dict(fac.known[txid])
            rec.pop("named")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            old = x402.Facilitator(self.rpc, "regtest", path, confirmations=1)
            self.assertNotIn("named", old.known[txid])
            return old
        # bound to the seller's name, the payload pointing elsewhere
        p = payment(wanted("article-1"), "https://seller.example/articles/1")
        old = as_before(fac.settle(p, wanted("article-1"))["transaction"])
        self.assertEqual([old.settle(p, wanted("article-1")).get("errorReason"), old.settle(p, wanted("article-2")).get("errorReason")],
                         [x402.AWAITING, "invalid_payment_requirements"])
        self.node.mine(1, SINK)
        self.assertTrue(old.settle(p, wanted("article-1"))["success"])
        # bound to a path by a gate that did not name its lot; the gate names it now
        q = payment(wanted(), "/v1/stamp/a")
        old = as_before(fac.settle(q, wanted())["transaction"])
        b = dict(q, resource={"url": "/v1/stamp/b"})
        self.assertEqual([old.settle(b, wanted("lot")).get("errorReason"), old.settle(q, wanted("lot")).get("errorReason"),
                          old.settle(b, wanted("lot")).get("errorReason")],
                         ["invalid_payment_requirements", x402.AWAITING, x402.AWAITING])
        self.node.mine(1, SINK)
        self.assertTrue(old.settle(b, wanted("lot"))["success"])

    def test_a_payment_refused_on_another_path_stays_for_its_own(self):
        """0.2.8: a seller names one thing on two paths, but its facilitator knows no extra.resource and binds a
        payment to its payload's path. Shown on the second path the payment is refused there and stays for the first,
        which takes it once it is in a block; asked again, the second path is paid apart."""
        self.addCleanup(self.node.mine, 1, SINK)
        old = PathOnly(self.rpc, "regtest", os.path.join(self.tmp, "used-per-path.jsonl"), confirmations=1)
        gate, base = self.gate_at("gate-per-path", facilitator=old)
        a, b = base + "/v1/unit/a", base + "/v1/unit/b"
        made = len(self.buyer.payments())
        first = x402.fetch(a, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual(first["status"], 202)
        refused = x402.fetch(b, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((refused["status"], refused["settlement"]["errorReason"]), (402, "invalid_payment_requirements"))
        self.assertIn("paid apart", refused["note"])
        self.assertEqual(len(self.buyer.payments()), made + 1)                  # the one at a, nothing more
        kept = self.book()["pending"][a]
        self.assertEqual((kept["txid"], "named" in kept), (first["txid"], False))   # kept for its own url, not shared
        apart = x402.fetch(b, self.buyer, units("1"), network=self.network, wait=0)  # asked again: b is paid apart
        self.assertEqual((apart["status"], len(self.buyer.payments())), (202, made + 2))
        self.node.mine(1, SINK)
        got = x402.fetch(a, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["settlement"]["success"], got["txid"]), (True, first["txid"]))
        got = x402.fetch(b, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["settlement"]["success"], got["txid"]), (True, apart["txid"]))
        self.assertEqual([k for k in self.book()["pending"] if k.startswith(base)], [])
        self.assertEqual(len(self.buyer.payments()), made + 2)

    def test_a_late_answer_takes_no_newer_payment_from_the_book(self):
        """0.2.8 (review M4): a call that shows a waiting payment and is answered late takes from the book only that
        payment, not a newer one that another call of the same wallet put in its place meanwhile."""
        self.addCleanup(self.node.mine, 1, SINK)
        gate, base = self.gate_at("gate-late", calls=1)
        a, b, c = base + "/v1/unit/a", base + "/v1/unit/b", base + "/v1/unit/c"
        first = x402.fetch(a, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual(first["status"], 202)
        newer = []

        def meanwhile(seconds):
            if not newer:
                self.node.mine(1, SINK)
                self.assertEqual(x402.fetch(b, self.buyer, units("1"), network=self.network, wait=0)["status"], 200)
                newer.append(x402.fetch(a, self.buyer, units("1"), network=self.network, wait=0))
        late = x402.fetch(c, self.buyer, units("1"), network=self.network, wait=60, sleep=meanwhile)
        self.assertEqual(late["txid"], first["txid"])
        self.assertEqual(newer[0]["status"], 202)
        self.assertNotEqual(newer[0]["txid"], first["txid"])
        self.assertEqual(self.book()["pending"][a]["txid"], newer[0]["txid"])      # the newer payment is still there

    def test_a_waiting_payment_is_shared_only_within_the_calls_most(self):
        """0.2.8 (review L1): a call that may pay less than the waiting payment does not use it."""
        self.addCleanup(self.node.mine, 1, SINK)
        gate, base = self.gate_at("gate-most")
        a, b = base + "/v1/unit/a", base + "/v1/unit/b"
        self.assertEqual(x402.fetch(a, self.buyer, units("1"), network=self.network, wait=0)["status"], 202)
        made = len(self.buyer.payments())
        with self.assertRaises(Refused) as caught:
            x402.fetch(b, self.buyer, PRICE - 1, network=self.network, wait=0)
        self.assertEqual(caught.exception.kind, "X402_TOO_MUCH")
        self.assertEqual(len(self.buyer.payments()), made)
        self.assertIn(a, self.book()["pending"])

    def test_a_borrowed_payment_is_shown_with_the_resource_of_its_url(self):
        """0.2.8 (review L2): a waiting payment shown on another url of its seller carries that url's resource, as a
        seller that checks the payload's resource asks; the transaction and the claim are the same, and the book keeps
        the payment as it was made."""
        seen, network, pay_to = [], self.network, self.seller.account.address(0, 11)

        class Seller(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def answer(self, code, headers):
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                signed = self.headers.get("PAYMENT-SIGNATURE")
                if signed:
                    seen.append(x402.from_b64json(signed))
                    return self.answer(202, {"PAYMENT-RESPONSE": x402.b64json({"success": False, "errorReason": x402.AWAITING})})
                offer = x402.requirements(network, PRICE, pay_to, confirmations=1)
                offer["extra"]["resource"] = "one thing"
                needed = {"x402Version": 2, "resource": {"url": "http://seller.example" + self.path}, "accepts": [offer]}
                return self.answer(402, {"PAYMENT-REQUIRED": x402.b64json(needed)})
        base = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Seller))
        x402.fetch(base + "/one", self.buyer, units("1"), network=self.network, wait=0)
        x402.fetch(base + "/two", self.buyer, units("1"), network=self.network, wait=0)
        self.rpc.call("sendrawtransaction", seen[0]["payload"]["transaction"])     # so that the buyer's coin comes back
        self.addCleanup(self.node.mine, 1, SINK)
        self.assertEqual([s["resource"]["url"] for s in seen], ["http://seller.example/one", "http://seller.example/two"])
        self.assertEqual(len({(s["payload"]["transaction"], s["payload"]["claim"]) for s in seen}), 1)
        kept = x402.from_b64json(self.book()["pending"][base + "/one"]["header"])
        self.assertEqual(kept["resource"]["url"], "http://seller.example/one")

    def old_entries(self, prefix, keep_marks=False):
        """Makes the buyer's waiting payments under `prefix` look as a client before 0.2.8 wrote them."""
        with x402.BOOK(self.buyer):
            book = x402._book(self.buyer)
            for k, h in book["pending"].items():
                if k.startswith(prefix):
                    for f in ("url",) if keep_marks else ("url", "origin", "named", "payTo"):
                        h.pop(f, None)
            x402._keep_book(self.buyer, book)

    def test_a_letters_payment_from_before_0_2_8_goes_back_to_its_inbox(self):
        """0.2.8 (third reading of the review, R3-L1): a letter's payment written by an older client (no url in the
        book), refused for good at a second inbox, goes back under the url its seller named in the offer, and the
        first letter goes on it once it is in a block."""
        self.addCleanup(self.node.mine, 1, SINK)
        from polarka_agent import mail
        office = mail.PostOffice(os.path.join(self.tmp, "office-old"))
        paid, free = mail.servers(office, "127.0.0.1:0", "127.0.0.1:0", per_minute=600)
        free_url = self.start(free)
        office.origin = mail.origin_of(free_url)
        self.start(paid)
        old = PathOnly(self.rpc, "regtest", os.path.join(self.tmp, "used-path-old.jsonl"), confirmations=1)
        gate = postage.Gate(self.make("gate-mail-old"), PRICE, 3, x402=old)
        front = self.start(ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(
            gate, "127.0.0.1:1", False, [("/v1/mail", "127.0.0.1:%d" % paid.server_address[1])])))
        one, two = self.make("reader-old-one"), self.make("reader-old-two")
        to_one, to_two = mail.my_inbox(one)["inbox"], mail.my_inbox(two)["inbox"]
        made = len(self.buyer.payments())
        first = mail.send(self.buyer, to_one, "to the first", units("1"), front, wait=0, network=self.network)
        self.assertEqual((first["sent"], first["pending"]), (False, True))
        self.old_entries(front)
        with self.assertRaises(mail.MailError) as caught:
            mail.send(self.buyer, to_two, "to the second", units("1"), front, wait=0, network=self.network)
        self.assertIn("kept for what it was bought for", str(caught.exception))
        self.assertEqual([e for e in self.book().get("kept", []) if e.get("url", "").startswith(front)], [])
        self.node.mine(1, SINK)
        self.assertTrue(mail.send(self.buyer, to_one, "to the first", units("1"), front, wait=0, network=self.network)["sent"])
        self.assertEqual(len(self.buyer.payments()), made + 1)
        self.assertEqual([x["text"] for x in mail.fetch_letters(one, free_url)["letters"]], ["to the first"])

    def test_a_lender_without_a_url_goes_back_under_its_own_key(self):
        """0.2.8 (third reading of the review, R3-L2): a waiting payment with its marks but no url, refused where it was
        borrowed, goes back under its own key, and its own url goes on it once it is in a block."""
        self.addCleanup(self.node.mine, 1, SINK)
        old = PathOnly(self.rpc, "regtest", os.path.join(self.tmp, "used-per-path-old.jsonl"), confirmations=1)
        gate, base = self.gate_at("gate-lender-old", facilitator=old)
        a, b = base + "/v1/unit/a", base + "/v1/unit/b"
        made = len(self.buyer.payments())
        first = x402.fetch(a, self.buyer, units("1"), network=self.network, wait=0)
        self.old_entries(base, keep_marks=True)
        refused = x402.fetch(b, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((refused["status"], refused.get("kept")), (402, True))
        self.assertEqual(self.book()["pending"][a]["txid"], first["txid"])
        self.node.mine(1, SINK)
        got = x402.fetch(a, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["settlement"]["success"], got["txid"], len(self.buyer.payments())), (True, first["txid"], made + 1))

    def test_a_payment_kept_aside_comes_back_when_its_url_is_free(self):
        """0.2.8 (second reading of the review, R-L5): a payment kept aside because its own url held another one takes
        that place when it is free again, the oldest first; one whose url is not known stays aside."""
        book = {"pending": {}, "lots": {}, "kept": [{"txid": "a", "url": "u1"}, {"txid": "b"}, {"txid": "c", "url": "u1"}]}
        x402._unpark(book, "u2")
        self.assertEqual(book["pending"], {})
        x402._unpark(book, "u1")
        self.assertEqual((book["pending"]["u1"]["txid"], [e["txid"] for e in book["kept"]]), ("a", ["b", "c"]))
        x402._unpark(book, "u1")                                                # the place is taken again: nothing moves
        self.assertEqual([e["txid"] for e in book["kept"]], ["b", "c"])

    def test_a_gate_names_its_lot_the_same_on_every_path(self):
        gate = postage.Gate(self.make("gate-named"), PRICE, 3, x402=self.patient)
        base = self.start(ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(gate, "127.0.0.1:1", False)))
        names = {self.offer(base + p)[1]["extra"]["resource"] for p in ("/v1/unit/a", "/v1/stamp/b", "/v1/mail/c")}
        self.assertEqual(names, {"lot of postage at " + gate.state["x402_address"]})

    def test_letters_to_two_inboxes_while_the_lot_waits_for_a_block(self):
        """0.2.8: the second letter, to another inbox, while the lot's payment waits for its block, pays nothing
        more; both letters arrive once the payment is in a block."""
        from polarka_agent import mail
        office = mail.PostOffice(os.path.join(self.tmp, "office-two"))
        paid, free = mail.servers(office, "127.0.0.1:0", "127.0.0.1:0", per_minute=600)
        free_url = self.start(free)
        office.origin = mail.origin_of(free_url)
        self.start(paid)
        gate = postage.Gate(self.make("gate-mail-two"), PRICE, 3, x402=self.patient)
        front = self.start(ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(
            gate, "127.0.0.1:1", False, [("/v1/mail", "127.0.0.1:%d" % paid.server_address[1])])))
        one, two = self.make("reader-one"), self.make("reader-two")
        to_one, to_two = mail.my_inbox(one)["inbox"], mail.my_inbox(two)["inbox"]
        made = len(self.buyer.payments())
        first = mail.send(self.buyer, to_one, "to the first", units("1"), front, wait=0, network=self.network)
        self.assertEqual((first["sent"], first["pending"]), (False, True))
        second = mail.send(self.buyer, to_two, "to the second", units("1"), front, wait=0, network=self.network)
        self.assertEqual((second["sent"], second.get("pending")), (False, True))
        self.assertEqual(len(self.buyer.payments()), made + 1)
        self.node.mine(1, SINK)
        self.assertTrue(mail.send(self.buyer, to_two, "to the second", units("1"), front, wait=0, network=self.network)["sent"])
        self.assertTrue(mail.send(self.buyer, to_one, "to the first", units("1"), front, wait=0, network=self.network)["sent"])
        self.assertEqual(len(self.buyer.payments()), made + 1)
        self.assertEqual([x["text"] for x in mail.fetch_letters(one, free_url)["letters"]], ["to the first"])
        self.assertEqual([x["text"] for x in mail.fetch_letters(two, free_url)["letters"]], ["to the second"])

    def test_a_letter_refused_at_a_second_inbox_keeps_the_first_ones_payment(self):
        """0.2.8 (review M3): letters to two inboxes share one payment while it waits. Where the seller's facilitator
        binds it to the path of the first inbox, the second letter is refused, and the payment stays for the first
        letter, which it brings once it is in a block. Sent again at once, the second letter is paid apart, and the first
        still goes on its own payment (R-L4)."""
        self.addCleanup(self.node.mine, 1, SINK)
        from polarka_agent import mail
        office = mail.PostOffice(os.path.join(self.tmp, "office-path"))
        paid, free = mail.servers(office, "127.0.0.1:0", "127.0.0.1:0", per_minute=600)
        free_url = self.start(free)
        office.origin = mail.origin_of(free_url)
        self.start(paid)
        old = PathOnly(self.rpc, "regtest", os.path.join(self.tmp, "used-path-mail.jsonl"), confirmations=1)
        gate = postage.Gate(self.make("gate-mail-path"), PRICE, 3, x402=old)
        front = self.start(ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(
            gate, "127.0.0.1:1", False, [("/v1/mail", "127.0.0.1:%d" % paid.server_address[1])])))
        one, two = self.make("reader-path-one"), self.make("reader-path-two")
        to_one, to_two = mail.my_inbox(one)["inbox"], mail.my_inbox(two)["inbox"]
        made = len(self.buyer.payments())
        first = mail.send(self.buyer, to_one, "to the first", units("1"), front, wait=0, network=self.network)
        self.assertEqual((first["sent"], first["pending"]), (False, True))
        waiting = [h["txid"] for k, h in self.book()["pending"].items() if k.startswith(front)]
        self.assertEqual(len(waiting), 1)
        with self.assertRaises(mail.MailError):
            mail.send(self.buyer, to_two, "to the second", units("1"), front, wait=0, network=self.network)
        self.assertEqual([h["txid"] for k, h in self.book()["pending"].items() if k.startswith(front)], waiting)
        self.assertEqual(len(self.buyer.payments()), made + 1)
        # sent again at once, the second letter is paid apart under the shared key; the first letter, sent again,
        # still finds its own payment first (R-L4)
        again = mail.send(self.buyer, to_two, "to the second", units("1"), front, wait=0, network=self.network)
        self.assertEqual((again["sent"], again["pending"], len(self.buyer.payments())), (False, True, made + 2))
        still = mail.send(self.buyer, to_one, "to the first", units("1"), front, wait=0, network=self.network)
        self.assertEqual((still["sent"], still["pending"], len(self.buyer.payments())), (False, True, made + 2))
        self.node.mine(1, SINK)
        self.assertTrue(mail.send(self.buyer, to_one, "to the first", units("1"), front, wait=0, network=self.network)["sent"])
        self.assertTrue(mail.send(self.buyer, to_two, "to the second", units("1"), front, wait=0, network=self.network)["sent"])
        self.assertEqual(len(self.buyer.payments()), made + 2)
        self.assertEqual([x["text"] for x in mail.fetch_letters(one, free_url)["letters"]], ["to the first"])
        self.assertEqual([x["text"] for x in mail.fetch_letters(two, free_url)["letters"]], ["to the second"])

    def test_what_is_paid_for_through_the_gate_can_be_found(self):
        from polarka_agent import bazaar

        class Up(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "7")
                self.end_headers()
                self.wfile.write(b"stamped")
        up = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Up))
        catalog = bazaar.Catalog(os.path.join(self.tmp, "bazaar.json"), ["https://gate.example.org"])
        keeper = x402.Facilitator(self.rpc, "regtest", os.path.join(self.tmp, "used-bazaar.jsonl"), confirmations=1, catalog=catalog)
        self.assertEqual(keeper.supported()["extensions"], ["bazaar"])
        base = self.start(x402.facilitator_server(keeper))
        remote = x402.RemoteFacilitator(base)
        offers = [{"prefix": "/v1/stamp", "description": "Stamps a SHA-256", "route": "/v1/stamp/:sha256",
                   "pathParams": {"sha256": "ab" * 32}, "output": {"state": "stamped"}, "serviceName": "Polarka stamps"}]
        gate = postage.Gate(self.make("gate-bazaar"), PRICE, 3, x402=remote, public_url="https://gate.example.org", offers=offers)
        url = self.start(ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(gate, up.split("//")[1], False))) + "/v1/stamp/" + "ab" * 32
        needed, _ = self.offer(url)
        self.assertEqual(needed["extensions"]["bazaar"]["routeTemplate"], "/v1/stamp/:sha256")
        self.assertEqual(x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)["status"], 202)
        self.assertEqual(json.loads(urllib.request.urlopen(base + "/discovery/resources", timeout=10).read())["items"], [])
        self.node.mine(1, SINK)
        got = x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual((got["status"], got["body"]), (200, b"stamped"))
        listed = json.loads(urllib.request.urlopen(base + "/discovery/resources?type=http", timeout=10).read())
        self.assertEqual(listed["pagination"]["total"], 1)
        item = listed["items"][0]
        self.assertEqual((item["resource"], item["serviceName"]), ("https://gate.example.org/v1/stamp/:sha256", "Polarka stamps"))
        self.assertEqual(item["accepts"][0]["payTo"], gate.state["x402_address"])
        found = json.loads(urllib.request.urlopen(base + "/discovery/search?query=stamps", timeout=10).read())
        self.assertEqual(len(found["resources"]), 1)
        try:
            urllib.request.urlopen(base + "/discovery/search", timeout=10)
            self.fail("a search without a query")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_a_cheap_payment_does_not_buy_a_dear_thing(self):
        pay_to = self.seller.account.address(0, 3)
        cheap = self.start(x402.paywall(self.patient, PRICE, pay_to, b"cheap")) + "/cheap"
        dear = self.start(x402.paywall(self.patient, 2 * PRICE, pay_to, b"dear")) + "/dear"
        self.assertEqual(x402.fetch(cheap, self.buyer, units("1"), network=self.network, wait=0)["status"], 202)
        header = self.book()["pending"][cheap]["header"]
        self.node.mine(1, SINK)
        status, headers, _ = self.ask(url=dear, header=header)
        self.assertEqual((status, x402.from_b64json(headers.get("PAYMENT-RESPONSE"))["errorReason"]), (402, "invalid_payment_requirements"))
        status, _, body = self.ask(url=cheap, header=header)
        self.assertEqual((status, body), (200, b"cheap"))

    def test_one_payment_buys_one_resource(self):
        pay_to = self.seller.account.address(0, 6)
        one = self.start(x402.paywall(self.patient, PRICE, pay_to, b"one")) + "/one"
        two = self.start(x402.paywall(self.patient, PRICE, pay_to, b"two")) + "/two"
        self.assertEqual(x402.fetch(one, self.buyer, units("1"), network=self.network, wait=0)["status"], 202)
        payment = x402.from_b64json(self.book()["pending"][one]["header"])
        self.node.mine(1, SINK)
        status, _, _ = self.ask(dict(payment, resource={"url": "/two"}), url=two)
        self.assertEqual(status, 402)
        self.assertEqual(self.ask(url=two, header=self.book()["pending"][one]["header"])[0], 402)
        got = x402.fetch(one, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual(got["body"], b"one")
        # the seller names the resource: the facilitator does not take the payment for another one, whatever the buyer wrote
        wanted = dict(payment["accepted"], extra=dict(payment["accepted"].get("extra") or {}, resource="/two"))
        self.assertEqual(self.patient.settle(payment, wanted)["errorReason"], "invalid_payment_requirements")
        again = self.patient.settle(payment, dict(wanted, extra=dict(wanted["extra"], resource="/one")))
        self.assertEqual((again["success"], again.get("redelivered")), (True, True))

    def test_a_payment_the_node_already_has_is_not_taken(self):
        _, offer = self.offer(self.deep_url)
        made = self.buyer.pay([(offer["payTo"], PRICE)], "paid straight, not over x402")
        self.node.mine(1, SINK)
        raw = self.node.cli("getrawtransaction", made["id"], "false", self.node.cli("getbestblockhash"))
        status, headers, _ = self.ask({"x402Version": 2, "accepted": offer, "payload": {"transaction": raw, "claim": "cd" * 16}},
                                      url=self.deep_url)
        self.assertEqual((status, x402.from_b64json(headers.get("PAYMENT-RESPONSE"))["errorReason"]), (402, "transaction_refused_by_node"))

    def test_the_seller_may_ask_for_more_blocks(self):
        wanted = x402.requirements(self.network, PRICE, self.seller.account.address(0, 4), confirmations=2)
        made = self.buyer.pay([(wanted["payTo"], PRICE)], "x402 test, two blocks", hand=False)
        payment = {"x402Version": 2, "accepted": wanted, "payload": {"transaction": made["raw"], "claim": "ef" * 16}}
        self.assertEqual(self.patient.settle(payment, wanted)["required"], 2)
        self.node.mine(1, SINK)
        self.assertEqual(self.patient.settle(payment, wanted)["errorReason"], "awaiting_confirmation")
        self.node.mine(1, SINK)
        self.assertTrue(self.patient.settle(payment, wanted)["success"])

    def test_no_redirect_and_a_failing_facilitator_keeps_the_payment(self):
        class Away(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
        url = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Away)) + "/x"
        self.assertEqual(x402.fetch(url, self.buyer, units("1"), network=self.network, wait=0)["status"], 302)
        with self.assertRaises(Refused):
            x402.fetch("http://localhost.example.org/x", self.buyer, units("1"), network=self.network)
        service = x402.facilitator_server(self.patient)
        remote = x402.RemoteFacilitator(self.start(service))
        wall = self.start(x402.paywall(remote, PRICE, self.seller.account.address(0, 5), b"later")) + "/later"
        service.shutdown()
        service.server_close()
        got = x402.fetch(wall, self.buyer, units("1"), network=self.network, wait=0)
        self.assertEqual(got["status"], 503)
        self.assertIn(wall, self.book()["pending"])

    def test_two_calls_at_the_same_moment_pay_once(self):
        """L3 and R-L2 (review of 0.2.8, from 0.2.4): two calls of one wallet to one gate at the same moment, neither
        with a lot, each had their 402 and each paid a lot; under one key the later payment took the earlier one's place
        and its claim. Now the one whose turn to pay comes second finds the first one's payment and shows it."""
        class Up(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                said = self.path.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(said)))
                self.end_headers()
                self.wfile.write(said)
        up = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Up))
        for name, paths in (("gate-same-url", ("/v1/unit/a", "/v1/unit/a")), ("gate-two-urls", ("/v1/unit/a", "/v1/unit/b"))):
            gate = postage.Gate(self.make(name), PRICE, 3, x402=self.patient)
            plain = postage.make_handler(gate, up.split("//")[1], False)
            both, holding = threading.Barrier(2, timeout=30), [True]

            class Held(plain):
                def held(self):
                    # the gate answers 402 to neither until both have asked: each call learns the price before either pays
                    if holding[0] and not self.headers.get("PAYMENT-SIGNATURE") and not self.headers.get("X-Postage"):
                        try:
                            both.wait()
                        except threading.BrokenBarrierError:
                            pass
                    return plain.handle_one(self)
                do_GET = held
            base = self.start(ThreadingHTTPServer(("127.0.0.1", 0), Held))
            made, got = len(self.buyer.payments()), []
            calls = [threading.Thread(target=lambda p=p: got.append(x402.fetch(
                base + p, self.buyer, units("1"), network=self.network, wait=0))) for p in paths]
            for t in calls:
                t.start()
            for t in calls:
                t.join(60)
            holding[0] = False
            self.assertEqual(len(self.buyer.payments()), made + 1, name)               # one payment, not two
            self.assertEqual([g["status"] for g in got], [202, 202], name)             # and both calls came back
            self.assertEqual(len({g["txid"] for g in got}), 1, name)                   # with the same payment
            self.node.mine(1, SINK)
            for p in paths:
                done = x402.fetch(base + p, self.buyer, units("1"), network=self.network, wait=0)
                self.assertEqual((done["status"], done["body"]), (200, p.encode()), name)
            self.assertEqual(len(self.buyer.payments()), made + 1, name)
            self.assertEqual([k for k in self.book()["pending"] if k.startswith(base)], [], name)

    # the tests of the seller that takes a payment at once are not repeated here
    test_paid_once_and_only_once = test_too_little_and_requirements_changed_on_the_way = None
    test_from_the_command_line_and_as_a_tool = test_what_the_buyer_does_not_pay = None


class PayingApartFromTheBook(unittest.TestCase):
    """L6 (review of the USDC): the book is held only while it is read or written. A payment under way holds a lock of
    its own, so that a call of the wallet that uses a lot or a payment made before does not wait for it, while two
    payments still come one after the other."""

    def test_a_payment_under_way_keeps_no_one_from_the_book_and_the_next_payment_waits(self):
        with tempfile.TemporaryDirectory() as d:
            wallet = type("Folder", (), {"folder": d})()
            paying, release, second, waited = threading.Event(), threading.Event(), threading.Event(), []

            def pay():
                with x402.PAYING(wallet):
                    paying.set()
                    release.wait(10)

            def pay_too():
                with x402.PAYING(wallet):
                    second.set()
            first = threading.Thread(target=pay)
            first.start()
            self.assertTrue(paying.wait(5))
            t0 = time.time()
            with x402.BOOK(wallet):
                waited.append(time.time() - t0)
            other = threading.Thread(target=pay_too)
            other.start()
            self.assertFalse(second.wait(0.5))      # one payment at a time
            release.set()
            first.join(10)
            self.assertTrue(second.wait(5))
            other.join(10)
            self.assertLess(waited[0], 1.0)
            self.assertNotEqual(x402.BOOK(wallet).path, x402.PAYING(wallet).path)     # and between processes too


if __name__ == "__main__":
    unittest.main()
