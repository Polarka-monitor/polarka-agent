"""The pool and the miner against the official node on regtest, with the real proof of work: the miner finds
shares through the pool, the node takes the blocks among them, and the ledger says what was earned.
Skipped without the node (TN_BIN) or without the proof-of-work library (build-rx)."""
import json
import os
import shutil
import tempfile
import threading
import unittest

from polarka_agent import miner, payout, pool, rx
from polarka_agent.keys import script_for_address
from polarka_agent.wallet import Wallet

from . import bench

BIN = bench.find_bin()


@unittest.skipUnless(BIN and rx.find_library(), "needs truenorthd (TN_BIN) and the proof-of-work library")
class PoolOnRegtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(BIN)
        cls.node.cli("createwallet", "pool")
        cls.pay_to = cls.node.cli("getnewaddress")
        cls.worker = cls.node.cli("getnewaddress")
        cls.node.mine(1, cls.pay_to)            # a node at height 0 hands out no template
        cls.folder = tempfile.mkdtemp(prefix="polarka-pool-node-")
        rpc = pool.Node("http://127.0.0.1:%d" % cls.node.port, cls.node.cookie)
        cls.pool = pool.Pool(rpc, "regtest", cls.pay_to, cls.folder, ratio=2048, fee_percent=5)
        cls.server = pool.make_server(cls.pool, "127.0.0.1:0")
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.node.stop()
        shutil.rmtree(cls.folder, ignore_errors=True)

    def test_mining_through_the_pool(self):
        before = self.node.cli("getblockcount")
        events = []
        sums = miner.mine(self.url, self.worker, threads=2, fast=False, shares=16, seconds=240, say=events.append, look_every=0.3)
        self.assertGreaterEqual(sums["shares"], 16)
        self.assertGreater(sums["blocks"], 0)       # on regtest about every second hash is a block
        # every block the pool says the node took is in the chain, and nothing else was added to it
        self.assertEqual(self.node.cli("getblockcount"), before + sums["blocks"])
        with open(os.path.join(self.folder, "pool-shares.jsonl")) as f:
            ledger = [json.loads(line) for line in f]
        self.assertEqual(len(ledger), sums["shares"])
        taken = [e for e in ledger if e["accepted"]]
        self.assertEqual(len(taken), sums["blocks"])
        for entry in taken:
            block = self.node.cli("getblock", entry["block"], 2)
            self.assertEqual(block["height"], entry["height"])
            coinbase = block["tx"][0]
            self.assertIn(pool.TAG.hex(), coinbase["vin"][0]["coinbase"])
            self.assertEqual(coinbase["vout"][0]["scriptPubKey"]["address"], self.pay_to)
        # what is owed is the sum of the ledger, all of it to the address that mined
        account = self.pool.account(self.worker)
        self.assertEqual(account["shares"], sums["shares"])
        self.assertEqual(account["earned"], pool.north(sum(e["value"] for e in ledger)))
        self.assertTrue(all(e["address"] == self.worker and e["value"] > 0 for e in ledger))
        # on regtest the network's target is so high that the share target cannot be 2048 times higher: a share
        # is then worth what its own target says, here about half a block less the fee
        template = self.node.cli("getblocktemplate", '{"rules": ["segwit"]}')
        network = rx.target_of(int(template["bits"], 16))
        self.assertEqual(ledger[-1]["value"] // 1000, template["coinbasevalue"] * network // pool.MOST * 95 // 100 // 1000)
        self.assertTrue(any(e["event"] == "share" and e["accepted"] for e in events))

    def test_the_node_refuses_a_wrong_nonce_as_the_pool_does(self):
        """The pool's check and the node's are the same rule: a nonce the library says is no block is not sent,
        and one it says is a block is taken by the node (the test above). Here: a job, then the tip moves under it."""
        work = miner._work(self.url, self.worker)
        self.node.mine(1, self.pay_to)
        self.pool.template_at = 0
        with self.assertRaises(miner.PoolSaid) as caught:
            miner._call(self.url + "/v1/share", {"job": work["job"], "nonce": 1})
        self.assertEqual(caught.exception.kind, "STALE")


SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"       # key 0/0 of the public test phrase


@unittest.skipUnless(BIN and rx.find_library(), "needs truenorthd (TN_BIN) and the proof-of-work library")
class PayingOnRegtest(unittest.TestCase):
    """Shares are mined through the pool, the pool's wallet pays them out through the stand-in wallet server,
    and the coins are in a block on the miner's address. Once, whatever is asked again."""

    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(BIN)
        cls.standin = bench.StandIn(cls.node)
        cls.tmp = tempfile.mkdtemp(prefix="polarka-pool-pay-")
        cls.folder = os.path.join(cls.tmp, "pool")
        Wallet.create(cls.folder, "regtest", cls.standin.url, None)
        with open(os.path.join(cls.folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "100000", "per_day_north": "1000000"}, f)
        cls.wallet = Wallet(cls.folder)
        Wallet.create(os.path.join(cls.tmp, "miner"), "regtest", cls.standin.url, None)
        cls.miner_wallet = Wallet(os.path.join(cls.tmp, "miner"))
        cls.worker = cls.miner_wallet.fresh_address()
        cls.pay_to = cls.wallet.account.address(0, 0)
        cls.node.mine(1, SINK)
        cls.rpc = pool.Node("http://127.0.0.1:%d" % cls.node.port, cls.node.cookie)
        cls.pool = pool.Pool(cls.rpc, "regtest", cls.pay_to, cls.folder, payouts=payout.Payouts(cls.wallet, cls.folder, minimum=pool.UNITS))
        cls.server = pool.make_server(cls.pool, "127.0.0.1:0")
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.standin.stop()
        cls.node.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_shares_are_paid_once(self):
        sums = miner.mine(self.url, self.worker, threads=1, fast=False, shares=5, seconds=240, look_every=0.3)
        owed = self.pool.account(self.worker)["owed"]
        self.assertEqual(owed, sums["credited"])
        # the pool's rewards are not ripe and it has nothing else: nobody is paid, and nothing is written down
        self.assertIsNone(self.pool.pay_round()["round"])
        self.assertEqual(self.pool.account(self.worker)["on_the_way"], "0.00000000")
        # the pool's own reserve (it pays for shares whether or not it found the blocks), and a hundred blocks on top
        self.node.mine(10, self.pay_to)
        self.node.mine(100, SINK)
        done = self.pool.pay_round()
        self.assertEqual((done["round"], done["state"], done["payees"], done["paid_now"]), (1, "handed", 1, owed))
        account = self.pool.account(self.worker)
        self.assertEqual((account["on_the_way"], account["owed"], account["paid"]), (owed, "0.00000000", "0.00000000"))
        self.assertEqual(self.pool.pay_round()["state"], "on the way")
        self.node.mine(1, SINK)
        self.assertEqual(self.pool.pay_round()["state"], "nothing is due")
        account = self.pool.account(self.worker)
        self.assertEqual((account["paid"], account["on_the_way"], account["owed"]), (owed, "0.00000000", "0.00000000"))
        got = self.miner_wallet.received(self.worker)
        self.assertEqual((pool.north(got["amount"]), got["confirmations"]), (owed, 1))
        self.assertEqual(len(self.wallet.payments()), 1)
        # a pool started again over the same folder knows what it paid
        again = pool.Pool(self.rpc, "regtest", self.pay_to, self.folder, payouts=payout.Payouts(self.wallet, self.folder, minimum=pool.UNITS))
        self.assertEqual(again.account(self.worker), account)
        self.assertEqual(again.pay_round()["state"], "nothing is due")

    def test_the_cap_reads_the_real_chain(self):
        self.node.mine(3, SINK)
        tagged = len(self.pool.blocks)
        height = self.node.cli("getblockcount")
        cap = pool.Cap(self.rpc, [], least=1, is_ours=self.pool.taken.__contains__)
        self.assertEqual(pool.Cap(self.rpc, [], least=1).measure()[86400][0], 0)      # the tag alone counts for nothing
        ours, blocks = cap.measure()[86400]
        self.assertEqual((ours, blocks), (len([b for b in self.pool.blocks if b["accepted"]]), height))
        self.assertLessEqual(ours, tagged)
        own = pool.Cap(self.rpc, [script_for_address("regtest", SINK)], least=1)
        self.assertGreater(own.measure()[86400][0], ours)             # the owner's own blocks count too


if __name__ == "__main__":
    unittest.main()
