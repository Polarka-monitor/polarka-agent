"""Paying out with the real wallet, the pool's own node and its depth on regtest, without shares (so without the
proof-of-work library): how deep the node sees a payment, also far back when the time of the payment is given,
and a round whose payment did not go through, shown again with the real Wallet.hand_again and paid once.
Skipped without the node (TN_BIN)."""
import json
import os
import shutil
import tempfile
import time
import unittest

from polarka_agent import payout, pool
from polarka_agent.wallet import Wallet, units

from . import bench

BIN = bench.find_bin()
SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"       # key 0/0 of the public test phrase


@unittest.skipUnless(BIN, "needs truenorthd (TN_BIN)")
class PayoutsOnRegtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(BIN)
        cls.standin = bench.StandIn(cls.node)
        cls.tmp = tempfile.mkdtemp(prefix="polarka-payout-node-")
        cls.rpc = pool.Node("http://127.0.0.1:%d" % cls.node.port, cls.node.cookie)
        cls.wallet = cls.make("pool", {"per_payment_north": "100000", "per_day_north": "1000000"})
        cls.miner = cls.make("miner")
        cls.node.mine(5, cls.wallet.fresh_address())
        cls.node.mine(100, SINK)

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
        cls.standin.stop()
        cls.node.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.standin.set(stale=False, busy=0, hold=False, refuse="", hide=False)

    def test_depth_by_the_node_also_beyond_three_hundred_blocks_when_the_time_is_given(self):
        since = int(time.time())
        done = self.wallet.pay([(self.miner.fresh_address(), units("1"))])
        self.assertEqual(done["state"], "handed", done)
        depth = pool.depth_by(self.rpc)
        self.assertEqual((depth(done["id"]), depth(done["id"], since)), (0, 0))       # waiting is no depth
        self.node.mine(1, SINK)
        self.assertEqual((depth(done["id"]), depth(done["id"], since)), (1, 1))
        self.node.mine(310, SINK)
        self.assertEqual(depth(done["id"], since), 311)
        self.assertEqual(depth(done["id"]), 0)          # without a time it looks 300 blocks back and no further
        self.assertEqual(pool.depth_by(self.rpc)(done["id"], since), 311)              # a fresh one, nothing remembered
        self.assertEqual(depth("ab" * 32, since), 0)
        # a payment made after every block was found (a time far ahead): the walk ends at once
        self.assertEqual(depth(done["id"], since + 10 ** 6), 0)
        self.assertEqual(depth.height(), self.node.cli("getblockcount"))

    def test_a_round_that_did_not_go_through_is_shown_again_and_paid_once(self):
        folder = os.path.join(self.tmp, "books")
        books = payout.Payouts(self.wallet, folder, minimum=pool.UNITS, depth=pool.depth_by(self.rpc), deep=6)
        miner = self.make("miner-of-the-round")
        worker = miner.fresh_address()
        self.standin.set(hold=True)                     # the server takes the payment and does not pass it on
        done = books.run({worker: 7 * pool.UNITS})
        self.assertEqual(done["round"], 1)
        self.assertEqual(done["state"], "unknown", done)
        txid = done["id"]
        self.assertNotIn(txid, self.node.cli("getrawmempool"))
        self.assertEqual(books.run({worker: 7 * pool.UNITS})["state"], "on the way")    # held: shown again, in vain
        self.assertNotIn(txid, self.node.cli("getrawmempool"))
        self.standin.set(hold=False)
        self.assertEqual(books.run({worker: 7 * pool.UNITS})["state"], "on the way")    # shown again: the same one
        self.assertIn(txid, self.node.cli("getrawmempool"))
        self.assertEqual([p["id"] for p in self.wallet.payments() if p.get("memo") == books.memo(1)], [txid])
        self.node.mine(5, SINK)
        self.assertEqual(books.run({worker: 7 * pool.UNITS})["state"], "on the way")    # five deep is not six
        self.node.mine(1, SINK)
        self.assertEqual(books.run({worker: 7 * pool.UNITS})["state"], "nothing is due")
        self.assertEqual(books.sums(), {worker: (7 * pool.UNITS, 0)})
        self.assertEqual(miner.balance()["spendable"], 7 * pool.UNITS)
        self.assertEqual(len([p for p in self.wallet.payments() if str(p.get("memo") or "").startswith(payout.NOTE)]), 1)

    def test_the_surplus_reaches_the_owner_and_stands_against_no_payout(self):
        owner = self.make("owner")
        to, worker = owner.fresh_address(), self.miner.fresh_address()
        wallet = self.make("pool-of-the-surplus")
        with open(os.path.join(wallet.folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "100000", "per_day_north": "1000000", "allow": [to, worker]}, f)
        wallet = Wallet(wallet.folder)
        self.node.mine(2, wallet.fresh_address())
        self.node.mine(100, SINK)
        surplus = payout.Surplus(to, keep=0, most=1000 * pool.UNITS, every=3600, least=pool.UNITS)
        books = payout.Payouts(wallet, os.path.join(self.tmp, "books-of-the-surplus"), minimum=pool.UNITS,
                               depth=pool.depth_by(self.rpc), deep=6, surplus=surplus)
        self.standin.set(hold=True)                                # a payout that does not go through ...
        self.assertEqual(books.run({worker: 3 * pool.UNITS})["state"], "unknown")
        self.assertIn("did not go through", books.sweep(0)["why"])  # ... is not stood against by a surplus
        self.standin.set(hold=False)
        self.assertEqual(books.run({worker: 3 * pool.UNITS})["state"], "on the way")
        # beside a payout that waits for a block: from the coins it does not spend, 50 above the reserve
        free = wallet.balance()["spendable"]
        surplus.keep = free - 50 * pool.UNITS
        done = books.sweep(0)
        self.assertEqual((done["state"], done["surplus"]), ("handed", "49.99000000"), done)
        self.assertIn(done["id"], self.node.cli("getrawmempool"))
        self.assertEqual(books.sweep(0)["why"], "sent not long ago")
        self.node.mine(6, SINK)
        self.assertEqual(books.run({worker: 3 * pool.UNITS})["state"], "nothing is due")
        self.assertEqual(books.sums(), {worker: (3 * pool.UNITS, 0)})
        self.assertEqual(owner.balance()["spendable"], 4_999_000_000)
        self.assertGreaterEqual(wallet.balance()["spendable"], surplus.keep)

    def test_hand_again_knows_only_the_wallets_own_payments(self):
        with self.assertRaises(Exception) as caught:
            self.wallet.hand_again("cd" * 32)
        self.assertEqual(getattr(caught.exception, "kind", None), "NOT_OPEN")


if __name__ == "__main__":
    unittest.main()
