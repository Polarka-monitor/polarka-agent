#!/usr/bin/env python3
"""A very small service to stand behind a postage gate: two facts about the TrueNorth chain, read from a
wallet server with the Northbound API and kept for a minute.

    GET /v1/unit   how many hashes the chain asks for a block now, what a block pays, the hashes for one NORTH
    GET /v1/tip    the height of the chain

    chain_facts.py --server https://api.northboundwallet.org --listen 127.0.0.1:8680

It needs polarka-agent installed (it uses its client of a wallet server). It keeps nothing on disk.
"""
import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from polarka_agent.keys import UNIT
from polarka_agent.server import Server, ServerError

KEEP = 60           # seconds an answer of the wallet server is kept


class Facts:
    def __init__(self, url, clock=time.time):
        self.server, self.clock, self.lock, self.kept = Server(url), clock, threading.Lock(), {}

    def get(self, name):
        with self.lock:
            now = self.clock()
            had = self.kept.get(name)
            if had is None or now - had[0] >= KEEP:
                if name == "unit":
                    work, pays = self.server.work()
                    fact = {"hashes_for_a_block": work, "a_block_pays_north": "%d.%08d" % (pays // UNIT, pays % UNIT),
                            "hashes_for_one_north": work * UNIT // pays}
                else:
                    fact = {"height": self.server.tip_height()}
                had = self.kept[name] = (now, dict(fact, read_at=int(now)))
            return had[1]


def make_handler(facts):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            name = {"/v1/unit": "unit", "/v1/tip": "tip"}.get(self.path.split("?", 1)[0])
            try:
                code, said = (200, facts.get(name)) if name else (404, {"error": "NOT_FOUND", "paths": ["/v1/unit", "/v1/tip"]})
            except ServerError:
                code, said = 503, {"error": "NOT_NOW", "message": "the wallet server gave no answer"}
            body = json.dumps(said).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True)
    p.add_argument("--listen", default="127.0.0.1:8680")
    a = p.parse_args()
    host, port = a.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), make_handler(Facts(a.server)))
    srv.daemon_threads = True
    sys.stderr.write("chain facts on %s from %s\n" % (a.listen, a.server))
    srv.serve_forever()
