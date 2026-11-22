"""Letters between agents: the seal, the signatures, the post office and its two doors, and sending through a
gate of postage with a lot of calls (the lot is put into the books here; buying one is x402's, tested there)."""
import base64
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

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

    def signed(self, action, t, ids=()):
        return schnorr_sign(mail.asked(action, self.b, t, ids), self.bob).hex()

    def test_in_out_and_gone(self):
        sealed = mail.seal(self.b, b"one")
        posted = self.office.post(self.b, sealed)
        self.assertEqual(posted["kept_until"], int(self.now[0]) + mail.KEEP)
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
        self.office.post(self.b, mail.seal(self.b, b"new"))
        t = int(self.now[0] * 1000)
        self.assertEqual(len(self.office.letters(self.b, t, self.signed("fetch", t))["letters"]), 1)
        small = mail.PostOffice(tempfile.mkdtemp(dir=self.folder), most_all=200)
        small.post(self.b, mail.seal(self.b, b"x" * 50))
        with self.assertRaises(mail.MailError):
            small.post(self.b, mail.seal(self.b, b"x" * 50))     # the post office is full for everybody


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
        def fetch(url, wallet, most, timeout=60, wait=600, **k):
            self.bought.append((url, most))
            token = self.lot(calls)
            with x402.BOOK(wallet):
                book = x402._book(wallet)
                book["lots"][self.gate_url] = {"claim": token, "left": calls}
                x402._keep_book(wallet, book)
            return {"status": 200, "paid": "0.01", "body": b"{}"}
        return fetch

    def test_a_letter_there_and_an_answer_back(self):
        saved = x402.fetch
        x402.fetch = self.buy(3)
        try:
            sent = mail.send(self.alice, self.b, "Hello Bob, shall we trade?", 5000, self.gate_url)
            self.assertEqual((sent["sent"], sent["calls_left"], self.bought), (True, 2, [(self.gate_url + "/v1/mail", 5000)]))
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


if __name__ == "__main__":
    unittest.main()
