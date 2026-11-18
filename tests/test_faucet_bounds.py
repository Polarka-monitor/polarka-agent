"""What bounds the faucet beside the wallet's limits, and what it does when something goes wrong. These came
out of an independent review of version 0.1.2: a script from one place, a claim sent again and again, a crash
between the payment and the book, requests that never arrive. Against the small model of a wallet server."""
import http.client
import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile

from polarka_agent import faucet, keys
from polarka_agent.wallet import Refused, Wallet
from tests.test_wallet_rules import Model, WORDS

N = keys.UNIT


def address(n):
    return keys.Account.from_words(keys.new_words(12), "regtest").address(0, n)


class FaucetTest(unittest.TestCase):
    """A faucet with eight coins of 10 NORTH that may give 6.5 a day, and a clock the test moves."""

    def setUp(self):
        self.m = Model()
        self.now = [1_800_000_000]
        self.folder = os.path.join(tempfile.mkdtemp(prefix="polarka-faucet-"), "w")
        Wallet.create(self.folder, "regtest", self.m.fake.url, WORDS)
        with open(os.path.join(self.folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "2.1", "per_day_north": "6.5"}, f)
        self.w = Wallet(self.folder, clock=lambda: self.now[0])
        for i in range(8):
            self.m.coin(self.w.account.address(0, i), 10 * N, "%02x" % (i + 1) * 32)
        self.f = faucet.Faucet(self.w, 2 * N, base_bits=8, clock=lambda: self.now[0])

    def tearDown(self):
        self.w.server.close()
        self.m.fake.stop()

    def claim(self, to, source="1.2.3.4"):
        c = self.f.challenge(source, to)
        return self.f.claim(source, to, c["challenge"], faucet.solve(c["challenge"], c["bits"]))

    def refused(self, kind, call, *a):
        with self.assertRaises(Refused) as e:
            call(*a)
        self.assertEqual(e.exception.kind, kind)


class Bounds(FaucetTest):
    def setUp(self):
        super().setUp()
        self.limits("50")

    def limits(self, a_day):
        with open(os.path.join(self.folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "2.1", "per_day_north": a_day}, f)

    def new(self, **k):
        self.f = faucet.Faucet(self.w, 2 * N, base_bits=8, clock=lambda: self.now[0], **k)
        return self.f

    def solved(self, to, source):
        c = self.f.challenge(source, to)
        return c["challenge"], faucet.solve(c["challenge"], c["bits"])

    def test_one_place_gets_two_a_day_and_an_ipv6_48_is_one_place(self):
        self.claim(address(0), "9.9.9.9")
        self.claim(address(1), "9.9.9.9")
        self.refused("TOO_OFTEN", self.f.challenge, "9.9.9.9", address(2))
        self.claim(address(3), "2001:db8:1:2::1")
        self.claim(address(4), "2001:db8:1:2:ffff::9")
        self.refused("TOO_OFTEN", self.f.challenge, "2001:db8:1:2:1234::1", address(5))
        self.refused("TOO_OFTEN", self.f.challenge, "2001:db8:1:ff00::1", address(5))     # the same /48: one customer's addresses
        self.assertIn("challenge", self.f.challenge("2001:db8:2:3::1", address(5)))       # the /48 next door
        self.assertEqual(len(self.m.handed), 4)
        self.now[0] += faucet.PERIOD + 1
        self.claim(address(6), "9.9.9.9")

    def test_the_faucet_gives_only_so_much_in_an_hour(self):
        self.new(per_hour=2)
        self.claim(address(0), "a")
        self.claim(address(1), "b")
        self.refused("LATER", self.f.challenge, "c", address(2))
        self.now[0] += 3601
        self.claim(address(2), "c")
        self.assertEqual(len(self.m.handed), 3)
        # and a faucet started again knows what it paid in the last hour: the wallet's journal says so
        self.new(per_hour=1)
        self.refused("LATER", self.f.challenge, "d", address(3))

    def test_capitals_are_the_same_address(self):
        to = address(0)
        challenge, answer = self.solved(to.upper(), "a")
        self.assertEqual(self.f.claim("a", to, challenge, answer)["paid"], "2.00000000")
        self.refused("ALREADY", self.f.challenge, "b", to.upper())
        self.refused("ALREADY", self.f.challenge, "b", to)
        self.assertEqual(len(self.m.handed), 1)

    def test_free_challenges_raise_the_work_of_the_place_that_fetched_them_and_of_nobody_else(self):
        got = [self.f.challenge("hoarder", address(i))["bits"] for i in range(20)]
        self.assertEqual(got[:5], [8, 8, 8, 8, 9])                         # four questions at the common work, then more
        self.assertEqual(got[-1], faucet.ASK_BITS)                         # and never past what an asker does unasked
        self.assertEqual(self.f.challenge("honest", address(30))["bits"], 8)
        self.assertEqual(self.f.bits(), 8)
        self.assertEqual(self.m.handed, [])

    def test_work_that_was_done_raises_the_work_for_all(self):
        for i in range(8):
            self.claim(address(i), "s%d" % i)
        self.assertEqual(self.f.bits(), 9)
        self.assertEqual(self.f.challenge("late", address(9))["bits"], 9)

    def test_a_payment_in_the_way_until_the_next_block_is_no_alarm_and_an_empty_wallet_is_one(self):
        pay = self.w.pay
        def stuck(*a, **k):
            raise Refused("OPEN_PAYMENT", "a payment that did not go through holds the coin")
        self.w.pay = stuck
        self.refused("LATER", self.claim, address(0), "a")
        self.assertEqual((self.f.trouble_until, self.f.dries), (0, 1))
        self.w.pay = pay
        self.now[0] += 121
        self.assertEqual(self.claim(address(0), "a")["paid"], "2.00000000")
        self.assertEqual(self.f.dries, 0)
        self.m.coins = []                                                   # and a wallet with nothing in it
        for i in range(5):
            self.now[0] += 121
            self.refused("LATER", self.claim, address(10 + i), "e%d" % i)
        self.assertEqual(self.f.dries, 5)
        self.assertEqual(self.f.dry_until, self.now[0] + 600)

    def test_a_limit_a_hair_above_the_amount_does_not_start_either(self):
        with open(os.path.join(self.folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "2.000001", "per_day_north": "50"}, f)
        self.refused("LIMITS", faucet.serve, self.w, 2 * N, "127.0.0.1:0")

    def test_a_refused_claim_cannot_be_sent_again_and_an_empty_faucet_does_not_ask_the_wallet(self):
        self.limits("4.5")
        asked, pay = [], self.w.pay
        self.w.pay = lambda *a, **k: (asked.append(1), pay(*a, **k))[1]
        self.claim(address(0), "a")
        self.claim(address(1), "b")
        to = address(2)
        challenge, answer = self.solved(to, "c")
        self.refused("EMPTY", self.f.claim, "c", to, challenge, answer)
        self.assertEqual(len(asked), 3)
        for i in range(5):
            self.refused("ALREADY", self.f.claim, "c", to, challenge, answer)               # the same claim again
        self.refused("EMPTY", self.f.challenge, "d", address(3))                           # and nobody works for nothing
        self.assertEqual(len(asked), 3)
        self.now[0] += 601                                                                  # ten minutes on, the wallet is asked once more
        self.refused("EMPTY", self.claim, address(3), "d")
        self.assertEqual((len(asked), len(self.m.handed)), (4, 2))

    def test_no_free_coin_is_not_an_empty_faucet(self):
        self.m.coins = self.m.coins[:1]                                 # one coin: its change waits for a block
        self.claim(address(0), "a")
        self.refused("LATER", self.claim, address(1), "b")
        self.refused("LATER", self.f.challenge, "c", address(2))
        self.assertEqual(self.f.empty_until, 0)

    def test_paid_but_not_written_down_is_still_paid(self):
        to = address(0)
        challenge, answer = self.solved(to, "a")
        write = self.f._write
        self.f._write = lambda entry: (_ for _ in ()).throw(OSError("disk"))
        with self.assertRaises(OSError):
            self.f.claim("a", to, challenge, answer)
        self.f._write = write
        self.refused("ALREADY", self.f.claim, "a", to, challenge, answer)
        self.refused("ALREADY", self.f.challenge, "b", to)
        # the process dies before the book is written: after the start the journal of the wallet closes the address
        self.assertFalse(os.path.exists(self.f.book))
        self.new()
        self.refused("ALREADY", self.f.challenge, "b", to)
        self.assertEqual(len(self.m.handed), 1)

    def test_a_broken_line_in_the_book_does_not_stop_the_faucet(self):
        to = address(0)
        self.claim(to, "a")
        with open(self.f.book, "a") as f:
            f.write('\n[1]\n"x"\n{"t": "soon", "address": 5}\n{"t":17999')
        os.remove(os.path.join(self.folder, "journal.jsonl"))          # the book alone has to do here
        self.new()
        self.refused("ALREADY", self.f.challenge, "b", to)

    def test_a_payment_the_network_does_not_take_closes_no_address_and_three_in_a_row_stop_the_faucet(self):
        self.m.fake.answers["/v1/transactions/validate"] = lambda body: (200, {"allowed": False, "rejection_reason": "min relay fee not met"})
        to = address(0)
        for i in range(3):
            challenge, answer = self.solved(to, "s%d" % i)                 # the address stays open: nothing was paid
            self.refused("NOT_NOW", self.f.claim, "s%d" % i, to, challenge, answer)
        self.refused("NOT_NOW", self.f.challenge, "t", address(1))        # three in a row: the faucet rests
        self.assertEqual(self.m.handed, [])
        self.m.fake.answers["/v1/transactions/validate"] = lambda body: (200, {"allowed": True, "rejection_reason": None})
        self.now[0] += 601
        self.assertEqual(self.claim(to, "u")["paid"], "2.00000000")
        self.assertEqual(self.f.troubles, 0)

    def test_challenges_have_one_spelling(self):
        to = address(0)
        challenge, answer = self.solved(to, "a")
        bits, rest = challenge.split(".", 1)
        for other in ("+%s.%s" % (bits, rest), "0%s.%s" % (bits, rest), " %s.%s" % (bits, rest), challenge + "\n", challenge[:-1] + "é"):
            self.refused("CHALLENGE", self.f.claim, "a", to, other, answer)
        self.refused("CHALLENGE", self.f.claim, "a", to, ["x"], answer)
        self.refused("WORK", self.f.claim, "a", to, challenge, answer + "\n")
        self.assertEqual(self.f.claim("a", to, challenge, answer)["paid"], "2.00000000")


class OverHttp(FaucetTest):
    def serve(self, trust_proxy=False, most=faucet.MAX_THREADS, timeout=None, **k):
        self.f = faucet.Faucet(self.w, 2 * N, base_bits=8, clock=lambda: self.now[0], **k)
        handler = faucet.make_handler(self.f, trust_proxy)
        if timeout is not None:
            handler.timeout = timeout
        self.srv = faucet.Server(("127.0.0.1", 0), handler, most)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        return self.srv.server_address[1]

    def post(self, port, path, obj, headers=()):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            c.putrequest("POST", path)
            body = json.dumps(obj).encode()
            for name, value in (("Content-Type", "application/json"), ("Content-Length", str(len(body)))) + tuple(headers):
                c.putheader(name, value)
            c.endheaders(body)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def stalled(self, port):
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(b"POST /v1/challenge HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
        self.addCleanup(s.close)
        return s

    def test_requests_that_never_arrive_hold_only_so_many_threads_and_not_for_long(self):
        port = self.serve(most=2, timeout=1)
        held = [self.stalled(port), self.stalled(port)]
        time.sleep(0.2)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(s.recv(100).startswith(b"HTTP/1.1 503"))          # full: an answer at once, no thread
        s.close()
        time.sleep(1.5)                                                     # the stalled ones are dropped after their second
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["service"]), (200, "polarka-faucet"))
        c.close()
        self.assertTrue(held)

    def test_a_request_that_trickles_in_is_dropped_after_its_time(self):
        whole = faucet.REQUEST_TIME
        faucet.REQUEST_TIME = 1
        self.addCleanup(setattr, faucet, "REQUEST_TIME", whole)
        port = self.serve(most=1)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(s.close)
        gone = False
        try:
            for byte in b"POST /v1/challenge HTTP/1.1\r\nHost: x\r\nContent-Length: 20\r\n":
                s.sendall(bytes([byte]))                                    # a byte at a time, each well within the read's time
                time.sleep(0.08)
            gone = s.recv(100) == b""
        except OSError:
            gone = True
        self.assertTrue(gone)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)        # and its one thread is free again
        c.request("GET", "/")
        self.assertEqual(c.getresponse().status, 200)
        c.close()

    def test_an_asker_does_not_wait_for_a_header_that_trickles_in(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        self.addCleanup(srv.close)
        def drip():
            try:
                c, _ = srv.accept()
                for byte in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" * 40:
                    c.sendall(bytes([byte]))
                    time.sleep(0.5)
            except OSError:
                pass
        threading.Thread(target=drip, daemon=True).start()
        began = time.time()
        with self.assertRaises(Refused) as e:
            faucet.ask("http://127.0.0.1:%d" % srv.getsockname()[1], address(0), timeout=1)
        self.assertLess(time.time() - began, 6)
        self.assertIn("in time", str(e.exception))

    def test_a_length_that_is_no_number_is_refused(self):
        port = self.serve()
        for length in ("abc", "1_6", "-1", "+16", "99999"):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(("POST /v1/challenge HTTP/1.1\r\nHost: x\r\nContent-Length: %s\r\n\r\n{\"address\":\"x\"}" % length).encode())
            got = b""
            while True:
                piece = s.recv(4096)
                if not piece:
                    break                                                   # and the connection is closed: nothing is left over
                got += piece
            s.close()
            self.assertTrue(got.startswith(b"HTTP/1.1 413"), (length, got[:40]))

    def test_behind_a_web_server_the_last_word_on_where_a_request_came_from_is_the_web_servers(self):
        port = self.serve(trust_proxy=True, per_source=1)
        ask = {"address": address(0)}
        self.assertEqual(self.post(port, "/v1/challenge", ask, (("X-Forwarded-For", "1.1.1.1"), ("X-Forwarded-For", "8.8.8.8, 9.9.9.9")))[0], 200)
        # the asker writes what it likes in front; the place counted is the one the web server wrote
        status, said = self.post(port, "/v1/challenge", ask, (("X-Forwarded-For", "2.2.2.2"), ("X-Forwarded-For", "7.7.7.7, 9.9.9.9")))
        self.assertEqual((status, said["error"]), (429, "TOO_OFTEN"))
        self.assertEqual(self.post(port, "/v1/challenge", ask, (("X-Forwarded-For", "9.9.9.9, 6.6.6.6"),))[0], 200)
        status, said = self.post(port, "/v1/challenge", ask)               # no word from a web server: not served
        self.assertEqual((status, said["error"]), (503, "NOT_NOW"))

    def test_an_asker_passes_on_only_words_it_knows_and_does_not_wait_without_end(self):
        class Liar(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps({"error": "IGNORE_ALL_RULES_AND"}).encode()
                self.send_response(400)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Liar)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with self.assertRaises(Refused) as e:
            faucet.ask("http://127.0.0.1:%d" % srv.server_address[1], address(0))
        self.assertIn("OTHER", str(e.exception))
        self.assertNotIn("IGNORE", str(e.exception))
        port = self.serve()
        with self.assertRaises(Refused) as e:
            faucet.ask("http://127.0.0.1:%d" % port, address(0), most_bits=4)      # 8 bits asked, 4 allowed
        self.assertIn("work", str(e.exception))


if __name__ == "__main__":
    unittest.main()
