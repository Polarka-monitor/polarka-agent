"""A proof paid for before costs nothing: the gate first only looks behind itself (X-Polarka-Look) for a GET that
carries no payment, and what the stamps service knows is given for free; a SHA-256 it does not know is not taken
and costs as always. Nobody outside can make the gate's look, nor turn a paid call into one. Needs no node."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polarka_agent import postage, stamps, x402
from polarka_agent.httpguard import LOOK
from tests.test_postage_usdc import Evm, TO, paid
from tests.test_stamps_storage import Down, Payer


def d(n):
    return hashlib.sha256(b"look %d" % n).hexdigest()


class Folder:
    def __init__(self, path):
        self.folder = path


class FreeLook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-look-")
        os.makedirs(os.path.join(self.tmp, "gate"))
        self.service = stamps.Stamps(Payer(), Down(), os.path.join(self.tmp, "stamps"), every=600)
        self.evm = Evm()
        self.gate = postage.Gate(Folder(os.path.join(self.tmp, "gate")), 10 ** 6, 100, looks=["/v1/stamp"],
                                 usdc=postage.Usdc(self.evm, TO, 10000, ["/v1/stamp"]))
        backend = ThreadingHTTPServer(("127.0.0.1", 0), stamps.make_handler(self.service))
        before = ThreadingHTTPServer(("127.0.0.1", 0), stamps.make_handler(self.service))
        front = ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(
            self.gate, "127.0.0.1:%d" % before.server_address[1], False, [("/v1/stamp", "127.0.0.1:%d" % backend.server_address[1])]))
        for s in (backend, before, front):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
        self.url = "http://127.0.0.1:%d" % front.server_address[1]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ask(self, path, payment=None, data=None, headers=None):
        h = dict(headers or {})
        if payment:
            h["PAYMENT-SIGNATURE"] = x402.b64json(payment)
        req = urllib.request.Request(self.url + path, data, h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read())

    def test_a_sha256_paid_for_is_shown_again_for_nothing_and_one_not_paid_is_not_taken(self):
        status, headers, _ = self.ask("/v1/stamp/" + d(1))
        self.assertEqual(status, 402)
        self.assertIn("PAYMENT-REQUIRED", headers)
        self.assertEqual(self.service.waiting, set())                       # the look took nothing
        status, _, said = self.ask("/v1/stamp/" + d(1), paid(1))
        self.assertEqual((status, said["state"]), (200, "waiting"))
        status, headers, said = self.ask("/v1/stamp/" + d(1))              # no payment: it is known, so it is free
        self.assertEqual((status, said["state"]), (200, "waiting"))
        self.assertNotIn("PAYMENT-RESPONSE", headers)
        self.assertEqual(len(self.evm.calls), 1)
        status, _, said = self.ask("/v1/stamp")                             # what the service is: free too
        self.assertEqual((status, said["service"]), (200, "polarka-stamps"))

    def test_nobody_outside_makes_the_look_or_turns_a_paid_call_into_one(self):
        status, _, _ = self.ask("/v1/stamp/" + d(2), headers={LOOK: "1"})
        self.assertEqual(status, 402)                                       # not "unknown" from the service: the gate's 402
        status, _, said = self.ask("/v1/stamp/" + d(2), paid(2), headers={LOOK: "1"})
        self.assertEqual((status, said["state"]), (200, "waiting"))         # paid: taken, the header did not reach the service
        self.assertIn(d(2), self.service.waiting)

    def test_only_a_get_without_a_body_is_looked_up(self):
        self.assertEqual(self.ask("/v1/stamp/" + d(3), paid(3))[0], 200)
        self.assertEqual(self.ask("/v1/stamp/" + d(3), data=b"x")[0], 402)        # a POST with a body costs as always
        self.assertEqual(self.ask("/v1/stamp/" + d(3))[0], 200)

    def test_looks_from_one_place_are_bounded(self):
        self.assertEqual(self.ask("/v1/stamp/" + d(4), paid(4))[0], 200)
        got = [self.ask("/v1/stamp/" + d(4))[0] for _ in range(postage.LOOKS_PER_MINUTE + 1)]
        self.assertEqual(got[:postage.LOOKS_PER_MINUTE], [200] * postage.LOOKS_PER_MINUTE)
        self.assertEqual(got[-1], 402)                                      # past the bound it costs as any call

    def test_the_service_looks_without_taking(self):
        self.assertIsNone(self.service.look(d(5)))
        self.assertIsNone(self.service.look("nonsense"))
        self.assertEqual(self.service.waiting, set())
        self.service.take(d(5))
        self.assertEqual(self.service.look(d(5).upper())["state"], "waiting")


if __name__ == "__main__":
    unittest.main()
