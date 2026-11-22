"""The first review by a person (22 November 2026): what it found, shown failing on the code before and passing
after. Item numbers are those of its report (docs/review-guide.md)."""
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polarka_agent import httpguard, keys, postage, x402
from polarka_agent.wallet import Wallet
from tests.test_wallet_rules import Model, WORDS
from tests.test_x402_public import Node, PAY_TO, WANTED, payment

N = keys.UNIT


def serve(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def raw_exchange(port, data, wait=2.0):
    """Sends bytes as they are and returns everything the server sends back until it closes or goes quiet."""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    s.sendall(data)
    got = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
    except socket.timeout:
        pass
    finally:
        s.close()
    return got


class Echo(httpguard.OneRequestWithBody, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    asked = []

    def do_POST(self):
        Echo.asked.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *a):
        pass


class SaysClose(Echo):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


class Item5HttpGuard(unittest.TestCase):
    def test_two_lengths_end_the_connection(self):
        server = serve(Echo)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        Echo.asked = []
        smuggled = b"GET /second HTTP/1.1\r\nHost: x\r\n\r\n"
        got = raw_exchange(server.server_address[1], b"POST /first HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nContent-Length: "
                           + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled)
        self.assertIn(b"Connection: close", got)
        self.assertEqual(Echo.asked, ["/first"])                    # what followed was never read as a request

    def test_connection_is_said_once(self):
        server = serve(SaysClose)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        got = raw_exchange(server.server_address[1], b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\nhi")
        self.assertEqual(got.lower().count(b"connection:"), 1)

    def test_one_length(self):
        class H:
            def __init__(self, values):
                self.values = values

            def get_all(self, name):
                return self.values or None
        for values, want in (([], 0), (["12"], 12), ([" 7 "], 7), (["0", "5"], None), (["abc"], None), (["²"], None),
                             (["-1"], None), (["1" * 13], None)):
            self.assertEqual(httpguard.one_length(H(values)), want, values)

    def test_one_place(self):
        self.assertEqual(httpguard.place("198.51.100.7"), "198.51.100.7")
        self.assertEqual(httpguard.place("::ffff:198.51.100.7"), "198.51.100.7")
        self.assertEqual(httpguard.place("2001:db8:1:2::1"), "2001:db8:1::/48")
        self.assertEqual(httpguard.place("2001:db8:1:2::1", 64), "2001:db8:1:2::/64")
        self.assertEqual(httpguard.place("not an address"), "not an address")


class SlowNode(Node):
    """Decodes one transaction slowly, as a node that is busy would."""

    def call(self, method, *params):
        if method == "decoderawtransaction" and params[0] == "%064x" % 1:
            time.sleep(1.5)
        return super().call(method, *params)


class Item1FacilitatorLock(unittest.TestCase):
    def test_a_slow_node_call_holds_up_nobody_else(self):
        tmp = tempfile.mkdtemp(prefix="polarka-hr-")
        f = x402.Facilitator(SlowNode(), "main", os.path.join(tmp, "used.jsonl"))
        slow = threading.Thread(target=f.verify, args=(payment(1), dict(WANTED)))
        slow.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        self.assertTrue(f.verify(payment(2), dict(WANTED))["isValid"])
        self.assertTrue(f.settle(payment(3), dict(WANTED))["success"])
        self.assertLess(time.monotonic() - t0, 1.0)                 # before: it waited for the slow one, 1.3 s
        slow.join()

    def test_two_at_once_write_one_payment_once(self):
        tmp = tempfile.mkdtemp(prefix="polarka-hr-")
        used = os.path.join(tmp, "used.jsonl")
        f = x402.Facilitator(Node(), "main", used)
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(f.settle(payment(4), dict(WANTED)))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(a["success"] for a in answers))
        self.assertEqual(sum(1 for a in answers if not a.get("redelivered")), 1)  # one sale, the rest the same answer
        with open(used) as fh:
            states = [json.loads(line)["state"] for line in fh]
        self.assertEqual(states, ["waiting", "delivered"])
        # another claim on the same transaction buys nothing
        self.assertEqual(f.settle(payment(4, claim="c2" * 16), dict(WANTED))["errorReason"], "transaction_already_used")


class GateTest(unittest.TestCase):
    def setUp(self):
        self.m = Model()
        self.now = [1_800_000_000]
        self.folder = os.path.join(tempfile.mkdtemp(prefix="polarka-hr-gate-"), "w")
        Wallet.create(self.folder, "regtest", self.m.fake.url, WORDS)
        self.w = Wallet(self.folder, clock=lambda: self.now[0])

    def tearDown(self):
        self.w.server.close()
        self.m.fake.stop()


class Facilitator:
    """Takes no payment and counts how often it was asked."""
    network = "bip122:regtest"

    def __init__(self):
        self.asked = 0

    def needed(self, amount):
        return 1

    def settle(self, payment, wanted):
        self.asked += 1
        return {"success": False, "errorReason": "invalid_transaction", "transaction": "", "network": self.network, "payer": None}


class Item1And4Gate(GateTest):
    def test_x402_attempts_from_one_place_are_bounded(self):
        f = Facilitator()
        g = postage.Gate(self.w, N // 100, 100, clock=lambda: self.now[0], look_every=0, x402=f)
        wanted = g.x402_wanted()

        def attempt(i, source):
            claim = hashlib.sha256(b"%d" % i).hexdigest()
            done, may = g.x402_pay({"x402Version": 2, "accepted": wanted, "payload": {"transaction": "00", "claim": claim}}, "/v1/x", source)
            return done["errorReason"]
        reasons = [attempt(i, "2001:db8:5:1::%x" % i) for i in range(postage.X402_PER_MINUTE + 1)]   # one /48, many machines
        self.assertEqual(reasons[:-1], ["invalid_transaction"] * postage.X402_PER_MINUTE)
        self.assertEqual(reasons[-1], postage.TOO_MANY)
        self.assertEqual(f.asked, postage.X402_PER_MINUTE)            # the refused one never reached the facilitator
        self.assertEqual(attempt(999, "198.51.100.9"), "invalid_transaction")   # another place is not held up

    def test_free_calls_count_by_place(self):
        g = postage.Gate(self.w, N // 100, 100, free=2, clock=lambda: self.now[0])
        self.assertEqual([g.spend(None, "2001:db8:9:%x::1" % i) for i in range(3)], [True, True, False])   # one /48
        self.assertTrue(g.spend(None, "2001:db8:a::1"))
        self.now[0] += 86400
        self.assertTrue(g.spend(None, "2001:db8:9:7::1"))
        self.assertEqual(len(g.free_used), 1)                       # yesterday's places are forgotten

    def test_lots_used_up_are_forgotten_after_a_month(self):
        g = postage.Gate(self.w, N // 100, 100, clock=lambda: self.now[0], look_every=0)
        with g.lock:
            g.state["tokens"]["spent"] = {"address": "a", "expires": self.now[0], "paid": True, "calls": 0, "price": 1}
            g.state["tokens"]["left"] = {"address": "a", "expires": self.now[0], "paid": True, "calls": 5, "price": 1}
        self.now[0] += postage.KEEP_SPENT + 1
        g.quote()
        self.assertNotIn("spent", g.state["tokens"])
        self.assertIn("left", g.state["tokens"])


class Behind(BaseHTTPRequestHandler):
    big = False

    def do_GET(self):
        body = b"x" * (postage.MAX_ANSWER + 10) if Behind.big else b"fine"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Item4GateOverHttp(GateTest):
    def setUp(self):
        super().setUp()
        self.behind = serve(Behind)
        self.addCleanup(self.behind.server_close)
        self.addCleanup(self.behind.shutdown)
        g = postage.Gate(self.w, N // 100, 100, free=100, clock=lambda: self.now[0])
        self.front = serve(postage.make_handler(g, "127.0.0.1:%d" % self.behind.server_address[1], False))
        self.addCleanup(self.front.server_close)
        self.addCleanup(self.front.shutdown)

    def ask(self, data):
        return raw_exchange(self.front.server_address[1], data, wait=5).split(b"\r\n", 1)[0]

    def test_a_request_is_read_only_when_whole(self):
        self.assertIn(b"411", self.ask(b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n"))
        self.assertIn(b"400", self.ask(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n"))
        self.assertIn(b"400", self.ask(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nhi"))
        self.assertIn(b"200", self.ask(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"))

    def test_an_answer_too_large_is_not_passed_on(self):
        Behind.big = True
        try:
            self.assertIn(b"502", self.ask(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"))
        finally:
            Behind.big = False


# ---------------------------------------------------------------------------------------------------- the wallet

class WalletTest(unittest.TestCase):
    def setUp(self):
        self.m = Model()
        self.now = [1_800_000_000]
        self.folder = os.path.join(tempfile.mkdtemp(prefix="polarka-hr-wallet-"), "w")
        Wallet.create(self.folder, "regtest", self.m.fake.url, WORDS)
        with open(os.path.join(self.folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "40", "per_day_north": "50"}, f)
        self.w = Wallet(self.folder, clock=lambda: self.now[0])
        self.to = keys.Account.from_words(keys.new_words(12), "regtest").address(0, 0)

    def tearDown(self):
        self.w.server.close()
        self.m.fake.stop()


class Item5WalletFiles(WalletTest):
    def test_the_wallets_files_are_its_owners_alone(self):
        old = os.umask(0o022)
        try:
            self.m.coin(self.w.account.address(0, 0), 10 * N, "aa" * 32)
            self.w.pay([(self.to, N)])
        finally:
            os.umask(old)
        for name in ("journal.jsonl", "state.json", "lock"):
            self.assertEqual(os.stat(os.path.join(self.folder, name)).st_mode & 0o777, 0o600, name)


class Item3WalletAfter200(WalletTest):
    def test_a_payment_is_looked_for_through_one_address_and_only_until_found(self):
        self.m.coin(self.w.account.address(0, 0), 10 * N, "aa" * 32)
        self.m.coin(self.w.account.address(0, 1), 10 * N, "bb" * 32)
        made = self.w.pay([(self.to, 15 * N)])                     # two coins, two addresses
        asked = []

        def history(body):
            asked.append(body)
            page = [self.m.line(t) for t in self.m.waiting] + [dict(self.m.line("%02x" % i * 32), block_height=50) for i in range(1, 10)]
            return 200, {"transactions": page, "next_cursor": "aa" * 8, "index": fake_index(self.m.height)}
        self.m.fake.answers["/v1/transactions"] = history
        self.assertEqual([p["state"] for p in self.w.payments() if p["id"] == made["id"]], ["handed"])
        self.assertEqual(len(asked), 1)                             # before: page after page, up to 2000 lines
        self.assertEqual(len(asked[0]["addresses"]), 1)             # before: every address the payment spends from

    def test_change_goes_round_the_window(self):
        from polarka_agent import wallet as wallet_module
        self.m.used.add(self.w.account.address(1, wallet_module.MAX_WINDOW - 1))   # the last change key was used
        self.m.coin(self.w.account.address(0, 0), 10 * N, "aa" * 32)
        made = self.w.pay([(self.to, N)])
        raw = [json.loads(l) for l in open(os.path.join(self.folder, "journal.jsonl")) if '"signed"' in l][-1]
        change = [s for s, _ in raw["outputs"] if s != keys.script_for_address("regtest", self.to).hex()]
        self.assertEqual(change, [self.w.account.script(1, 0).hex()])   # before: 1/199, again and again
        self.assertTrue(made["id"])


class FirstAuditF6(WalletTest):
    def test_a_payment_shown_again_a_day_later_counts_for_that_day(self):
        self.m.coin(self.w.account.address(0, 0), 40 * N, "aa" * 32)
        self.m.coin(self.w.account.address(0, 1), 40 * N, "bb" * 32)
        self.m.fake.answers["/v1/transactions/broadcast"] = [(409, {"message": "bad-txns-inputs-missingorspent"})]
        first = self.w.pay([(self.to, 30 * N)])
        self.assertEqual(first["state"], "kept")
        self.now[0] += 25 * 3600                                    # a day later the same payment is asked for again
        self.m.fake.answers["/v1/transactions/broadcast"] = self.m.broadcast
        again = self.w.pay([(self.to, 30 * N)])
        self.assertEqual(again["same_as"], first["id"])
        with self.assertRaises(Exception) as e:                     # it counts for today: 30 more is over the 50
            self.w.pay([(self.to, 30 * N)])
        self.assertEqual(getattr(e.exception, "kind", None), "OVER_DAY")


def fake_index(height):
    from tests.fake import index
    return index(height)


# ---------------------------------------------------------------------------------------------------- the agent

class Item5Mcp(unittest.TestCase):
    def test_an_agents_tools_reach_public_https_only(self):
        from polarka_agent import mcp
        from polarka_agent.wallet import Refused
        for url in ("http://127.0.0.1:11434/", "https://localhost/", "https://10.0.0.1/", "https://[::1]/", "https://192.168.1.1/",
                    "https://169.254.169.254/latest/", "https://printer.local/", "https://[::ffff:127.0.0.1]/", "ftp://x.org/", None):
            with self.assertRaises(Refused, msg=url):
                mcp.outside(url)
        public = lambda host, port, **k: [(2, 1, 6, "", ("185.33.145.183", port))]     # what the name stands for, here
        for url in ("https://gate.polarka.org/v1/stamp/x", "https://8.8.8.8/"):
            mcp.outside(url, public)

    def test_a_line_is_read_with_a_bound_and_deep_json_is_refused(self):
        import io
        from polarka_agent import mcp
        ping = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"
        source = io.StringIO("x" * (mcp.MAX_LINE + 5) + "\n" + "[" * 100000 + "\n" + ping)
        sink = io.StringIO()
        mcp.serve(None, source, sink)
        replies = [json.loads(l) for l in sink.getvalue().splitlines()]
        self.assertEqual([r.get("error", {}).get("code") for r in replies[:2]], [-32700, -32700])
        self.assertEqual(replies[2], {"jsonrpc": "2.0", "id": 7, "result": {}})


class Offers(BaseHTTPRequestHandler):
    def do_GET(self):
        needed = {"x402Version": 2, "accepts": [{"scheme": "exact", "network": x402.NETWORKS["main"], "amount": 1000, "asset": "NORTH",
                                                  "payTo": PAY_TO, "maxTimeoutSeconds": 60}]}
        self.send_response(402)
        self.send_header("PAYMENT-REQUIRED", x402.b64json(needed))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class Item5Fetch(unittest.TestCase):
    def test_an_amount_that_is_a_number_is_not_paid(self):
        from polarka_agent.wallet import Refused
        server = serve(Offers)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        class NoWallet:
            chain, folder = "main", tempfile.mkdtemp(prefix="polarka-hr-")

            def pay(self, *a, **k):
                raise AssertionError("nothing may be paid")
        with self.assertRaises(Refused) as e:
            x402.fetch("http://127.0.0.1:%d/x" % server.server_address[1], NoWallet(), 10 ** 6)
        self.assertIn("cannot be read", str(e.exception))

    def test_digits(self):
        self.assertTrue(x402.digits("1000"))
        for text in ("²", "1²", "", "1" * 19, "-1", " 1", 1000, None):
            self.assertFalse(x402.digits(text), text)


# ---------------------------------------------------------------------------------------------------- stamps

class Item5Stamps(unittest.TestCase):
    def test_flags_are_read_without_a_list_of_every_bit(self):
        from polarka_agent import stamps
        raw = bytes(80) + (5).to_bytes(4, "little") + b"\x00" + b"\xfe\xff\xff\x3f\x00" + bytes(4 * 1024 * 1024 - 2)
        t0 = time.monotonic()
        with self.assertRaises(ValueError):
            stamps.parse_txoutproof(raw)
        self.assertLess(time.monotonic() - t0, 2)

    def test_what_the_node_does_not_know_is_not_valid(self):
        from polarka_agent import stamps, walletd

        class Node:
            def call(self, method, *params):
                raise walletd.Refused("No such transaction found in the provided block")
        original = stamps.check
        stamps.check = lambda proof: True
        try:
            with self.assertRaises(ValueError):
                stamps.verify({"txid": "aa" * 32, "block_hash": "bb" * 32, "transaction": "00", "block_height": 5}, Node())
            with self.assertRaises(ValueError):                    # no block_height in the proof
                stamps.verify({"txid": "aa" * 32, "block_hash": "bb" * 32, "transaction": "00"}, type("N", (), {"call": lambda s, m, *p: {"hex": "00", "confirmations": 3, "height": 5}})())
        finally:
            stamps.check = original


class StampWallet:
    def __init__(self, state):
        self.state = state

    def stamp(self, root, memo=None):
        return {"id": "cc" * 32, "state": self.state, "note": "the server said that its node does not take it"}


class Rpc:
    def __init__(self):
        self.count = 100
        self.calls = []

    def call(self, method, *params):
        self.calls.append(method)
        if method == "getblockcount":
            return self.count
        if method == "getblockhash":
            return "%064x" % params[0]
        if method == "getblock":
            return {"tx": []}
        if method == "getmempoolentry":
            self.count += 1                     # a block came while it was asked: the transaction may be in it
            raise RuntimeError("Transaction not in mempool")
        raise RuntimeError(method)


class Item5StampsService(unittest.TestCase):
    def setUp(self):
        from polarka_agent import stamps
        self.stamps = stamps
        self.now = [1_800_000_000]
        self.folder = tempfile.mkdtemp(prefix="polarka-hr-stamps-")

    def test_a_batch_the_node_refused_is_not_taken_for_written(self):
        s = self.stamps.Stamps(StampWallet("kept"), Rpc(), self.folder, every=600, clock=lambda: self.now[0])
        s.take("ab" * 32)
        self.assertIsNone(s.tick())
        self.assertEqual(s.state["batches"], [])
        self.assertEqual(s.take("ab" * 32)["state"], "waiting")       # it waits for the next batch

    def test_a_batch_is_not_called_dead_while_a_block_comes(self):
        rpc = Rpc()
        s = self.stamps.Stamps(StampWallet("handed"), rpc, self.folder, every=600, clock=lambda: self.now[0])
        s.take("ab" * 32)
        s.tick()
        self.assertEqual(len(s.state["batches"]), 1)
        self.now[0] += self.stamps.DEAD + 1
        s._scan()
        self.assertFalse(s.state["batches"][0].get("dead"))         # before: dead, and its stamps paid for again

    def test_stamps_json_is_written_only_when_something_changed(self):
        s = self.stamps.Stamps(StampWallet("handed"), Rpc(), self.folder, every=600, clock=lambda: self.now[0])
        s._scan()
        book = os.path.join(self.folder, "stamps.json")
        before = os.stat(book).st_mtime_ns
        time.sleep(0.05)
        s._scan()
        s._scan()
        self.assertEqual(os.stat(book).st_mtime_ns, before)          # before: written at every look, twice a minute


# ---------------------------------------------------------------------------------------------------- walletd

@unittest.skipUnless(__import__("tests.bench", fromlist=["find_bin"]).find_bin(), "truenorthd and truenorth-cli are needed (TN_BIN)")
class Item2WalletdMemory(unittest.TestCase):
    """walletd keeps only the last blocks as read, and a change of branch takes blocks off instead of building the
    whole chain again; what it then holds is what a fresh reading holds."""

    def same_as_fresh(self, index, rpc, node):
        from polarka_agent import walletd
        tip = node.cli("getblockcount")
        self.assertEqual(index.chain.hashes, [node.cli("getblockhash", i) for i in range(tip + 1)])
        fresh = walletd.Index(rpc)
        fresh.look()
        for name in ("outputs", "where", "spends", "inputs", "count"):
            self.assertEqual(getattr(index.chain, name), getattr(fresh.chain, name), name)
        self.assertEqual({k: sorted(v) for k, v in index.chain.by_script.items()}, {k: sorted(v) for k, v in fresh.chain.by_script.items()})
        self.assertLessEqual(len(index.blocks), walletd.KEEP_BLOCKS)

    def test_a_change_of_branch(self):
        from polarka_agent import walletd
        from polarka_agent.wallet import units
        from tests import bench
        sink = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"
        node = bench.Node(bench.find_bin())
        self.addCleanup(node.stop)
        standin = bench.StandIn(node)
        self.addCleanup(standin.stop)
        folder = os.path.join(tempfile.mkdtemp(prefix="polarka-hr-walletd-"), "w")
        Wallet.create(folder, "regtest", standin.url, None)
        with open(os.path.join(folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "100", "per_day_north": "1000"}, f)
        w = Wallet(folder)
        node.mine(1, w.fresh_address())
        node.mine(101, sink)
        old = walletd.KEEP_BLOCKS
        walletd.KEEP_BLOCKS = 20
        self.addCleanup(setattr, walletd, "KEEP_BLOCKS", old)
        rpc = walletd.Node("http://127.0.0.1:%d" % node.port, node.cookie)
        index = walletd.Index(rpc)
        index.look()
        self.assertLessEqual(len(index.blocks), 20)                     # before: every block of the chain, as read
        paid = w.pay([(sink, units("3"))])
        node.mine(1, sink)                                              # the payment is in this block
        index.look()
        self.assertIn(paid["id"], index.chain.where)
        tip = node.cli("getblockcount")
        node.cli("invalidateblock", node.cli("getblockhash", tip - 1))  # two blocks come off, the payment with them
        node.mine(4, w.account.address(0, 5))
        index.look()
        self.same_as_fresh(index, rpc, node)
        # a change deeper than the blocks kept: what comes off is read from the node again
        tip = node.cli("getblockcount")
        node.cli("invalidateblock", node.cli("getblockhash", tip - 30))
        node.mine(35, w.account.address(0, 6))                      # to another address: not the same blocks again
        index.look()
        self.same_as_fresh(index, rpc, node)


if __name__ == "__main__":
    unittest.main()
