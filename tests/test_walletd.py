"""walletd, the wallet server for one's own node, against TrueNorth Core nodes on regtest. The tests' stand-in is
walletd with switches on top, so every test of the wallet, the faucet and the pool runs its code; what is
checked here is what walletd does beside the stand-in: it answers as the stand-in does, keeps to loopback and
to its chain, says when its node is not ready, reads bodies with care, closes idle and surplus connections,
and forgets waiting transactions that no longer wait."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from polarka_agent import walletd
from polarka_agent.server import Server
from polarka_agent.wallet import Wallet, units
from tests import bench

SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"       # key 0/0 of the public test phrase


class Walletd:
    """walletd as its own process, as it runs on a server."""

    def __init__(self, node, *extra):
        self.log = tempfile.TemporaryFile(mode="w+")
        self.p = subprocess.Popen([sys.executable, "-B", "-m", "polarka_agent.walletd", "--chain", "regtest", "--rpc-url",
                                   "http://127.0.0.1:%d" % node.port, "--cookie", node.cookie, "--listen", "127.0.0.1:0"] + list(extra),
                                  cwd=ROOT, stdout=subprocess.PIPE, stderr=self.log, universal_newlines=True)
        line = self.p.stdout.readline()
        if not line.startswith("listening "):
            self.stop()
            raise RuntimeError("walletd did not start: %r %s" % (line, self.errors()))
        self.port = int(line.split()[1])
        self.url = "http://127.0.0.1:%d" % self.port

    def errors(self):
        self.log.seek(0)
        return self.log.read()

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()
        self.p.stdout.close()


def ask(url, path, body=None, raw=None, headers=None):
    """(status, answer as JSON) of one request on a connection of its own."""
    host, port = url.split("//")[1].split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=30)
    try:
        if body is None and raw is None:
            c.request("GET", path, headers=headers or {})
        else:
            c.request("POST", path, raw if raw is not None else json.dumps(body), dict({"Content-Type": "application/json"}, **(headers or {})))
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"null")
    finally:
        c.close()


def plain(answer):
    """
    An answer without what belongs to the server that gave it: its clock (when it last looked, when it first saw
    a waiting transaction) and its count of changes since it started (the revision, also inside a cursor;
    whether there is a cursor stays).
    """
    if isinstance(answer, dict):
        return {k: (v is not None if k == "next_cursor" else plain(v)) for k, v in answer.items()
                if k not in ("last_node_contact", "pending_observed_at", "pending_revision")
                and not (k == "timestamp" and answer.get("block_height") is None)}
    if isinstance(answer, (list, tuple)):
        return [plain(v) for v in answer]
    return answer


@unittest.skipUnless(bench.find_bin(), "truenorthd and truenorth-cli are needed (TN_BIN names their folder)")
class WalletdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(bench.find_bin())
        cls.node.mine(1, SINK)                                # a regtest node leaves its first download with a block of now
        cls.server = Walletd(cls.node)
        cls.standin = bench.StandIn(cls.node)
        cls.tmp = tempfile.mkdtemp(prefix="polarka-agent-walletd-")
        Wallet.create(os.path.join(cls.tmp, "w"), "regtest", cls.server.url, None)
        with open(os.path.join(cls.tmp, "w", "limits.json"), "w") as f:
            json.dump({"per_payment_north": "100", "per_day_north": "1000"}, f)
        cls.wallet = Wallet(os.path.join(cls.tmp, "w"))
        cls.address = cls.wallet.fresh_address()
        cls.node.mine(2, cls.address)
        cls.node.mine(100, SINK)

    @classmethod
    def tearDownClass(cls):
        cls.standin.stop()
        cls.server.stop()
        cls.node.stop()

    def test_pays_through_it_and_answers_as_the_stand_in(self):
        done = self.wallet.pay([(SINK, units("3"))])
        self.assertEqual(done["state"], "handed", done)
        self.assertIn(done["id"], self.node.cli("getrawmempool"))
        for _ in (0, 1):                                      # once with the payment waiting, once with it in a block
            for path, body in (("/v1/addresses/activity", {"addresses": [self.address, SINK]}),
                               ("/v1/utxos", {"addresses": [self.address]}), ("/v1/utxos", {"addresses": [self.address], "limit": 1}),
                               ("/v1/transactions", {"addresses": [self.address, SINK], "limit": 3}),
                               ("/v1/transactions/status", {"transaction_id": done["id"]}),
                               ("/v1/transactions/status", {"transaction_id": "ab" * 32}),
                               ("/v1/utxos", {"addresses": ["north1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"]})):
                mine, theirs = ask(self.server.url, path, body), ask(self.standin.url, path, body)
                self.assertEqual(plain(mine), plain(theirs), path)
            for path in ("/ready", "/v1/chain/tip", "/v1/fees/estimate", "/v2/fees/estimate", "/nothing"):
                self.assertEqual(plain(ask(self.server.url, path)), plain(ask(self.standin.url, path)), path)
            self.node.mine(1, SINK)
        lines = ask(self.server.url, "/v1/transactions", {"addresses": [self.address]})[1]["transactions"]
        self.assertEqual([line["block_height"] is not None for line in lines if line["transaction_id"] == done["id"]], [True])

    def test_no_switches(self):
        self.assertEqual(ask(self.server.url, "/__standin/set", {"stale": True})[0], 404)
        self.assertEqual(ask(self.server.url, "/ready")[0], 200)

    def test_bodies_are_read_with_care(self):
        for length, status in (("-5", 400), ("abc", 400), ("2000000", 413), ("1e3", 400)):
            with socket.create_connection(("127.0.0.1", self.server.port), timeout=10) as s:
                s.sendall(("POST /v1/utxos HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: %s\r\n\r\n" % length).encode())
                self.assertTrue(s.recv(4096).startswith(b"HTTP/1.1 %d" % status), length)
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=10) as s:
            s.sendall(b"POST /v1/utxos HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
            self.assertTrue(s.recv(4096).startswith(b"HTTP/1.1 411"))
        self.assertEqual(ask(self.server.url, "/v1/utxos", raw="[1]")[0], 422)
        self.assertEqual(ask(self.server.url, "/v1/utxos", raw="{", headers={"Content-Type": "text/plain"})[0], 415)

    def test_forgets_what_no_longer_waits(self):
        app = walletd.App(walletd.Node("http://127.0.0.1:%d" % self.node.port, self.node.cookie), "regtest")
        done = self.wallet.pay([(SINK, units("1"))])
        self.assertEqual(done["state"], "handed", done)
        app.index.look()
        self.assertIn(done["id"], app.index.pending)
        self.node.mine(1, SINK)
        app.index.look()
        self.assertNotIn(done["id"], app.index.pending)
        self.assertNotIn(done["id"], app.index.first_seen)

    def test_an_address_is_named_back_as_it_was_asked(self):
        """Human review, L-4: an address in upper case is that address, and a client checks for the name it asked with."""
        status, answer = ask(self.server.url, "/v1/addresses/activity", {"addresses": [self.address.upper()]})
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["used_addresses"], [self.address.upper()])

    def test_a_history_cursor_reads_its_id_in_either_case(self):
        """Human review, L-4: the id in a history cursor is compared in lower case, as in a cursor of coins."""
        body = {"addresses": [self.address], "limit": 1}
        status, first = ask(self.server.url, "/v1/transactions", body)
        self.assertEqual(status, 200, first)
        self.assertIsNotNone(first["next_cursor"])
        cursor = json.loads(bytes.fromhex(first["next_cursor"]).decode())
        cursor["txid"] = cursor["txid"].upper()
        upper = json.dumps(cursor, separators=(",", ":")).encode().hex()
        lower = ask(self.server.url, "/v1/transactions", dict(body, cursor=first["next_cursor"]))
        self.assertEqual(lower[0], 200, lower)
        self.assertEqual(ask(self.server.url, "/v1/transactions", dict(body, cursor=upper)), lower)


@unittest.skipUnless(bench.find_bin(), "truenorthd and truenorth-cli are needed (TN_BIN names their folder)")
class WalletdEdgesTest(unittest.TestCase):
    """Each case with a node and a walletd of its own."""

    def setUp(self):
        self.node = bench.Node(bench.find_bin())
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()
        self.node.stop()

    def start(self, *extra):
        s = Walletd(self.node, *extra)
        self.servers.append(s)
        return s

    def run_walletd(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "polarka_agent.walletd", "--rpc-url", "http://127.0.0.1:%d" % self.node.port,
                               "--cookie", self.node.cookie] + list(args), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)

    def test_keeps_to_loopback_and_to_its_chain(self):
        for listen in ("0.0.0.0:0", "192.168.1.2:9560", "localhost:9560", "[::]:0", "127.0.0.1", "127.0.0.1:99999"):
            r = self.run_walletd("--chain", "regtest", "--listen", listen)
            self.assertEqual(r.returncode, 2, listen)
            self.assertIn("loopback", r.stderr, listen)
        r = self.run_walletd("--chain", "main", "--listen", "127.0.0.1:0")
        self.assertEqual(r.returncode, 2)
        self.assertIn("the node runs regtest, not main", r.stderr)

    def test_not_ready_while_the_node_catches_up(self):
        s = self.start()                                      # a fresh regtest node is in its first download
        status, answer = ask(s.url, "/ready")
        self.assertEqual((status, answer["ready"]), (503, False))
        self.assertEqual(ask(s.url, "/v1/utxos", {"addresses": [SINK]}), (503, {"message": walletd.STALE}))
        self.assertEqual(ask(s.url, "/v1/chain/tip")[0], 200)  # what is not wallet data is answered
        self.node.mine(1, SINK)
        self.assertEqual(ask(s.url, "/ready")[0], 200)
        self.assertEqual(ask(s.url, "/v1/utxos", {"addresses": [SINK]})[0], 200)

    def test_idle_and_surplus_connections_are_closed(self):
        self.node.mine(1, SINK)
        s = self.start("--idle", "1", "--most-connections", "2")
        client = Server(s.url)
        self.assertEqual(client.tip_height(), 1)
        time.sleep(2)                                         # walletd has closed the client's connection meanwhile
        self.assertEqual(client.tip_height(), 1)
        client.close()
        time.sleep(0.5)
        held = [socket.create_connection(("127.0.0.1", s.port), timeout=5) for _ in range(2)]
        try:
            time.sleep(0.3)
            with socket.create_connection(("127.0.0.1", s.port), timeout=5) as third:
                third.sendall(b"GET /v1/chain/tip HTTP/1.1\r\nHost: x\r\n\r\n")
                try:
                    got = third.recv(100)
                except ConnectionResetError:
                    got = b""
                self.assertEqual(got, b"")                    # closed unanswered
        finally:
            for h in held:
                h.close()
        time.sleep(0.5)
        self.assertEqual(ask(s.url, "/v1/chain/tip"), (200, {"height": 1}))


    def test_a_request_that_trickles_in_is_cut_off(self):
        """Human review of 23 November, L-4: once its first byte is there, a request has --request-time to arrive."""
        s = self.start("--request-time", "1", "--idle", "10")
        with socket.create_connection(("127.0.0.1", s.port), timeout=10) as slow:
            slow.sendall(b"POST /v1/utxos HTTP/1.1\r\nHost: x\r\n")
            started, closed = time.monotonic(), False
            while not closed and time.monotonic() - started < 5:
                time.sleep(0.25)
                try:
                    slow.sendall(b"X")                        # a header that never ends, a byte at a time
                    slow.settimeout(0.05)
                    try:
                        closed = slow.recv(100) == b""
                    except socket.timeout:
                        pass
                    slow.settimeout(10)
                except OSError:
                    closed = True
            self.assertTrue(closed, "the request was given more than five seconds")
            self.assertLess(time.monotonic() - started, 3)
        # the wait between two requests on one connection is the idle time, not the time a request has to arrive
        c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
        try:
            for _ in range(2):
                c.request("GET", "/v1/chain/tip")
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                r.read()
                time.sleep(1.5)
        finally:
            c.close()

class RevisionTest(unittest.TestCase):
    def test_the_revision_starts_at_a_number_drawn_at_random(self):
        """Human review, L-4: a cursor from before a restart does not meet the same tip and revision by chance."""
        starts = {walletd.Index(None).revision for _ in range(20)}
        self.assertGreater(len(starts), 15)
        self.assertTrue(all(0 <= r < 1 << 48 for r in starts))


class ReadinessTest(unittest.TestCase):
    """When walletd takes its node's picture to be one a payout may rest on."""

    class FakeNode:
        def __init__(self, peers=8, age=60):
            self.peers, self.age = peers, age

        def call(self, method, *params):
            if method == "getconnectioncount":
                return self.peers
            if method == "getblockheader":
                return {"time": int(time.time()) - self.age}
            raise AssertionError(method)

    def ready(self, chain, info, **node):
        return walletd.App(self.FakeNode(**node), chain, max_tip_age=7200).node_ready(dict({"bestblockhash": "00" * 32}, **info))

    def test_cases(self):
        good = {"initialblockdownload": False, "headers": 100, "blocks": 100}
        self.assertTrue(self.ready("main", good))
        self.assertTrue(self.ready("main", dict(good, headers=101)))
        self.assertFalse(self.ready("main", dict(good, headers=102)))
        self.assertFalse(self.ready("main", dict(good, initialblockdownload=True)))
        self.assertFalse(self.ready("main", good, peers=0))
        self.assertFalse(self.ready("main", good, age=7300))
        self.assertTrue(self.ready("regtest", good, peers=0, age=10 ** 6))

    def test_loopback(self):
        self.assertEqual(walletd.loopback("127.0.0.1:9560"), ("127.0.0.1", 9560))
        self.assertEqual(walletd.loopback("[::1]:0"), ("::1", 0))
        self.assertEqual(walletd.loopback("127.0.0.2:1"), ("127.0.0.2", 1))
        for bad in ("0.0.0.0:1", "localhost:1", "10.0.0.1:1", "127.0.0.1:", "127.0.0.1:x", "[::]:1"):
            with self.assertRaises(ValueError, msg=bad):
                walletd.loopback(bad)


if __name__ == "__main__":
    unittest.main()
