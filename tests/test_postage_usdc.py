"""USDC at the gate: one payment for one call on the paths that take it, settled by the facilitator once; shown
again for the same call it passes again a few times within ten minutes and is never paid twice, for another call it
is refused; a payment whose block has not come waits and is then matched with its transfer. Against a stand-in of
the facilitator, without a chain."""
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
from polarka_agent import keys, postage, x402
from polarka_agent.wallet import Refused, Wallet
from tests.test_postage import Behind
from tests.test_wallet_rules import Model, WORDS

N = keys.UNIT
PAYER = "0x" + "11" * 20
TO = "0x0000000000000000000000000000000000c0ffee"
TX = "0x" + "ab" * 32


class Evm:
    """A stand-in for x402.EvmFacilitator: it settles what it is given, answers as told, and counts."""
    network, asset = "eip155:8453", x402.USDC["eip155:8453"][0]

    def __init__(self, answers=()):
        self.calls, self.answers, self.hold = [], list(answers), None

    def wanted(self, amount, pay_to, timeout=300):
        return {"scheme": "exact", "network": self.network, "amount": str(amount), "asset": self.asset, "payTo": pay_to,
                "maxTimeoutSeconds": timeout, "extra": {"name": "USD Coin", "version": "2"}}

    def settle(self, payment, wanted):
        self.calls.append((payment, wanted))
        if self.hold is not None:
            self.hold.wait(10)
        if self.answers:
            return self.answers.pop(0)
        return {"success": True, "transaction": TX, "network": self.network, "payer": PAYER}


def paid(n=1, network="eip155:8453", payer=PAYER):
    return {"x402Version": 2,
            "accepted": {"scheme": "exact", "network": network, "amount": "10000", "asset": Evm.asset, "payTo": TO,
                         "maxTimeoutSeconds": 300},
            "payload": {"signature": "0x" + "22" * 65,
                        "authorization": {"from": payer, "to": TO, "value": "10000", "validAfter": "0",
                                          "validBefore": "9999999999", "nonce": "0x%064x" % n}}}


class UsdcGateTest(unittest.TestCase):
    def setUp(self):
        self.m = Model()
        self.now = [1_800_000_000]
        self.folder = os.path.join(tempfile.mkdtemp(prefix="polarka-usdc-"), "w")
        Wallet.create(self.folder, "regtest", self.m.fake.url, WORDS)
        self.w = Wallet(self.folder, clock=lambda: self.now[0])
        self.evm = Evm()
        self.g = self.gate()

    def gate(self):
        return postage.Gate(self.w, N // 100, 100, clock=lambda: self.now[0], look_every=0,
                            usdc=postage.Usdc(self.evm, TO, 10000, ["/v1/stamp"]))

    def tearDown(self):
        self.w.server.close()
        self.m.fake.stop()

    def test_a_payment_passes_one_call_and_is_settled_with_the_gates_own_terms(self):
        said, may = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "1.2.3.4")
        self.assertEqual((may, said["success"], said["transaction"]), (True, True, TX))
        payment, wanted = self.evm.calls[0]
        self.assertEqual((wanted["amount"], wanted["payTo"], wanted["asset"]), ("10000", TO, Evm.asset))
        self.assertEqual(payment["resource"]["url"], "/v1/stamp/aa")     # what it buys is what the gate says

    def test_shown_again_for_the_same_call_it_passes_a_few_times_and_is_never_paid_again(self):
        for _ in range(postage.USDC_AGAIN_MOST):
            self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])
        said, may = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["errorReason"]), (False, "payment_already_used"))
        self.assertEqual(len(self.evm.calls), 1)

    def test_each_payment_settled_is_written_down_once_without_its_path(self):
        self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])
        self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])        # shown again: not a new payment
        with open(os.path.join(self.folder, "usdc-paid.jsonl")) as f:
            got = [json.loads(line) for line in f]
        self.assertEqual(got, [{"t": self.now[0], "tx": TX, "payer": PAYER, "amount": "10000", "network": "eip155:8453",
                                "for": "/v1/stamp"}])
        self.assertEqual(os.stat(os.path.join(self.folder, "usdc-paid.jsonl")).st_mode & 0o077, 0)

    def test_shown_for_another_call_or_late_it_is_refused(self):
        self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/bb", "s")[0]["errorReason"], "payment_is_for_another_call")
        self.assertEqual(self.g.usdc_pay(paid(), "POST", "/v1/stamp/aa", "s")[0]["errorReason"], "payment_is_for_another_call")
        self.now[0] += postage.USDC_AGAIN_FOR + 1
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "payment_already_used")
        self.assertEqual(len(self.evm.calls), 1)

    def test_only_on_the_paths_that_take_it_and_only_a_payment_that_can_be_read(self):
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/mail/x", "s")[0]["errorReason"], "usdc_is_not_taken_here")
        broken = paid()
        del broken["payload"]["authorization"]
        self.assertEqual(self.g.usdc_pay(broken, "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "invalid_payload")
        odd = paid()
        odd["payload"]["authorization"]["nonce"] = "0x12"
        self.assertEqual(self.g.usdc_pay(odd, "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "invalid_payload")
        self.assertEqual(self.evm.calls, [])

    def test_a_payment_whose_block_has_not_come_waits_and_is_then_matched(self):
        self.evm.answers = [{"success": False, "errorReason": "settlement_pending", "transaction": TX, "errorMessage": "late"}]
        said, may = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["errorReason"], said["transaction"]), (False, x402.SETTLING, TX))
        self.assertNotIn("errorMessage", said)
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/bb", "s")[0]["errorReason"], "payment_is_for_another_call")
        restarted = self.gate()                 # the gate's book keeps it over a restart
        said, may = restarted.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["transaction"]), (True, TX))
        self.assertEqual(len(self.evm.calls), 2)    # the second time the facilitator matched it with the transfer it sent

    def test_what_the_facilitator_says_goes_out_only_as_a_short_reason(self):
        self.evm.answers = [{"success": False, "errorReason": "invalid_exact_evm_insufficient_balance", "errorMessage": "0x08c3..."},
                            {"success": False, "errorReason": "Some <b>thing</b> odd"}]
        said = self.g.usdc_pay(paid(1), "GET", "/v1/stamp/aa", "s")[0]
        self.assertEqual(sorted(said), ["errorReason", "network", "payer", "success", "transaction"])
        self.assertEqual(said["errorReason"], "invalid_exact_evm_insufficient_balance")
        self.assertEqual(self.g.usdc_pay(paid(2), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "unexpected_settle_error")

    def test_new_payments_from_one_place_are_limited_as_in_north(self):
        for n in range(postage.X402_PER_MINUTE):
            self.assertTrue(self.g.usdc_pay(paid(n + 1), "GET", "/v1/stamp/%02x" % n, "9.9.9.9")[1])
        self.assertEqual(self.g.usdc_pay(paid(99), "GET", "/v1/stamp/ff", "9.9.9.9")[0]["errorReason"], postage.TOO_MANY)
        self.assertTrue(self.g.usdc_pay(paid(1), "GET", "/v1/stamp/00", "9.9.9.9")[1])    # one already paid is no new payment

    def test_the_same_payment_twice_at_once_is_settled_once(self):
        self.evm.hold = threading.Event()
        got = []
        t = threading.Thread(target=lambda: got.append(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")))
        t.start()
        while not self.evm.calls:
            pass
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], postage.SETTLING_NOW)
        self.evm.hold.set()
        t.join(10)
        self.assertTrue(got[0][1])
        self.assertEqual(len(self.evm.calls), 1)

    def serve(self, gate):
        behind = ThreadingHTTPServer(("127.0.0.1", 0), Behind)
        front = ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(gate, "127.0.0.1:%d" % behind.server_address[1], False))
        for s in (behind, front):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
        url = "http://127.0.0.1:%d" % front.server_address[1]

        def ask(path, payment=None):
            req = urllib.request.Request(url + path, None, {"PAYMENT-SIGNATURE": x402.b64json(payment)} if payment else {})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, dict(r.headers), json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), json.loads(e.read())
        return ask

    def test_over_http_the_402_offers_usdc_where_it_is_taken_and_a_payment_passes_one_call(self):
        ask = self.serve(self.g)
        status, headers, said = ask("/v1/stamp/aa")
        self.assertEqual(status, 402)
        needed = x402.from_b64json(headers["PAYMENT-REQUIRED"])
        self.assertEqual([(a["network"], a["amount"], a["payTo"]) for a in needed["accepts"]], [("eip155:8453", "10000", TO)])
        self.assertEqual((said["usdc"]["price"], said["usdc"]["paths"], said["usdc"]["for_calls"]), ("0.01", ["/v1/stamp"], 1))
        status, headers, _ = ask("/v1/mail/x")
        self.assertEqual(status, 402)
        self.assertNotIn("PAYMENT-REQUIRED", headers)       # nothing there is paid over x402 at this gate
        status, headers, said = ask("/v1/stamp/aa", paid())
        self.assertEqual((status, said["served"]), (200, "/v1/stamp/aa"))
        self.assertEqual(x402.from_b64json(headers["PAYMENT-RESPONSE"])["transaction"], TX)
        self.assertNotIn("X-Postage-Calls-Left", headers)
        status, headers, said = ask("/v1/stamp/bb", paid())
        self.assertEqual(status, 402)
        self.assertEqual(x402.from_b64json(headers["PAYMENT-RESPONSE"])["errorReason"], "payment_is_for_another_call")
        self.assertIn("PAYMENT-REQUIRED", headers)
        north = paid(2, network=x402.NETWORKS["main"])      # a payment in NORTH, at a gate that takes only USDC
        self.assertEqual(x402.from_b64json(ask("/v1/stamp/cc", north)[1]["PAYMENT-RESPONSE"])["errorReason"], "unsupported_network")

    def test_over_http_a_payment_whose_block_has_not_come_is_answered_202(self):
        self.evm.answers = [{"success": False, "errorReason": "settlement_pending", "transaction": TX}]
        ask = self.serve(self.g)
        status, headers, said = ask("/v1/stamp/aa", paid())
        self.assertEqual((status, headers.get("Retry-After")), (202, "5"))
        self.assertEqual(ask("/v1/stamp/aa", paid())[0], 200)

    def test_with_north_too_north_comes_first_and_alone_where_usdc_is_not_taken(self):
        class North:
            network, chain = x402.NETWORKS["main"], None

            def needed(self, amount):
                return 1
        g = postage.Gate(self.w, N // 100, 100, clock=lambda: self.now[0], look_every=0, x402=North(),
                         usdc=postage.Usdc(self.evm, TO, 10000, ["/v1/stamp"]))
        self.assertEqual([a["asset"] for a in g.x402_needed("/v1/stamp/aa")["accepts"]], ["NORTH", Evm.asset])
        self.assertEqual([a["asset"] for a in g.x402_needed("/v1/mail/x")["accepts"]], ["NORTH"])

    def test_prices_and_addresses(self):
        self.assertEqual([postage.usdc_units(t) for t in ("0.01", "1", "0.000001", "0.5")], [10000, 1000000, 1, 500000])
        for bad in ("", "0.0000001", "1e-2", "-1", "0,01", "abc", None):
            with self.assertRaises(Refused):
                postage.usdc_units(bad)
        for args in (("0x12", 10000, ["/v1/stamp"]), (TO, 0, ["/v1/stamp"]), (TO, 2_000_000, ["/v1/stamp"]), (TO, 10000, []),
                     (TO, 10000, ["v1/stamp"])):
            with self.assertRaises(Refused):
                postage.Usdc(self.evm, *args)
        self.assertEqual(postage.Usdc(self.evm, TO, 10000, ["/v1/stamp"]).shown(), "0.01")
        self.assertEqual(postage.Usdc(self.evm, TO, 1_000_000, ["/v1/stamp"]).shown(), "1")


class EvmFacilitatorTest(unittest.TestCase):
    def facilitator(self, kinds):
        class F(BaseHTTPRequestHandler):
            def answer(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.answer({"kinds": kinds})

            def do_POST(self):
                got = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.answer({"success": True, "transaction": TX, "network": got["paymentRequirements"]["network"], "seen": got})

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), F)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def test_it_takes_a_facilitator_that_settles_usdc_on_the_network_and_no_other(self):
        url = self.facilitator([{"x402Version": 2, "scheme": "exact", "network": "eip155:8453"}])
        f = x402.EvmFacilitator(url, "eip155:8453")
        self.assertEqual(f.wanted(10000, TO), {"scheme": "exact", "network": "eip155:8453", "amount": "10000",
                                               "asset": x402.USDC["eip155:8453"][0], "payTo": TO, "maxTimeoutSeconds": 300,
                                               "extra": {"name": "USD Coin", "version": "2"}})
        got = f.settle(paid(), f.wanted(10000, TO))
        self.assertEqual((got["seen"]["x402Version"], got["seen"]["paymentRequirements"]["payTo"]), (2, TO))
        with self.assertRaises(Refused):
            x402.EvmFacilitator(url, "eip155:84532")                    # it does not settle there
        with self.assertRaises(Refused):
            x402.EvmFacilitator(url, "eip155:1")                        # USDC is not known there here
        with self.assertRaises(Refused):
            x402.EvmFacilitator("http://example.com:8402", "eip155:8453")   # https, or this machine


if __name__ == "__main__":
    unittest.main()
