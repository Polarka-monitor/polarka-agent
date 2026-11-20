"""How the stamps service keeps what it took: a digest is one line appended, a batch's digests are written once
into their own file, the format before is moved on start, and a line torn by a crash is not taken for a digest.
Needs no node."""
import hashlib
import json
import os
import shutil
import tempfile
import unittest

from polarka_agent import postage, stamps


def d(n):
    return hashlib.sha256(b"storage %d" % n).hexdigest()


class Down:
    def call(self, *a):
        raise OSError("no node here")


class Payer:
    def __init__(self):
        self.n = 0

    def stamp(self, root, memo=None):
        self.n += 1
        return {"id": "%064x" % self.n}


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-stamps-storage-")
        self.now = [1_000_000.0]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, wallet=None):
        return stamps.Stamps(wallet or Payer(), Down(), self.tmp, every=600, clock=lambda: self.now[0])

    def book(self):
        with open(os.path.join(self.tmp, "stamps.json")) as f:
            return json.load(f)

    def test_taking_appends_and_survives_a_restart(self):
        s = self.make()
        s.take(d(1))
        s.take(d(2))
        s.take(d(1))                                        # the same one twice is one line
        with open(os.path.join(self.tmp, "waiting.log")) as f:
            self.assertEqual(f.read().split(), [d(1), d(2)])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "stamps.json")))   # nothing rewritten for a take
        with open(os.path.join(self.tmp, "waiting.log"), "a") as f:
            f.write(d(3)[:20])                              # a crash in the middle of a line
        again = self.make()
        self.assertEqual(again.waiting, {d(1), d(2)})
        self.assertEqual(oct(os.stat(os.path.join(self.tmp, "waiting.log")).st_mode & 0o777), "0o600")

    def test_a_batch_goes_to_its_own_file_and_empties_the_log(self):
        s = self.make()
        for n in range(5):
            s.take(d(n))
        self.now[0] += 601
        made = s.tick()
        self.assertEqual(made["stamps"], 5)
        with open(os.path.join(self.tmp, "batches", "0")) as f:
            self.assertEqual(sorted(f.read().split()), sorted(d(n) for n in range(5)))
        with open(os.path.join(self.tmp, "waiting.log")) as f:
            self.assertEqual(f.read(), "")
        b = self.book()
        self.assertNotIn("digests", b["batches"][0])
        self.assertNotIn("waiting", b)
        self.assertEqual(b["batches"][0]["count"], 5)
        self.assertEqual(s.take(d(2))["state"], "written")
        again = self.make()
        self.assertEqual(again.take(d(4))["state"], "written")
        self.assertEqual(again.waiting, set())

    def test_a_crash_after_the_batch_before_the_log(self):
        s = self.make()
        s.take(d(7))
        self.now[0] += 601
        s.tick()
        with open(os.path.join(self.tmp, "waiting.log"), "w") as f:
            f.write(d(7) + "\n")                            # the log as it was before its rewrite
        again = self.make()
        self.assertEqual(again.waiting, set())
        self.assertEqual(again.take(d(7))["state"], "written")

    def test_the_format_before_is_moved(self):
        old = {"waiting": [d(9)], "scanned": None, "last": 0,
               "batches": [{"root": "00" * 32, "digests": [d(5), d(6)], "txid": "ab" * 32, "transaction": None,
                            "made": 1, "block": None}]}
        with open(os.path.join(self.tmp, "stamps.json"), "w") as f:
            json.dump(old, f)
        s = self.make()
        self.assertEqual(s.waiting, {d(9)})
        self.assertEqual(s.take(d(6))["state"], "written")
        b = self.book()
        self.assertNotIn("digests", b["batches"][0])
        self.assertNotIn("waiting", b)
        with open(os.path.join(self.tmp, "batches", "0")) as f:
            self.assertEqual(f.read().split(), [d(5), d(6)])
        with open(os.path.join(self.tmp, "waiting.log")) as f:
            self.assertEqual(f.read().split(), [d(9)])


class GateTimeout(unittest.TestCase):
    def test_the_gate_does_not_wait_for_ever_on_a_client(self):
        H = postage.make_handler(None, "127.0.0.1:1", False)
        self.assertEqual(H.timeout, 60)


if __name__ == "__main__":
    unittest.main()
