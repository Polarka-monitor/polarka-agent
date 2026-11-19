"""The pool and the miner against the official node on regtest, with the real proof of work: the miner finds
shares through the pool, the node takes the blocks among them, and the ledger says what was earned.
Skipped without the node (TN_BIN) or without the proof-of-work library (build-rx)."""
import json
import os
import shutil
import tempfile
import threading
import unittest

from polarka_agent import miner, pool, rx

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
        sums = miner.mine(self.url, self.worker, threads=2, fast=False, shares=8, seconds=240, say=events.append, look_every=0.3)
        self.assertGreaterEqual(sums["shares"], 8)
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


if __name__ == "__main__":
    unittest.main()
