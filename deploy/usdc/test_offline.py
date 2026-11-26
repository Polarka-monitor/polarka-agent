#!/usr/bin/env python3
"""Offline tests of the USDC facilitator's round-4 pieces that need no chain: which receipt pays us (G0, a use of the
nonce tied to the Transfer right after it), how a broadcast is told (G1, G4), whose nonce comes next (G4), and the
balance of a payer's payments in flight (G5). Run: python -m unittest usdc_offline_test (from this folder)."""
import os
import tempfile
import types
import unittest

from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3

import usdc_facilitator as uf

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x522475540CA835D14E287a645d22c4B367703dac"


class Clock:
    def __init__(self):
        self.t, self.mono, self.slept = 1_800_000_000.0, 1000.0, 0

    def time(self):
        return self.t

    def monotonic(self):
        return self.mono

    def sleep(self, s):
        self.t += s
        self.mono += s
        self.slept += 1


def fac(state_dir=None):
    f = uf.Facilitator.__new__(uf.Facilitator)
    f.network, f.chain_id, f.asset, f.pay_to = "eip155:8453", 8453, USDC, PAY_TO
    f.account = Account.create()
    f.journal = uf.Journal(state_dir or tempfile.mkdtemp(prefix="offline-journal-"))
    f.runtime()
    return f


def used(payer, nonce, i=None, address=USDC):
    lg = {"address": address, "topics": [HexBytes(uf.AUTH_USED), HexBytes(uf.topic(payer)), HexBytes(nonce)], "data": HexBytes(b"")}
    if i is not None:
        lg["logIndex"] = i
    return lg


def moved(frm, to, value, i=None, address=USDC):
    lg = {"address": address, "topics": [HexBytes(uf.TRANSFER), HexBytes(uf.topic(frm)), HexBytes(uf.topic(to))],
          "data": HexBytes(int(value).to_bytes(32, "big"))}
    if i is not None:
        lg["logIndex"] = i
    return lg


def auth(payer, nonce, value="10000"):
    return {"from": payer, "to": PAY_TO, "value": value, "validAfter": "0", "validBefore": "1800000300", "nonce": nonce}


class PaidIn(unittest.TestCase):
    """G0: a receipt pays us for an authorization only through the Transfer logged right after its own use."""

    def setUp(self):
        self.f = fac()
        self.payer, self.own = Account.create().address, Account.create().address
        self.n, self.m = "0x" + "11" * 32, "0x" + "22" * 32

    def test_our_own_transfer(self):
        rc = {"status": 1, "logs": [used(self.payer, self.n), moved(self.payer, PAY_TO, 10000)]}
        self.assertTrue(self.f.paid_in(rc, auth(self.payer, self.n)))

    def test_the_nonce_used_to_pay_the_payer_itself(self):
        rc = {"status": 1, "logs": [used(self.payer, self.n), moved(self.payer, self.own, 1)]}
        self.assertFalse(self.f.paid_in(rc, auth(self.payer, self.n)))

    def test_one_transaction_two_uses_each_tied_to_its_own_transfer(self):
        # a batch: nonce n pays the payer itself 1 unit, nonce m pays us; the Transfer to us belongs to m alone
        rc = {"status": 1, "logs": [used(self.payer, self.n, 0), moved(self.payer, self.own, 1, 1),
                                    used(self.payer, self.m, 2), moved(self.payer, PAY_TO, 10000, 3)]}
        self.assertFalse(self.f.paid_in(rc, auth(self.payer, self.n)))
        self.assertTrue(self.f.paid_in(rc, auth(self.payer, self.m)))

    def test_less_than_the_whole_value_or_to_someone_else(self):
        for after in (moved(self.payer, PAY_TO, 9999), moved(self.payer, self.own, 10000), moved(self.own, PAY_TO, 10000)):
            rc = {"status": 1, "logs": [used(self.payer, self.n), after]}
            self.assertFalse(self.f.paid_in(rc, auth(self.payer, self.n)))

    def test_order_by_log_index(self):
        rc = {"status": 1, "logs": [moved(self.payer, PAY_TO, 10000, 8), used(self.payer, self.n, 7)]}
        self.assertTrue(self.f.paid_in(rc, auth(self.payer, self.n)))

    def test_logs_of_another_contract_do_not_count(self):
        other = Account.create().address
        rc = {"status": 1, "logs": [used(self.payer, self.n, address=other), moved(self.payer, PAY_TO, 10000, address=other)]}
        self.assertFalse(self.f.paid_in(rc, auth(self.payer, self.n)))
        rc = {"status": 1, "logs": [used(self.payer, self.n), moved(self.payer, PAY_TO, 10000, address=other)]}
        self.assertFalse(self.f.paid_in(rc, auth(self.payer, self.n)))

    def test_failed_or_no_transfer_after_the_use(self):
        self.assertFalse(self.f.paid_in({"status": 0, "logs": [used(self.payer, self.n), moved(self.payer, PAY_TO, 10000)]},
                                        auth(self.payer, self.n)))
        self.assertFalse(self.f.paid_in({"status": 1, "logs": [moved(self.payer, PAY_TO, 10000), used(self.payer, self.n)]},
                                        auth(self.payer, self.n)))
        self.assertFalse(self.f.paid_in(None, auth(self.payer, self.n)))

    def test_topics_given_as_text(self):
        lg = used(self.payer, self.n)
        lg["topics"] = [Web3.to_hex(t) for t in lg["topics"]]
        rc = {"status": 1, "logs": [lg, moved(self.payer, PAY_TO, 10000)]}
        self.assertTrue(self.f.paid_in(rc, auth(self.payer, self.n)))


class Broadcast(unittest.TestCase):
    """G1 and G4: a broadcast tried within its request, and what becomes of its nonce."""

    def setUp(self):
        self.clock = Clock()
        self.saved = uf.time
        shim = types.SimpleNamespace(**{k: getattr(uf.time, k) for k in dir(uf.time) if not k.startswith("__")})
        shim.time, shim.sleep, shim.monotonic = self.clock.time, self.clock.sleep, self.clock.monotonic
        uf.time = shim
        self.f = fac()
        self.answers = []
        self.sent = 0

        def send_raw_transaction(raw):
            self.sent += 1
            a = self.answers.pop(0) if self.answers else None
            if a is not None:
                raise a
            return HexBytes(b"\x01" * 32)
        self.f.w3 = types.SimpleNamespace(eth=types.SimpleNamespace(send_raw_transaction=send_raw_transaction))
        self.ready = (None, 80_000, 10 ** 7, 10 ** 6, 1000)

    def tearDown(self):
        uf.time = self.saved

    def put(self, nonce=5, left=30):
        return self.f.put_out("0xaa", "0xraw", nonce, self.clock.monotonic() + left, self.ready)

    def test_took(self):
        self.assertEqual(self.put(), "took")
        self.assertEqual((self.f.next_nonce, self.sent), (6, 1))
        self.assertIn("0xaa", self.f.took)
        self.assertEqual(self.f.ours[5]["tx"], "0xaa")

    def test_a_rate_limit_once_is_tried_again_within_the_request(self):
        self.answers = [Exception("429 Client Error: Too Many Requests")]
        self.assertEqual(self.put(), "took")
        self.assertEqual((self.sent, self.clock.slept), (2, 1))

    def test_already_known_is_taken(self):
        self.answers = [Exception("already known")]
        self.assertEqual(self.put(), "took")

    def test_refused_every_time_gives_its_nonce_back(self):
        self.answers = [Exception("429 Too Many Requests")] * 3
        self.assertEqual(self.put(), "refused")
        self.assertEqual((self.sent, self.f.next_nonce, self.f.ours), (3, 5, {}))
        self.assertNotIn("0xaa", self.f.took)

    def test_no_try_after_the_time_left(self):
        self.answers = [Exception("429")] * 3
        self.assertEqual(self.put(left=0.5), "refused")
        self.assertEqual(self.sent, 1)

    def test_a_lost_answer_keeps_the_nonce_taken(self):
        self.answers = [Exception("Read timed out.")] * 3
        self.assertEqual(self.put(), "unsure")
        self.assertEqual((self.f.next_nonce, self.f.ours[5]["tx"]), (6, "0xaa"))

    def test_nonce_too_low_at_once_is_stale(self):
        self.answers = [Exception("nonce too low: next nonce 9, tx nonce 5")]
        self.assertEqual(self.put(), "stale")
        self.assertEqual(self.sent, 1)

    def test_nonce_too_low_after_a_lost_answer_is_most_likely_that_try(self):
        self.answers = [Exception("Read timed out."), Exception("nonce too low")]
        self.assertEqual(self.put(), "unsure")

    def test_transfers_that_can_never_be_in_a_block_are_marked_gone(self):
        """A transfer whose nonce was used before it was handed over, or that no RPC took, is "gone": reconcile does
        not wait for its block before deciding on the others."""
        counted = {"pending": 5}
        eth = self.f.w3.eth
        eth.get_transaction_count = lambda a, w: counted[w]
        fn = types.SimpleNamespace(build_transaction=lambda tx: dict(tx, to=USDC, value=0, data=b""))
        ready = (fn, 80_000, 10 ** 7, 10 ** 6, 1000)
        key = "0xpayer:0x01"
        a = auth(Account.create().address, "0x" + "99" * 32)
        txh, raw, n = self.f.signed(ready)
        self.f.journal.put(key, {"auth": a, "state": "sent", "txs": [txh], "tx": txh, "raw": raw, "at": 1, "last": 1})
        self.answers = [Exception("nonce too low")]           # then the one signed above it is taken
        last, how = self.f.handed(key, ready, txh, raw, n, self.clock.monotonic() + 30)
        e = self.f.journal.get(key)
        self.assertEqual((how, e["gone"], e["txs"], e["refused"]), ("took", [txh], [txh, last], False))
        self.answers = [Exception("429")] * 3
        again, how = self.f.handed(key, ready, last, raw, n + 1, self.clock.monotonic() + 30)
        e = self.f.journal.get(key)
        self.assertEqual((how, sorted(e["gone"]), e["refused"]), ("refused", sorted([txh, last]), True))

    def test_the_next_nonce_is_ours_while_the_rpc_counts_less(self):
        self.f.next_nonce = 7
        counted = {"pending": 5}
        self.f.w3 = types.SimpleNamespace(eth=types.SimpleNamespace(get_transaction_count=lambda a, w: counted[w]))
        fn = types.SimpleNamespace(build_transaction=lambda tx: dict(tx, to=USDC, value=0, data=b""))
        ready = (fn, 80_000, 10 ** 7, 10 ** 6, 1000)
        self.assertEqual(self.f.signed(ready)[2], 7)
        counted["pending"] = 9
        self.assertEqual(self.f.signed(ready)[2], 9)
        self.assertEqual(self.f.signed(ready, above=12)[2], 13)


class Mend(unittest.TestCase):
    """G4 and the fourth reading's R1 to R3: the lowest nonce of ours that the chain lost is filled with a transfer of
    nothing to ourselves, one nonce a look, the bid capped, a filler kept only once handed over."""

    def setUp(self):
        from web3.exceptions import TransactionNotFound
        self.f = fac()
        self.f.chain_id = 8453
        self.mined, self.known, self.sent, self.tried, self.refuse = 5, set(), [], [], None

        def get_transaction(h):
            if h in self.known:
                return {"hash": h}
            raise TransactionNotFound("not found")

        def send_raw_transaction(raw):
            self.tried.append(raw)
            if self.refuse:
                raise Exception(self.refuse)
            self.sent.append(raw)
        self.f.w3 = types.SimpleNamespace(eth=types.SimpleNamespace(
            get_transaction_count=lambda a, w: self.mined, get_transaction=get_transaction, max_priority_fee=10 ** 6,
            get_block=lambda w: {"baseFeePerGas": 5 * 10 ** 6}, send_raw_transaction=send_raw_transaction))
        self.now = uf.time.time()

    @staticmethod
    def decoded(raws):
        from eth_account.typed_transactions import TypedTransaction
        return [TypedTransaction.from_bytes(HexBytes(r)).as_dict() for r in raws]

    def lost(self, n, fee=10 ** 7, tip=10 ** 6, ago=200, tx=None):
        return {"tx": tx or "0x%02x" % n, "at": self.now - ago, "fee": fee, "tip": tip, "first_fee": fee, "first_tip": tip}

    def test_a_lost_one_is_filled_and_a_late_one_is_not(self):
        self.f.next_nonce = 7
        self.f.ours = {5: self.lost(5, fee=4 * 10 ** 7, tip=2 * 10 ** 6), 6: self.lost(6)}
        self.f.mend()
        (d,) = self.decoded(self.sent)
        self.assertEqual((d["nonce"], d["value"], d["gas"]), (5, 0, 21000))
        self.assertGreaterEqual(d["maxFeePerGas"], 4 * 10 ** 7 * 12 // 10)       # it replaces the lost one if it is anywhere
        self.assertGreaterEqual(d["maxPriorityFeePerGas"], 2 * 10 ** 6 * 12 // 10)
        self.sent.clear()
        self.f.ours[5]["at"] = self.now - 200       # that filler lost too, but the chain holds nonce 5 now: nothing more
        self.known = {self.f.ours[5]["tx"]}
        self.f.mend()
        self.assertEqual(self.sent, [])

    def test_one_nonce_a_look_the_next_after_it_is_in_a_block(self):
        """R2: a transfer queued behind a lost one is as old and, on a node without our pool, as unknown; it is judged
        only once the filler below it is in a block."""
        self.f.next_nonce = 7
        self.f.ours = {5: self.lost(5), 6: self.lost(6)}
        self.f.mend()
        self.assertEqual([d["nonce"] for d in self.decoded(self.sent)], [5])
        self.f.mend()                               # nonce 5 not in a block yet: nothing more, its filler is recent
        self.assertEqual(len(self.sent), 1)
        self.mined = 6                              # the filler is in a block; 6 was lost too
        self.f.mend()
        self.assertEqual([d["nonce"] for d in self.decoded(self.sent)], [5, 6])
        self.mined = 7
        self.f.mend()
        self.assertIsNone(self.f.gap)

    def test_nothing_before_lost_after(self):
        self.f.next_nonce = 6
        self.f.ours = {5: self.lost(5, ago=10)}
        self.f.mend()
        self.assertEqual(self.sent, [])

    def test_a_gap_from_before_a_restart_below_ours_that_wait(self):
        """R3: nonce 5 was counted by the RPC at this start and is gone; ours from 6 on wait behind it."""
        self.f.next_nonce = 8
        self.f.ours = {6: self.lost(6), 7: self.lost(7, ago=150)}
        self.known = {"0x06", "0x07"}
        self.f.mend()
        (d,) = self.decoded(self.sent)
        self.assertEqual((d["nonce"], d["maxPriorityFeePerGas"], d["maxFeePerGas"]), (5, 2 * 10 ** 6, 2 * (10 ** 7 + 10 ** 6)))
        self.sent.clear()
        self.f.gap = None
        self.f.ours = {6: self.lost(6, ago=10)}
        self.f.next_nonce = 7
        self.f.mend()                               # ours above it have not waited long: nothing yet
        self.assertEqual(self.sent, [])

    def test_a_filler_not_handed_over_is_not_kept_and_the_bid_does_not_grow(self):
        """R1: a refused filler is not taken as the transfer to replace next time; an ALARM says so."""
        self.f.next_nonce = 6
        self.f.ours = {5: self.lost(5, fee=2 * 10 ** 7, tip=2 * 10 ** 6)}
        self.refuse = "insufficient funds for gas * price + value"
        with self.assertLogs("usdc-facilitator", "WARNING") as said:
            for _ in range(5):
                self.f.mend()
        bids = {(d["maxFeePerGas"], d["maxPriorityFeePerGas"]) for d in self.decoded(self.tried)}
        self.assertEqual((len(self.tried), len(bids), self.f.ours[5]["tx"]), (5, 1, "0x05"))
        self.assertEqual(sum("ALARM" in line for line in said.output), 1)      # at once, then at most every ALARM_EVERY

    def test_the_bid_is_capped(self):
        # a run of fillers each handed over and lost: the bid stays within FILL_MOST times today's fee or the first one's
        self.f.next_nonce = 6
        self.f.ours = {5: dict(self.lost(5), fee=10 ** 12, tip=10 ** 11, first_fee=10 ** 7, first_tip=10 ** 6)}
        self.f.mend()
        (d,) = self.decoded(self.sent)
        self.assertEqual((d["maxFeePerGas"], d["maxPriorityFeePerGas"]), (4 * (10 ** 7 + 10 ** 6), 4 * 10 ** 6))

    def test_while_a_lost_nonce_stays_open_new_payments_are_refused_with_nothing_sent(self):
        self.f.gap = {"nonce": 5, "since": self.now - uf.GAP_HOLD - 1, "first": self.now - 999, "told": 0.0}
        self.assertTrue(self.f.held())
        self.f.w3 = None                            # nothing may be asked of the chain
        a = auth(Account.create().address, "0x" + "ab" * 32)
        a["validBefore"] = str(int(self.now) + 300)
        said = self.f.first("k", a, {}, uf.time.monotonic())
        self.assertEqual((said["errorReason"], said["sent"]), ("facilitator_busy", False))
        self.f.gap["since"] = self.now
        self.assertFalse(self.f.held())

    def test_a_second_filler_for_the_same_nonce_holds_from_when_it_was_found_lost(self):
        """Fifth reading: an RPC that takes fillers and loses them; after the first filler payments may go on for
        GAP_HOLD, after a second one for the same nonce they are held until the nonce is in a block."""
        self.f.next_nonce = 6
        self.f.ours = {5: self.lost(5)}
        self.f.mend()
        self.assertEqual((self.f.gap["fillers"], self.f.held()), (1, False))
        self.f.ours[5]["at"] = self.now - 200           # that filler lost as well
        self.f.gap["first"] = self.now - 300
        self.f.mend()
        self.assertEqual((self.f.gap["fillers"], self.f.gap["since"], self.f.held()), (2, self.f.gap["first"], True))
        self.mined = 6
        self.f.mend()
        self.assertIsNone(self.f.gap)

    def test_the_transfers_not_yet_decided_are_taken_from_the_journal_at_the_start(self):
        """R3: after a restart a lost nonce of ours is seen, and the next one is counted above them."""
        folder = tempfile.mkdtemp(prefix="offline-journal-")
        f = fac(folder)
        acct = Account.create()
        raw = acct.sign_transaction({"to": PAY_TO, "value": 0, "gas": 21000, "nonce": 9, "chainId": 8453,
                                     "maxFeePerGas": 3 * 10 ** 7, "maxPriorityFeePerGas": 2 * 10 ** 6})
        txh, rawh = Web3.to_hex(raw.hash), Web3.to_hex(raw.raw_transaction)
        a = auth(acct.address, "0x" + "cd" * 32)
        f.journal.put(acct.address.lower() + ":" + a["nonce"], {"auth": a, "state": "sent", "txs": [txh], "tx": txh,
                                                                "raw": rawh, "at": 1000, "last": 1000})
        uf.os.close(f.journal.held)                 # the process is gone, its lock with it
        g = fac(folder)
        self.assertEqual((g.next_nonce, g.ours[9]["tx"], g.ours[9]["fee"], g.ours[9]["at"]), (10, txh, 3 * 10 ** 7, 1000))


class Outcome(unittest.TestCase):
    """What a payment none of whose transfers paid is decided as, from what became of its authorization."""

    def setUp(self):
        self.f = fac()
        self.payer = Account.create().address

    def decided(self, how, free):
        n = "0x" + os.urandom(32).hex()
        key = self.payer.lower() + ":" + n
        e = {"auth": auth(self.payer, n), "state": "sent", "txs": ["0xours"], "tx": "0xours", "at": 1, "last": 1}
        self.f.journal.put(key, e)
        said = self.f.otherwise(key, e, how, "0xtheirs" if how != "free" else None, "0xours", free)
        return said, self.f.journal.get(key)

    def test_paid_by_another_transaction(self):
        said, e = self.decided("paid", "authorization_expired")
        self.assertEqual((said["success"], said["transaction"], e["state"]), (True, "0xtheirs", "paid"))

    def test_used_elsewhere_or_canceled_moved_nothing(self):
        for how, reason in (("elsewhere", "authorization_used_elsewhere"), ("canceled", "authorization_canceled")):
            said, e = self.decided(how, "transaction_failed")
            self.assertEqual((said["errorReason"], said["sent"], said["transaction"], e["by"]), (reason, False, "0xours", "0xtheirs"))

    def test_never_taken(self):
        said, _ = self.decided("free", "authorization_expired")      # its end has passed: nothing can move
        self.assertEqual((said["errorReason"], said["sent"]), ("authorization_expired", False))
        said, _ = self.decided("free", "transaction_failed")         # still usable by anyone until its end (N4)
        self.assertEqual((said["errorReason"], said["sent"]), ("transaction_failed", True))


class InFlight(unittest.TestCase):
    """G5: a payer's payments not yet decided must fit its balance together."""

    def setUp(self):
        self.f = fac()
        self.payer = Account.create().address
        self.asked = []
        self.balance = 15000

        def balance_of(who):
            def call(block_identifier=None):
                self.asked.append(who)
                return self.balance
            return types.SimpleNamespace(call=call)
        self.f.usdc = types.SimpleNamespace(functions=types.SimpleNamespace(balanceOf=balance_of))

    def test_alone_it_is_asked_too(self):
        # the simulation came before the lock: another payment of the payer may have been paid and decided since
        self.assertIsNone(self.f.short_of(auth(self.payer, "0x" + "33" * 32)))
        self.balance = 9999
        self.assertEqual(self.f.short_of(auth(self.payer, "0x" + "33" * 32)), "insufficient_funds")
        self.assertEqual(len(self.asked), 2)

    def test_with_one_in_flight(self):
        n = "0x" + "44" * 32
        self.f.journal.put(self.payer.lower() + ":" + n, {"auth": auth(self.payer, n), "state": "sent", "at": 1})
        self.assertEqual(self.f.short_of(auth(self.payer, "0x" + "55" * 32)), "insufficient_funds")
        self.balance = 20000
        self.assertIsNone(self.f.short_of(auth(self.payer, "0x" + "55" * 32)))
        other = Account.create().address           # another payer's payments are not counted
        self.assertIsNone(self.f.short_of(auth(other, "0x" + "66" * 32)))

    def test_decided_ones_do_not_count(self):
        n = "0x" + "77" * 32
        self.f.journal.put(self.payer.lower() + ":" + n, {"auth": auth(self.payer, n), "state": "paid", "at": 1, "decided": 2})
        self.assertIsNone(self.f.short_of(auth(self.payer, "0x" + "88" * 32)))


if __name__ == "__main__":
    unittest.main()
