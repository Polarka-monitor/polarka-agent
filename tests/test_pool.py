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
        if method == "getbestblockhash":
            return self.previous
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
        good = search(work, want_block=False)
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
        self.pool.share(work["job"], search(work, want_block=False))     # for a short while the last tip still counts
        self.now[0] += pool.NODE_SILENCE
        self.refused("PAUSED", self.pool.share, work["job"], search(work, want_block=False, start=5000))
        self.node.down = False
        self.assertFalse(self.pool.info()["paused"])
        self.refused("STALE", self.pool.share, work["job"], search(work))    # after a pause nothing old is taken

    def test_no_work_when_the_pool_owes_too_much_or_an_address_finds_no_blocks(self):
        self.assertAlmostEqual(pool.at_most(0, 7.0), 0.000912, places=5)
        self.assertAlmostEqual(pool.at_most(2, 3.0), 0.42319, places=4)
        self.assertGreater(pool.at_most(900, 800.0), 0.99)           # large numbers: more found than due is no bad luck
        self.assertLess(pool.at_most(600, 800.0), 1e-12)
        self.assertEqual(pool.at_most(0, 0.0), 1.0)
        work = self.pool.work(ALICE)
        self.pool.share(work["job"], search(work, want_block=False))
        self.pool.most_owed = 30 * pool.UNITS                        # one share is 30.4
        self.assertEqual(self.refused("FUNDS", self.pool.work, BOB).status, 503)
        self.pool.most_owed, self.pool.assets = None, 20 * pool.UNITS
        self.refused("FUNDS", self.pool.work, BOB)
        self.pool.assets = None
        self.pool.work(BOB)
        self.assertAlmostEqual(self.pool.accounts[ALICE]["expected"], 1 / 16)
        self.pool.accounts[ALICE]["expected"] = 7.0                  # seven blocks' worth of shares and no block
        self.assertEqual(self.refused("LUCK", self.pool.work, ALICE).status, 403)
        self.pool.accounts[ALICE]["blocks"] = 1
        self.pool.work(ALICE)
        self.pool.expected_total, self.pool.found_total = 14.0, 0    # all the pool's shares together found nothing
        self.assertEqual(self.refused("LUCK", self.pool.work, BOB).status, 503)
        self.pool.found_total = 3
        self.pool.work(BOB)

    def test_a_pool_not_open_to_others_yet_gives_work_only_to_its_miners(self):
        p = pool.Pool(self.node, "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), miners=[ALICE.upper() + "\n", ""])
        p.work(ALICE)
        p.work(ALICE.upper())
        with self.assertRaises(pool.PoolError) as caught:
            p.work(BOB)
        self.assertEqual((caught.exception.kind, caught.exception.status), ("CLOSED", 403))
        self.assertIsNone(self.pool.miners)                        # without the list anybody may mine
        self.pool.work(BOB)

    def test_a_share_for_a_tip_that_moved_is_stale_at_once(self):
        work = self.pool.work(ALICE)
        good = search(work, want_block=False)
        self.node.height, self.node.previous = 3001, "22" * 32      # somebody else found a block
        self.now[0] += pool.TIP_AGE                                  # well within the life of a template
        self.refused("STALE", self.pool.share, work["job"], good)
        self.assertEqual(self.pool.account(ALICE)["shares"], 0)
        self.assertEqual(self.pool.work(ALICE)["height"], 3001)

    def test_the_ledger_is_what_the_pool_owes(self):
        work = self.pool.work(ALICE)
        first = search(work, want_block=False)
        self.pool.share(work["job"], first)
        self.pool.share(work["job"], search(work, want_block=False, start=first + 1))
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
        status, stats = self.get("/v1/stats")
        self.assertEqual((status, stats["coin"], stats["pool"]["miners"], stats["pool"]["fee_percent"], stats["chain"]["height"]),
                         (200, "NORTH", 1, 5, 3000))
        self.assertEqual(self.get("/v1/pool")[1]["speed"]["windows"]["600"]["shares"], 1)
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


class Speed(unittest.TestCase):
    """The speed shown is the shares taken in a window times what a share stands for, over the window: an estimate."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-pool-test-")
        self.node, self.now = FakeNode(), [100000.0]
        self.pool = self.make()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def make(self):
        return pool.Pool(self.node, "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), clock=lambda: self.now[0])

    def take(self, address, n):
        work, start = self.pool.work(address), 0
        for _ in range(n):
            nonce = search(work, want_block=False, start=start)
            self.pool.share(work["job"], nonce)
            start = nonce + 1

    def test_nothing_taken_shows_nothing(self):
        stats = self.pool.stats()
        self.assertEqual((stats["coin"], stats["pool"]["hashrate"], stats["pool"]["miners"], stats["pool"]["blocks_found"]), ("NORTH", 0, 0, 0))
        self.assertIsNone(stats["chain"]["hashrate"])       # no cap, no count of the chain's blocks

    def test_shares_make_the_speed(self):
        self.take(ALICE, 3)
        self.take(BOB, 1)
        per = (1 << 256) / ((16 << 250) + 1)                # about 4 hashes a share at a share target of 2^254
        speed = self.pool.speed()
        self.assertEqual(speed["hashes_a_share"], round(per))
        self.assertEqual((speed["windows"]["600"]["shares"], speed["windows"]["600"]["miners"]), (4, 2))
        self.assertEqual(speed["windows"]["600"]["hashrate"], round(4 * per / 600))
        self.assertEqual(speed["windows"]["3600"]["hashrate"], round(4 * per / 3600))
        self.now[0] += 700                                  # out of the ten minutes, still in the hour
        speed = self.pool.speed()
        self.assertEqual((speed["windows"]["600"]["shares"], speed["windows"]["3600"]["shares"]), (0, 4))
        self.now[0] += 86400                                # out of the day: forgotten
        self.assertEqual(self.pool.speed()["windows"]["86400"]["shares"], 0)
        self.assertEqual(len(self.pool.recent), 0)

    def test_after_a_restart_the_ledger_and_the_template_tell_the_speed(self):
        self.take(ALICE, 2)
        self.now[0] += 60
        again = self.make()                                 # no job handed out yet: what a share stands for comes from the template
        stats = again.stats()
        per = (1 << 256) / ((16 << 250) + 1)
        self.assertEqual((stats["pool"]["hashrate"], stats["pool"]["miners"], stats["pool"]["miners_24h"]), (round(2 * per / 3600), 1, 1))
        self.assertEqual((again.speed()["hashes_a_share"], again.speed()["windows"]["3600"]["shares"]), (round(per), 2))

    def test_the_chain_speed_from_the_cap(self):
        cap = pool.Cap(FakeNode(), own_scripts=())
        cap.last = {86400: (3, 720), 3600: (0, 30)}
        p = pool.Pool(self.node, "regtest", PAY, tempfile.mkdtemp(dir=self.folder), ratio=16, hasher=FakeHash(), clock=lambda: self.now[0], cap=cap)
        p.work(ALICE)
        per = (1 << 256) / ((16 << 250) + 1)
        self.assertEqual(p.stats()["chain"], {"height": 3000, "blocks_24h": 720, "hashrate": round(720 * round(per) * 16 / 86400)})


class FakeChain:
    """A chain for the cap: blocks as (seconds ago, whose), the newest last."""

    def __init__(self, now, blocks):
        self.now, self.blocks, self.calls, self.down = now, blocks, 0, False

    def call(self, method, *params):
        if self.down:
            raise OSError("no node")
        if method == "getbestblockhash":
            return "h%d" % (len(self.blocks) - 1)
        if method == "getblock":
            self.calls += 1
            i = int(params[0][1:])
            ago, whose = self.blocks[i]
            coinbase = "03b80b00" + (pool.TAG.hex() if whose == "pool" else "aa")
            script = "5220" + ("77" * 32 if whose == "own" else "88" * 32)
            outs = [{"value": 512, "scriptPubKey": {"hex": script}}]
            if whose == "framed":           # a stranger's block with outputs of nothing to the owner's address
                outs = [{"value": 0, "scriptPubKey": {"hex": "5220" + "77" * 32}}] + outs + [{"value": 0, "scriptPubKey": {"hex": "5220" + "77" * 32}}]
            block = {"time": self.now - ago, "tx": [{"vin": [{"coinbase": coinbase}], "vout": outs}]}
            if i:
                block["previousblockhash"] = "h%d" % (i - 1)
            return block
        raise OSError(method)


class CapRules(unittest.TestCase):
    def cap(self, blocks, **more):
        self.now = [100000.0]
        self.chain = FakeChain(self.now[0], blocks)
        more.setdefault("stops", ((3600, 32, 30), (86400, 32, 30)))
        return pool.Cap(self.chain, [bytes.fromhex("5220" + "77" * 32)], clock=lambda: self.now[0],
                        is_ours=lambda h: self.chain.blocks[int(h[1:])][1] == "pool", **more)

    def test_the_pool_and_the_owner_count_together(self):
        day = [(90000, "x")] + [(80000 - i * 100, "x") for i in range(60)]        # one block older than a day, 60 within it
        hour = [(3000 - i * 100, w) for i, w in enumerate(["pool"] * 4 + ["own"] * 3 + ["x"] * 13)]
        cap = self.cap(day + hour)
        self.assertEqual(cap.measure(), {3600: (7, 20), 86400: (7, 80)})
        self.assertTrue(cap.is_closed())                              # 35 % of the last hour
        self.assertEqual(cap.shown()["windows"]["3600"], {"ours": 7, "blocks": 20, "stop_percent": 32, "resume_percent": 30})
        # as the pool is started: 32 % is for the day, an hour stops it only at 40 %; and a tag alone is nobody's block
        usual = pool.Cap(self.chain, [], clock=lambda: self.now[0])
        self.assertEqual(usual.measure(), {86400: (0, 80), 3600: (0, 20)})
        usual = self.cap(day + hour, stops=((86400, 32, 30), (3600, 40, 38)))
        self.assertFalse(usual.is_closed())                           # 35 % of the hour, 9 % of the day
        usual = self.cap(day + hour + [(50, "pool")] * 2, stops=((86400, 32, 30), (3600, 40, 38)))
        self.assertTrue(usual.is_closed())                            # 9 of 22 in the hour

    def test_it_opens_again_only_below_the_lower_mark(self):
        cap = self.cap([(3000 - i * 100, w) for i, w in enumerate(["pool"] * 7 + ["x"] * 13)])
        self.assertTrue(cap.is_closed())
        seen = self.chain.calls
        self.assertTrue(cap.is_closed())
        self.assertEqual(self.chain.calls, seen)                      # asked again within the minute: the node is left alone
        for others, closed in ((2, True), (3, False)):                # 7 of 22 is 31.8 %: still closed; 7 of 24 is 29.2 %: open
            self.chain.blocks += [(50, "x")] * (others if closed else 2)
            self.now[0] += 60
            self.chain.now = self.now[0]
            self.assertEqual(cap.is_closed(), closed)
        self.assertEqual(self.chain.calls, seen + 6)       # the old ones are remembered, young ones asked about again;                  # only the new blocks were read

    def test_a_stranger_cannot_close_the_pool(self):
        cap = self.cap([(2000 - i * 100, "framed") for i in range(12)])
        self.assertEqual(cap.measure()[3600], (0, 12))
        self.assertFalse(cap.is_closed())

    def test_few_blocks_say_nothing_and_no_node_changes_nothing(self):
        cap = self.cap([(100, "pool")] * 9)
        self.assertFalse(cap.is_closed())                             # nine blocks, all ours: fewer than ten says nothing
        cap = self.cap([(100, "pool")] * 10)
        self.assertTrue(cap.is_closed())
        self.chain.down = True
        self.now[0] += 60
        self.assertTrue(cap.is_closed())

    def test_a_closed_pool_hands_out_no_work(self):
        folder = tempfile.mkdtemp(prefix="polarka-pool-test-")
        try:
            cap = self.cap([(100, "pool")] * 10)
            p = pool.Pool(FakeNode(), "regtest", PAY, folder, ratio=16, hasher=FakeHash(), cap=cap)
            with self.assertRaises(pool.PoolError) as caught:
                p.work(ALICE)
            self.assertEqual((caught.exception.kind, caught.exception.status), ("CAP", 503))
            self.assertTrue(p.info()["cap"]["closed"])
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class GuardRules(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-pool-test-")
        self.now = [1000.0]
        self.guard = pool.Guard(work_a_minute=5, addresses_a_block=2, bad_shares=3, ban=600, clock=lambda: self.now[0])
        self.pool = pool.Pool(FakeNode(), "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), clock=lambda: self.now[0], guard=self.guard)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def kind(self, call, *a):
        with self.assertRaises(pool.PoolError) as caught:
            call(*a)
        return caught.exception.kind

    def test_places(self):
        self.assertEqual(pool.place("203.0.113.9"), "203.0.113.9")
        self.assertEqual(pool.place("::ffff:203.0.113.9"), "203.0.113.9")
        self.assertEqual(pool.place("2001:db8:1:2::5"), pool.place("2001:db8:1:ffff::9"))      # one customer's /48
        self.assertNotEqual(pool.place("2001:db8:1::5"), pool.place("2001:db8:2::5"))
        self.assertEqual(pool.place("nonsense"), "?")
        self.assertEqual(pool.place("203.0.113.9:4711"), "203.0.113.9")
        self.assertEqual(pool.place("[2001:db8:1::5]:4711"), pool.place("2001:db8:1::5"))
        self.assertIsNone(pool.place("127.0.0.1"))                  # a proxy that was not named is nobody's place
        self.assertIsNone(pool.place("::1"))
        for _ in range(10):
            self.guard.wrong("127.0.0.1")
        self.guard.check("127.0.0.1")                                # and nobody is shut out for what came through it

    def test_asking_too_often_or_for_too_many(self):
        carol = encode_address("rnorth", 2, bytes([11]) * 32)
        self.pool.work(ALICE, "203.0.113.9")
        self.pool.work(BOB, "203.0.113.9")
        self.assertEqual(self.kind(self.pool.work, carol, "203.0.113.9"), "SLOW")       # a third address in one block
        self.pool.work(carol, "203.0.113.10")                                           # another place is not bound by it
        for _ in range(2):
            self.pool.work(ALICE, "203.0.113.9")
        self.assertEqual(self.kind(self.pool.work, ALICE, "203.0.113.9"), "SLOW")       # the sixth question in a minute
        self.now[0] += 60
        self.pool.work(ALICE, "203.0.113.9")

    def test_wrong_shares_close_the_door_for_a_while(self):
        work = self.pool.work(ALICE, "203.0.113.9")
        header, key, share = bytes.fromhex(work["header"]), bytes.fromhex(work["seed"]), int(work["share_target"], 16)
        low = [n for n in range(10000) if int.from_bytes(hashlib.sha256(key + header + n.to_bytes(4, "little")).digest(), "little") > share][:3]
        for nonce in low:
            self.assertEqual(self.kind(self.pool.share, work["job"], nonce, "203.0.113.9"), "LOW")
        good = search(work, want_block=False)
        self.assertEqual(self.kind(self.pool.share, work["job"], good, "203.0.113.9"), "BANNED")
        self.assertEqual(self.kind(self.pool.work, ALICE, "203.0.113.9"), "BANNED")
        self.assertTrue(self.pool.share(work["job"], good, "203.0.113.10")["ok"])        # the share itself was good
        self.now[0] += 600
        self.pool.work(ALICE, "203.0.113.9")
        # stale and duplicate shares happen to honest miners and are not counted against them
        for _ in range(5):
            self.assertEqual(self.kind(self.pool.share, work["job"], good, "203.0.113.9"), "DUPLICATE")
        self.pool.work(ALICE, "203.0.113.9")


class Owed(unittest.TestCase):
    def test_the_account_shows_what_was_paid_and_what_is_on_the_way(self):
        from polarka_agent import payout
        from tests.test_payout import FakeWallet
        folder = tempfile.mkdtemp(prefix="polarka-pool-test-")
        try:
            wallet = FakeWallet()
            p = pool.Pool(FakeNode(), "regtest", PAY, folder, ratio=16, hasher=FakeHash(), payouts=payout.Payouts(wallet, folder, minimum=pool.UNITS))
            work = p.work(ALICE)
            first = search(work, want_block=False)
            p.share(work["job"], first)
            self.assertEqual(p.pay_round()["paid_now"], "30.40000000")
            p.share(work["job"], search(work, want_block=False, start=first + 1))
            account = p.account(ALICE)
            self.assertEqual((account["earned"], account["paid"], account["on_the_way"], account["owed"]),
                             ("60.80000000", "0.00000000", "30.40000000", "30.40000000"))
            wallet.made[0]["state"] = "in_block"
            p.pay_round()
            account = p.account(ALICE)
            self.assertEqual((account["paid"], account["on_the_way"], account["owed"]), ("30.40000000", "30.40000000", "0.00000000"))
            info = p.info()
            self.assertTrue(info["pays_out"])
            self.assertEqual((info["earned"], info["paid"], info["on_the_way"], info["owed"]),
                             ("60.80000000", "30.40000000", "30.40000000", "0.00000000"))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def make_paying(self, folder, wallet):
        from polarka_agent import payout
        return pool.Pool(FakeNode(), "regtest", PAY, folder, ratio=16, hasher=FakeHash(), payouts=payout.Payouts(wallet, folder, minimum=pool.UNITS))

    def test_an_address_held_for_bad_luck_is_not_paid_until_a_person_lets_it_back(self):
        """The pool decides who is held (pay_round), the payouts only leave them out: the two together."""
        from tests.test_payout import FakeWallet
        folder = tempfile.mkdtemp(prefix="polarka-pool-test-")
        try:
            wallet = FakeWallet()
            p = self.make_paying(folder, wallet)
            for who in (ALICE, BOB):
                work = p.work(who)
                p.share(work["job"], search(work, want_block=False))
            p.accounts[ALICE]["expected"] = 7.0                          # seven blocks' worth of shares and no block
            done = p.pay_round()
            self.assertEqual((done["payees"], done["paid_now"]), (1, "30.40000000"))
            self.assertEqual([a for a, _ in wallet.made[0]["outputs"]], [BOB])
            self.assertEqual(p.account(ALICE)["owed"], "30.40000000")
            wallet.made[0]["state"] = "in_block"
            self.assertEqual(p.pay_round()["state"], "nothing is due")    # Alice is still held: nothing for her
            self.assertEqual(len(wallet.made), 1)
            with open(os.path.join(folder, "luck-cleared.txt"), "w") as f:
                f.write(ALICE + "\n")                                     # the owner looked and let her back in
            p = self.make_paying(folder, wallet)
            done = p.pay_round()
            self.assertEqual((done["payees"], done["paid_now"]), (1, "30.40000000"))
            self.assertEqual([a for a, _ in wallet.made[1]["outputs"]], [ALICE])
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_the_surplus_goes_with_the_rounds_and_never_when_one_is_refused(self):
        from polarka_agent import payout
        from tests.test_payout import FakeWallet
        folder = tempfile.mkdtemp(prefix="polarka-pool-test-")
        try:
            wallet = FakeWallet()                                            # 1000 NORTH free, 500 a payment
            surplus = payout.Surplus(PAY, keep=900 * pool.UNITS, most=500 * pool.UNITS, every=0, least=pool.UNITS)
            p = pool.Pool(FakeNode(), "regtest", PAY, folder, ratio=16, hasher=FakeHash(),
                          payouts=payout.Payouts(wallet, folder, minimum=pool.UNITS, surplus=surplus))
            work = p.work(ALICE)
            first = search(work, want_block=False)
            p.share(work["job"], first)                                      # Alice is owed 30.4
            wallet.refuse = "OVER_DAY"
            done = p.pay_round()
            self.assertEqual((done["state"], "surplus" in done), ("refused", False))
            wallet.refuse = None
            done = p.pay_round()                                             # her round, and then the surplus beside it
            self.assertEqual((done["round"], done["paid_now"]), (1, "30.40000000"))
            # 1000 free (the made-up wallet does not take the round off), 900 kept, nothing owed that is not on the way
            self.assertEqual(done["surplus"]["surplus"], "99.99000000")
            self.assertEqual([x["memo"] for x in wallet.made], [p.payouts.memo(1), payout.SURPLUS])
            self.assertEqual(p.account(ALICE)["on_the_way"], "30.40000000")
            p.share(work["job"], search(work, want_block=False, start=first + 1))     # owed again: 30.4 more is kept back
            self.assertEqual(p.pay_round()["surplus"]["surplus"], "69.59000000")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_what_is_on_the_way_is_not_owed_any_more_also_after_a_restart(self):
        """flying_total: a round on the way has left the wallet, so it does not count against the most the pool
        may owe; and a pool that starts again knows it from the ledger before its first round."""
        from tests.test_payout import FakeWallet
        folder = tempfile.mkdtemp(prefix="polarka-pool-test-")
        try:
            wallet = FakeWallet()
            p = self.make_paying(folder, wallet)
            work = p.work(ALICE)
            p.share(work["job"], search(work, want_block=False))
            p.most_owed = 30 * pool.UNITS                                 # one share is 30.4
            with self.assertRaises(pool.PoolError) as caught:
                p.work(BOB)
            self.assertEqual(caught.exception.kind, "FUNDS")
            self.assertEqual(p.pay_round()["paid_now"], "30.40000000")
            self.assertEqual((p.flying_total, p.paid_total), (3_040_000_000, 0))
            p.work(BOB)                                                   # nothing is owed: the round is on the way
            again = self.make_paying(folder, wallet)
            self.assertEqual((again.flying_total, again.paid_total), (3_040_000_000, 0))
            again.most_owed = 30 * pool.UNITS
            again.work(BOB)
            wallet.made[0]["state"] = "in_block"
            again.pay_round()
            self.assertEqual((again.flying_total, again.paid_total), (0, 3_040_000_000))
            again.work(BOB)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
