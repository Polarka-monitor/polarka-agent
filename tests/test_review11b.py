"""Review 11b (22 November 2026), the second look at the mending of review 11: what it found, as tests, each of which
fails on 3149728 but the last of N7RoomInStratum, the reviewer's check of the counters, kept as it is. Most are the
reviewer's own demonstrations, turned round to say what holds now. They need no node."""
import base64
import collections
import json
import os
import random
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from polarka_agent import httpguard, mail, pool, postage, stratum
from polarka_agent.keys import BadInput, schnorr_sign

from .test_pool import ALICE, PAY, FakeHash, FakeNode, search
from .test_stratum import FakeWriter


class FakeWallet:
    def __init__(self, folder, chain="regtest"):
        self.folder, self.chain = folder, chain
        os.makedirs(folder, exist_ok=True)


class N1TheTipThePoolKnows(unittest.TestCase):
    """pool.look: a template asked before the tip moved is not put in once the pool has learned of the new tip from a
    question asked after it; a block of the pool's own is the tip at once."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-review11b-")
        self.node, self.now = FakeNode(), [1000.0]
        self.pool = pool.Pool(self.node, "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), clock=lambda: self.now[0])

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_an_answer_overtaken_by_a_newer_tip_is_not_put_in(self):
        X, Y = self.node.previous, "22" * 32
        self.pool.look()                                    # tip X and its template
        asked, release, call = threading.Event(), threading.Event(), self.node.call

        def slow_template(method, *params):
            got = call(method, *params)                     # the node answers for the tip it has now ...
            if method == "getblocktemplate" and threading.current_thread().name == "S1":
                asked.set()
                release.wait(10)                            # ... and the answer is slow to come back
            return got
        self.node.call = slow_template
        self.now[0] += pool.TEMPLATE_AGE + 0.5              # the template is old: stratum's refresh asks for a new one
        s1 = threading.Thread(target=self.pool.look, name="S1")
        s1.start()
        self.assertTrue(asked.wait(5))
        self.node.previous, self.node.height = Y, self.node.height + 1     # a block from elsewhere
        self.now[0] += pool.TIP_AGE + 0.1
        self.assertEqual(self.pool.look(template=False), Y)                 # a share asks: the pool learns Y
        release.set()
        s1.join(10)
        self.assertEqual(self.pool.best, Y)
        held = self.pool.template
        self.assertTrue(held["previousblockhash"] == Y or self.now[0] - self.pool.template_at >= pool.TEMPLATE_AGE)   # no fresh X
        work = self.pool.work(ALICE)                                        # HTTP hands out work on Y, not on X
        self.assertEqual(bytes.fromhex(work["header"])[4:36][::-1].hex(), Y)
        s = stratum.Stratum(self.pool, "127.0.0.1:0")
        self.assertIsNotNone(s._refresh(set()))                             # and stratum's job is on Y
        self.assertEqual(s.job.previous, Y)

    def test_a_newer_template_is_not_thrown_away_for_an_older_word_about_the_tip(self):
        """The other side of N1: a tip asked before a template was asked does not overrule that template."""
        X, Y = self.node.previous, "33" * 32
        self.pool.look()
        self.now[0] += pool.TEMPLATE_AGE + 0.5
        call = self.node.call

        def meanwhile(method, *params):
            got = call(method, *params)
            if method == "getbestblockhash":
                self.now[0] += 0.1                          # another thread's question about the tip, asked a moment
                with self.pool.lock:                        # after this one: X too
                    self.pool._saw(X, self.now[0])
                self.now[0] += 0.1
                self.node.previous, self.node.height = Y, self.node.height + 1     # then a block comes ...
            return got                                      # ... and the template asked after it is on Y
        self.node.call = meanwhile
        self.pool.look()
        self.assertEqual((self.pool.best, self.pool.template["previousblockhash"]), (Y, Y))

    def test_a_block_of_the_pool_s_own_is_the_tip_at_once(self):
        work = self.pool.work(ALICE)
        took = self.pool.share(work["job"], search(work, want_block=True))
        self.assertTrue(took["accepted"])
        self.assertEqual((self.pool.best, self.pool.tip_tried, self.pool.template_at), (took["block"], None, 0))


def post_letter(front, inbox, token, xff, sealed):
    body = json.dumps({"sealed": base64.b64encode(sealed).decode()}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/mail/%s" % (front.server_address[1], inbox), data=body,
                                 headers={"X-Postage": token, "Content-Type": "application/json", "X-Forwarded-For": xff})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("X-Postage-Calls-Left")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("X-Postage-Calls-Left")


class N2N3GateAndOffice(unittest.TestCase):
    """The gate in front of the post office's paid door, as deployed (--route /v1/mail), with small limits: two letters a
    minute from a place, six kept a minute in all."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-review11b-")
        self.office = mail.PostOffice(os.path.join(self.tmp, "office"), letters_per_minute=6)
        self.paid = mail.Bounded(("127.0.0.1", 0), mail.make_handler(self.office, "paid", True, httpguard.Limiter(2, everyone=0)), most=16)
        self.gate = postage.Gate(FakeWallet(os.path.join(self.tmp, "gate")), 1, 100)
        self.front = postage.Bounded(("127.0.0.1", 0), postage.make_handler(
            self.gate, "127.0.0.1:1", False, [("/v1/mail", "127.0.0.1:%d" % self.paid.server_address[1])]), most=16)
        for srv in (self.paid, self.front):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.saved_most = mail.MOST_LETTERS
        self.full = mail.inbox_of(0xF0F)

    def tearDown(self):
        mail.MOST_LETTERS = self.saved_most
        for srv in (self.paid, self.front):
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lot(self, calls):
        token = os.urandom(16).hex()
        self.gate.state["tokens"][postage.mark(token)] = {"address": "x", "expires": 0, "paid": True, "calls": calls, "price": 1}
        return token

    def fill(self):
        """An inbox of the attacker's own, full: what it posts there is refused, and costs it no call."""
        mail.MOST_LETTERS = 1
        self.office.post(self.full, mail.seal(self.full, b"x"))

    def test_N2_letters_refused_take_nothing_from_the_bucket_of_all(self):
        self.fill()
        attacker = self.lot(1)
        codes = [post_letter(self.front, self.full, attacker, "198.51.100.%d" % (i // 2 + 1), mail.seal(self.full, b"y"))
                 for i in range(6)]
        self.assertEqual(codes, [(507, "1")] * 6)
        self.assertEqual(self.gate.left(attacker), 1)
        mail.MOST_LETTERS = self.saved_most
        honest, got = self.lot(10), []
        for i in range(6):                                  # five more kept this minute (the one above was the sixth)
            inbox = mail.inbox_of(0xB0B + i)
            got.append(post_letter(self.front, inbox, honest, "203.0.113.%d" % (i // 2 + 1), mail.seal(inbox, b"hello"))[0])
        self.assertEqual(got, [200] * 5 + [429])
        self.assertEqual(self.gate.left(honest), 5)        # the letter not kept cost nothing

    def test_N3_a_call_given_back_writes_nothing(self):
        self.fill()
        attacker = self.lot(1)
        writes, save = [], self.gate._save

        def counting(lazy=False):
            writes.append(lazy)
            return save(lazy)
        self.gate._save = counting
        codes = [post_letter(self.front, self.full, attacker, "198.51.100.9", mail.seal(self.full, b"z"))[0] for _ in range(12)]
        self.assertEqual(codes, [507, 507] + [429] * 10)
        self.assertEqual((writes, self.gate.left(attacker)), ([], 1))

    def test_N3_a_call_in_flight_is_written_as_not_spent(self):
        token = self.lot(5)
        key = postage.mark(token)
        self.assertEqual(self.gate.reserve(token, None), "lot")
        self.gate._save()                                   # whatever is written meanwhile
        with open(self.gate.book) as f:
            self.assertEqual(json.load(f)["tokens"][key]["calls"], 5)
        self.assertEqual(self.gate.left(token), 4)
        again = postage.Gate(FakeWallet(os.path.join(self.tmp, "gate")), 1, 100)      # the gate falls and comes back
        self.assertEqual(again.left(token), 5)              # the buyer has that call
        self.gate.done(token)
        self.gate.flush()
        with open(self.gate.book) as f:
            self.assertEqual(json.load(f)["tokens"][key]["calls"], 4)
        self.gate.give_back(token)                          # spent: nothing to give back any more
        self.assertEqual(self.gate.left(token), 4)

    def test_a_call_the_service_never_answered_goes_back(self):
        dead = postage.Bounded(("127.0.0.1", 0), postage.make_handler(self.gate, "127.0.0.1:1", False, [("/v1/mail", "127.0.0.1:1")]), most=4)
        threading.Thread(target=dead.serve_forever, daemon=True).start()
        try:
            token = self.lot(3)
            self.assertEqual(post_letter(dead, self.full, token, "198.51.100.10", mail.seal(self.full, b"x")), (502, None))
            self.assertEqual(self.gate.left(token), 3)
        finally:
            dead.shutdown()
            dead.server_close()


class N4N5N6Reader(unittest.TestCase):
    """A post office on this machine, and Bob's wallet reading it with fetch_letters."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-review11b-")
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
        time.sleep(0.005)                                   # a signed time is used once
        return mail.fetch_letters(self.bob, self.free_url)

    def test_N4_a_letter_read_while_the_ack_fails_is_shown(self):
        self.office.post(self.b, mail.seal(self.b, mail.letter(self.alice, self.b, "the contract is signed")))
        call = mail._call

        def ack_fails(url, headers=None, obj=None, timeout=30):
            if url.endswith("/ack"):
                raise urllib.error.URLError("connection reset")      # the network, for a moment
            return call(url, headers, obj, timeout)
        mail._call = ack_fails
        try:
            first = self.read()
        finally:
            mail._call = call
        self.assertEqual(([x.get("text") for x in first["letters"]], first["acknowledged"]), (["the contract is signed"], None))
        again = self.read()                                 # the office still has it: shown as read before, and let go
        self.assertEqual((len(again["letters"]), again["letters"][0].get("repeat"), again["acknowledged"]), (1, True, 1))
        self.assertEqual(self.read()["letters"], [])

    def test_N5_two_readers_of_one_wallet_show_a_letter_once(self):
        self.office.post(self.b, mail.seal(self.b, mail.letter(self.alice, self.b, "pay the bearer 5 NORTH")))
        read, inner = mail.read, {}

        def meanwhile(secret, sealed):
            if not inner:
                inner["busy"] = True
                inner["got"] = self.read()                  # the MCP server of the same wallet reads meanwhile
            return read(secret, sealed)
        mail.read = meanwhile
        try:
            outer = self.read()                             # the command line
        finally:
            mail.read = read
        self.assertEqual([x.get("text") for x in inner["got"]["letters"]], ["pay the bearer 5 NORTH"])
        self.assertEqual(([x.get("text") for x in outer["letters"]], outer["letters"][0].get("repeat")), ([None], True))

    def test_N6_a_letter_dated_far_ahead_is_not_shown(self):
        later = time.time() + 10 * 365 * 86400
        self.office.post(self.b, mail.seal(self.b, mail.letter(0xBAD, self.b, "from the future", now=later)))
        shown = self.read()["letters"]
        self.assertIn("after this machine's clock", shown[0]["error"])
        self.assertNotIn("text", shown[0])
        self.assertFalse(os.path.exists(os.path.join(self.bob.folder, mail.SEEN_FILE)))     # and nothing of it is kept

    def test_N6_records_let_go_for_room_raise_a_floor(self):
        saved = mail._keep_seen.__defaults__
        mail._keep_seen.__defaults__ = (3,)                 # 100 000 in the package; three here
        try:
            real = mail.seal(self.b, mail.letter(self.alice, self.b, "pay the bearer 5 NORTH", now=time.time() - 60))
            self.office.post(self.b, real)
            self.assertEqual(self.read()["letters"][0]["text"], "pay the bearer 5 NORTH")
            for i in range(3):                              # three newer letters push Alice's record out ...
                self.office.post(self.b, mail.seal(self.b, mail.letter(0xBAD + i, self.b, "hello %d" % i)))
            self.assertEqual(len([x for x in self.read()["letters"] if "text" in x]), 3)
            self.office.post(self.b, real)                  # ... and whoever holds the office puts hers back
            again = self.read()["letters"]
        finally:
            mail._keep_seen.__defaults__ = saved
        self.assertEqual((again[0].get("text"), again[0].get("repeat")), (None, True))
        with open(os.path.join(self.bob.folder, mail.SEEN_FILE)) as f:
            held = json.load(f)
        self.assertEqual(len(held["seen"]), 3)
        self.assertGreater(held["floor"], 0)


def come(s, now, source, authorized=False):
    """A connection, as _client lets it in; None when there is no room for it. Each a millisecond after the one before."""
    place = pool.place(source)
    if not s._room(place):
        return None
    m = stratum.Miner(FakeWriter(), source, b"\0\0\0\1", place, now[0])
    s._enter(m)
    if authorized:
        m.workers = {ALICE: ALICE}
        s._authorized(m)
    now[0] += 0.001
    return m


class N7RoomInStratum(unittest.TestCase):
    """Stratum's tables, with the numbers deployed: no number of places keeps a newcomer out for long."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-review11b-")
        self.now = [1000.0]
        self.pool = pool.Pool(FakeNode(), "regtest", PAY, self.folder, ratio=16, hasher=FakeHash(), clock=lambda: self.now[0])
        self.s = stratum.Stratum(self.pool, "127.0.0.1:0")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_silent_connections_of_many_places_make_room_for_newcomers(self):
        held = [come(self.s, self.now, "198.18.%d.%d" % (i // 200, i % 200 + 1)) for i in range(stratum.MOST_WAITING)]
        self.assertTrue(all(held))
        newcomers = [come(self.s, self.now, a) for a in ("203.0.113.9", "192.0.2.1", "2001:db8:1::1")]
        self.assertTrue(all(newcomers))
        self.assertEqual([m.closed for m in held[:4]], [True, True, True, False])    # those that came first went
        self.assertEqual(held[0].writer.said[-1]["method"], "client.show_message")
        self.assertEqual(sum(self.s.waiting.values()), stratum.MOST_WAITING)

    def test_a_place_opens_so_many_connections_a_minute(self):
        made = [come(self.s, self.now, "198.51.100.1", True) for _ in range(stratum.CONNECTS_PER_MINUTE)]
        self.assertTrue(all(made))
        self.assertIsNone(come(self.s, self.now, "198.51.100.1", True))           # one more within the minute
        self.assertIsNotNone(come(self.s, self.now, "198.51.100.2", True))        # another place has its own
        self.now[0] += 60.0 / stratum.CONNECTS_PER_MINUTE
        self.assertIsNotNone(come(self.s, self.now, "198.51.100.1", True))
        self.assertIsNotNone(come(self.s, self.now, "127.0.0.1", True))           # the pool's own machine is nobody's place

    def test_when_all_are_taken_one_without_shares_for_long_makes_room(self):
        s = stratum.Stratum(self.pool, "127.0.0.1:0", most=4)
        miners = [come(s, self.now, "198.51.100.%d" % (i + 1), True) for i in range(4)]     # four places, one each
        for m in miners:
            m.active = self.now[0]
        self.assertIsNone(come(s, self.now, "203.0.113.9"))      # all taken, none idle for long, no place holds two more
        miners[2].active -= stratum.STALE_SLOT                   # one that sent no share for STALE_SLOT
        self.assertIsNotNone(come(s, self.now, "203.0.113.9"))
        self.assertEqual([m.closed for m in miners], [False, False, True, False])
        self.assertIn("no share", miners[2].writer.said[-1]["params"][0])
        self.assertEqual((len(s.miners), sum(s.places.values())), (4, 4))

    def test_the_counters_hold_whatever_the_order(self):
        """The reviewer's check, kept: the counters of connections stay equal to the connections, whatever order things
        happen in (an authorization that ends after its connection was let go, rooms made, idle ones let go, loopback)."""
        s = stratum.Stratum(self.pool, "127.0.0.1:0", most=20, per_place=5, idle=50)
        s.most_waiting, s.waiting_per_place, s.stale_slot = 8, 3, 30
        rng = random.Random(7)
        sources = ["198.51.100.%d" % i for i in range(1, 9)] + ["127.0.0.1", "2001:db8:%x::1" % 5]
        pending = []                                        # authorizations in flight (an await in _authorize)
        for _ in range(20000):
            self.now[0] += rng.random()
            act = rng.random()
            if act < 0.35:
                src = rng.choice(sources)
                place = pool.place(src)
                if s._room(place):
                    m = stratum.Miner(FakeWriter(), src, b"\0\0\0\1", place, self.now[0])
                    s._enter(m)
                    pending.append(m)
            elif act < 0.6 and pending:
                m = pending.pop(rng.randrange(len(pending)))
                if not m.closed:                            # what _authorize does after its await
                    m.workers[ALICE] = ALICE
                    s._authorized(m)
                    m.active = self.now[0]
                elif rng.random() < 0.5:
                    s._authorized(m)                        # called anyway on a closed one: must change nothing
            elif act < 0.8 and s.miners:
                s._close(rng.choice(sorted(s.miners, key=id)))
            elif act < 0.9:
                s._let_go_idle(self.now[0])
            elif s.miners:
                m = rng.choice(sorted(s.miners, key=id))
                s._close(m)
                s._close(m)                                 # twice: counted once
            live = list(s.miners)
            self.assertTrue(all(not m.closed for m in live))
            self.assertEqual(dict(s.places), dict(collections.Counter(m.place for m in live)))
            self.assertEqual(dict(s.waiting), dict(collections.Counter(m.place for m in live if m.waiting)))
            self.assertLessEqual(len(live), s.most)
            self.assertLessEqual(sum(s.waiting.values()), s.most_waiting)


class N8N9N11TheOffice(unittest.TestCase):
    """The post office itself: what the bucket of all counts, what a used time tells, and what `asked` holds."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-review11b-")
        self.now = [1_800_000_000.0]
        self.bob = 0xB0B
        self.b = mail.inbox_of(self.bob)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def office(self, **k):
        return mail.PostOffice(tempfile.mkdtemp(dir=self.folder), clock=lambda: self.now[0], **k)

    def signed(self, office, t, secret=None, inbox=None):
        return schnorr_sign(mail.asked("fetch", inbox or self.b, t, (), office.origin), secret or self.bob).hex()

    def test_N8_requests_that_never_come_to_a_signature_take_nothing_from_anybody(self):
        office, t = self.office(signed_per_minute=5), int(self.now[0] * 1000)
        for bad in (("00", t, "00" * 64), (self.b, t - mail.SKEW - 1, "00" * 64), (self.b, t, "zz")):
            for _ in range(50):
                with self.assertRaises((mail.MailError, BadInput)):
                    office.letters(*bad)
        for i in range(5):                                  # Bob's five: the bucket is whole
            office.letters(self.b, t + i, self.signed(office, t + i))
        with self.assertRaises(mail.MailError) as caught:
            office.letters(self.b, t + 5, self.signed(office, t + 5))
        self.assertEqual((caught.exception.kind, caught.exception.status), ("SLOW", 429))

    def test_N9_a_used_time_is_told_only_to_whoever_signs_it(self):
        office, t = self.office(), int(self.now[0] * 1000)
        office.letters(self.b, t, self.signed(office, t))   # Bob reads
        for when in (t, t - 5, t + 5):                      # used, older, newer: all the same to a stranger
            with self.assertRaises(mail.MailError) as caught:
                office.letters(self.b, when, "00" * 64)
            self.assertEqual((caught.exception.kind, caught.exception.status), ("SIGNATURE", 403))
        with self.assertRaises(mail.MailError) as caught:
            office.letters(self.b, t, self.signed(office, t))
        self.assertEqual((caught.exception.kind, caught.exception.status), ("AGAIN", 409))

    def test_N11_the_table_of_inboxes_that_asked(self):
        office, t = self.office(new_inboxes=3), int(self.now[0] * 1000)
        plans = [row[-1] for row in office.db.execute("EXPLAIN QUERY PLAN DELETE FROM asked WHERE t < 5")]
        self.assertTrue(any("asked_t" in p for p in plans), plans)
        keys = [0x1000 + i for i in range(5)]
        for k in keys:                                      # five new inboxes from one place: all served ...
            inbox = mail.inbox_of(k)
            self.assertEqual(office.letters(inbox, t, self.signed(office, t, k, inbox), "198.51.100.7")["letters"], [])

        def rows():
            return office.db.execute("SELECT COUNT(*) FROM asked").fetchone()[0]
        self.assertEqual(rows(), 3)                         # ... three written down
        other = mail.inbox_of(0x2000)
        office.letters(other, t, self.signed(office, t, 0x2000, other), "198.51.100.8")
        self.assertEqual(rows(), 4)                         # another place has its own
        first = mail.inbox_of(keys[0])
        office.letters(first, t + 1, self.signed(office, t + 1, keys[0], first), "198.51.100.7")
        self.assertEqual((rows(), office._last(first)), (4, t + 1))     # one known already moves on, whatever the count
        self.now[0] += 86400                                # a new day, a new count
        t = int(self.now[0] * 1000)
        late = mail.inbox_of(keys[4])
        office.letters(late, t, self.signed(office, t, keys[4], late), "198.51.100.7")
        self.assertEqual(rows(), 5)


if __name__ == "__main__":
    unittest.main()
