"""Review 11c (third look at 0.2.4, 23 November 2026): its demonstrations, kept as tests of the mending in 0.2.5.

T2 (C1), T1 (C3), T5 (C4) and T4 (C6) are the reviewer's, T4 with the asker's place now given. T3 is new. What is
left of N1 (C2) and of N4 (C5) is not mended in 0.2.5: those two stay here as expected failures, so that the day
they pass is seen."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from polarka_agent import keys, mail, pool, postage, stratum
from polarka_agent.wallet import Refused, Wallet

from .test_pool import ALICE, BOB, PAY, FakeHash, FakeNode, search
from .test_review11b import come
from .test_wallet_rules import Model, WORDS


class FakeWallet:
    def __init__(self, folder, chain="regtest"):
        self.folder, self.chain = folder, chain
        os.makedirs(folder, exist_ok=True)


class T2OwnBlockLeavesNoOldJobs(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="r11c-")
        self.node, self.now = FakeNode(), [1000.0]
        self.pool = pool.Pool(self.node, "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), clock=lambda: self.now[0])

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_share_on_the_tip_the_pool_left_is_not_paid(self):
        a, b = self.pool.work(ALICE), self.pool.work(BOB)
        self.assertTrue(self.pool.share(b["job"], search(b, want_block=True))["accepted"])
        call = self.node.call

        def no_template(method, *params):
            if method == "getblocktemplate":
                raise OSError("the node is busy")
            return call(method, *params)
        self.node.call = no_template
        outcomes, nonce = [], -1
        for later in (0.2, 20.0):
            self.now[0] = 1000.0 + later
            nonce = search(a, want_block=False, start=nonce + 1)
            try:
                outcomes.append(self.pool.share(a["job"], nonce)["credited"])
            except pool.PoolError as e:
                outcomes.append(e.kind)
        self.assertEqual(outcomes, ["STALE", "STALE"])


class N1TipAskedFirstAnsweredLast(unittest.TestCase):
    """Open in 0.2.5 (review 11c, N1): a tip question asked before a template question but answered after a block
    still lets the template on the old tip in, for about a second."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="r11c-")
        self.node, self.now = FakeNode(), [1000.0]
        self.pool = pool.Pool(self.node, "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), clock=lambda: self.now[0])

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    @unittest.expectedFailure
    def test_the_tip_the_pool_learned_stands(self):
        Y = "44" * 32
        self.pool.look()
        self.now[0] += pool.TEMPLATE_AGE + 0.5
        tip_asked, tip_go, built, back = (threading.Event() for _ in range(4))
        call = self.node.call

        def node(method, *params):
            who = threading.current_thread().name
            if method == "getbestblockhash" and who == "share":
                tip_asked.set()
                tip_go.wait(10)
                return call(method, *params)
            if method == "getblocktemplate" and who == "refresh":
                got = call(method, *params)
                built.set()
                back.wait(10)
                return got
            return call(method, *params)
        self.node.call = node
        share = threading.Thread(target=self.pool.look, kwargs={"template": False}, name="share")
        share.start()
        self.assertTrue(tip_asked.wait(5))
        self.now[0] += 0.01
        refresh = threading.Thread(target=self.pool.look, name="refresh")
        refresh.start()
        self.assertTrue(built.wait(5))
        self.node.previous, self.node.height = Y, self.node.height + 1
        tip_go.set()
        share.join(5)
        back.set()
        refresh.join(5)
        self.assertEqual(self.pool.best, Y)


class Reader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r11c-")
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

    def record(self):
        with open(os.path.join(self.bob.folder, mail.SEEN_FILE)) as f:
            return json.load(f)


class T1FloorRaisedAhead(Reader):
    def test_letters_dated_ahead_do_not_make_honest_letters_vanish(self):
        saved = mail._keep_seen.__defaults__
        mail._keep_seen.__defaults__ = (3,)
        try:
            ahead = time.time() + mail.FUTURE - 60                        # within FUTURE
            for i in range(4):
                self.post(0xBAD + i, "hello %d" % i, now=ahead + i)
            self.assertEqual(len([x for x in self.read()["letters"] if "text" in x]), 4)
            self.post(self.alice, "the contract is signed")
            got = self.read()
            held = self.record()
        finally:
            mail._keep_seen.__defaults__ = saved
        self.assertEqual([x.get("text") for x in got["letters"]], ["the contract is signed"])
        self.assertNotIn("maybe_repeat", got["letters"][0])
        self.assertLessEqual(held["floor"], time.time() * 1000 - mail.FLOOR_LAG * 1000)
        self.assertLessEqual(max(held["seen"].values()), time.time() * 1000)     # no record dated ahead of the clock

    def test_a_letter_older_than_the_record_can_tell_is_shown_with_a_warning(self):
        saved = mail._keep_seen.__defaults__
        mail._keep_seen.__defaults__ = (2,)
        try:
            old = time.time() - 2 * mail.FLOOR_LAG                         # written two days ago
            for i in range(3):
                self.post(0xBAD + i, "old %d" % i, now=old + i)
            self.read()                                                    # the record keeps two: its floor is old + 0
            self.post(self.alice, "late but honest", now=old)
            got = self.read()
        finally:
            mail._keep_seen.__defaults__ = saved
        self.assertEqual([x.get("text") for x in got["letters"]], ["late but honest"])
        self.assertTrue(got["letters"][0]["maybe_repeat"])
        self.assertIn("do not act on it twice", got["letters"][0]["warning"])

    def test_a_letter_far_ahead_is_named_not_shown(self):
        self.post(self.alice, "from the future", now=time.time() + mail.FUTURE + 120)
        got = self.read()["letters"]
        self.assertNotIn("text", got[0])
        self.assertIn("minutes after this machine's clock", got[0]["error"])


class T5RecordOfAnEarlierVersion(Reader):
    def test_a_record_kept_by_an_earlier_version_still_counts(self):
        real = mail.seal(self.b, mail.letter(self.alice, self.b, "pay the bearer 5 NORTH"))
        body = mail.read(mail.mail_key(self.bob.folder), real)
        with open(os.path.join(self.bob.folder, mail.SEEN_FILE), "w") as f:
            json.dump({body["digest"]: body["t"]}, f)
        self.office.post(self.b, real)
        again = self.read()["letters"]
        self.assertTrue(again[0].get("repeat"))


class N4StoppedDuringTheAck(Reader):
    """Open in 0.2.5 (review 11c, N4): a reader that ends while it tells the post office never hands the letters
    over; the next read shows them as repeats without their text."""

    @unittest.expectedFailure
    def test_a_letter_not_handed_over_is_shown_later(self):
        self.post(self.alice, "the contract is signed")
        call = mail._call

        def stopped(url, headers=None, obj=None, timeout=30):
            if url.endswith("/ack"):
                raise KeyboardInterrupt
            return call(url, headers, obj, timeout)
        mail._call = stopped
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.read()
        finally:
            mail._call = call
        again = self.read()
        self.assertEqual([x.get("text") for x in again["letters"]], ["the contract is signed"])


class T3MinersComingBackTogether(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="r11c-")
        self.now = [1000.0]
        self.pool = pool.Pool(FakeNode(), "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), clock=lambda: self.now[0])
        self.s = stratum.Stratum(self.pool, "127.0.0.1:0")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_newcomer_waits_while_those_waiting_are_within_an_honest_authorization(self):
        held = [come(self.s, self.now, "198.18.%d.%d" % (i // 200, i % 200 + 1)) for i in range(stratum.MOST_WAITING)]
        self.assertTrue(all(held))
        self.assertIsNone(come(self.s, self.now, "203.0.113.9"))          # the table is full of fresh ones: it waits
        self.assertFalse(any(m.closed for m in held))
        self.now[0] = held[0].came + stratum.WAIT_FIRST
        self.assertIsNotNone(come(self.s, self.now, "192.0.2.1"))         # the first has waited too long: it goes
        self.assertEqual([m.closed for m in held[:2]], [True, False])


class T4Quotes(unittest.TestCase):
    def setUp(self):
        self.m = Model()
        self.now = [1_800_000_000]
        self.folder = os.path.join(tempfile.mkdtemp(prefix="r11c-"), "w")
        Wallet.create(self.folder, "regtest", self.m.fake.url, WORDS)
        self.w = Wallet(self.folder, clock=lambda: self.now[0])
        self.g = postage.Gate(self.w, keys.UNIT // 100, 100, clock=lambda: self.now[0], look_every=0)

    def tearDown(self):
        self.w.server.close()
        self.m.fake.stop()

    def test_a_stranger_s_quotes_leave_quotes_for_others(self):
        taken = 0
        for minute in range(10):
            for _ in range(3):
                try:
                    self.g.quote("198.51.100.66")
                    taken += 1
                except Refused as e:
                    self.assertIn(e.kind, ("BUSY", "SLOW"))
            self.now[0] += 60
        self.assertEqual(taken, postage.UNPAID_PER_PLACE)
        self.assertIn("pay_to", self.g.quote("203.0.113.5"))              # an honest buyer gets a quote

    def test_so_many_quotes_a_minute_from_one_place(self):
        with self.assertRaises(Refused) as e:
            for _ in range(postage.QUOTES_PER_MINUTE + 1):
                try:
                    self.g.quote("2001:db8:5::1")
                except Refused as busy:
                    if busy.kind != "BUSY":
                        raise
        self.assertEqual(e.exception.kind, "SLOW")

    def test_the_last_addresses_are_for_places_that_hold_none(self):
        for i in range(15):                                               # fifteen places, one quote each
            self.g.quote("198.51.100.%d" % (i + 1))
        with self.assertRaises(Refused) as e:
            self.g.quote("198.51.100.1")                                  # a second one for a place that holds one
        self.assertEqual(e.exception.kind, "BUSY")
        self.assertIn("pay_to", self.g.quote("203.0.113.77"))             # a newcomer still gets one
        self.assertIn("pay_to", self.g.quote(None))                       # and the gate's own uses are not bound


if __name__ == "__main__":
    unittest.main()
