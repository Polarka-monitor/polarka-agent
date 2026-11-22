"""Stratum: what the pool sends is what IronMiner reads, and what it sends back is checked and paid like any share.

`ironminer_header` and `ironminer_target` do step by step what truenorth-ironminer's C code does (util.c
stratum_notify and diff_to_hash, cpu-miner.c std_build_extraheader, std_build_block_header and
std_le_build_stratum_request, randomx-miner.cpp scanhash_randomx; read at 78f3afd on 22 November 2026), so that
the byte orders are tested against the miner's code and not against the pool's own idea of them. The node and the
proof of work are made up here (SHA-256 for RandomX) as in test_pool.py; the real ones are in test_stratum_node.py."""
import hashlib
import json
import math
import os
import random
import shutil
import socket
import tempfile
import threading
import time
import unittest
from fractions import Fraction

from polarka_agent import pool, rx, stratum

from .test_pool import ALICE, BOB, PAY, FakeHash, FakeNode

UNITS = pool.UNITS


def dsha(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def ironminer_header(notify, extranonce1_hex, extranonce2, n):
    """The 80 bytes IronMiner hashes for work n of a job, and the nonce and time as it submits them."""
    job_id, prevhash, coinb1, coinb2, merkle, version, nbits, stime, clean = notify
    le32dec, be32dec = (lambda b: int.from_bytes(b, "little")), (lambda b: int.from_bytes(b, "big"))
    coinbase = bytes.fromhex(coinb1) + bytes.fromhex(extranonce1_hex) + extranonce2 + bytes.fromhex(coinb2)
    merkle_root = dsha(coinbase)                    # sha256d_gen_merkle_root
    for m in merkle:
        merkle_root = dsha(merkle_root + bytes.fromhex(m))
    prev = bytes.fromhex(prevhash)
    data = [0] * 20
    data[0] = le32dec(bytes.fromhex(version))       # std_build_extraheader: le32dec(job.version)
    for i in range(8):
        data[1 + i] = le32dec(prev[4 * i:4 * i + 4])         # have_stratum: le32dec(prevhash + i)
        data[9 + i] = be32dec(merkle_root[4 * i:4 * i + 4])  # be32dec(merkle_tree + i)
    data[17] = le32dec(bytes.fromhex(stime))
    data[18] = le32dec(bytes.fromhex(nbits))
    data[19] = n
    header = b"".join(w.to_bytes(4, "big") for w in data)    # scanhash_randomx: put_word, big-endian, nonce too
    nonce_hex = data[19].to_bytes(4, "little").hex()          # std_le_build_stratum_request: le32enc, bin2hex
    time_hex = data[17].to_bytes(4, "little").hex()
    return header, time_hex, nonce_hex


def ironminer_target(diff, int128=True):
    """diff_to_hash() in util.c, both ways it is built; exact where the long double is (powers of two)."""
    m = Fraction(1) / Fraction(diff)
    if int128:
        return (int(m * (1 << 96)) << 128) | ((1 << 128) - 1)
    m *= 1 << 32
    top = int(m)
    return (top << 192) | (int((m - top) * (1 << 64)) << 128) | ((1 << 128) - 1)


class Shape(unittest.TestCase):
    def test_the_target_of_a_difficulty_is_the_miners(self):
        for k in range(stratum.HARDEST, stratum.EASIEST + 1):
            sent = json.loads(json.dumps(stratum.difficulty(k)))         # through json, as the miner reads it
            self.assertEqual(sent, stratum.difficulty(k))
            self.assertEqual(ironminer_target(sent), stratum.share_target(k))
            self.assertEqual(ironminer_target(sent, int128=False), stratum.share_target(k))
            self.assertLess(stratum.share_target(k), pool.MOST + 1)

    def test_the_bounds(self):
        network = 1 << 230
        low, high = stratum.bounds(network, 2048)
        self.assertLessEqual(stratum.share_target(high), network * 2048)
        self.assertGreater(stratum.share_target(high + 1), network * 2048)
        self.assertGreaterEqual(stratum.share_target(low), network * stratum.MIN_ODDS)
        self.assertLess(stratum.share_target(low - 1), network * stratum.MIN_ODDS)
        self.assertEqual(stratum.bounds(rx.target_of(0x207fffff), 2048), (31, 31))        # regtest: only the easiest
        for ratio in (1, 2, 3):                 # never a share target below the network's (review 11, I3)
            low, high = stratum.bounds(network, ratio)
            self.assertLessEqual(low, high)
            self.assertGreaterEqual(stratum.share_target(low), network)
        self.assertEqual(stratum.bounds(network, 1), (6, 6))        # 2^230 + 2^128 - 1: a little easier than a block

    def test_the_branch_makes_the_merkle_root(self):
        rng = random.Random(5)
        for n in range(0, 13):
            txids = [bytes(rng.getrandbits(8) for _ in range(32)) for _ in range(n)]
            coinbase = bytes(rng.getrandbits(8) for _ in range(32))
            self.assertEqual(stratum.root(coinbase, stratum.branch(txids)), pool.merkle_root([coinbase] + txids))

    def template(self, txs=()):
        node = FakeNode()
        node.transactions = list(txs)
        return node.call("getblocktemplate", {})

    def test_the_coinbase_cut_in_two(self):
        txs = [{"txid": "%064x" % (i + 1), "hash": "%064x" % (i + 100), "data": "00", "fee": 1000} for i in range(3)]
        template = self.template(txs)
        script = pool.script_for_address("regtest", PAY)
        job = stratum.Job(1, template, bytes(32), 0, script, 0)
        issue, xn1, xn2 = bytes.fromhex("00000007"), bytes.fromhex("0a0b0c0d"), bytes.fromhex("01020304")
        extranonce = int.from_bytes(xn1 + xn2, "little")
        plain = pool.coinbase(template["height"], template["coinbasevalue"], script, extranonce, pool.commitment_of(template), False,
                              stratum.MARK + issue)
        self.assertEqual(job.coinb1(issue) + xn1 + xn2 + job.tail, plain)
        # the same extranonce over HTTP makes another coinbase: no header can be both
        self.assertNotEqual(pool.coinbase(template["height"], template["coinbasevalue"], script, extranonce, pool.commitment_of(template), False), plain)
        # the header is the one the pool would make of the whole coinbase, and the block carries that coinbase
        header = job.header(issue, xn1, xn2, 7)
        self.assertEqual(header, pool.assemble(template, script, extranonce, stratum.MARK + issue)[0] + (7).to_bytes(4, "little"))
        self.assertNotEqual(job.header(bytes.fromhex("00000008"), xn1, xn2, 7), header)     # another issue, another header
        self.assertEqual(job.reward, 512 * UNITS)
        block = job.block(issue, xn1, xn2, header, script)
        self.assertEqual(block[:80], header)
        self.assertIn(plain[:4] + b"\x00\x01" + plain[4:-4], block)       # the coinbase with its witness

    def test_the_miner_hashes_the_header_the_pool_checks(self):
        txs = [{"txid": "%064x" % (i + 7), "hash": "%064x" % (i + 9), "data": "00", "fee": 0} for i in range(5)]
        template = self.template(txs)
        template["version"], template["previousblockhash"] = 0x20000004, "00000000a1b2c3d4" + "e5f60718" * 6
        script = pool.script_for_address("regtest", PAY)
        job = stratum.Job(1, template, bytes(32), 0, script, 0)
        issue = bytes.fromhex("01020304")
        for n in (0, 1, 0x01020304, 0xfffffffe):
            header, time_hex, nonce_hex = ironminer_header(job.notify("1", issue, True), "0a0b0c0d", b"\x00\x00\x00\x05", n)
            self.assertEqual(int(time_hex, 16), job.time)               # the time it sends back is the job's
            self.assertEqual(header, job.header(issue, bytes.fromhex("0a0b0c0d"), b"\x00\x00\x00\x05", int(nonce_hex, 16)))
            self.assertEqual(header[4:36], bytes.fromhex(template["previousblockhash"])[::-1])
            self.assertEqual(header[:4], (0x20000004).to_bytes(4, "little"))
            self.assertEqual(header[72:76], bytes.fromhex(template["bits"])[::-1])
            self.assertEqual(header[68:72], template["curtime"].to_bytes(4, "little"))


class Client:
    """A miner over a real socket, as plain as can be."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.buf, self.notes, self.rid = b"", [], 10

    def close(self):
        self.sock.close()

    def send_raw(self, data):
        self.sock.sendall(data)

    def line(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("the pool closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, method, params):
        self.rid += 1
        self.sock.sendall((json.dumps({"id": self.rid, "method": method, "params": params}) + "\n").encode())
        while True:
            said = self.line()
            if said.get("method"):
                self.notes.append(said)
            elif said.get("id") == self.rid:
                return said

    def note(self, method):
        for i, said in enumerate(self.notes):
            if said["method"] == method:
                return self.notes.pop(i)["params"]
        while True:
            said = self.line()
            if said.get("method") == method:
                return said["params"]
            if said.get("method"):
                self.notes.append(said)

    def start(self, user):
        subscribed = self.call("mining.subscribe", ["test/1.0"])["result"]
        self.extranonce1, size = subscribed[1], subscribed[2]
        authorized = self.call("mining.authorize", [user, "x"])
        if authorized["result"] is not True:
            return authorized
        self.k = round(-math.log2(self.note("mining.set_difficulty")[0]))
        seed = self.note("mining.set_randomx_seed")
        self.key = bytes.fromhex(seed[0])[::-1]
        self.job = self.note("mining.notify")
        return authorized


class OverTheLine(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-stratum-test-")
        self.node = FakeNode()
        self.pool = self.make()
        self.stratum = stratum.Stratum(self.pool, "127.0.0.1:0", welcome="hello {address}")
        self.port = self.stratum.start()
        self.clients = []

    def make(self, **k):
        return pool.Pool(self.node, "regtest", PAY, self.folder, ratio=16, fee_percent=5, hasher=FakeHash(), **k)

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.stratum.stop()
        shutil.rmtree(self.folder, ignore_errors=True)

    def client(self, user=ALICE):
        c = Client(self.port)
        self.clients.append(c)
        c.answered = c.start(user)
        return c

    def find(self, c, block, start=0, job=None, extranonce2=b"\x00\x00\x00\x01"):
        """A nonce for the client's job by the made-up proof of work: a share, and a block or not as asked."""
        job = job or c.job
        network = rx.target_of(int(job[6], 16))
        for n in range(start, start + 200000):
            header, time_hex, nonce_hex = ironminer_header(job, c.extranonce1, extranonce2, n)
            proof = int.from_bytes(hashlib.sha256(c.key + header).digest(), "little")
            if proof <= stratum.share_target(c.k) and (proof <= network) == block:
                return [ALICE, job[0], extranonce2.hex(), time_hex, nonce_hex], n, header
        raise AssertionError("no nonce found")

    def ledger(self):
        with open(os.path.join(self.folder, "pool-shares.jsonl")) as f:
            return [json.loads(line) for line in f]

    def test_a_share_from_subscribe_to_the_ledger(self):
        c = self.client()
        self.assertEqual(c.answered["result"], True)
        self.assertEqual(c.note("client.show_message"), ["hello " + ALICE])
        self.assertEqual(len(bytes.fromhex(c.extranonce1)), 4)
        self.assertEqual(c.k, 29)                       # the easiest a share may be: 2^253 + 2^128 - 1 <= 16 * 2^250
        self.assertEqual(c.key, bytes.fromhex("%064x" % 2048)[::-1])        # the key for height 3000, as the header holds it
        self.assertEqual(c.job[8], True)
        submit, n, header = self.find(c, block=False)
        self.assertEqual(c.call("mining.submit", submit), {"id": c.rid, "result": True, "error": None})
        entry = self.ledger()[-1]
        self.assertEqual((entry["address"], entry["via"], entry["height"], entry["accepted"]), (ALICE, "stratum", 3000, None))
        share = stratum.share_target(29)
        network = rx.target_of(int(c.job[6], 16))
        self.assertEqual(entry["value"], 512 * UNITS * network // share * 95 // 100)
        self.assertEqual((entry["odds"], entry["work"]), (share // network, stratum.hashes(29)))
        self.assertEqual(self.pool.account(ALICE)["shares"], 1)
        # the same share again, under the same job or another one of the same tip
        self.assertEqual(c.call("mining.submit", submit)["error"][0], 22)
        stats = self.pool.stats()
        self.assertEqual(stats["pool"]["stratum"], {"port": self.port, "connections": 1, "addresses": 1})
        self.assertEqual(stats["pool"]["hashrate_10m"], round(stratum.hashes(29) / 600))

    def test_what_is_not_a_share(self):
        c = self.client()
        submit, n, header = self.find(c, block=False)
        low = list(submit)
        for m in range(n + 1, n + 100000):          # a nonce whose hash is above the target
            header, time_hex, nonce_hex = ironminer_header(c.job, c.extranonce1, b"\x00\x00\x00\x01", m)
            if int.from_bytes(hashlib.sha256(c.key + header).digest(), "little") > stratum.share_target(c.k):
                low[4] = nonce_hex
                break
        self.assertEqual(c.call("mining.submit", low)["error"][0], 23)
        self.assertEqual(c.call("mining.submit", [BOB] + submit[1:])["error"][0], 24)       # not authorized here
        self.assertEqual(c.call("mining.submit", submit[:1] + ["ff"] + submit[2:])["error"][0], 21)
        self.assertEqual(c.call("mining.submit", submit[:2] + ["0102"] + submit[3:])["error"][0], 20)
        for later in (1, 601):                  # the time is the job's (review 11, H1)
            self.assertEqual(c.call("mining.submit", submit[:3] + ["%08x" % (int(submit[3], 16) + later)] + submit[4:])["error"][0], 20)
        self.assertEqual(c.call("mining.submit", submit[:4] + ["0x123456"])["error"][0], 20)
        self.assertEqual(c.call("mining.submit", submit[:4])["error"][0], 20)
        self.assertEqual(c.call("mining.unknown", [])["error"][0], 20)
        self.assertEqual(c.call("mining.submit", submit)["result"], True)                   # and the good one still counts
        self.assertEqual(len(self.ledger()), 1)

    def test_a_block_goes_to_the_node_and_everybody_gets_the_new_tip(self):
        c, d = self.client(), self.client(BOB + ".rig2")
        self.assertNotEqual(c.extranonce1, d.extranonce1)
        submit, n, header = self.find(c, block=True)
        self.assertEqual(c.call("mining.submit", submit)["result"], True)
        entry = self.ledger()[-1]
        self.assertEqual((entry["accepted"], entry["block"]), (True, dsha(header)[::-1].hex()))
        block = bytes.fromhex(self.node.sent[-1])
        self.assertEqual(block[:80], header)
        pushed = bytes.fromhex(c.job[2])[-(1 + len(stratum.MARK) + stratum.ISSUE):]     # the push, MARK and the issue
        self.assertEqual(pushed[:1 + len(stratum.MARK)], bytes([len(stratum.MARK) + stratum.ISSUE + 8]) + stratum.MARK)
        self.assertIn(pushed + bytes.fromhex(c.extranonce1) + b"\x00\x00\x00\x01", block)
        for client in (c, d):
            job = client.note("mining.notify")
            self.assertEqual((job[8], job[1]), (True, stratum.swapped(self.node.previous)))
        self.assertEqual(self.pool.stats()["pool"]["blocks_found"], 1)

    def test_a_share_for_a_tip_that_moved_is_stale(self):
        c = self.client()
        submit, n, header = self.find(c, block=False)
        self.node.height, self.node.previous = self.node.height + 1, "22" * 32
        self.pool.tip_seen = None                   # past the second the pool believes its node's word
        self.assertEqual(c.call("mining.submit", submit)["error"][0], 21)
        job = c.note("mining.notify")
        self.assertEqual((job[8], job[1]), (True, stratum.swapped("22" * 32)))
        c.job = job
        self.assertEqual(c.call("mining.submit", self.find(c, block=False)[0])["result"], True)

    def test_who_may_not_mine(self):
        c = Client(self.port)
        self.clients.append(c)
        self.assertEqual(c.call("mining.authorize", [ALICE, "x"])["error"][0], 25)          # subscribe first
        said = c.start("nonsense")
        self.assertEqual(said["error"][0], 24)
        self.assertIn("address", c.note("client.show_message")[0])
        said = c.call("mining.authorize", ["north1zwfk57mzt9v2eqk7dg270drfs7afk5tksq42ga7anpaxackfl36pskryq05", "x"])
        self.assertEqual(said["error"][0], 24)      # an address of another network
        said = c.call("mining.authorize", [ALICE[:6].upper() + ALICE[6:], "x"])
        self.assertEqual(said["error"][0], 24)      # written in both cases at once: refused, as over HTTP (review 11, I2)
        self.assertEqual(c.call("mining.authorize", [ALICE + ".a", "x"])["result"], True)
        self.assertEqual(c.call("mining.extranonce.subscribe", [])["result"], False)

    def test_a_line_that_is_no_line_ends_the_connection(self):
        c = Client(self.port)
        self.clients.append(c)
        c.send_raw(b"{" * 5000 + b"\n")
        with self.assertRaises((EOFError, ConnectionError)):
            c.line()
        d = Client(self.port)
        self.clients.append(d)
        d.send_raw(b"not json\n")
        with self.assertRaises((EOFError, ConnectionError)):
            d.line()
        self.assertEqual(self.client().answered["result"], True)        # and the pool goes on

    def test_a_socket_made_elsewhere(self):
        """What systemd hands over in socket activation: a socket that listens already."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        other = stratum.Stratum(self.make(), "systemd", sock=sock)
        try:
            self.assertEqual(other.start(), sock.getsockname()[1])
            c = Client(other.port)
            self.clients.append(c)
            self.assertEqual(c.start(ALICE)["result"], True)
        finally:
            other.stop()
        saved = {k: os.environ.get(k) for k in ("LISTEN_PID", "LISTEN_FDS")}
        try:
            os.environ["LISTEN_PID"], os.environ["LISTEN_FDS"] = str(os.getpid() + 1), "1"
            self.assertIsNone(stratum.inherited())                  # meant for another process
            os.environ["LISTEN_PID"], os.environ["LISTEN_FDS"] = str(os.getpid()), "0"
            self.assertIsNone(stratum.inherited())
            with self.assertRaises(ValueError):
                stratum.Stratum(self.pool, "systemd")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class FakeWriter:
    class Transport:
        def get_write_buffer_size(self):
            return 0

    def __init__(self):
        self.said, self.transport, self.closed = [], FakeWriter.Transport(), False

    def write(self, data):
        self.said.append(json.loads(data))

    def close(self):
        self.closed = True


class Rules(unittest.TestCase):
    """The difficulty, the limits and what the pool refuses, without a socket: a network target of 2^220."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-stratum-test-")
        self.node, self.now = FakeNode(), [1000.0]
        self.guard = pool.Guard(bad_shares=3, clock=lambda: self.now[0])
        self.pool = pool.Pool(self.node, "regtest", PAY, self.folder, ratio=2048, hasher=FakeHash(), clock=lambda: self.now[0],
                              guard=self.guard)
        self.s = stratum.Stratum(self.pool, "127.0.0.1:0")
        template = self.node.call("getblocktemplate", {})
        template["bits"] = "1c100000"
        self.s.job = stratum.Job(1, template, bytes(32), 0, self.pool.pay_script, self.now[0])
        self.s.number = 1

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def miner(self, source="203.0.113.9"):
        m = stratum.Miner(FakeWriter(), source, b"\x00\x00\x00\x01")
        m.workers = {ALICE: ALICE}
        self.s._give(m, self.s.job, True)
        return m

    def test_the_difficulty_follows_the_miner(self):
        self.assertEqual(stratum.bounds(1 << 220, 2048), (0, 6))
        m = self.miner()
        self.assertEqual(m.k, 6)                                    # it starts at the easiest share
        self.assertEqual([s["method"] for s in m.writer.said], ["mining.set_difficulty", "mining.set_randomx_seed", "mining.notify"])
        for _ in range(stratum.RETUNE_SHARES // 2):                 # a share a second: fifteen times too fast,
            self.now[0] += 1
            m.found.append(stratum.hashes(m.k))
        self.assertFalse(self.s._retune(m))                         # but six shares in six seconds say too little
        for _ in range(stratum.RETUNE_SHARES // 2):
            self.now[0] += 1
            m.found.append(stratum.hashes(m.k))
        self.assertTrue(self.s._retune(m))
        self.assertEqual(m.k, 2)                                    # about 2^4 harder, as far as one step goes
        self.s._give(m, self.s.job, False)
        self.assertEqual(m.writer.said[-2]["params"], [0.25])
        self.assertEqual(m.writer.said[-1]["params"][8], False)
        self.assertEqual(m.jobs[m.writer.said[-1]["params"][0]][1], 2)        # the job given now is paid at its own k
        self.assertEqual(m.jobs["1"][1], 6)                                     # and the one before at the old one
        self.assertNotEqual(m.jobs["1"][2], m.jobs["2"][2])                     # each with an issue, and so headers, of its own
        self.now[0] += 59
        self.assertFalse(self.s._retune(m))                         # quiet, but not long enough
        self.now[0] += 1
        self.assertTrue(self.s._retune(m))                          # a minute without a share: easier
        self.assertEqual(m.k, 3)
        m.k = -10
        self.s._give(m, self.s.job, False)
        self.assertEqual(m.k, 0)                                    # never harder than 1/16 of a block
        m.k = None
        self.s._suggest(m, ["0.0156"])
        self.assertEqual(m.k, 6)
        self.s._suggest(m, [10 ** 400])                             # no float: passed over (review 11, L1)
        self.assertEqual(m.k, 6)

    def test_a_difficulty_decided_on_too_little_does_not_move(self):
        """Review 11, M1: half the shares a decision needs, over five minutes, and only by a whole step or more."""
        m = self.miner()
        m.k = 2
        self.s._give(m, self.s.job)
        for _ in range(stratum.RETUNE_SHARES // 2):                 # a share a minute: four times too slow at k = 2
            self.now[0] += 60
            m.found.append(stratum.hashes(2))
        self.assertTrue(self.s._retune(m))
        self.assertEqual(m.k, 4)                                    # four times easier
        self.s._give(m, self.s.job)
        for _ in range(stratum.RETUNE_SHARES):                      # a share every 16 seconds at k = 4: near enough
            self.now[0] += 16
            m.found.append(stratum.hashes(4))
        self.assertFalse(self.s._retune(m))
        self.assertEqual(m.k, 4)

    def test_a_connection_that_sends_too_much(self):
        m = self.miner()
        self.assertTrue(all(self.s._spend(m) for _ in range(int(stratum.BURST))))
        self.assertFalse(self.s._spend(m))
        self.now[0] += 0.1
        self.assertTrue(self.s._spend(m))
        self.assertFalse(self.s._spend(m))

    def test_wrong_shares_close_the_place_for_a_while(self):
        m = self.miner()
        for _ in range(2):
            self.s._wrong(m, 4, 23, "low")
            self.assertFalse(m.closed)
        self.s._wrong(m, 4, 23, "low")
        self.assertTrue(m.closed and m.writer.closed)
        self.assertEqual(m.writer.said[-1]["method"], "client.show_message")
        with self.assertRaises(pool.PoolError):
            self.guard.check("203.0.113.9")
        local = self.miner(source="127.0.0.1")                      # the pool's own machine is nobody's place
        for _ in range(5):
            self.s._wrong(local, 4, 23, "low")
        self.assertFalse(local.closed)

    def test_what_the_pool_refuses_comes_with_the_next_job(self):
        self.assertIsNone(self.s._refresh({ALICE}))                 # the newest job is new and on the tip
        self.now[0] += stratum.JOB_REFRESH
        self.assertEqual(self.s._refresh({ALICE}), {})
        self.assertEqual(self.s.job.number, 2)
        self.pool.most_owed, self.pool.earned_total = 10, 11
        self.now[0] += stratum.JOB_REFRESH
        self.assertIn("owes", self.s._refresh({ALICE})[ALICE])

        class Closed:
            is_ours, last = None, {}

            def is_closed(self):
                return True
        self.pool.most_owed, self.pool.cap = None, Closed()
        self.node.previous = "33" * 32                              # a new tip: a new job at once
        self.pool.tip_seen = None
        self.assertIn("*", self.s._refresh({ALICE}))
        with self.assertRaises(pool.PoolError) as caught:
            self.s._may_mine("203.0.113.9", ALICE)
        self.assertEqual(caught.exception.kind, "CAP")


class ValueHash:
    """A proof of work whose answer the test chooses: every header hashes to `value`. It keeps what it hashed."""

    def __init__(self):
        self.value, self.hashed = 0, []

    def hash(self, key, header80):
        self.hashed.append(header80)
        return self.value.to_bytes(32, "little")


class OneHeaderOneId(unittest.TestCase):
    """Review 11, H1 and M1: a header is good under one job id only, so the difficulty a hash is paid at is the one
    its id was given with, not the hardest of several the miner holds; and a miner that starts a job over after a
    new difficulty hashes headers it has not hashed before. A network target of 2^240: k from 20 to 26."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-stratum-test-")
        self.node, self.now, self.hasher = FakeNode(), [1000.0], ValueHash()
        self.pool = pool.Pool(self.node, "regtest", PAY, self.folder, ratio=2048, fee_percent=5, hasher=self.hasher,
                              clock=lambda: self.now[0])
        self.s = stratum.Stratum(self.pool, "127.0.0.1:0")
        template = self.node.call("getblocktemplate", {})
        template["bits"] = "1f010000"
        self.s.job = stratum.Job(1, template, bytes(32), 0, self.pool.pay_script, self.now[0])
        self.s.number = 1
        self.m = stratum.Miner(FakeWriter(), "203.0.113.9", b"\x0a\x0b\x0c\x0d")
        self.m.workers = {ALICE: ALICE}

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def check(self, job_id, extranonce2="00000007", nonce=12345, later=0):
        job, k, issue = self.m.jobs[job_id]
        return self.s._check(self.m.extranonce1, ALICE, job, k, issue, extranonce2, "%08x" % (job.time + later), "%08x" % nonce)

    def test_seven_difficulties_and_no_choice(self):
        self.assertEqual(stratum.bounds(self.s.job.network, 2048), (20, 26))
        ids = {}
        for k in range(26, 19, -1):             # what vardiff or suggest_difficulty may give over a few minutes
            self.m.k = k
            self.s._give(self.m, self.s.job, False)
            ids[k] = self.m.writer.said[-1]["params"][0]
        self.assertEqual(len({issue for _, _, issue in self.m.jobs.values()}), 7)
        self.assertEqual(len({note["params"][2] for note in self.m.writer.said if note["method"] == "mining.notify"}), 7)
        # the same numbers under the easiest id and under the hardest are two headers, each found by work of its own,
        # and each paid at its own id's difficulty
        self.hasher.value = stratum.share_target(20)
        self.assertEqual(self.check(ids[26])["work"], stratum.hashes(26))
        self.assertEqual(self.check(ids[20])["work"], stratum.hashes(20))
        self.assertNotEqual(self.hasher.hashed[0], self.hasher.hashed[1])
        with self.assertRaises(pool.PoolError) as caught:
            self.check(ids[26])
        self.assertEqual(caught.exception.kind, "DUPLICATE")
        with self.assertRaises(pool.PoolError) as caught:
            self.check(ids[26], nonce=5, later=60)              # the time of a later job of the same template
        self.assertEqual(caught.exception.kind, "INPUT")

    def test_a_miner_that_starts_over_after_a_new_difficulty_hashes_new_headers(self):
        self.s._give(self.m, self.s.job, True)
        first = self.m.writer.said[-1]["params"]
        self.hasher.value = stratum.share_target(25)            # every header a share, at k = 26 and at k = 25
        # IronMiner: extranonce2 from 0 for a new job id (util.c stratum_notify), nonces from the thread's start
        header, time_hex, nonce_hex = ironminer_header(first, self.m.extranonce1.hex(), b"\x00\x00\x00\x00", 1000)
        self.assertEqual(self.check(first[0], "00000000", int.from_bytes(bytes.fromhex(nonce_hex), "big"))["work"],
                         stratum.hashes(26))
        self.m.k = 25                                           # a new difficulty: the same template under a new id
        self.s._give(self.m, self.s.job, False)
        second = self.m.writer.said[-1]["params"]
        again, time2, nonce2 = ironminer_header(second, self.m.extranonce1.hex(), b"\x00\x00\x00\x00", 1000)
        self.assertNotEqual(again, header)
        self.assertEqual(self.check(second[0], "00000000", int.from_bytes(bytes.fromhex(nonce2), "big"))["work"],
                         stratum.hashes(25))
        self.assertEqual(self.hasher.hashed, [header, again])   # the pool checked what the miner hashed


class Slots(unittest.TestCase):
    """Review 11, H2: no place keeps the others out of stratum, and no slot is held for nothing."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-stratum-test-")
        self.now = [1000.0]
        self.pool = pool.Pool(FakeNode(), "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), clock=lambda: self.now[0])
        self.s = stratum.Stratum(self.pool, "127.0.0.1:0", most=8, per_place=4)
        self.s.most_waiting, self.s.waiting_per_place = 4, 2

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def come(self, source, authorized=True):
        """A connection, as _client lets it in; None when there is no room for it."""
        place = pool.place(source)
        if not self.s._room(place):
            return None
        m = stratum.Miner(FakeWriter(), source, b"\x00\x00\x00\x01", place, self.now[0])
        self.s._enter(m)
        if authorized:
            m.workers = {ALICE: ALICE}
            self.s._authorized(m)
        self.now[0] += 1
        return m

    def test_a_place_that_holds_many_makes_room(self):
        held = [self.come("198.51.100.%d" % (i // 4 + 1)) for i in range(8)]     # two places of four: all taken
        self.assertTrue(all(held))
        self.assertIsNone(self.come("198.51.100.1"))            # a place at its own limit
        newcomer = self.come("203.0.113.9")                     # a place with none: the longest idle of a full place goes
        self.assertIsNotNone(newcomer)
        self.assertTrue(held[0].closed and held[0].writer.closed)
        self.assertEqual(held[0].writer.said[-1]["method"], "client.show_message")
        self.assertEqual((len(self.s.miners), self.s.places["198.51.100.1"], self.s.places["203.0.113.9"]), (8, 3, 1))
        self.assertIsNone(self.come("198.51.100.1"))            # and it does not take the slot back: three against four
        self.assertIsNotNone(self.come("203.0.113.9"))          # the newcomer's place may have one more: one against four
        self.assertIsNone(self.come("203.0.113.9"))             # but not a third: two against three
        self.assertEqual(sum(self.s.places.values()), len(self.s.miners))

    def test_those_not_authorized_yet(self):
        a = [self.come("198.51.100.1", False) for _ in range(2)]
        self.assertIsNone(self.come("198.51.100.1", False))     # two from one place at most
        b = [self.come("198.51.100.2", False) for _ in range(2)]
        self.assertTrue(all(a + b))                             # four in all: the most
        c = self.come("203.0.113.9", False)                     # a place with none waiting: room is made
        self.assertIsNotNone(c)
        self.assertTrue(a[0].closed)
        self.s._authorized(c)
        self.assertEqual(sum(self.s.waiting.values()), 3)
        for m in a[1:] + b + [c]:
            self.s._close(m)
        self.assertEqual((self.s.places, self.s.waiting, self.s.miners), ({}, {}, set()))

    def test_a_connection_without_shares_is_let_go(self):
        m = self.come("198.51.100.1")
        m.active = self.now[0]
        self.now[0] += self.s.idle - 1
        self.s._let_go_idle(self.now[0])
        self.assertFalse(m.closed)
        m.active = self.now[0]                                  # a share the pool took
        self.now[0] += self.s.idle - 1
        self.s._let_go_idle(self.now[0])
        self.assertFalse(m.closed)
        self.now[0] += 1
        self.s._let_go_idle(self.now[0])
        self.assertTrue(m.closed)
        self.assertEqual(m.writer.said[-1]["method"], "client.show_message")
        self.assertEqual((self.s.places, self.s.miners), ({}, set()))


class QuietOverTheLine(unittest.TestCase):
    """A connection that is not authorized in time is let go, however much it says meanwhile (review 11, H2)."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-stratum-test-")
        self.pool = pool.Pool(FakeNode(), "regtest", PAY, self.folder, ratio=16, hasher=FakeHash())
        self.stratum = stratum.Stratum(self.pool, "127.0.0.1:0", quiet=1)
        self.port = self.stratum.start()

    def tearDown(self):
        self.stratum.stop()
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_talk_is_no_authorization(self):
        c = Client(self.port)
        try:
            started = time.monotonic()
            with self.assertRaises((EOFError, ConnectionError)):
                for _ in range(30):
                    c.call("mining.extranonce.subscribe", [])
                    time.sleep(0.2)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            c.close()
        d = Client(self.port)                                   # whoever authorizes in time stays
        try:
            self.assertEqual(d.start(ALICE)["result"], True)
            time.sleep(1.5)
            self.assertEqual(d.call("mining.extranonce.subscribe", [])["result"], False)
        finally:
            d.close()


class TheNodeOutsideTheLock(unittest.TestCase):
    """Review 11, L2: stratum asks the node outside the pool's lock, and a silent moment keeps the HTTP jobs."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-stratum-test-")
        self.node, self.now = FakeNode(), [1000.0]
        self.pool = pool.Pool(self.node, "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), clock=lambda: self.now[0])

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_slow_node_holds_up_nobody(self):
        slow, asked, call = threading.Event(), threading.Event(), self.node.call

        def slowly(method, *params):
            if method == "getbestblockhash":
                asked.set()
                slow.wait(10)
            return call(method, *params)
        self.node.call = slowly
        looking = threading.Thread(target=self.pool.look)
        looking.start()
        try:
            self.assertTrue(asked.wait(5))
            self.assertTrue(self.pool.lock.acquire(timeout=2))  # the pool's lock is free while the node thinks
            self.pool.lock.release()
        finally:
            slow.set()
            looking.join(10)
        self.assertEqual(self.pool.best, self.node.previous)
        self.assertEqual(self.pool.template["previousblockhash"], self.node.previous)

    def test_a_silent_moment_keeps_the_jobs_handed_out(self):
        job = self.pool.work(ALICE)["job"].split(".")[0]
        self.now[0] += pool.TEMPLATE_AGE
        self.node.down = True
        with self.assertRaises(pool.PoolError):
            self.pool.work(ALICE)                               # the node is silent: no template for a moment
        self.assertIsNone(self.pool.template)
        self.pool.look()                                        # nor does stratum's look change anything
        self.node.down = False
        self.now[0] += 1
        self.pool.work(ALICE)
        self.assertIn(job, self.pool.jobs)                      # the same tip: what was handed out still counts
        self.node.previous = "44" * 32
        self.now[0] += pool.TIP_AGE
        self.assertEqual(self.pool.look(), "44" * 32)
        self.assertNotIn(job, self.pool.jobs)                   # a new tip: stale at once
        self.assertEqual(self.pool.template["previousblockhash"], "44" * 32)


if __name__ == "__main__":
    unittest.main()
