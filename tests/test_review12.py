"""Review 12 (23 November 2026): the reviewer's demonstrations for the remote connector and the mending of review
11c, written against polarka-agent 0.2.5.dev1 (6981971), kept here as tests of what was mended (0.2.5).

Every test in a class named H, M, L or I says what should hold; on 6981971 each of them failed, showing the fault
named in its class. The class Sound holds what the reviewer tried and found to hold. Three tests were changed with
the mending, where it chose one of the ways the review offered and not the one the test was written for (each says
so): I1 (2025-03-26 is no longer spoken, rather than batches taken), I3 (the read is warned once records younger
than a day went, rather than each letter flagged) and I4 (a letter is shown up to a day ahead, with a warning past
an hour; one dated further ahead is still not shown). The field free_stamps_left_today is now
free_stamps_left_today_for_all_callers (I6). Nothing here needs a node, a network or a wallet of anybody's: the
services are stand-ins, or the real classes on the loopback, in temporary folders."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from polarka_agent import connector, keys, mail, mcp, pool, postage, stamps, walletd   # noqa: E402
from polarka_agent.httpguard import Bounded, Limiter                                     # noqa: E402
from polarka_agent.wallet import STAMP, Refused, Wallet                                  # noqa: E402
from tests.test_connector import Clock, Services, call, digest                         # noqa: E402
from tests.test_pool import PAY, FakeHash, FakeNode                                     # noqa: E402
from tests.test_wallet_rules import Model, WORDS                                        # noqa: E402


def made_up_proof(d, height=30580):
    """A proof that holds together by itself: a transaction that carries the root, alone in a block made up here
    (no proof of work). Only a node can tell that this block was never mined."""
    root = stamps.leaf(bytes.fromhex(d))
    script = b"\x6a" + bytes([len(STAMP) + 32]) + STAMP + root
    tx = (b"\x02\x00\x00\x00" + b"\x01" + bytes(32) + b"\xff\xff\xff\xff" + b"\x00" + b"\xff\xff\xff\xff"
          + b"\x01" + bytes(8) + bytes([len(script)]) + script + bytes(4))
    inner = stamps.dsha(tx)
    header = b"\x00\x00\x00\x20" + bytes(32) + inner + bytes(12)
    merkle_block = header + (1).to_bytes(4, "little") + b"\x01" + inner + b"\x01\x01"
    return {"version": 1, "digest": d, "root": root.hex(), "path": [], "txid": inner[::-1].hex(), "transaction": tx.hex(),
            "block_hash": stamps.dsha(header)[::-1].hex(), "block_height": height, "txoutproof": merkle_block.hex()}


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r12-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def app(self, stamps_url="http://127.0.0.1:9", pool_url="http://127.0.0.1:9", node=None, day=5, place=3):
        return connector.Connector(os.path.join(self.tmp, "connector"), stamps_url, pool_url, node, day, place, Clock())


class H1NewlineStampsForFree(Folder):
    """H1 (connector.DIGEST, Connector.tool_stamp): DIGEST ends in `$`, which in Python also matches before a last
    newline, so a SHA-256 followed by "\\n" passes. urllib strips the newline from the URL: the real stamp service
    takes the plain SHA-256 and answers with it. The connector sees another digest than it asked about, takes the
    answer for a refusal, gives the free stamp back and tells the caller UNAVAILABLE. The hash waits in the batch all
    the same, and nothing was counted. One place so puts any number of hashes into the batches, past both bounds,
    until the batch is full and the paying users' stamps through the gate are refused with BUSY."""

    def setUp(self):
        super().setUp()
        self.service = stamps.Stamps(None, None, os.path.join(self.tmp, "stamps"), every=600, most_waiting=40)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), stamps.make_handler(self.service))
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.http.server_address[1]

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def test_one_place_puts_no_more_hashes_into_the_batch_than_its_free_stamps(self):
        app = self.app(self.url)
        told = [call(app, "north_stamp", {"sha256": digest(n) + "\n"}, where="198.51.100.7")[0] for n in range(40)]
        try:
            self.service.take(digest(1000))                         # a paying user, as the gate would pass it on
            paying = "taken"
        except Refused as e:
            paying = e.kind
        self.assertLessEqual(len(self.service.waiting), app.free_per_place,
                             "the stamp service holds %d hashes of one place; free stamps counted: %d; the caller was told "
                             "%s; a paying user's stamp: %s" % (len(self.service.waiting), app.state["free"],
                                                               told[0].get("error"), paying))


class M1TenPlacesShutTheDoor(unittest.TestCase):
    """M1 (connector.serve): the bound of requests is Limiter(per_minute), whose bucket of all holds ten times one
    place's share. Ten places that each keep within their own 300 a minute empty it, and every other caller is
    answered 429 for as long as they keep it up. A chat application reaches the connector from a few addresses of its
    own, so ten busy places may also be honest: ten applications, or one with ten addresses."""

    def test_a_newcomer_is_served_while_ten_places_keep_within_their_bounds(self):
        caught = {}

        class Stop(Exception):
            pass

        class NoServer:
            def __init__(self, address, handler, most=64):
                self.server_address = address

            def serve_forever(self):
                raise Stop()

        made, real = connector.make_handler, connector.Bounded

        def capture(app, trust_proxy, limiter):
            caught["limiter"] = limiter
            return made(app, trust_proxy, limiter)
        folder = tempfile.mkdtemp(prefix="r12-")
        connector.make_handler, connector.Bounded = capture, NoServer
        try:
            app = connector.Connector(folder, "http://127.0.0.1:9", "http://127.0.0.1:9", None, 5, 3, Clock())
            with self.assertRaises(Stop):
                connector.serve(app, "127.0.0.1:0", True, 300)    # as the unit starts it: --per-minute left at 300
        finally:
            connector.make_handler, connector.Bounded = made, real
            shutil.rmtree(folder, ignore_errors=True)
        limiter = caught["limiter"]
        t = [limiter.clock()]
        limiter.clock = lambda: t[0]                               # all within the same moment
        limiter.all, limiter.swept = (float(limiter.everyone), t[0]), t[0]
        for i in range(10):
            for _ in range(300):
                limiter.take("198.51.100.%d" % (i + 1))
        self.assertTrue(limiter.take("203.0.113.9"), "after ten places used their own bounds (all together: %d a minute), "
                        "a newcomer is refused" % limiter.everyone)


class M2TwoSavesAtOnce(Folder):
    """M2 (Connector.save, _write): two saves at once write the same state.json.new. The second opens it, and so
    empties it, while the first is still writing; the first then moves the second's file into place, and its own
    replace finds nothing. A stamp that was taken is answered "the tool failed (FileNotFoundError)", and state.json
    holds the first's text over the second's longer one. At the next start it cannot be read: the day's count of free
    stamps begins again at nothing, and the SHA-256s taken are forgotten."""

    def test_a_stamp_taken_is_not_answered_as_failed_and_the_count_survives(self):
        services = Services()
        app = self.app(services.url, services.url, place=5)
        dump, entered, go, calls = json.dump, threading.Event(), threading.Event(), [0]

        def slow(obj, f, **kw):
            calls[0] += 1
            if calls[0] == 1:                                      # the first save, held in its write for a moment
                entered.set()
                go.wait(5)
            return dump(obj, f, **kw)
        got = {}
        json.dump = slow
        try:
            first = threading.Thread(target=lambda: got.update(first=call(app, "north_stamp", {"sha256": digest(1)}, where="a")))
            first.start()
            self.assertTrue(entered.wait(5))
            got["second"] = call(app, "north_stamp", {"sha256": digest(2)}, where="b")
            go.set()
            first.join(5)
        finally:
            json.dump = dump
            services.stop()
        again = connector.Connector(os.path.join(self.tmp, "connector"), "http://127.0.0.1:9", "http://127.0.0.1:9", None, 5, 5, Clock())
        self.assertEqual((got["first"][1], got["second"][1], again.state["free"]), (False, False, 2),
                         "first answer: %s; second: %s; after a restart %d free stamps are counted for today"
                         % (got["first"][0], got["second"][0].get("state"), again.state["free"]))

    def test_five_stamps_at_once_are_each_answered_as_taken(self):
        # no delay put in: in my runs on this Mac three or four of the five were answered FAILED, and about one run
        # in five left a state.json that the next start could not read
        services = Services()
        try:
            app = self.app(services.url, services.url, day=20, place=5)
            told = []
            threads = [threading.Thread(target=lambda n=n: told.append(call(app, "north_stamp", {"sha256": digest(100 + n)}, where="b")[0]))
                       for n in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        finally:
            services.stop()
        self.assertEqual(sorted(s.get("state") or s.get("error") for s in told), ["waiting"] * 5)


class L1NodeWarmingUp(Folder):
    """L1 (stamps.verify, Connector.tool_verify): every error the node answers with is taken for "the proof does not
    hold". A node that is starting (-28, loading the block index) or has pruned the block (-1, block not available)
    makes a good proof "valid": false; only the reason, in the node's words, tells otherwise."""

    def test_a_node_that_is_starting_is_unavailable_not_a_no(self):
        class Starting:
            def call(self, method, *params):
                raise walletd.Refused(-28, "Loading block index...")
        shown, failed, _ = call(self.app(node=Starting()), "north_stamp_verify", {"proof": made_up_proof(digest(7))})
        self.assertTrue(failed and shown.get("error") == "UNAVAILABLE", "a node that is starting made the answer %s" % shown)


class L2HeightIsTheNextBlock(Folder):
    """L2 (Connector.tool_network with pool.stats): the pool's chain height is the height of its template, the block
    being mined (getblocktemplate's height, which the pool's coinbase carries for BIP34), one more than the node's
    block count. north_network shows it as the chain's height, and counts the reward of the block not yet found
    as issued."""

    def test_the_height_shown_is_the_node_s(self):
        class Node(FakeNode):
            def call(self, method, *params):
                if method == "getblockcount":
                    return self.height - 1                         # the tip; the template builds the block after it
                return FakeNode.call(self, method, *params)
        services, node = Services(), Node()
        try:
            p = pool.Pool(node, "regtest", PAY, self.tmp, ratio=16, hasher=FakeHash(), clock=lambda: 1000.0)
            services.stats = json.loads(json.dumps(p.stats()))
            shown, failed, _ = call(self.app(services.url, services.url), "north_network")
        finally:
            services.stop()
        self.assertFalse(failed, shown)
        tip = node.call("getblockcount")
        self.assertEqual((shown["chain"]["height"], shown["chain"]["issued_north"]), (tip, connector.issued(tip)),
                         "the node's tip is at %d" % tip)


class L4MailReadSaysItDeletesNothing(unittest.TestCase):
    """L4 (mcp.py, _SHOWN): north_mail_read is annotated destructiveHint false, though without keep the post office
    deletes the letters it handed over, and a reader stopped during the ack never shows them (N4, open). A host
    that asks its user before a destructive tool does not ask before this one."""

    def test_mail_read_is_marked_as_a_tool_that_may_delete(self):
        hints = {t["name"]: t["annotations"] for t in mcp.TOOLS}
        self.assertTrue(hints["north_mail_read"].get("destructiveHint"), hints["north_mail_read"])


class L5QuotesFromThirteenPlaces(unittest.TestCase):
    """L5 (postage.Gate.quote; T4 mended in part): a place holds two unpaid quotes, and the last five of the wallet's
    GAP addresses are for places that hold none. So 13 places (seven with two, one with one, five newcomers) hold all
    20 for forty minutes, and an honest buyer gets BUSY all that time. Before, one place did it."""

    def setUp(self):
        self.m = Model()
        self.now = [1_800_000_000]
        self.folder = os.path.join(tempfile.mkdtemp(prefix="r12-"), "w")
        Wallet.create(self.folder, "regtest", self.m.fake.url, WORDS)
        self.w = Wallet(self.folder, clock=lambda: self.now[0])
        self.g = postage.Gate(self.w, keys.UNIT // 100, 100, clock=lambda: self.now[0], look_every=0)

    def tearDown(self):
        self.w.server.close()
        self.m.fake.stop()

    def test_an_honest_buyer_gets_a_quote_after_thirteen_places_asked(self):
        held = 0
        for i in range(13):
            for _ in range(2):
                try:
                    self.g.quote("198.51.100.%d" % (i + 1))
                    held += 1
                except Refused:
                    pass
        self.now[0] += 120                                         # two minutes later: no bucket is empty
        try:
            got = self.g.quote("203.0.113.5")
        except Refused as e:
            got = e.kind
        self.assertIsInstance(got, dict, "13 places hold %d quotes; an honest buyer gets %s" % (held, got))


class L3CheckedHereWithoutTrust(unittest.TestCase):
    """L3 (connector.TOOLS, GUIDE["stamps"]): the texts say that a proof can be checked "with north_stamp_verify ...
    without trusting Polarka". north_stamp_verify is Polarka's server asking Polarka's node: it is the one check that
    does need that trust."""

    def test_no_text_says_the_check_here_needs_no_trust(self):
        texts = [t["description"] for t in connector.TOOLS] + [s for g in connector.GUIDE.values() for s in g["text"]]
        said = [s for text in texts for s in text.split(". ") if "north_stamp_verify" in s and "without trusting Polarka" in s]
        self.assertEqual(said, [])


class Endpoint12(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="r12-")
        self.app = connector.Connector(self.folder, "http://127.0.0.1:9", "http://127.0.0.1:9", None, 5, 3, Clock())
        self.http = Bounded(("127.0.0.1", 0), connector.make_handler(self.app, False, Limiter(600)))
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def ask(self, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.http.server_address[1], timeout=10)
        sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        sent.update(headers or {})
        c.request("POST", "/mcp", body=json.dumps(body).encode(), headers=sent)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw else None)


class I1Protocol(Endpoint12):
    """I1 (make_handler, do_POST): (a) initialize answers 2025-03-26 when a client asks for it, and a request without
    MCP-Protocol-Version is to be taken as 2025-03-26 (transport, 2025-06-18 and 2025-11-25), whose servers MUST take
    batches; every batch is refused with 400. (b) A body that is JSON but no JSON-RPC message at all gets 200 with an
    error, where the transport asks for an HTTP error status."""

    def test_2025_03_26_is_not_spoken_and_a_batch_is_refused(self):
        # Mended the other way the review offered: 2025-03-26, whose servers must take batches, is no longer agreed to.
        # A client that asks for it is offered the newest version; a batch is refused with an HTTP error.
        status, got = self.ask({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "old"}}})
        self.assertEqual((status, got["result"]["protocolVersion"]), (200, "2025-11-25"), got)
        self.assertNotIn("2025-03-26", connector.VERSIONS)
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        status, got = self.ask([ping, dict(ping, id=2)])
        self.assertEqual(status, 400, got)

    def test_initialize_is_not_refused_for_its_own_version_header(self):
        # the header belongs to the requests after initialize (I1, 3): a client that sends a newer one there can still agree
        status, got = self.ask({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}},
                               {"MCP-Protocol-Version": "2099-01-01"})
        self.assertEqual((status, got["result"]["protocolVersion"]), (200, "2025-11-25"), got)
        status, got = self.ask({"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"MCP-Protocol-Version": "2099-01-01"})
        self.assertEqual(status, 400, got)

    def test_what_is_no_message_gets_an_http_error(self):
        status, got = self.ask("hello")
        self.assertEqual(status, 400, got)


class I2SmallThings(Folder):
    """I2: (a) north_guide with a topic that is no text fails as "the tool failed (TypeError)" (and a line in the log),
    not as INPUT. (b) A reservation is given back only when local_json raises OSError: an answer of the stamp
    service that is no HTTP (http.client.BadStatusLine, an HTTPException) keeps the free stamp taken for the rest of
    the day. Contrived for our own service; a `finally` costs nothing."""

    def test_a_topic_that_is_no_text_is_an_input_error(self):
        shown, failed, _ = call(self.app(), "north_guide", {"topic": ["start"]})
        self.assertEqual(shown.get("error"), "INPUT", shown)

    def test_a_free_stamp_is_given_back_whatever_went_wrong(self):
        class Cut(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.wfile.write(b"polarka-stamps restarting\r\n\r\n")      # no HTTP status line: BadStatusLine
                self.close_connection = True
        cut = ThreadingHTTPServer(("127.0.0.1", 0), Cut)
        threading.Thread(target=cut.serve_forever, daemon=True).start()
        try:
            app = self.app("http://127.0.0.1:%d" % cut.server_address[1])
            shown, failed, _ = call(app, "north_stamp", {"sha256": digest(3)})
        finally:
            cut.shutdown()
            cut.server_close()
        self.assertEqual((failed, app.state["free"]), (True, 0), "told %s; free stamps used today: %d" % (shown, app.state["free"]))


class FakeWallet:
    def __init__(self, folder, chain="regtest"):
        self.folder, self.chain = folder, chain
        os.makedirs(folder, exist_ok=True)


class Reader(unittest.TestCase):
    """A post office on this machine, and Bob's wallet reading it with fetch_letters (as in review 11c)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r12-")
        self.office = mail.PostOffice(os.path.join(self.tmp, "office"))
        self.paid, self.free = mail.servers(self.office, "127.0.0.1:0", "127.0.0.1:0", per_minute=600)
        for s in (self.paid, self.free):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.free_url = "http://127.0.0.1:%d" % self.free.server_address[1]
        self.office.origin = mail.origin_of(self.free_url)
        self.bob = FakeWallet(os.path.join(self.tmp, "bob"))
        self.b = mail.my_inbox(self.bob)["inbox"]
        self.alice = 0xA11CE

    def tearDown(self):
        for s in (self.paid, self.free):
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self):
        time.sleep(0.005)
        return mail.fetch_letters(self.bob, self.free_url)

    def post(self, secret, text, now=None):
        self.office.post(self.b, mail.seal(self.b, mail.letter(secret, self.b, text, now=now)))


class I3ReplayWithinADayAfterAnOverflow(Reader):
    """I3 (mail._keep_seen, _take_new: the trade-off of T1's mending, as the author describes it): once the record
    lets go of letters younger than FLOOR_LAG for room, a letter among them that is delivered again is shown as new,
    with its text and without maybe_repeat. 0.2.4 hid it. Here SEEN_MOST is 3; in the package it takes 100 001 letters
    read within a day, and the sealed letter, which only the post office and the writer hold."""

    def test_a_letter_delivered_again_after_its_record_went_is_flagged(self):
        saved = mail._keep_seen.__defaults__
        mail._keep_seen.__defaults__ = (3,)                        # most = 3 (flooded, added with the mending, is given)
        try:
            real = mail.seal(self.b, mail.letter(self.alice, self.b, "pay the bearer 5 NORTH"))
            self.office.post(self.b, real)
            self.assertEqual([x.get("text") for x in self.read()["letters"]], ["pay the bearer 5 NORTH"])
            for i in range(3):                                     # a flood: Alice's record is the oldest, and goes
                self.post(0xBAD + i, "hello %d" % i)
            self.read()
            self.office.post(self.b, real)                         # whoever holds the office delivers it again
            read = self.read()
            again, self.warned = read["letters"], read.get("warning", "")
        finally:
            mail._keep_seen.__defaults__ = saved
        # Mended as the review suggested in its judgement of the trade-off: not each letter, but the read is warned for
        # a day once the record let go of records younger than a day (the 0.2.4 floor that hid it let strangers hide
        # honest letters, review 11c T1).
        self.assertTrue(again[0].get("repeat") or again[0].get("maybe_repeat") or "cannot be told apart" in self.warned,
                        "shown again as new, and nothing said: %r" % again[0])


class I4AnHourAhead(Reader):
    """I4 (mail.fetch_letters, FUTURE an hour): a letter dated more than an hour after the reader's clock is named
    without its text, and acknowledged: the post office deletes it. A writer whose clock is an hour fast (or a reader
    whose clock is an hour slow) loses every letter, and the writer was told 200. Since records are now made at no
    later than the reader's clock, such a letter could be shown with a warning instead."""

    def test_a_letter_dated_ahead_is_not_deleted_unread(self):
        # Mended with FUTURE at a day: a letter dated two hours ahead is shown, with a word that a clock is wrong, and
        # its record outlives it (KEEP and FUTURE after the record), so it cannot come again as new
        self.post(self.alice, "from a fast clock", now=time.time() + 2 * 3600)
        got = self.read()
        letter = got["letters"][0]
        self.assertEqual(letter.get("text"), "from a fast clock", letter)
        self.assertIn("clock", letter.get("warning", ""))

    def test_a_letter_dated_more_than_a_day_ahead_is_still_not_shown(self):
        self.post(self.alice, "from a clock a day off", now=time.time() + mail.FUTURE + 120)
        letter = self.read()["letters"][0]
        self.assertNotIn("text", letter)
        self.assertIn("after this machine's clock", letter["error"])


class Sound(Folder):
    """What I tried and found to hold on 6981971 (these pass)."""

    def test_a_made_up_proof_is_not_called_valid(self):
        proof = made_up_proof(digest(7))
        shown, failed, _ = call(self.app(), "north_stamp_verify", {"proof": proof})
        self.assertEqual((failed, shown["valid"]), (False, None))          # no node: never "valid": true
        self.assertIn("no node was asked", shown["note"])

        class Unknown:
            def call(self, method, *params):
                raise walletd.Refused(-5, "Block hash not found")
        shown, failed, _ = call(self.app(node=Unknown()), "north_stamp_verify", {"proof": proof})
        self.assertEqual((failed, shown["valid"]), (False, False))

        class Stale:
            def call(self, method, *params):
                if method == "getrawtransaction":
                    return {"hex": proof["transaction"], "in_active_chain": False}
                return {"confirmations": -1, "height": 30580, "time": 1795392000}
        shown, failed, _ = call(self.app(node=Stale()), "north_stamp_verify", {"proof": proof})
        self.assertEqual((failed, shown["valid"]), (False, False))

        class Other:
            def call(self, method, *params):
                if method == "getrawtransaction":
                    return {"hex": "00" + proof["transaction"]}
                return {"confirmations": 5, "height": 30580, "time": 1795392000}
        shown, failed, _ = call(self.app(node=Other()), "north_stamp_verify", {"proof": proof})
        self.assertEqual((failed, shown["valid"]), (False, False))

        class Down:
            def call(self, method, *params):
                raise walletd.NodeDown("getrawtransaction: refused")
        shown, failed, _ = call(self.app(node=Down()), "north_stamp_verify", {"proof": proof})
        self.assertEqual((failed, shown["error"]), (True, "UNAVAILABLE"))

    def test_a_good_proof_with_a_node_that_knows_it(self):
        proof = made_up_proof(digest(8))

        class Knows:
            def call(self, method, *params):
                if method == "getrawtransaction":
                    return {"hex": proof["transaction"]}
                return {"confirmations": 3, "height": 30580, "time": 1795392000}
        shown, failed, _ = call(self.app(node=Knows()), "north_stamp_verify", {"proof": proof})
        self.assertEqual((failed, shown["valid"], shown["confirmations"]), (False, True, 3))
        bad = dict(proof, block_height=30581)
        shown, failed, _ = call(self.app(node=Knows()), "north_stamp_verify", {"proof": bad})
        self.assertEqual(shown["valid"], False)

    def test_uppercase_and_lowercase_are_one_stamp_and_concurrency_keeps_the_bound(self):
        services = Services()
        try:
            app = self.app(services.url, services.url, day=20, place=5)
            shown = [call(app, "north_stamp", {"sha256": d}, where="a")[0] for d in (digest(1), digest(1).upper())]
            self.assertEqual([s["free_stamps_left_today_for_all_callers"] for s in shown], [19, 19])
            told = []
            threads = [threading.Thread(target=lambda n=n: told.append(call(app, "north_stamp", {"sha256": digest(100 + n)}, where="b")[0]))
                       for n in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            # the reservation holds the bound whatever the order: five go to the service, seven are told ENOUGH_TODAY
            # (what the five are told is M2's matter)
            self.assertEqual((len(services.asked), [s.get("error") for s in told].count("ENOUGH_TODAY"), app.state["free"]), (7, 7, 6))
        finally:
            services.stop()


if __name__ == "__main__":
    unittest.main()


class AfterTheMending(Endpoint12):
    """What the mending of review 12 added beside the reviewer's own demonstrations."""

    def raw(self, head, body=b""):
        import socket
        s = socket.create_connection(("127.0.0.1", self.http.server_address[1]), timeout=10)
        s.sendall(head + body)
        got = b""
        while True:
            piece = s.recv(65536)
            if not piece:
                break
            got += piece
        s.close()
        status = int(got.split(b" ", 2)[1])
        text = got.split(b"\r\n\r\n", 1)[1]
        return status, (json.loads(text) if text else None)

    def test_L6_a_body_sent_in_chunks_is_read(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode()
        chunks = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in (body[:10], body[10:])) + b"0\r\n\r\n"
        head = (b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n")
        self.assertEqual(self.raw(head, chunks), (200, {"jsonrpc": "2.0", "id": 7, "result": {}}))
        # both a length and chunks, or something else than chunks: refused, never guessed
        both = head.replace(b"Connection", b"Content-Length: 5\r\nConnection")
        self.assertEqual(self.raw(both, chunks)[0], 400)
        self.assertEqual(self.raw(head.replace(b"chunked", b"gzip, chunked"), chunks)[0], 400)
        # more than a message may be, in chunks too
        big = b"".join(b"%x\r\n%s\r\n" % (4096, b"x" * 4096) for _ in range(17)) + b"0\r\n\r\n"
        self.assertEqual(self.raw(head, big)[0], 413)
        self.assertEqual(self.raw(head, b"zz\r\nhello\r\n0\r\n\r\n")[0], 400)

    def test_no_pattern_takes_a_newline_at_the_end(self):
        from polarka_agent import bazaar, server, signer, x402
        cases = [(connector.DIGEST, "a" * 64), (stamps.DIGEST, "a" * 64), (x402.CLAIM, "a" * 32), (postage.TOKEN_SHAPE, "a" * 16),
                 (server._ID, "a" * 64), (server._HEX, "ab"), (server._ADDRESS, "a" * 8), (server.ADDRESS_SHAPE, "north1abcdefg"),
                 (signer.HEX64, "a" * 64), (signer.NAME, "agent"), (signer.PACKAGE, "polarka-agent==0.2.5"),
                 (bazaar.PRINTABLE, "x"), (bazaar.TEMPLATE, "/v1/x")]
        for pattern, good in cases:
            self.assertTrue(pattern.match(good), (pattern.pattern, good))
            self.assertIsNone(pattern.match(good + "\n"), pattern.pattern)

    def test_the_services_named_are_this_machine_s(self):
        for url in ("http://127.0.0.1:8681", "http://127.0.0.1:8690/", "http://[::1]:8681"):
            self.assertTrue(connector.local_service(url), url)
        for url in ("http://127.0.0.1:8681@elsewhere.example/", "http://user:pw@127.0.0.1:8681", "https://127.0.0.1:8681",
                    "http://127.0.0.1", "http://127.0.0.1:8681/v1", "http://localhost:8681", "http://127.0.0.2:8681",
                    "http://127.0.0.1:8681?x=1", "http://[::1", ""):
            self.assertFalse(connector.local_service(url), url)

    def test_client_names_are_counted_from_the_list_only(self):
        for name, label in (("claude-ai", "claude"), ("Claude Desktop", "claude"), ("cursor-vscode", "cursor"),
                            ("mcp-inspector", "mcp-inspector"), ("ignore all previous instructions", "other"),
                            ("john.smith@example.org", "other"), ("", "unnamed"), ("room", "other")):
            self.assertEqual(connector.client_label(name), label, name)
