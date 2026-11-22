"""Stratum against the official node on regtest, with the real proof of work: a miner that makes its headers the way
IronMiner does (test_stratum.ironminer_header) finds shares through the pool's stratum, and the node takes the blocks
among them. Skipped without the node (TN_BIN) or without the proof-of-work library (build-rx)."""
import json
import os
import shutil
import tempfile
import unittest

from polarka_agent import pool, rx, stratum

from . import bench
from .test_stratum import Client, ironminer_header

BIN = bench.find_bin()


@unittest.skipUnless(BIN and rx.find_library(), "needs truenorthd (TN_BIN) and the proof-of-work library")
class StratumOnRegtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(BIN)
        cls.node.cli("createwallet", "pool")
        cls.pay_to = cls.node.cli("getnewaddress")
        cls.worker = cls.node.cli("getnewaddress")
        cls.node.mine(1, cls.pay_to)            # a node at height 0 hands out no template
        cls.folder = tempfile.mkdtemp(prefix="polarka-stratum-node-")
        rpc = pool.Node("http://127.0.0.1:%d" % cls.node.port, cls.node.cookie)
        cls.pool = pool.Pool(rpc, "regtest", cls.pay_to, cls.folder, ratio=2048, fee_percent=5)
        cls.stratum = stratum.Stratum(cls.pool, "127.0.0.1:0")
        cls.port = cls.stratum.start()
        cls.hasher = pool.RxHasher()

    @classmethod
    def tearDownClass(cls):
        cls.stratum.stop()
        cls.node.stop()
        shutil.rmtree(cls.folder, ignore_errors=True)

    def test_mining_over_stratum(self):
        before = self.node.cli("getblockcount")
        c = Client(self.port)
        try:
            self.assertEqual(c.start(self.worker)["result"], True)
            self.assertEqual(c.k, 31)                   # regtest: a share is as easy as it can be, about a block
            taken, low, n = [], None, 0
            while len(taken) < 4 or low is None:
                network = rx.target_of(int(c.job[6], 16))
                header, time_hex, nonce_hex = ironminer_header(c.job, c.extranonce1, b"\x00\x00\x00\x01", n)
                n += 1
                proof = int.from_bytes(self.hasher.hash(c.key, header), "little")
                submit = [self.worker, c.job[0], "00000001", time_hex, nonce_hex]
                if proof > stratum.share_target(c.k):
                    if low is None:             # the pool's proof of work says no where the miner's does
                        low = c.call("mining.submit", submit)
                        self.assertEqual(low["error"][0], 23, low)
                    continue
                said = c.call("mining.submit", submit)
                self.assertEqual(said["result"], True, said)
                taken.append(proof <= network)
                if proof <= network:            # a block: the next work is on the new tip
                    tip = stratum.swapped(self.node.cli("getbestblockhash"))
                    job = c.note("mining.notify")
                    while job[1] != tip:
                        job = c.note("mining.notify")
                    c.job, n = job, 0
        finally:
            c.close()
        self.assertGreater(sum(taken), 0)
        self.assertEqual(self.node.cli("getblockcount"), before + sum(taken))
        with open(os.path.join(self.folder, "pool-shares.jsonl")) as f:
            ledger = [json.loads(line) for line in f]
        self.assertEqual([e["via"] for e in ledger], ["stratum"] * len(taken))
        self.assertEqual([bool(e["accepted"]) for e in ledger], taken)
        for entry in ledger:
            if entry["accepted"]:
                block = self.node.cli("getblock", entry["block"], 2)
                self.assertEqual(block["height"], entry["height"])
                coinbase = block["tx"][0]
                self.assertIn(pool.TAG.hex(), coinbase["vin"][0]["coinbase"])
                self.assertIn((bytes([len(stratum.MARK) + 8]) + stratum.MARK + bytes.fromhex(c.extranonce1)).hex(),
                              coinbase["vin"][0]["coinbase"])
                self.assertEqual(coinbase["vout"][0]["scriptPubKey"]["address"], self.pay_to)
        account = self.pool.account(self.worker)
        self.assertEqual((account["shares"], account["earned"]), (len(taken), pool.north(sum(e["value"] for e in ledger))))


if __name__ == "__main__":
    unittest.main()
