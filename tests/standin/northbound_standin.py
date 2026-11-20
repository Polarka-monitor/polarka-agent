#!/usr/bin/env python3
"""A stand-in for a Northbound wallet server (API version 1), for tests.

It takes the same requests and answers in the same shapes as the server of github.com/nova9/northbound, from
a TrueNorth Core node. It was written from that server's sources (tag v0.1.0-rc.5 and the commits up to
07523b6 of 15 November 2026, which brought coins in pages and history lines as signed amounts). None of that
code is used or run here, and where this program and the real server differ the real server is right.

It is polarka_agent/walletd.py, the wallet server meant for one's own node, with the switches below on top, so
that what the tests run is what a pool or a faucet runs. It keeps no database: every answer is worked out from
the node's blocks and waiting transactions as they are at that moment. It listens on 127.0.0.1 only, trusts its
node, takes the node to be ready whenever the switch "stale" is off, and is not meant to serve anybody.

  northbound_standin.py --rpc-url http://127.0.0.1:PORT --cookie DATADIR/regtest/.cookie [--port 0] [--warm]

It prints "listening PORT" when it is ready. With --warm it reads the whole chain before that.

Beside the API it takes POST /__standin/set with a JSON object:
  {"stale": true}   the index says it is not up to date, and what is read from it is answered 503
  {"busy": N}       the next N requests are answered 429 with Retry-After: 1
  {"hold": true}    a transaction handed over for broadcast is not passed on; the answer is the node's error, 502
  {"refuse": "WHY"} the server says no and keeps what it was shown: validation answers that the node does not
                    take the transaction (WHY), broadcast answers the node's error, the node is asked neither
                    time, and the transaction is kept. "" turns it off
  {"hide": true}    the coins that a kept transaction spends are left out of /v1/utxos
  {"page": N}       /v1/utxos gives N coins to a page when the request names no limit (0: a thousand, as the
                    real server does)
The answer names every switch, and under "kept" the transactions that a server which said no was shown. The
real server has no such switches. The last two play a server that is not honest, which the real one is not
known to be: a wallet has to stay safe with one all the same.
"""
import argparse
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polarka_agent.walletd import STALE, Answer, App, Listener, Node, NodeDown  # noqa: E402


class StandIn(App):
    """walletd with the switches of the tests."""

    def __init__(self, node):
        super().__init__(node)              # the network is asked of the node when it is needed
        self.stale = False
        self.busy = 0
        self.hold = False
        self.refuse = ""                    # a reason: every transaction is refused with it, and kept
        self.hide = False
        self.page = 0                       # coins on a page of /v1/utxos when the request names no limit; 0: as the real server
        self.kept = []                      # what a server that said no was shown, as hexadecimal text
        self.kept_coins = set()             # (txid, n) of every coin those transactions spend

    def node_ready(self, info):
        return True                         # a test node is as ready as the switch "stale" says

    def route(self, method, path, content_type, raw):
        if path == "/__standin/set" and method == "POST":
            wanted = json.loads(raw.decode())
            self.stale = bool(wanted.get("stale", self.stale))
            self.busy = int(wanted.get("busy", self.busy))
            self.hold = bool(wanted.get("hold", self.hold))
            self.refuse = str(wanted.get("refuse", self.refuse) or "")
            self.hide = bool(wanted.get("hide", self.hide))
            self.page = int(wanted.get("page", self.page))
            return 200, {"stale": self.stale, "busy": self.busy, "hold": self.hold, "refuse": self.refuse,
                         "hide": self.hide, "page": self.page, "kept": self.kept}
        if path.startswith(("/v1/", "/v2/")) and self.busy > 0:
            # the real server counts every request against its limit, valid or not, and leaves /ready out
            self.busy -= 1
            raise Answer(429, "The server request rate limit was reached; retry in one second.", {"Retry-After": "1"})
        if method == "GET" and path == "/ready":
            return (503 if self.stale else 200), self.index.look().status(not self.stale)
        return super().route(method, path, content_type, raw)

    def picture(self):
        if self.stale:
            raise Answer(503, STALE)
        return super().picture()

    def hidden(self):
        return self.kept_coins if self.hide else ()

    def utxo_page(self):
        return self.page or super().utxo_page()

    def keep(self, text):
        """Remembers a transaction that the server refused, and the coins it spends. (The test switch "refuse".)"""
        if text not in self.kept:
            self.kept.append(text)
            for spent in self.node.call("decoderawtransaction", text)["vin"]:
                self.kept_coins.add((spent["txid"], spent["vout"]))

    def validate(self, body):
        if self.refuse:
            self.keep(self.signed(body))
            return {"allowed": False, "rejection_reason": self.refuse}
        return super().validate(body)

    def broadcast(self, body):
        if self.refuse or self.hold:
            text = self.signed(body)
            if self.refuse:
                self.keep(text)
                raise NodeDown("refused by the test")
            raise NodeDown("held back by the test")
        return super().broadcast(body)


def main():
    parser = argparse.ArgumentParser(description="A stand-in for a Northbound wallet server, for tests.")
    parser.add_argument("--rpc-url", required=True, help="the node's RPC address, for example http://127.0.0.1:18443")
    parser.add_argument("--cookie", required=True, help="the node's cookie file (DATADIR/regtest/.cookie)")
    parser.add_argument("--port", type=int, default=0, help="0 takes a free port")
    parser.add_argument("--warm", action="store_true", help="read the whole chain before listening")
    parser.add_argument("--exit-with-stdin", action="store_true", help="stop when standard input closes, so that it never outlives its test")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    app = StandIn(Node(args.rpc_url, args.cookie))
    if args.warm:
        app.index.look()
    # no idle limit and room for many: the tests' own clients decide how long a connection lives
    server = Listener(("127.0.0.1", args.port), app, most=256, idle=None, verbose=args.verbose, report=False)
    if args.exit_with_stdin:
        def leave():
            sys.stdin.buffer.read()
            server.shutdown()
        threading.Thread(target=leave, daemon=True).start()
    print("listening %d" % server.server_address[1], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
