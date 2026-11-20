"""The rules of paying out, with a wallet that is made up here: what is on the way is not paid again, a fall
in the middle of a round neither loses a payment nor makes it twice, and a damaged ledger pays nothing.
The real wallet and node are in test_pool_node.py."""
import json
import os
import shutil
import tempfile
import unittest

from polarka_agent import payout
from polarka_agent.wallet import OPEN, Refused

N = payout.UNITS
A, B, C = "rnorth1a", "rnorth1b", "rnorth1c"


class FakeWallet:
    def __init__(self):
        self.spendable, self.per_payment = 1000 * N, 500 * N
        self.made, self.refuse, self.fall, self.state, self.now = [], None, False, "handed", 0

    def limits(self):
        return {"per_payment": self.per_payment, "per_day": 10 ** 6 * N}

    def balance(self):
        return {"spendable": self.spendable, "set_aside": 0,
                "open_payments": [dict(p) for p in self.made if p["state"] in OPEN + ("handed",)]}

    def payments(self):
        return [dict(p) for p in self.made]

    def pay(self, outputs, memo=None, dry_run=False):
        if self.refuse:
            raise Refused(self.refuse, "no")
        if dry_run:
            return {"state": "not made (a dry run)"}
        p = {"id": "tx%d" % (len(self.made) + 1), "state": self.state, "memo": memo, "outputs": list(outputs), "made": self.now}
        self.made.append(p)
        if self.fall:
            raise RuntimeError("the machine fell after the wallet wrote the payment down")
        return dict(p)

    def hand_again(self, txid):
        p = next(p for p in self.made if p["id"] == txid)
        p["state"] = self.state
        return dict(p)


class Rules(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-payout-test-")
        self.wallet = FakeWallet()
        self.pay = self.make()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def make(self):
        return payout.Payouts(self.wallet, self.folder, minimum=N)

    def test_a_round_and_what_is_on_the_way(self):
        earned = {A: 30 * N, B: 5 * N, C: N // 2}
        done = self.pay.run(earned)
        self.assertEqual((done["round"], done["state"], done["payees"], done["paid_now"]), (1, "handed", 2, "35.00000000"))
        self.assertEqual(self.wallet.made[0]["outputs"], [(A, 30 * N), (B, 5 * N)])       # C is below the minimum
        self.assertEqual(self.pay.sums(), {A: (0, 30 * N), B: (0, 5 * N)})
        # more is earned meanwhile: nothing new is paid while the round waits for its block
        earned[A] += 10 * N
        self.assertEqual(self.pay.run(earned)["state"], "on the way")
        self.assertEqual(len(self.wallet.made), 1)
        self.wallet.made[0]["state"] = "in_block"
        done = self.pay.run(earned)                                  # settles round 1 and makes round 2 for the rest
        self.assertEqual((done["round"], done["paid_now"]), (2, "10.00000000"))
        self.assertEqual(self.pay.sums(), {A: (30 * N, 10 * N), B: (5 * N, 0)})
        again = self.make()                                           # the ledger says the same after a new start
        self.assertEqual(again.sums(), self.pay.sums())

    def test_paid_is_what_the_pools_own_node_sees_deep_enough(self):
        deep = {}
        pay = payout.Payouts(self.wallet, self.folder, minimum=N, depth=lambda txid, since=None: deep.get(txid, 0), deep=6)
        pay.run({A: 30 * N})
        self.wallet.made[0]["state"] = "in_block"                    # the wallet server's word
        self.assertEqual(pay.run({A: 30 * N})["state"], "on the way")       # the node does not see it
        deep["tx1"] = 5
        self.assertEqual(pay.run({A: 30 * N})["state"], "on the way")
        self.assertEqual(pay.sums(), {A: (0, 30 * N)})
        deep["tx1"] = 6
        self.assertEqual(pay.run({A: 30 * N})["state"], "nothing is due")
        self.assertEqual(pay.sums(), {A: (30 * N, 0)})

    def test_who_is_held_is_not_paid(self):
        done = self.pay.run({A: 30 * N, B: 5 * N}, held={A})
        self.assertEqual(self.wallet.made[0]["outputs"], [(B, 5 * N)])
        self.assertEqual(done["paid_now"], "5.00000000")

    def test_with_a_node_a_round_is_taken_back_only_on_the_nodes_evidence(self):
        deep, spent, height = {}, set(), [100]
        depth = lambda txid, since=None: deep.get(txid, 0)
        depth.unspent = lambda txid, n: (txid, n) not in spent
        depth.height = lambda: height[0]
        self.wallet.inputs_of = lambda txid: [("coin", 0)]
        pay = payout.Payouts(self.wallet, self.folder, minimum=N, depth=depth, deep=6)
        pay.run({A: 30 * N})
        self.wallet.made[0]["state"] = "forgotten"                   # the owner forgot it; somebody may still pass it on
        self.assertEqual(pay.run({A: 30 * N})["state"], "on the way")
        self.assertEqual(len(self.wallet.made), 1)                   # nothing is paid again
        deep["tx1"] = 8                                              # and it did come into a block
        self.assertEqual(pay.run({A: 30 * N})["state"], "nothing is due")
        self.assertEqual(pay.sums(), {A: (30 * N, 0)})
        # another round: its coin is spent by something else, and only six blocks later is the round taken back
        pay.run({A: 30 * N, B: 5 * N})
        self.wallet.made[1]["state"] = "overtaken"
        spent.add(("coin", 0))
        self.assertEqual(pay.run({A: 30 * N, B: 5 * N})["state"], "on the way")
        height[0] += 5
        self.assertEqual(pay.run({A: 30 * N, B: 5 * N})["state"], "on the way")
        height[0] += 1
        done = pay.run({A: 30 * N, B: 5 * N})
        self.assertEqual((pay.rounds[2]["state"], done["round"], done["paid_now"]), ("void", 3, "5.00000000"))

    def test_a_payment_the_server_does_not_show_and_the_node_does(self):
        deep = {"tx1": 0}
        pay = payout.Payouts(self.wallet, self.folder, minimum=N, depth=lambda txid, since=None: deep[txid], deep=6)
        self.wallet.state = "kept"
        pay.run({A: 30 * N})
        deep["tx1"] = 8                                              # the wallet still says "kept"; the node knows better
        self.assertEqual(pay.run({A: 30 * N})["state"], "nothing is due")
        self.assertEqual(pay.sums(), {A: (30 * N, 0)})
        # and a payment the server calls "in a block" while the node does not see it is shown again, not paid twice
        self.wallet.state = "in_block"
        pay.run({A: 30 * N, B: 5 * N})
        deep["tx2"] = 0
        self.assertEqual(pay.run({A: 30 * N, B: 5 * N})["state"], "on the way")
        self.assertEqual(len(self.wallet.made), 2)

    def test_nothing_due_and_what_is_refused(self):
        self.assertEqual(self.pay.run({A: N - 1})["state"], "nothing is due")
        self.wallet.refuse = "OVER_DAY"
        done = self.pay.run({A: 5 * N})
        self.assertEqual((done["round"], done["why"]), (None, "OVER_DAY"))
        self.assertEqual(self.pay.rounds, {})                        # a dry run that is refused leaves no round
        self.wallet.limits = lambda: None
        self.assertEqual(self.pay.run({A: 5 * N})["why"], "NO_LIMITS")

    def test_cut_to_what_is_there(self):
        self.wallet.spendable = 12 * N
        earned = {A: 30 * N, B: 9 * N, C: 2 * N}
        done = self.pay.run(earned)
        self.assertEqual(self.wallet.made[0]["outputs"], [(B, 9 * N), (C, 2 * N)])         # the small debts first
        self.assertEqual(done["paid_now"], "11.00000000")
        self.wallet.made[0]["state"] = "in_block"
        self.wallet.spendable, self.wallet.per_payment = 1000 * N, 20 * N
        self.pay.run(earned)                                         # the owner's limit for one payment cuts the next round
        self.assertEqual(self.wallet.made[1]["outputs"], [(A, 20 * N - payout.MARGIN)])     # and A gets what there is
        self.wallet.made[1]["state"] = "in_block"
        self.wallet.per_payment = 1000 * N
        self.pay.run(earned)
        self.wallet.made[2]["state"] = "in_block"
        self.assertEqual(self.pay.run(earned)["state"], "nothing is due")
        self.assertEqual(self.pay.sums(), {A: (30 * N, 0), B: (9 * N, 0), C: (2 * N, 0)})  # in the end everybody has all of it

    def test_a_fall_before_the_wallet_signed(self):
        self.pay._write({"event": "round", "round": 1, "pay": [[A, 30 * N]], "mark": "aa"})
        self.assertEqual(self.pay.sums(), {A: (0, 30 * N)})
        again = self.make()
        done = again.run({A: 30 * N})        # round 1 was never signed: taken back, and paid in round 2
        self.assertEqual((again.rounds[1]["state"], done["round"]), ("void", 2))
        self.assertEqual(len(self.wallet.made), 1)
        self.assertEqual(again.sums(), {A: (0, 30 * N)})

    def test_a_fall_after_the_wallet_signed(self):
        self.wallet.fall = True
        with self.assertRaises(RuntimeError):
            self.pay.run({A: 30 * N})
        self.wallet.fall = False
        again = self.make()
        self.assertEqual(again.rounds[1]["txids"], [])               # the ledger does not know the payment
        self.assertEqual(again.run({A: 30 * N})["state"], "on the way")
        self.assertEqual(again.rounds[1]["txids"], ["tx1"])          # found in the wallet under the round's note
        self.assertEqual(len(self.wallet.made), 1)                   # and not made a second time
        self.wallet.made[0]["state"] = "in_block"
        self.assertEqual(again.run({A: 30 * N})["state"], "nothing is due")
        self.assertEqual(again.sums(), {A: (30 * N, 0)})

    def test_a_payment_that_did_not_go_through(self):
        self.wallet.state = "kept"
        self.pay.run({A: 30 * N})
        self.assertEqual(self.pay.run({A: 30 * N})["state"], "on the way")     # still not through: shown again, as it was
        self.assertEqual((len(self.wallet.made), self.wallet.made[0]["state"]), (1, "kept"))
        self.wallet.state = "unknown"                                # the server forgot it; it may remember it later
        self.pay.run({A: 30 * N})
        self.wallet.state = "handed"
        self.assertEqual(self.pay.run({A: 30 * N})["state"], "on the way")
        self.pay.run({A: 30 * N})
        self.assertEqual(len(self.wallet.made), 1)                   # never a second transaction for the round
        self.assertEqual(self.pay.rounds[1]["txids"], ["tx1"])
        self.wallet.made[0]["state"] = "in_block"
        self.pay.run({A: 30 * N})
        self.assertEqual(self.pay.sums(), {A: (30 * N, 0)})

    def test_a_round_whose_payments_are_dead(self):
        self.pay.run({A: 30 * N})
        self.wallet.made[0]["state"] = "forgotten"
        done = self.pay.run({A: 30 * N})
        self.assertEqual((self.pay.rounds[1]["state"], done["round"]), ("void", 2))

    def test_an_old_payment_of_the_wallet_is_not_taken_for_a_new_round(self):
        self.pay.run({A: 30 * N})
        self.wallet.made[0]["state"] = "in_block"
        os.remove(os.path.join(self.folder, "pool-payouts.jsonl"))
        fresh = self.make()                  # the ledger is gone and the wallet remembers a round: nothing is paid again
        with self.assertRaises(payout.BooksDisagree):
            fresh.run({A: 30 * N})
        self.assertEqual(len(self.wallet.made), 1)

    def test_a_lost_journal_or_ledger_stops_the_payouts(self):
        self.pay.run({A: 30 * N})
        kept, self.wallet.made = self.wallet.made, []            # the wallet's journal is gone, the round is open
        with self.assertRaises(payout.BooksDisagree):
            self.pay.run({A: 30 * N})
        self.assertEqual((self.pay.rounds[1]["state"], self.wallet.made), ("open", []))     # not voided, not paid again
        self.wallet.made = kept
        os.remove(os.path.join(self.folder, "pool-payouts.jsonl"))
        with open(os.path.join(self.folder, "pool-shares.jsonl"), "w") as f:
            f.write("{}\n")
        with self.assertRaises(payout.BooksDisagree):          # shares are written down and the payout ledger is missing
            self.make()

    def test_one_process_over_a_folder_and_one_round_of_a_number(self):
        import subprocess, sys
        code = "import sys; from polarka_agent import payout; payout.hold_folder(sys.argv[1])"
        other = subprocess.run([sys.executable, "-B", "-c", code, self.folder], stderr=subprocess.PIPE, universal_newlines=True,
                               cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("another pool process", other.stderr)
        self.pay.run({A: 30 * N})
        with open(os.path.join(self.folder, "pool-payouts.jsonl"), "a") as f:
            f.write(json.dumps({"event": "round", "round": 1, "pay": [[B, N]], "mark": "bb", "t": 1}) + "\n")
        with self.assertRaises(ValueError):
            self.make()

    def test_ledgers_that_do_not_agree(self):
        self.pay.run({A: 30 * N})
        self.wallet.made[0]["state"] = "in_block"
        with self.assertRaises(ValueError):
            self.pay.run({A: 20 * N})                                # less earned than was paid
        with open(os.path.join(self.folder, "pool-payouts.jsonl"), "a") as f:
            f.write(json.dumps({"event": "paid", "round": 9}) + "\n")
        with self.assertRaises(ValueError):
            self.make()


class SurplusRules(unittest.TestCase):
    """What the pool holds beyond its reserve and what it owes goes to its owner, a part at a time."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-payout-test-")
        self.wallet = FakeWallet()
        self.now = [100000.0]
        self.pay = payout.Payouts(self.wallet, self.folder, minimum=N, clock=lambda: self.now[0],
                                  surplus=payout.Surplus(C, keep=100 * N, most=300 * N, every=3600, least=10 * N))

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def sent(self):
        return [p for p in self.wallet.made if p["memo"] == payout.SURPLUS]

    def test_above_the_reserve_and_the_debt_a_part_at_a_time(self):
        self.wallet.now = self.now[0]                        # the wallet writes down when it made a payment
        done = self.pay.sweep(200 * N)                      # 1000 held, 100 kept, 200 owed: 699.99 above, 300 at a time
        self.assertEqual((done["surplus"], done["to"]), ("300.00000000", C))
        self.assertEqual(self.sent()[0]["outputs"], [(C, 300 * N)])
        self.now[0] += 3599
        self.assertEqual(self.pay.sweep(200 * N)["why"], "sent not long ago")
        self.now[0] += 1
        self.assertEqual(self.pay.sweep(200 * N)["surplus"], "300.00000000")
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(self.pay.sums(), {})                # a surplus is nobody's payout

    def test_nothing_below_the_least_and_never_into_the_reserve_or_the_debt(self):
        self.wallet.spendable = 305 * N
        self.assertEqual(self.pay.sweep(200 * N)["why"], "nothing above the reserve and what is owed")
        self.wallet.spendable = 50 * N
        self.assertEqual(self.pay.sweep(200 * N)["why"], "nothing above the reserve and what is owed")
        self.wallet.spendable = 315 * N
        self.assertEqual(self.pay.sweep(200 * N)["surplus"], "14.99000000")
        self.assertEqual(self.sent()[0]["outputs"], [(C, 315 * N - 100 * N - 200 * N - payout.MARGIN)])

    def test_within_the_limit_for_one_payment(self):
        self.wallet.per_payment = 250 * N
        self.assertEqual(self.pay.sweep(0)["surplus"], "249.99000000")

    def test_not_while_a_payment_did_not_go_through_and_waiting_is_no_hindrance(self):
        self.wallet.state = "unknown"
        self.wallet.pay([(A, N)], "something open")
        self.assertIn("did not go through", self.pay.sweep(0)["why"])
        self.assertEqual(self.sent(), [])
        self.wallet.made[0]["state"] = "handed"              # it waits for a block: its coins are set aside, the rest is free
        self.wallet.state = "handed"
        self.assertEqual(self.pay.sweep(0)["surplus"], "300.00000000")

    def test_what_the_wallet_refuses(self):
        self.wallet.refuse = "OVER_DAY"
        self.assertEqual(self.pay.sweep(0)["why"], "OVER_DAY")
        self.assertIsNone(payout.Payouts(self.wallet, self.folder, minimum=N).sweep(0))     # no Surplus: nothing to do

    def test_the_rounds_do_not_mistake_it_for_theirs(self):
        self.pay.sweep(0)
        done = self.pay.run({A: 30 * N})
        self.assertEqual((done["round"], done["paid_now"]), (1, "30.00000000"))
        self.assertEqual(self.pay.run({A: 30 * N})["state"], "on the way")


if __name__ == "__main__":
    unittest.main()
