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
            "payload": {"signature": "0x" + "22" * 64 + "1b",
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
        self.poll, postage.USDC_POLL = postage.USDC_POLL, (0, 0)
        self.g = self.gate()

    def gate(self):
        return postage.Gate(self.w, N // 100, 100, clock=lambda: self.now[0], look_every=0,
                            usdc=postage.Usdc(self.evm, TO, 10000, ["/v1/stamp"]))

    def tearDown(self):
        postage.USDC_POLL = self.poll
        self.w.server.close()
        self.m.fake.stop()

    def test_a_payment_passes_one_call_and_is_settled_with_the_gates_own_terms(self):
        said, may, _ = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "1.2.3.4")
        self.assertEqual((may, said["success"], said["transaction"]), (True, True, TX))
        payment, wanted = self.evm.calls[0]
        self.assertEqual((wanted["amount"], wanted["payTo"], wanted["asset"]), ("10000", TO, Evm.asset))
        self.assertEqual(payment["resource"]["url"], "/v1/stamp/aa")     # what it buys is what the gate says

    def test_shown_again_for_the_same_call_it_passes_a_few_times_and_is_never_paid_again(self):
        for _ in range(postage.USDC_AGAIN_MOST):
            self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])
        said, may, _ = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
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

    def test_another_authorization_under_the_same_payer_and_nonce_changes_nothing(self):
        """Round 2 of the review: a payment bound to its call is the payment itself. Another authorization of the same
        payer and nonce (rebuilt from the chain, or signed anew), shown for the same call, is refused at the gate: it
        does not pass as the one paid, the facilitator is not asked, and what is bound stays as it was."""
        def other():
            p = paid()
            p["payload"]["signature"] = "0x" + "33" * 64 + "1c"
            return p
        self.evm.answers = [{"success": False, "errorReason": "unexpected_settle_error"}]
        self.assertFalse(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])         # outcome not known yet
        self.now[0] += postage.USDC_ASK_EVERY
        said, may, _ = self.g.usdc_pay(other(), "GET", "/v1/stamp/aa", "t")
        self.assertEqual((may, said["errorReason"], len(self.evm.calls)), (False, "authorization_conflict", 1))
        self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])           # the one bound is settled
        self.assertEqual(len(self.evm.calls), 2)
        self.assertEqual(self.g.usdc_pay(other(), "GET", "/v1/stamp/aa", "t")[0]["errorReason"], "authorization_conflict")
        self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])           # and passes again as before
        self.assertEqual(len(self.evm.calls), 2)

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
        late = {"success": False, "errorReason": "settlement_pending", "transaction": TX, "errorMessage": "late"}
        self.evm.answers = [dict(late) for _ in postage.USDC_POLL] + [dict(late)]
        said, may, _ = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["errorReason"], said["transaction"]), (False, x402.SETTLING, TX))
        self.assertNotIn("errorMessage", said)
        # asked again within the request, as long as the facilitator does not say that the chain holds the transfer
        self.assertEqual(len(self.evm.calls), 1 + len(postage.USDC_POLL))
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/bb", "s")[0]["errorReason"], "payment_is_for_another_call")
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], x402.SETTLING)
        self.assertEqual(len(self.evm.calls), 1 + len(postage.USDC_POLL))    # asked again at once: from the gate's record (L2)
        restarted = self.gate()                 # the gate's book keeps it over a restart
        self.now[0] += postage.USDC_ASK_EVERY
        said, may, _ = restarted.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["transaction"]), (True, TX))
        self.assertEqual(len(self.evm.calls), 2 + len(postage.USDC_POLL))    # the facilitator matched it with its transfer

    def test_bound_to_its_call_before_the_facilitator_answers(self):
        """L1: the facilitator did not answer (or not in time): the payment stays bound to its call, nobody can take
        it for another, and shown again it is asked once more, at most every USDC_ASK_EVERY seconds."""
        self.evm.answers = [{"success": False, "errorReason": "unexpected_settle_error", "transaction": "", "network": "x"}]
        said, may, _ = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["errorReason"]), (False, "unexpected_settle_error"))
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/bb", "t")[0]["errorReason"], "payment_is_for_another_call")
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], postage.SETTLING_NOW)
        self.assertEqual(len(self.evm.calls), 1)
        self.now[0] += postage.USDC_ASK_EVERY
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["success"], True)   # the same answer now
        self.assertEqual(len(self.evm.calls), 2)

    def test_what_never_went_to_the_chain_binds_nothing_and_a_failure_stays_failed(self):
        self.evm.answers = [{"success": False, "errorReason": "invalid_exact_evm_insufficient_balance", "sent": False},
                            {"success": False, "errorReason": "transaction_failed", "transaction": TX, "sent": True}]
        self.assertEqual(self.g.usdc_pay(paid(1), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "invalid_exact_evm_insufficient_balance")
        self.assertNotIn(postage.mark(PAYER.lower() + ":" + "0x%064x" % 1), self.g.state["usdc"])
        self.assertEqual(self.g.usdc_pay(paid(2), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "transaction_failed")
        self.now[0] += postage.USDC_ASK_EVERY
        self.assertEqual(self.g.usdc_pay(paid(2), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "transaction_failed")
        self.assertEqual(len(self.evm.calls), 2)        # a payment that failed in the chain is not asked about again

    def test_a_transfer_the_chain_holds_lets_the_call_pass_before_its_block(self):
        """M2: standard buyers do not come back with the same payment after a 202; a transfer the chain holds passes."""
        self.evm.answers = [{"success": False, "errorReason": "settlement_pending", "transaction": TX, "known": False, "sent": True},
                            {"success": False, "errorReason": "settlement_pending", "transaction": TX, "known": True, "sent": True}]
        said, may, _ = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["success"], said["transaction"]), (True, True, TX))
        self.assertEqual(len(self.evm.calls), 2)        # asked again within the request while the chain did not hold it
        self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])      # and shown again it passes again

    def test_asking_again_counts_against_the_place(self):
        """L2: a payment shown again while its outcome is not known goes to the facilitator within the bound of the place."""
        self.evm.answers = [{"success": False, "errorReason": "unexpected_settle_error"}] * (postage.X402_PER_MINUTE + 5)
        for n in range(postage.X402_PER_MINUTE):
            self.now[0] += postage.USDC_ASK_EVERY
            self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "9.9.9.9")
        self.now[0] += postage.USDC_ASK_EVERY
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "9.9.9.9")[0]["errorReason"], postage.TOO_MANY)

    def test_a_call_the_service_could_not_take_does_not_count(self):
        """N6: 429, 503 or 507 from behind gives the showing back."""
        said, may, key = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.g.usdc_give_back(key)
        for _ in range(postage.USDC_AGAIN_MOST):
            self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "payment_already_used")

    def test_paths_that_name_more_than_one_thing_are_not_paid_in_usdc(self):
        """N10."""
        for path in ("/v1/stamp/../mail/x", "/v1/stamp/%2e%2e/x", "/v1/stamp//x", "/v1/stamp/./x", "/v1/stamp/x/..",
                     "/v1/stamp\\x"):
            self.assertEqual(self.g.usdc_pay(paid(), "GET", path, "s")[0]["errorReason"], "usdc_is_not_taken_here", path)
        self.assertFalse(postage.Gate(self.w, N // 100, 100, looks=["/v1/stamp"]).looks_at("/v1/stamp/../x"))

    def test_the_gate_does_not_start_with_a_facilitator_that_settles_something_else(self):
        """N5."""
        class Says(Evm):
            def __init__(self, **h):
                super().__init__()
                self.said = dict({"protocol": postage.USDC_PROTOCOL, "network": "eip155:8453", "asset": Evm.asset, "payTo": TO,
                                  "maxAmount": "1000000", "confirmTimeout": 15, "sendWithin": 20}, **h)
                self.timeout = 45

            def health(self):
                return self.said
        self.assertTrue(postage.Usdc(Says(), TO, 10000, ["/v1/stamp"]).ready)
        for wrong in ({"payTo": PAYER}, {"network": "eip155:84532"}, {"asset": PAYER}, {"maxAmount": "5000"},
                      {"confirmTimeout": 40}, {"confirmTimeout": None}, {"sendWithin": 30}, {"protocol": "polarka-usdc/1"},
                      {"protocol": None}):
            with self.assertRaises(Refused):
                postage.Usdc(Says(**wrong), TO, 10000, ["/v1/stamp"])

    def test_the_gate_starts_without_usdc_while_its_facilitator_cannot_be_reached(self):
        """Second reading, F4: a facilitator not up yet (a restart, a reboot) does not keep the gate down, NORTH with
        it; USDC is neither offered nor taken until the facilitator says it settles what the gate asks for."""
        class Away(Evm):
            up = False
            timeout = 45

            def health(self):
                if not self.up:
                    raise Refused("X402", "the facilitator cannot be reached (URLError)")
                return {"protocol": postage.USDC_PROTOCOL, "network": "eip155:8453", "asset": Evm.asset, "payTo": TO,
                        "maxAmount": "1000000", "confirmTimeout": 15, "sendWithin": 20}
        away = Away()
        u = postage.Usdc(away, TO, 10000, ["/v1/stamp"])
        g = postage.Gate(self.w, N // 100, 100, clock=lambda: self.now[0], look_every=0, usdc=u)
        self.assertFalse(u.ready)
        self.assertEqual([a["network"] for a in g.x402_needed("/v1/stamp/aa")["accepts"]], [])
        said, may, _ = g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["errorReason"], g.state["usdc"], away.calls), (False, "usdc_not_available", {}, []))
        away.up = True
        self.assertTrue(u.check(starting=False))
        self.assertEqual([a["network"] for a in g.x402_needed("/v1/stamp/aa")["accepts"]], ["eip155:8453"])
        self.assertTrue(g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])

    def test_what_can_never_be_paid_is_neither_bound_nor_sent_on(self):
        """Second reading, F2: a payload the facilitator would refuse for its shape is refused at the gate."""
        def bent(**change):
            p = paid()
            for k, v in change.items():
                if k == "signature":
                    p["payload"]["signature"] = v
                elif k == "extra":
                    p["payload"]["anything"] = v
                else:
                    p["payload"]["authorization"][k] = v
            return p
        for p in (bent(signature="0x" + "22" * 65), bent(signature="0x" + "22" * 64), bent(signature="0X" + "22" * 64 + "1b"),
                  bent(to=PAYER), bent(value="20000"), bent(value="1e4"), bent(validBefore="9" * 31), bent(extra="x" * 1000),
                  bent(nonce="0x" + "ab" * 31)):
            said, may, _ = self.g.usdc_pay(p, "GET", "/v1/stamp/aa", "s")
            self.assertEqual((may, said["errorReason"]), (False, "invalid_payload"), p["payload"])
        odd = paid()
        odd["accepted"] = {"x": "y" * 3000}
        self.assertEqual(self.g.usdc_pay(odd, "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "invalid_payload")
        self.assertEqual((self.g.state["usdc"], self.evm.calls), ({}, []))

    def test_a_binding_is_small_and_a_long_path_is_not_paid(self):
        """F2: the call is kept as its mark; a path over USDC_PATH_MOST is not paid in USDC at all."""
        self.assertTrue(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa?x=1", "s")[1])
        (b,) = self.g.state["usdc"].values()
        self.assertEqual(b["call"], postage.mark("GET /v1/stamp/aa?x=1"))
        long = "/v1/stamp/" + "a" * postage.USDC_PATH_MOST
        self.assertEqual(self.g.usdc_pay(paid(2), "GET", long, "s")[0]["errorReason"], "usdc_is_not_taken_here")
        self.assertEqual(len(self.evm.calls), 1)

    def test_one_place_holds_few_payments_whose_outcome_is_not_known(self):
        """F2: a place whose payments are never answered binds at most USDC_OPEN_PER_PLACE of them at once."""
        self.evm.answers = [{"success": False, "errorReason": "unexpected_settle_error"}] * (postage.USDC_OPEN_PER_PLACE + 2)
        for n in range(postage.USDC_OPEN_PER_PLACE):
            self.assertEqual(self.g.usdc_pay(paid(10 + n), "GET", "/v1/stamp/%02x" % n, "9.9.9.9")[0]["errorReason"],
                             "unexpected_settle_error")
        said, may, _ = self.g.usdc_pay(paid(99), "GET", "/v1/stamp/99", "9.9.9.9")
        self.assertEqual((may, said["errorReason"], len(self.g.state["usdc"])), (False, postage.TOO_MANY, postage.USDC_OPEN_PER_PLACE))
        self.assertEqual(self.g.usdc_pay(paid(98), "GET", "/v1/stamp/98", "8.8.8.8")[0]["errorReason"], "unexpected_settle_error")

    def test_few_payments_are_asked_of_the_facilitator_at_once(self):
        """F3: USDC_AT_ONCE payments at the facilitator; one more is told to try again, with nothing bound."""
        self.evm.hold = threading.Event()
        threads = [threading.Thread(target=self.g.usdc_pay, args=(paid(20 + n), "GET", "/v1/stamp/%02x" % n, "p%d" % n))
                   for n in range(postage.USDC_AT_ONCE)]
        for t in threads:
            t.start()
        for _ in range(200):
            if len(self.evm.calls) == postage.USDC_AT_ONCE:
                break
            threading.Event().wait(0.01)
        said, may, _ = self.g.usdc_pay(paid(77), "GET", "/v1/stamp/77", "q")
        self.assertEqual((may, said["errorReason"], len(self.g.state["usdc"])), (False, "facilitator_busy", postage.USDC_AT_ONCE))
        self.evm.hold.set()
        for t in threads:
            t.join(10)
        self.assertTrue(self.g.usdc_pay(paid(77), "GET", "/v1/stamp/77", "q")[1])

    def test_a_payment_whose_transfer_went_out_is_ended_not_set_free(self):
        """F1, at the gate: "sent": false for a payment that holds a transfer ends it (failed); it stays bound."""
        late = {"success": False, "errorReason": "settlement_pending", "transaction": TX, "sent": True, "known": False}
        self.evm.answers = [dict(late) for _ in range(1 + len(postage.USDC_POLL))] + [
            {"success": False, "errorReason": "authorization_expired", "transaction": TX, "sent": False}]
        self.assertFalse(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[1])
        self.now[0] += postage.USDC_ASK_EVERY
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "authorization_expired")
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/bb", "s")[0]["errorReason"], "payment_is_for_another_call")
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "authorization_expired")
        self.assertEqual(len(self.evm.calls), 2 + len(postage.USDC_POLL))

    def test_a_use_of_its_nonce_elsewhere_ends_the_payment(self):
        """Third reading, G0: the facilitator found the payment's nonce used by another authorization of the payer, which
        paid us nothing; its own transfer failed. Over at once, not set free, and told so when shown again."""
        self.assertIn("authorization_used_elsewhere", postage.USDC_FINAL)
        self.evm.answers = [{"success": False, "errorReason": "authorization_used_elsewhere", "transaction": TX, "sent": False}]
        said, may, key = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["errorReason"], self.g.state["usdc"][key]["state"]), (False, "authorization_used_elsewhere", "failed"))
        self.now[0] += postage.USDC_ASK_EVERY
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "authorization_used_elsewhere")
        self.assertEqual(self.g.usdc_pay(paid(), "GET", "/v1/stamp/bb", "s")[0]["errorReason"], "payment_is_for_another_call")
        self.assertEqual(len(self.evm.calls), 1)

    def test_its_background_look_holding_the_payment_is_asked_again_within_the_request(self):
        """Third reading, N1: "settling_now" (the facilitator's own look holds the payment this moment) is asked again
        after a pause, as a transfer that waits for its block."""
        self.evm.answers = [{"success": False, "errorReason": postage.SETTLING_NOW, "transaction": TX, "sent": True},
                            {"success": True, "transaction": TX, "network": "eip155:8453", "payer": PAYER}]
        said, may, _ = self.g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")
        self.assertEqual((may, said["transaction"], len(self.evm.calls)), (True, TX, 2))

    def test_a_facilitator_lost_after_the_start_is_not_offered_until_it_answers(self):
        """Third reading, G3: a facilitator that cannot be reached any more is neither offered nor asked; payments of
        one place bound without a transfer count against it no longer than an authorization can last."""
        class Away(Evm):
            up = True
            timeout = 45

            def health(self):
                if not self.up:
                    raise Refused("X402", "the facilitator cannot be reached (URLError)")
                return {"protocol": postage.USDC_PROTOCOL, "network": "eip155:8453", "asset": Evm.asset, "payTo": TO,
                        "maxAmount": "1000000", "confirmTimeout": 15, "sendWithin": 20}
        away = Away()
        u = postage.Usdc(away, TO, 10000, ["/v1/stamp"])
        u.stopped.set()
        g = postage.Gate(self.w, N // 100, 100, clock=lambda: self.now[0], look_every=0, usdc=u)
        self.assertTrue(u.ready)
        away.up = False
        self.assertFalse(u.check(starting=False))
        self.assertFalse(u.ready)
        self.assertEqual(g.x402_needed("/v1/stamp/aa")["accepts"], [])
        self.assertEqual(g.usdc_pay(paid(), "GET", "/v1/stamp/aa", "s")[0]["errorReason"], "usdc_not_available")
        self.assertIn("usdc_not_available", postage.USDC_NOT_NOW)
        away.up = True
        self.assertTrue(u.check(starting=False))
        # bindings with no transfer (answers lost) shut a place out for at most USDC_OPEN_FOR
        away.answers = [{"success": False, "errorReason": "unexpected_settle_error"}] * (postage.USDC_OPEN_PER_PLACE + 1)
        for n in range(postage.USDC_OPEN_PER_PLACE):
            g.usdc_pay(paid(30 + n), "GET", "/v1/stamp/%02x" % n, "7.7.7.7")
        self.assertEqual(g.usdc_pay(paid(60), "GET", "/v1/stamp/60", "7.7.7.7")[0]["errorReason"], postage.TOO_MANY)
        self.now[0] += postage.USDC_OPEN_FOR
        self.assertEqual(g.usdc_pay(paid(61), "GET", "/v1/stamp/61", "7.7.7.7")[0]["errorReason"], "unexpected_settle_error")

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
        self.evm.answers = [{"success": False, "errorReason": "settlement_pending", "transaction": TX}
                            for _ in range(1 + len(postage.USDC_POLL))]
        ask = self.serve(self.g)
        status, headers, said = ask("/v1/stamp/aa", paid())
        self.assertEqual((status, headers.get("Retry-After")), (202, "5"))
        self.assertEqual(ask("/v1/stamp/aa", paid())[0], 202)          # at once: from the gate's record
        self.now[0] += postage.USDC_ASK_EVERY
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
        self.assertEqual(x402.EvmFacilitator(url, "eip155:84532").health(), {})    # it does not settle there
        with self.assertRaises(Refused):                                # and the gate does not start with it
            postage.Usdc(x402.EvmFacilitator(url, "eip155:84532"), TO, 10000, ["/v1/stamp"])
        with self.assertRaises(Refused):
            x402.EvmFacilitator(url, "eip155:1")                        # USDC is not known there here
        with self.assertRaises(Refused):
            x402.EvmFacilitator("http://example.com:8402", "eip155:8453")   # https, or this machine

    def test_a_refusal_of_the_http_kind_is_told_as_nothing_sent(self):
        """Second reading, F2: a facilitator that refuses the request itself (4xx) took nothing to the chain."""
        class No(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(413)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), No)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        f = x402.EvmFacilitator("http://127.0.0.1:%d" % srv.server_address[1], "eip155:8453")
        got = f.settle(paid(), f.wanted(10000, TO))
        self.assertEqual((got["success"], got["errorReason"], got["sent"]), (False, "facilitator_refused", False))


    def test_a_connection_refused_is_told_as_nothing_sent(self):
        """Third reading, G3: nothing listens where the facilitator should: it never saw the payment, so the gate binds
        nothing and the buyer is told to try again (503)."""
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()                    # nothing listens there now
        f = x402.EvmFacilitator("http://127.0.0.1:%d" % port, "eip155:8453")
        got = f.settle(paid(), f.wanted(10000, TO))
        self.assertEqual((got["success"], got["errorReason"], got["sent"]), (False, "facilitator_unreachable", False))
        self.assertIn("facilitator_unreachable", postage.USDC_NOT_NOW)

if __name__ == "__main__":
    unittest.main()
