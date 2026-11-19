"""The pool's rules, with a node and a proof of work that are made up here (SHA-256 in place of RandomX), so
that they run anywhere. The real node and the real proof of work are in test_pool_node.py."""
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from polarka_agent import miner, pool, rx
from polarka_agent.keys import encode_address

PAY = encode_address("rnorth", 2, bytes(range(32)))
ALICE = encode_address("rnorth", 2, bytes([7]) * 32)
BOB = encode_address("rnorth", 2, bytes([9]) * 32)
BITS = "20040000"           # a network target of 2^250: one hash in 64 is a block


class FakeHash:
    def hash(self, key, header80):
        return hashlib.sha256(key + header80).digest()


class FakeNode:
    def __init__(self):
        self.height, self.previous, self.down, self.sent, self.refuse = 3000, "11" * 32, False, [], None
        self.transactions = []

    def call(self, method, *params):
        if self.down:
            raise OSError("no node")
        if method == "getblocktemplate":
            return {"version": 0x20000000, "previousblockhash": self.previous, "height": self.height, "curtime": 1795000000,
                    "bits": BITS, "coinbasevalue": 512 * pool.UNITS + sum(t["fee"] for t in self.transactions),
                    "transactions": self.transactions, "default_witness_commitment": "6a24aa21a9ed" + "00" * 32}
        if method == "getblockhash":
            return "%064x" % params[0]
        if method == "submitblock":
            self.sent.append(params[0])
            if self.refuse:
                return self.refuse
            self.height, self.previous = self.height + 1, hashlib.sha256(bytes.fromhex(params[0])[:80]).hexdigest()
            return None
        raise OSError("unknown " + method)


def search(work, want_block=None, start=0):
    """A nonce for this work by the made-up proof of work: a share, and a block or not as asked."""
    header, key = bytes.fromhex(work["header"]), bytes.fromhex(work["seed"])
    share, network = int(work["share_target"], 16), rx.target_of(int(BITS, 16))
    for nonce in range(start, start + 100000):
        proof = int.from_bytes(hashlib.sha256(key + header + nonce.to_bytes(4, "little")).digest(), "little")
        if proof <= share and (want_block is None or (proof <= network) == want_block):
            return nonce
    raise AssertionError("no nonce found")


class PoolRules(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-pool-test-")
        self.node, self.now = FakeNode(), [1000.0]
        self.pool = self.make()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def make(self):
        return pool.Pool(self.node, "regtest", PAY, self.folder, ratio=16, fee_percent=5, hasher=FakeHash(), clock=lambda: self.now[0])

    def refused(self, kind, call, *a):
        with self.assertRaises(pool.PoolError) as caught:
            call(*a)
        self.assertEqual(caught.exception.kind, kind)
        return caught.exception

    def test_work_is_one_header_for_one_address(self):
        a, b = self.pool.work(ALICE), self.pool.work(BOB)
        self.assertNotEqual(a["header"], b["header"])               # another number in the coinbase, another merkle root
        self.assertEqual(self.pool.work(ALICE), a)                   # asking again allocates nothing
        self.assertEqual(int(a["share_target"], 16), 16 << 250)
        self.assertEqual(a["seed"], bytes.fromhex("%064x" % 2048)[::-1].hex())      # the hash of block 2048, as a header holds it
        self.assertEqual(a["share_value"], "30.40000000")           # 512 / 16, less 5 %
        self.now[0] += pool.JOB_AGE
        self.assertNotEqual(self.pool.work(ALICE)["header"], a["header"])

    def test_fees_are_not_paid_for(self):
        self.node.transactions = [{"txid": "22" * 32, "hash": "33" * 32, "data": "00", "fee": 7 * pool.UNITS}]
        self.assertEqual(self.pool.work(ALICE)["share_value"], "30.40000000")

    def test_the_coinbase(self):
        cb = pool.coinbase(3000, 512 * pool.UNITS, self.pool.pay_script, 5, bytes(32), False)
        self.assertIn(pool.TAG, cb)
        self.assertIn(self.pool.pay_script, cb)
        self.assertIn(bytes.fromhex("02b80b"), cb)                  # the height 3000, as BIP 34 writes it
        self.assertEqual(pool.height_push(16), b"\x60")
        self.assertEqual(pool.height_push(128), b"\x02\x80\x00")

    def test_addresses(self):
        self.refused("ADDRESS", self.pool.work, "north1zwfk57mzt9v2eqk7dg270drfs7afk5tksq42ga7anpaxackfl36pskryq05")   # another network
        self.refused("ADDRESS", self.pool.work, "nonsense")
        self.refused("ADDRESS", self.pool.work, None)

    def test_a_share_is_written_down_once(self):
        work = self.pool.work(ALICE)
        nonce = search(work, want_block=False)
        took = self.pool.share(work["job"], nonce)
        self.assertEqual((took["credited"], took["block"], took["accepted"]), ("30.40000000", None, None))
        self.assertEqual(self.node.sent, [])
        self.refused("DUPLICATE", self.pool.share, work["job"], nonce)
        account = self.pool.account(ALICE)
        self.assertEqual((account["shares"], account["earned"], account["owed"], account["paid"]), (1, "30.40000000", "30.40000000", "0.00000000"))
        self.assertEqual(self.pool.account(BOB)["shares"], 0)

    def test_what_is_not_a_share(self):
        work = self.pool.work(ALICE)
        header, key, share = bytes.fromhex(work["header"]), bytes.fromhex(work["seed"]), int(work["share_target"], 16)
        low = next(n for n in range(10000) if int.from_bytes(hashlib.sha256(key + header + n.to_bytes(4, "little")).digest(), "little") > share)
        self.refused("LOW", self.pool.share, work["job"], low)
        good = search(work)
        job_id, mark = work["job"].split(".")
        self.refused("INPUT", self.pool.share, job_id + "." + "0" * len(mark), good)        # a mark that was made up
        self.refused("STALE", self.pool.share, "00" * 8 + "." + mark, good)                 # a job that never was
        for bad in (-1, 1 << 32, "5", 5.0, True, None):
            self.refused("INPUT", self.pool.share, work["job"], bad)
        self.refused("INPUT", self.pool.share, None, good)
        self.assertEqual(self.pool.account(ALICE)["shares"], 0)
        # Bob cannot hand in Alice's header as his: the job is hers, and the share is written down for her
        self.pool.share(work["job"], good)
        self.assertEqual((self.pool.account(ALICE)["shares"], self.pool.account(BOB)["shares"]), (1, 0))

    def test_a_block_goes_to_the_node(self):
        work = self.pool.work(ALICE)
        took = self.pool.share(work["job"], search(work, want_block=True))
        self.assertTrue(took["accepted"])
        self.assertEqual(len(self.node.sent), 1)
        block = bytes.fromhex(self.node.sent[0])
        self.assertEqual(block[:76].hex(), work["header"])
        self.assertEqual(took["block"], pool.dsha(block[:80])[::-1].hex())
        self.assertEqual(self.pool.blocks[-1]["height"], 3000)
        # the tip moved: the old job is stale, new work is for the next height
        self.refused("STALE", self.pool.share, work["job"], search(work, start=50000))
        self.assertEqual(self.pool.work(ALICE)["height"], 3001)

    def test_a_block_the_node_refuses_is_still_a_share(self):
        self.node.refuse = "high-hash"
        work = self.pool.work(ALICE)
        took = self.pool.share(work["job"], search(work, want_block=True))
        self.assertEqual((took["accepted"], took["credited"]), (False, "30.40000000"))
        self.assertEqual(self.pool.account(ALICE)["shares"], 1)

    def test_no_node_no_work(self):
        work = self.pool.work(ALICE)
        self.node.down = True
        self.now[0] += pool.TEMPLATE_AGE
        error = self.refused("PAUSED", self.pool.work, BOB)
        self.assertEqual(error.status, 503)
        self.assertTrue(self.pool.info()["paused"])
        self.node.down = False
        self.assertFalse(self.pool.info()["paused"])
        self.refused("STALE", self.pool.share, work["job"], search(work))    # after a pause nothing old is taken

    def test_the_ledger_is_what_the_pool_owes(self):
        work = self.pool.work(ALICE)
        first = search(work)
        self.pool.share(work["job"], first)
        self.pool.share(work["job"], search(work, start=first + 1))
        again = self.make()
        self.assertEqual(again.account(ALICE)["earned"], self.pool.account(ALICE)["earned"])
        self.assertEqual(again.account(ALICE)["shares"], 2)
        self.assertEqual(again.secret, self.pool.secret)
        with open(os.path.join(self.folder, "pool-shares.jsonl"), "a") as f:
            f.write("{half a line\n")
        with self.assertRaises(ValueError):
            self.make()


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-pool-test-")
        self.node = FakeNode()
        self.pool = pool.Pool(self.node, "regtest", PAY, self.folder, ratio=16, hasher=FakeHash())
        self.server = pool.make_server(self.pool, "127.0.0.1:0")
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def get(self, path, body=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_the_calls(self):
        status, info = self.get("/v1/pool")
        self.assertEqual((status, info["service"], info["share_ratio"], info["fee_percent"], info["paused"]), (200, "polarka-pool", 16, 5, False))
        status, work = self.get("/v1/work?address=" + ALICE)
        self.assertEqual(status, 200)
        status, took = self.get("/v1/share", json.dumps({"job": work["job"], "nonce": search(work, want_block=False)}).encode())
        self.assertEqual((status, took["ok"]), (200, True))
        self.assertEqual(self.get("/v1/account?address=" + ALICE)[1]["shares"], 1)
        self.assertEqual(self.get("/v1/tip")[1]["height"], 3000)
        self.assertEqual(self.get("/v1/blocks")[1], {"blocks": []})
        self.assertEqual(self.get("/v1/work?address=x")[0], 400)
        self.assertEqual(self.get("/v1/work")[0], 400)
        self.assertEqual(self.get("/nothing")[0], 404)
        self.assertEqual(self.get("/v1/share", b"[1]")[0], 400)
        self.assertEqual(self.get("/v1/share", b"x" * 2000)[0], 400)
        self.assertEqual(self.get("/v1/share", json.dumps({"job": work["job"], "nonce": "1"}).encode())[1]["error"], "INPUT")

    def test_what_the_miner_does_with_a_bad_answer(self):
        with self.assertRaises(miner.PoolSaid) as caught:
            miner._work(self.url, "x")
        self.assertEqual(caught.exception.kind, "ADDRESS")
        work = miner._work(self.url, ALICE)
        self.assertEqual((len(work["header"]), len(work["key"]), work["height"]), (76, 32, 3000))


if __name__ == "__main__":
    unittest.main()
