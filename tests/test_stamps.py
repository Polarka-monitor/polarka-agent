"""Stamps as a service: the tree and its paths, and on regtest a batch written by a wallet, its block found, a
proof that checks against the node, a reorganization that takes the block away, and the gate that routes to it."""
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from polarka_agent import postage, stamps, walletd
from polarka_agent.wallet import Refused, Wallet, units

from . import bench

SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"


def digest(n):
    return hashlib.sha256(b"file %d" % n).digest()


class Tree(unittest.TestCase):
    def test_every_path_leads_to_the_root(self):
        for size in range(1, 12):
            ds = sorted(digest(n) for n in range(size))
            root, paths = stamps.tree(ds)
            self.assertEqual(len(paths), size)
            for d in ds:
                self.assertEqual(stamps.climb(d, paths[d]), root)
            if size == 1:
                self.assertEqual(root, stamps.leaf(ds[0]))

    def test_a_wrong_path_or_digest_does_not(self):
        ds = sorted(digest(n) for n in range(5))
        root, paths = stamps.tree(ds)
        p = paths[ds[2]]
        self.assertNotEqual(stamps.climb(ds[3], p), root)
        bent = [("left" if s == "right" else "right", h) for s, h in p]
        self.assertNotEqual(stamps.climb(ds[2], bent), root)
        # a node of the tree is not a leaf: its two children cannot be shown as one digest
        self.assertNotEqual(stamps.leaf(paths[ds[0]][0][1]), stamps.node(stamps.leaf(ds[0]), paths[ds[0]][0][1]))


@unittest.skipUnless(bench.find_bin(), "truenorthd and truenorth-cli are needed (TN_BIN names their folder)")
class StampsOnRegtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(bench.find_bin())
        cls.standin = bench.StandIn(cls.node)
        cls.tmp = tempfile.mkdtemp(prefix="polarka-stamps-")
        folder = os.path.join(cls.tmp, "stamps")
        Wallet.create(folder, "regtest", cls.standin.url, None)
        with open(os.path.join(folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "1", "per_day_north": "10"}, f)
        cls.wallet = Wallet(folder)
        cls.node.mine(3, cls.wallet.fresh_address())
        cls.node.mine(100, SINK)
        cls.rpc = walletd.Node("http://127.0.0.1:%d" % cls.node.port, cls.node.cookie)
        cls.now = [1_000_000.0]
        cls.service = stamps.Stamps(cls.wallet, cls.rpc, folder, every=600, clock=lambda: cls.now[0])
        cls.service.tick()                                   # nothing waits: no batch, the scan starts at the tip

    @classmethod
    def tearDownClass(cls):
        cls.standin.stop()
        cls.node.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_batch_its_block_and_proofs(self):
        ds = [digest(n).hex() for n in range(100, 105)]
        for d in ds:
            self.assertEqual(self.service.take(d)["state"], "waiting")
        self.assertEqual(self.service.take(ds[0].upper())["state"], "waiting")     # the same one twice is one
        with self.assertRaises(Refused):
            self.service.take("ab" * 31)
        self.now[0] += 601
        made = self.service.tick()
        self.assertEqual(made["stamps"], 5)
        self.assertIn(made["txid"], self.node.cli("getrawmempool"))
        self.assertEqual(self.service.take(ds[1])["state"], "written")
        self.node.mine(1, SINK)
        self.service.tick()
        proof = self.service.take(ds[3])
        self.assertEqual((proof["state"], proof["txid"], proof["confirmations"]), ("stamped", made["txid"], 1))
        checked = stamps.verify(proof, self.rpc)
        self.assertTrue(checked["block"])
        self.assertEqual(stamps.verify(proof)["root_in_transaction"], True)
        for bad in (dict(proof, digest=ds[2]), dict(proof, root="00" * 32),
                    dict(proof, transaction=proof["transaction"].replace(proof["root"], "11" * 32))):
            with self.assertRaises(ValueError):
                stamps.verify(bad, self.rpc)
        # a reorganization takes the block away, and the next block brings the batch back
        self.node.cli("invalidateblock", proof["block_hash"])
        self.service.tick()
        self.assertEqual(self.service.take(ds[3])["state"], "written")
        self.node.mine(1, self.wallet.account.address(0, 7))       # another coinbase: not the block that was taken away
        self.service.tick()
        again = self.service.take(ds[3])
        self.assertEqual(again["state"], "stamped")
        self.assertNotEqual(again["block_hash"], proof["block_hash"])
        self.assertTrue(stamps.verify(again, self.rpc)["block"])
        with self.assertRaises(ValueError):
            stamps.verify(proof, self.rpc)                  # the old block is not in the chain any more
        # a proof made up offline: everything consistent but the chain
        forged = dict(again, txoutproof=again["txoutproof"][:-2] + "00")
        with self.assertRaises(ValueError):
            stamps.check(forged)
        with self.assertRaises(ValueError):
            stamps.check(dict(again, txid="00" * 32))
        self.assertIn("not checked", stamps.verify(again)["block"])
        # after a start again, the same answers
        fresh = stamps.Stamps(self.wallet, self.rpc, self.wallet.folder, clock=lambda: self.now[0])
        self.assertEqual(fresh.take(ds[0])["root"], again["root"])

    def test_behind_the_gate(self):
        backend = ThreadingHTTPServer(("127.0.0.1", 0), stamps.make_handler(self.service))
        facts = ThreadingHTTPServer(("127.0.0.1", 0), stamps.make_handler(self.service))
        gate_folder = os.path.join(self.tmp, "gate")
        Wallet.create(gate_folder, "regtest", self.standin.url, None)
        gate = postage.Gate(Wallet(gate_folder), units("0.01"), 10, free=3)
        front = ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(
            gate, "127.0.0.1:%d" % facts.server_address[1], False, [("/v1/stamp", "127.0.0.1:%d" % backend.server_address[1])]))
        for s in (backend, facts, front):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/v1/stamp/%s" % (front.server_address[1], digest(7).hex())
            with self.assertRaises(urllib.error.HTTPError) as caught:     # no calls for nothing behind a route
                urllib.request.urlopen(url, timeout=30)
            self.assertEqual(caught.exception.code, 402)
            token = "ab" * 16
            gate.state["tokens"][postage.mark(token)] = {"address": "x", "expires": 0, "paid": True, "calls": 5, "price": 1}
            with urllib.request.urlopen(urllib.request.Request(url, headers={"X-Postage": token}), timeout=30) as r:
                self.assertEqual((json.loads(r.read())["state"], r.headers["X-Postage-Calls-Left"]), ("waiting", "4"))
            with self.assertRaises(urllib.error.HTTPError) as caught:    # a call for nothing to the service before the gate
                urllib.request.urlopen("http://127.0.0.1:%d/v1/unit" % front.server_address[1], timeout=30)
            self.assertEqual(caught.exception.code, 404)                   # answered by it (it has no /v1/unit), not 402
        finally:
            for s in (backend, facts, front):
                s.shutdown()


if __name__ == "__main__":
    unittest.main()
