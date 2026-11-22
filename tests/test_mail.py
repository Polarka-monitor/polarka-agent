"""Letters between agents: the seal, the signatures, the post office and its two doors, and sending through a
gate of postage with a lot of calls (the lot is put into the books here; buying one is x402's, tested there)."""
import base64
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from polarka_agent import mail, postage, x402
from polarka_agent.keys import BadInput, schnorr_sign


class FakeWallet:
    def __init__(self, folder, chain="regtest"):
        self.folder, self.chain = folder, chain
        os.makedirs(folder, exist_ok=True)

    def fresh_address(self):
        return "rnorth1unused"


class Seal(unittest.TestCase):
    def setUp(self):
        self.alice, self.bob = 0x1234567, 0xABCDEF0123
        self.a, self.b = mail.inbox_of(self.alice), mail.inbox_of(self.bob)

    def test_only_the_inbox_opens_it(self):
        for text in (b"", b"x", b"hello " * 1000, os.urandom(16000)):
            sealed = mail.seal(self.b, text)
            self.assertEqual(len(sealed), mail.OVERHEAD + len(text))
            self.assertEqual(mail.unseal(self.bob, sealed), text)
            if len(text) >= 16:
                self.assertNotIn(text[:64], sealed)
            with self.assertRaises(BadInput):
                mail.unseal(self.alice, sealed)                 # not for her
        sealed = bytearray(mail.seal(self.b, b"pay me 5"))
        sealed[-1] ^= 1
        with self.assertRaises(BadInput):
            mail.unseal(self.bob, bytes(sealed))                # changed on the way
        self.assertNotEqual(mail.seal(self.b, b"same"), mail.seal(self.b, b"same"))      # a key of its own every time

    def test_a_letter_says_who_wrote_it(self):
        got = mail.read(self.bob, mail.seal(self.b, mail.letter(self.alice, self.b, "ahoj, Bobe ✓", now=1000)))
        self.assertEqual(len(got.pop("digest")), 64)                  # what the reader's record of letters read keeps
        self.assertEqual(got, {"from": self.a, "said_from": self.a, "signed": True, "t": 1_000_000, "text": "ahoj, Bobe ✓"})
        # a letter that names Alice but was signed by someone else is read, but not as hers
        body = json.loads(mail.letter(0x999, self.b, "it is me, Alice"))
        body["from"] = self.a
        got = mail.read(self.bob, mail.seal(self.b, json.dumps(body).encode()))
        self.assertEqual((got["from"], got["said_from"], got["signed"]), (None, self.a, False))
        # one that Alice wrote to Carol and Carol passes on to Bob is not for Bob
        carol = 0x77
        to_carol = mail.letter(self.alice, mail.inbox_of(carol), "for Carol")
        with self.assertRaises(BadInput):
            mail.read(self.bob, mail.seal(self.b, to_carol))
        for bad in ("", "x" * (mail.MOST_TEXT + 1), "\ud800"):
            with self.assertRaises(BadInput):
                mail.letter(self.alice, self.b, bad)
        for bad in ("AB" * 32, "ab" * 31, "zz" * 32, "ff" * 32):     # upper case, short, not hex, not on the curve
            with self.assertRaises(BadInput):
                mail.check_inbox(bad)

    def test_the_key_of_a_folder(self):
        folder = tempfile.mkdtemp(prefix="polarka-mail-test-")
        try:
            with self.assertRaises(mail.MailError):
                mail.mail_key(folder, create=False)
            first = mail.mail_key(folder)
            self.assertEqual(mail.mail_key(folder), first)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, mail.KEY_FILE)).st_mode), 0o600)
            with open(os.path.join(folder, mail.KEY_FILE)) as f:
                self.assertEqual(json.load(f)["inbox"], mail.inbox_of(first))
            self.assertEqual(os.listdir(folder), [mail.KEY_FILE])      # written beside it and linked: nothing else left
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_key_is_made_once_whoever_asks_at_the_same_time(self):
        """Review 11, L4: the file comes into its place whole; two makers at once end with one key."""
        folder = tempfile.mkdtemp(prefix="polarka-mail-test-")
        try:
            got, start = [], threading.Barrier(8)

            def make():
                start.wait()
                got.append(mail.mail_key(folder))
            threads = [threading.Thread(target=make) for _ in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(len(set(got)), 1)
            self.assertEqual(os.listdir(folder), [mail.KEY_FILE])
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class Office(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="polarka-mail-test-")
        self.now = [1_800_000_000.0]
        self.office = mail.PostOffice(self.folder, clock=lambda: self.now[0])
        self.bob = 0xB0B
        self.b = mail.inbox_of(self.bob)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def signed(self, action, t, ids=(), office=None):
        return schnorr_sign(mail.asked(action, self.b, t, ids, self.office.origin if office is None else office), self.bob).hex()

    def test_in_out_and_gone(self):
        sealed = mail.seal(self.b, b"one")
        posted = self.office.post(self.b, sealed)
        self.assertEqual(posted["kept_until"], int(self.now[0]) + mail.UNREAD_KEEP)      # an inbox that never asked
        self.office.post(self.b, mail.seal(self.b, b"two"))
        t = int(self.now[0] * 1000)
        got = self.office.letters(self.b, t, self.signed("fetch", t))
        self.assertEqual([mail.unseal(self.bob, base64.b64decode(x["sealed"])) for x in got["letters"]], [b"one", b"two"])
        self.assertFalse(got["more"])
        with self.assertRaises(mail.MailError) as caught:       # the same signature again
            self.office.letters(self.b, t, self.signed("fetch", t))
        self.assertEqual(caught.exception.kind, "AGAIN")
        ids = [got["letters"][0]["id"]]
        self.assertEqual(self.office.ack(self.b, ids, t + 1, self.signed("ack", t + 1, ids)), {"gone": 1})
        left = self.office.letters(self.b, t + 2, self.signed("fetch", t + 2))["letters"]
        self.assertEqual([x["id"] for x in left], [got["letters"][1]["id"]])
        self.assertEqual(self.office.post(self.b, mail.seal(self.b, b"three"))["kept_until"], int(self.now[0]) + mail.KEEP)
        with self.assertRaises(mail.MailError) as caught:       # signed for another post office (review 11, L3)
            self.office.letters(self.b, t + 3, self.signed("fetch", t + 3, office="https://elsewhere.example"))
        self.assertEqual(caught.exception.kind, "SIGNATURE")
        self.assertEqual(self.office.total, sum(len(r[0]) for r in self.office.db.execute("SELECT sealed FROM letters")))

    def test_who_may_take_them(self):
        self.office.post(self.b, mail.seal(self.b, b"secret"))
        t = int(self.now[0] * 1000)
        other = schnorr_sign(mail.asked("fetch", self.b, t), 0xBAD).hex()
        for args, kind in (((t, other), "SIGNATURE"), ((t - mail.SKEW - 1, self.signed("fetch", t - mail.SKEW - 1)), "TIME"),
                           ((t, "00"), "INPUT"), ((str(t), self.signed("fetch", t)), "INPUT"),
                           ((t, schnorr_sign(mail.asked("ack", self.b, t), self.bob).hex()), "SIGNATURE")):
            with self.assertRaises(mail.MailError) as caught:
                self.office.letters(self.b, *args)
            self.assertEqual(caught.exception.kind, kind)
        ids = ["ab" * 12]
        with self.assertRaises(mail.MailError):                 # a signature over other ids
            self.office.ack(self.b, ids, t, self.signed("ack", t, ["cd" * 12]))
        for bad in ([], ["x"], "ab" * 12, ["ab" * 12] * (mail.FETCH_MOST * 2 + 1)):
            with self.assertRaises(mail.MailError):
                self.office.ack(self.b, bad, t, self.signed("ack", t, ids))

    def test_bounds_and_time(self):
        for bad in (b"", mail.VERSION * mail.OVERHEAD, b"\x02" + bytes(100), bytes([1]) + bytes(mail.MOST_SEALED)):
            with self.assertRaises(mail.MailError):
                self.office.post(self.b, bad)
        with self.assertRaises(BadInput):
            self.office.post("ab" * 31, mail.seal(self.b, b"x"))
        sealed = mail.seal(self.b, bytes(mail.MOST_SEALED - mail.OVERHEAD))
        for _ in range(mail.MOST_INBOX // len(sealed)):
            self.office.post(self.b, sealed)
        with self.assertRaises(mail.MailError) as caught:
            self.office.post(self.b, sealed)
        self.assertEqual((caught.exception.kind, caught.exception.status), ("FULL", 507))
        self.now[0] += mail.KEEP + 1                            # a week later nothing is left
        self.office.expire()
        self.assertEqual(self.office.total, 0)
        self.office.post(self.b, mail.seal(self.b, b"new"))
        t = int(self.now[0] * 1000)
        self.assertEqual(len(self.office.letters(self.b, t, self.signed("fetch", t))["letters"]), 1)
        small = mail.PostOffice(tempfile.mkdtemp(dir=self.folder), most_all=200)
        small.post(self.b, mail.seal(self.b, b"x" * 50))
        with self.assertRaises(mail.MailError):
            small.post(self.b, mail.seal(self.b, b"x" * 50))     # the post office is full for everybody

    def test_what_a_place_and_an_unread_inbox_may_hold(self):
        """Review 11, H5 and M2: letters to an inbox that has not asked for its letters go after UNREAD_KEEP, expiring
        reads the index on time, and one place posts at most PLACE_BYTES_A_DAY a day."""
        carol = mail.inbox_of(0xCA401)
        self.office.post(carol, mail.seal(carol, b"for nobody who reads"))
        self.office.post(self.b, mail.seal(self.b, b"for Bob"))
        t = int(self.now[0] * 1000)
        self.office.letters(self.b, t, self.signed("fetch", t))  # Bob asks: his inbox reads
        self.now[0] += mail.UNREAD_KEEP + 1
        self.office.expire()
        self.assertEqual([r[0] for r in self.office.db.execute("SELECT inbox FROM letters")], [self.b])
        self.assertEqual(self.office.total, sum(len(r[0]) for r in self.office.db.execute("SELECT sealed FROM letters")))
        plans = [row[-1] for row in self.office.db.execute(
            "EXPLAIN QUERY PLAN DELETE FROM letters WHERE t < ? AND (t < ? OR inbox NOT IN (SELECT inbox FROM asked))", (1, 1))]
        self.assertTrue(any("letters_t" in p for p in plans), plans)
        small = mail.PostOffice(tempfile.mkdtemp(dir=self.folder), place_bytes=300)
        sealed = mail.seal(self.b, b"x" * 100)
        small.post(self.b, sealed, "198.51.100.7")
        with self.assertRaises(mail.MailError) as caught:
            small.post(self.b, sealed, "198.51.100.7")
        self.assertEqual((caught.exception.kind, caught.exception.status), ("QUOTA", 429))
        small.post(self.b, sealed, "198.51.100.8")              # another place has its own

    def test_an_answer_holds_what_a_reader_takes(self):
        """Review 11, H3: fifty letters of the largest size go out in answers of at most FETCH_BYTES."""
        sealed = mail.seal(self.b, bytes(mail.MOST_SEALED - mail.OVERHEAD))
        for _ in range(mail.FETCH_MOST):
            self.office.post(self.b, sealed)
        t, taken = int(self.now[0] * 1000), 0
        while True:
            got = self.office.letters(self.b, t, self.signed("fetch", t))
            self.assertLessEqual(sum(len(base64.b64decode(x["sealed"])) for x in got["letters"]), mail.FETCH_BYTES)
            self.assertLess(len(json.dumps(got)), mail.MOST_ANSWER)
            ids = [x["id"] for x in got["letters"]]
            taken += len(ids)
            self.office.ack(self.b, ids, t + 1, self.signed("ack", t + 1, ids))
            t += 2
            if not got["more"]:
                break
        self.assertEqual(taken, mail.FETCH_MOST)


class ThroughTheGate(unittest.TestCase):
    """Alice writes to Bob through a gate of postage before the paid door; Bob reads at the free door."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-mail-test-")
        self.office = mail.PostOffice(os.path.join(self.tmp, "office"))
        self.paid, self.free = mail.servers(self.office, "127.0.0.1:0", "127.0.0.1:0", per_minute=600)
        self.gate = postage.Gate(FakeWallet(os.path.join(self.tmp, "gate")), 1, 10)
        self.front = postage.Bounded(("127.0.0.1", 0), postage.make_handler(
            self.gate, "127.0.0.1:1", False, [("/v1/mail", "127.0.0.1:%d" % self.paid.server_address[1])]), most=16)
        for s in (self.paid, self.free, self.front):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.gate_url = "http://127.0.0.1:%d" % self.front.server_address[1]
        self.free_url = "http://127.0.0.1:%d" % self.free.server_address[1]
        self.office.origin = mail.origin_of(self.free_url)     # the post office as its readers call it here
        self.alice = FakeWallet(os.path.join(self.tmp, "alice"))
        self.bob = FakeWallet(os.path.join(self.tmp, "bob"))
        self.b = mail.my_inbox(self.bob)["inbox"]
        self.bought = []

    def tearDown(self):
        for s in (self.paid, self.free, self.front):
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lot(self, calls):
        """A lot of postage as x402 would have bought it: known to the gate, and in Alice's books."""
        token = os.urandom(16).hex()
        self.gate.state["tokens"][postage.mark(token)] = {"address": "x", "expires": 0, "paid": True, "calls": calls, "price": 1}
        return token

    def buy(self, calls):
        """x402.fetch as it buys a lot with the letter itself: a lot of `calls` the gate knows, the letter posted as its
        first call, and what is left of it in the writer's books (review 11, L5)."""
        def fetch(url, wallet, most, timeout=60, wait=600, method="GET", body=None, key=None, **k):
            self.bought.append((key or url, most, method))
            token = self.lot(calls)
            req = urllib.request.Request(url, data=body, method=method, headers={"X-Postage": token, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    status, headers, answer = r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                status, headers, answer = e.code, e.headers, e.read()
            left = int(headers.get("X-Postage-Calls-Left"))
            with x402.BOOK(wallet):
                book = x402._book(wallet)
                if left:
                    book["lots"][self.gate_url] = {"claim": token, "left": left}
                x402._keep_book(wallet, book)
            return {"status": status, "paid": "0.01", "calls_left": left, "body": answer}
        return fetch

    def test_a_letter_there_and_an_answer_back(self):
        saved = x402.fetch
        x402.fetch = self.buy(3)
        try:
            sent = mail.send(self.alice, self.b, "Hello Bob, shall we trade?", 5000, self.gate_url)
            self.assertEqual((sent["sent"], sent["calls_left"], self.bought), (True, 2, [(self.gate_url + "/v1/mail", 5000, "POST")]))
            self.assertEqual(mail.send(self.alice, self.b, "And a second one.", 5000, self.gate_url)["calls_left"], 1)
            self.assertEqual(len(self.bought), 1)                  # the lot is used before anything is bought again
            mail.send(self.alice, self.b, "A third.", 5000, self.gate_url)
            self.assertEqual(len(self.bought), 1)
            mail.send(self.alice, self.b, "The lot is used up: a new one.", 5000, self.gate_url)
            self.assertEqual(len(self.bought), 2)
        finally:
            x402.fetch = saved
        got = mail.fetch_letters(self.bob, self.free_url)
        a = mail.my_inbox(self.alice)["inbox"]
        self.assertEqual([(x["from"], x["text"]) for x in got["letters"]],
                         [(a, "Hello Bob, shall we trade?"), (a, "And a second one."), (a, "A third."), (a, "The lot is used up: a new one.")])
        self.assertEqual(got["acknowledged"], 4)
        self.assertEqual(mail.fetch_letters(self.bob, self.free_url)["letters"], [])
        # Bob answers to the inbox the letter came from, with a lot of his own
        x402.fetch = self.buy(10)
        try:
            mail.send(self.bob, a, "Yes.", 5000, self.gate_url)
        finally:
            x402.fetch = saved
        self.assertEqual([x["text"] for x in mail.fetch_letters(self.alice, self.free_url, keep=True)["letters"]], ["Yes."])
        self.assertEqual(len(mail.fetch_letters(self.alice, self.free_url)["letters"]), 1)       # kept, then let go

    def test_no_postage_no_letter(self):
        url = self.gate_url + "/v1/mail/" + self.b
        body = json.dumps({"sealed": base64.b64encode(mail.seal(self.b, b"spam")).decode()}).encode()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=10)
        self.assertEqual(caught.exception.code, 402)
        # the free door takes no letters, and the paid door hands none out
        for base, path, data in ((self.free_url, "/v1/mail/" + self.b, body), (self.gate_url.replace(str(self.front.server_address[1]), str(self.paid.server_address[1])), "/v1/inbox/" + self.b + "?t=1&sig=00", None)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(base + path, data=data), timeout=10)
            self.assertEqual(caught.exception.code, 404)
        # a lot the gate does not know is let go by the sender, who buys one and sends again
        with x402.BOOK(self.alice):
            book = x402._book(self.alice)
            book["lots"][self.gate_url] = {"claim": "ab" * 16, "left": 7}
            x402._keep_book(self.alice, book)
        saved = x402.fetch
        x402.fetch = self.buy(2)
        try:
            self.assertEqual(mail.send(self.alice, self.b, "after all", 5000, self.gate_url)["sent"], True)
        finally:
            x402.fetch = saved
        self.assertEqual(len(self.bought), 1)

    def test_too_long_or_too_many_before_anything_is_paid(self):
        with self.assertRaises(mail.MailError) as caught:
            mail.send(self.alice, self.b, "\U0001F600" * 7000, 5000, self.gate_url)      # 7000 characters, 28 000 bytes
        self.assertEqual(caught.exception.kind, "TOO_LONG")
        self.assertEqual(self.bought, [])
        # one place sends at most LETTERS_PER_MINUTE letters a minute, whatever postage it holds
        quick = mail.servers(self.office, "127.0.0.1:0", "127.0.0.1:0", letters_per_minute=2)[0]
        threading.Thread(target=quick.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/v1/mail/%s" % (quick.server_address[1], self.b)
            body = json.dumps({"sealed": base64.b64encode(mail.seal(self.b, b"x")).decode()}).encode()
            codes = []
            for _ in range(3):
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=10) as r:
                        codes.append(r.status)
                except urllib.error.HTTPError as e:
                    codes.append(e.code)
            self.assertEqual(codes, [200, 200, 429])
        finally:
            quick.shutdown()
            quick.server_close()

    def test_what_the_doors_say(self):
        with urllib.request.urlopen(self.free_url + "/v1/inbox", timeout=10) as r:
            self.assertEqual(json.loads(r.read())["service"], "polarka-mail")
        with urllib.request.urlopen("http://127.0.0.1:%d/v1/mail" % self.paid.server_address[1], timeout=10) as r:
            self.assertIn("POST /v1/mail/<inbox>", json.loads(r.read())["how"][0])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen("%s/v1/inbox/%s?t=x&sig=00" % (self.free_url, self.b), timeout=10)
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(self.free_url + "/v1/inbox/%s/ack" % self.b, data=b"[1]"), timeout=10)
        self.assertEqual(caught.exception.code, 400)

    def test_many_large_letters_come_in_more_than_one_answer(self):
        """Review 11, H3: an inbox full of large letters is read whole, in answers that a reader takes."""
        alice = 0xA11CE
        for i in range(mail.FETCH_MOST):
            self.office.post(self.b, mail.seal(self.b, mail.letter(alice, self.b, "é" * 7000 + "%02d" % i)))
        first = mail.fetch_letters(self.bob, self.free_url)
        self.assertTrue(first["more"])
        self.assertEqual(first["acknowledged"], len(first["letters"]))
        second = mail.fetch_letters(self.bob, self.free_url)
        self.assertFalse(second["more"])
        self.assertEqual([x["text"][-2:] for x in first["letters"] + second["letters"]], ["%02d" % i for i in range(mail.FETCH_MOST)])

    def test_an_answer_that_cannot_be_read_is_no_answer(self):
        class Cut(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"letters": [' + b" " * 100
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        cut = ThreadingHTTPServer(("127.0.0.1", 0), Cut)
        threading.Thread(target=cut.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(mail.MailError) as caught:
                mail.fetch_letters(self.bob, "http://127.0.0.1:%d" % cut.server_address[1])
            self.assertEqual(caught.exception.kind, "ANSWER")
        finally:
            cut.shutdown()
            cut.server_close()

    def test_asking_what_it_is_takes_nobody_s_calls(self):
        """Review 11, H4: the free door's page about itself takes from no bucket, and each place has a share of signed
        calls of its own."""
        door = mail.servers(self.office, "127.0.0.1:0", "127.0.0.1:0", trust_proxy=True, per_minute=2)[1]
        threading.Thread(target=door.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % door.server_address[1]
            self.office.origin = mail.origin_of(base)

            def get(path, who):
                req = urllib.request.Request(base + path, headers={"X-Forwarded-For": who})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code
            self.assertEqual({get("/v1/inbox", "198.51.100.%d" % (i % 12 + 1)) for i in range(60)}, {200})
            key, codes = mail.mail_key(self.bob.folder), []
            for who in ("203.0.113.77", "203.0.113.77", "203.0.113.77", "203.0.113.78"):
                t = int(time.time() * 1000) + len(codes)
                sig = schnorr_sign(mail.asked("fetch", self.b, t, (), self.office.origin), key).hex()
                codes.append(get("/v1/inbox/%s?t=%d&sig=%s" % (self.b, t, sig), who))
            self.assertEqual(codes, [200, 200, 429, 200])
        finally:
            door.shutdown()
            door.server_close()

    def test_a_letter_refused_costs_no_call(self):
        """Review 11, H5: the call of a letter that the post office refuses for its limit goes back to the lot."""
        quick = mail.servers(self.office, "127.0.0.1:0", "127.0.0.1:0", letters_per_minute=1)[0]
        front = postage.Bounded(("127.0.0.1", 0), postage.make_handler(
            self.gate, "127.0.0.1:1", False, [("/v1/mail", "127.0.0.1:%d" % quick.server_address[1])]), most=16)
        for srv in (quick, front):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            token, codes = self.lot(10), []
            body = json.dumps({"sealed": base64.b64encode(mail.seal(self.b, b"x")).decode()}).encode()
            for _ in range(2):
                req = urllib.request.Request("http://127.0.0.1:%d/v1/mail/%s" % (front.server_address[1], self.b), data=body,
                                             headers={"X-Postage": token, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        codes.append((r.status, r.headers.get("X-Postage-Calls-Left")))
                except urllib.error.HTTPError as e:
                    codes.append((e.code, e.headers.get("X-Postage-Calls-Left")))
            self.assertEqual(codes, [(200, "9"), (429, "9")])
            self.assertEqual(self.gate.left(token), 9)
        finally:
            for srv in (quick, front):
                srv.shutdown()
                srv.server_close()

    def test_a_letter_delivered_again_is_not_shown_again(self):
        """Review 11, M4: the post office cannot hand a letter over twice as if it were new, nor an old one."""
        alice = 0xA11CE
        sealed = mail.seal(self.b, mail.letter(alice, self.b, "pay the bearer 5 NORTH"))
        self.office.post(self.b, sealed)
        self.office.post(self.b, sealed)                        # twice in one batch
        got = mail.fetch_letters(self.bob, self.free_url)["letters"]
        self.assertEqual([x.get("text") for x in got], ["pay the bearer 5 NORTH", None])
        self.assertTrue(got[1]["repeat"])
        self.office.post(self.b, sealed)                        # and a week later, by whoever kept it
        again = mail.fetch_letters(self.bob, self.free_url)
        self.assertEqual(([x.get("text") for x in again["letters"]], again["letters"][0]["repeat"], again["acknowledged"]), ([None], True, 1))
        self.office.post(self.b, mail.seal(self.b, mail.letter(alice, self.b, "an old one", now=time.time() - mail.KEEP - 600)))
        late = mail.fetch_letters(self.bob, self.free_url)["letters"]
        self.assertIn("days ago", late[0]["error"])
        self.assertNotIn("text", late[0])
        with open(os.path.join(self.bob.folder, mail.SEEN_FILE)) as f:
            self.assertEqual(len(json.load(f)), 1)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.bob.folder, mail.SEEN_FILE)).st_mode), 0o600)

    def test_two_senders_of_one_wallet_lose_no_lot(self):
        """Review 11, M3: the command line and an MCP server of one wallet use the same lot at once; what each hears of
        it goes to that lot only, and the spare one bought before stays."""
        origin = self.gate_url
        used, spare = {"claim": "aa" * 16, "left": 2}, {"claim": "bb" * 16, "left": 100}
        with x402.BOOK(self.alice):
            book = x402._book(self.alice)
            book["lots"][origin], book["spare"] = dict(used), {origin: [dict(spare)]}
            x402._keep_book(self.alice, book)
        slow = threading.Event()

        def call(url, headers=None, obj=None, timeout=30):
            if threading.current_thread().name == "A":
                slow.wait(10)                                   # A's answer comes late
                return 200, {"X-Postage-Calls-Left": "1"}, {"ok": True, "id": "a" * 24}
            return 200, {"X-Postage-Calls-Left": "0"}, {"ok": True, "id": "b" * 24}
        saved, mail._call = mail._call, call
        try:
            a = threading.Thread(target=mail.send, args=(self.alice, self.b, "from A", 1000, origin), name="A")
            a.start()
            time.sleep(0.5)
            mail.send(self.alice, self.b, "from B", 1000, origin)   # B: the lot is used up, the spare one takes its place
            slow.set()
            a.join(10)
        finally:
            mail._call = saved
        with x402.BOOK(self.alice):
            book = x402._book(self.alice)
        self.assertEqual(book["lots"][origin]["claim"], spare["claim"])


if __name__ == "__main__":
    unittest.main()
