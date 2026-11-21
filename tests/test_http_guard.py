"""A request that carries a body ends its connection (polarka_agent/httpguard.py). Review 8 showed that otherwise a
GET with a body reaches what is behind a proxy as two requests (H1, M1); these tests send exactly that."""
import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from polarka_agent import bazaar, keys, x402
from polarka_agent.httpguard import OneRequestWithBody

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.join(os.path.dirname(HERE), "polarka_agent")
MAIN_GENESIS = "470c8b8ae824c06575cfae0851282c49" + "00" * 16


def exchange(port, data, wait=1.5):
    """Bytes on one connection; everything that comes back within `wait` seconds."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(data)
    s.settimeout(0.2)
    got, until = b"", time.time() + wait
    while time.time() < until:
        try:
            piece = s.recv(65536)
            if not piece:
                break
            got += piece
        except socket.timeout:
            continue
    s.close()
    return got


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


INNER = b"POST /settle HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 6.6.6.6\r\nContent-Length: 2\r\n\r\n{}"


class Plain(OneRequestWithBody, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def do_GET(self):
        self.seen.append(("GET", self.path))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        self.seen.append(("POST", self.path))
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class TheRule(unittest.TestCase):
    def setUp(self):
        Plain.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Plain)
        self.port = serve(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_a_get_with_a_body_is_one_request_and_closes(self):
        got = exchange(self.port, b"GET /supported HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(INNER) + INNER)
        self.assertEqual(got.count(b"HTTP/1.1 "), 1)
        self.assertIn(b"Connection: close", got)
        self.assertEqual(Plain.seen, [("GET", "/supported")])

    def test_a_chunked_body_closes_too(self):
        chunk = b"%x\r\n" % len(INNER) + INNER + b"\r\n0\r\n\r\n"
        got = exchange(self.port, b"GET /supported HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n" + chunk)
        self.assertEqual(got.count(b"HTTP/1.1 "), 1)
        self.assertEqual(Plain.seen, [("GET", "/supported")])

    def test_a_post_refused_before_its_body_is_read_does_not_leave_the_body_behind(self):
        got = exchange(self.port, b"POST /unknown HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(INNER) + INNER)
        self.assertEqual(got.count(b"HTTP/1.1 "), 1)
        self.assertEqual(Plain.seen, [("POST", "/unknown")])

    def test_requests_without_a_body_keep_their_connection(self):
        two = b"GET /a HTTP/1.1\r\nHost: x\r\n\r\nGET /b HTTP/1.1\r\nHost: x\r\n\r\n"
        got = exchange(self.port, two)
        self.assertEqual(got.count(b"HTTP/1.1 200"), 2)
        self.assertNotIn(b"Connection: close", got)
        self.assertEqual(Plain.seen, [("GET", "/a"), ("GET", "/b")])

    def test_no_python_version_in_the_server_header(self):
        got = exchange(self.port, b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertNotIn(b"Python/", got)


class EveryServerHasIt(unittest.TestCase):
    def test_every_handler_class_of_the_package(self):
        handlers = 0
        for name in sorted(os.listdir(PACKAGE)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as f:
                text = f.read()
            bare = re.findall(r"class \w+\(BaseHTTPRequestHandler\)", text)
            self.assertEqual(bare, [], "%s has a handler without OneRequestWithBody" % name)
            handlers += len(re.findall(r"class \w+\(OneRequestWithBody, BaseHTTPRequestHandler\)", text))
        self.assertGreaterEqual(handlers, 7)


class FakeNode:
    """Just enough of a node for the facilitator: every transaction pays `amount` to the address the test names."""

    def __init__(self, pay_to, amount=100000):
        self.pay_to, self.amount, self.calls = pay_to, amount, []

    def call(self, method, *params):
        self.calls.append(method)
        if method == "getblockhash":
            return MAIN_GENESIS
        if method == "decoderawtransaction":
            return {"txid": hashlib.sha256(params[0].encode()).hexdigest(),
                    "vout": [{"n": 0, "value": Decimal(self.amount) / Decimal(10 ** 8), "scriptPubKey": {"address": self.pay_to}}]}
        if method == "testmempoolaccept":
            return [{"allowed": True}]
        if method == "sendrawtransaction":
            return hashlib.sha256(params[0].encode()).hexdigest()
        if method == "gettxout":
            return {"confirmations": 1}
        if method == "getmempoolentry":
            return {}
        raise RuntimeError(method)


class TheFacilitatorBehindAProxy(unittest.TestCase):
    """Review 8, H1: a GET /supported whose body is a whole POST /settle must not settle anything."""

    def test_a_settle_hidden_in_a_get_is_not_read(self):
        tmp = tempfile.mkdtemp(prefix="guard-")
        try:
            stranger = keys.Account.from_words(keys.new_words(12), "main").address(0, 0)
            catalog = bazaar.Catalog(os.path.join(tmp, "x402-bazaar.json"), ["https://gate.example"])
            node = FakeNode(stranger)
            fac = x402.Facilitator(node, "main", os.path.join(tmp, "x402-used.jsonl"), confirmations=1, catalog=catalog)
            server = x402.facilitator_server(fac)
            port = serve(server)
            wanted = {"scheme": "exact", "network": fac.network, "amount": "100000", "asset": "NORTH", "payTo": stranger,
                      "maxTimeoutSeconds": 60, "extra": {"confirmations": 1}}
            payment = {"x402Version": 2, "resource": {"url": "https://gate.example/v1/free", "description": "pay me"},
                       "accepted": wanted, "payload": {"transaction": "02" * 120, "claim": "ab" * 16},
                       "extensions": {"bazaar": {"info": {"input": {"type": "http", "method": "GET"}}, "schema": {}}}}
            body = json.dumps({"x402Version": 2, "paymentPayload": payment, "paymentRequirements": wanted}).encode()
            inner = b"POST /settle HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body) + body
            got = exchange(port, b"GET /supported HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(inner) + inner)
            server.shutdown()
            server.server_close()
            self.assertEqual(got.count(b"HTTP/1.1 "), 1)
            self.assertNotIn("sendrawtransaction", node.calls)
            self.assertEqual(catalog.resources({})["items"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
