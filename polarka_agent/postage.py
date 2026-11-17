"""Postage: a small gate that stands in front of a web service and lets through who paid a little NORTH.

Whoever wants to keep robots from wearing a service out makes them burn processor time for nothing today.
With postage the caller pays instead, and the one who runs the service gets the coin.

    python3 -m polarka_agent --dir FOLDER postage-serve --upstream 127.0.0.1:9000 --price 0.01 --calls 100

    any request without paid postage  -> 402 with what to do, as JSON
    POST /_postage/quote              -> {"token", "pay_to", "price", "calls", "expires"}
    GET  /_postage/status             -> {"calls_left", "paid"}          (header X-Postage: token)
    any request with X-Postage: token -> passed on to the service while calls are left

How a payment is told apart: every quote gets an address of the gate's wallet for itself, for half an hour.
What arrives on that address in that time (and ten minutes more), in a block, belongs to that token. An address that was not paid is
given to the next asker afterwards, so the wallet does not walk through its keys.

The gate holds nobody's coins to give back: postage is counted in calls, it is not a balance and is not paid
out. The gate's wallet only receives here; this program never pays from it.

What it keeps: postage.json beside the wallet, with a hash of every token, its address and its calls. No
internet addresses on disk.
"""
import hashlib
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .server import ServerError
from .wallet import Refused, north

QUOTE_LIFE = 1800               # seconds an address waits for its payment
MAX_BODY = 1 << 20              # the largest request passed on
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
       "x-postage", "host", "content-length"}


def mark(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Gate:
    def __init__(self, wallet, price, calls, free=0, clock=time.time, look_every=20):
        self.wallet, self.price, self.calls, self.free, self.clock, self.look_every = wallet, price, calls, free, clock, look_every
        self.lock = threading.Lock()
        self.book = os.path.join(wallet.folder, "postage.json")
        self.salt = os.urandom(16)
        self.free_used = {}         # salted hash of a source -> (day, calls)
        self.looked = {}            # address -> when the wallet was last asked about it
        try:
            with open(self.book) as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = {"tokens": {}, "addresses": {}}     # addresses: address -> the amount on it already counted

    def _save(self):
        tmp = self.book + ".new"
        with open(tmp, "w") as f:
            json.dump(self.state, f, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.book)

    def quote(self):
        with self.lock:
            now = self.clock()
            # an address stays with its token until the time for a late payment is over too
            waiting = {t["address"] for t in self.state["tokens"].values() if not t["paid"] and now < t["expires"] + 600}
            idle = [a for a in self.state["addresses"] if a not in waiting]
            if idle:
                address = idle[0]
            else:
                try:
                    address = self.wallet.fresh_address()
                except Refused as e:
                    if e.kind in ("TOO_MANY_OPEN", "TOO_MANY_ADDRESSES"):
                        raise Refused("BUSY", "too many are about to pay just now; ask again in a few minutes")
                    raise
                self.state["addresses"][address] = 0
            token = os.urandom(24).hex()
            self.state["tokens"][mark(token)] = {"address": address, "expires": int(now) + QUOTE_LIFE, "paid": False, "calls": 0}
            # tokens that were never paid and whose time is long over are dropped
            self.state["tokens"] = {k: t for k, t in self.state["tokens"].items() if t["paid"] or now < t["expires"] + 3600}
            self._save()
            return {"token": token, "pay_to": address, "price": north(self.price), "calls": self.calls, "expires": int(now) + QUOTE_LIFE,
                    "then": "pay the price to pay_to; when it is in a block, send the token in the header X-Postage"}

    def _settle(self, t):
        """Looks whether the payment for a token has arrived. Called with the lock held."""
        now = self.clock()
        if t["paid"] or now > t["expires"] + 600 or now - self.looked.get(t["address"], 0) < self.look_every:
            return
        self.looked[t["address"]] = now
        seen = self.wallet.received(t["address"])
        counted = self.state["addresses"].get(t["address"], 0)
        if seen["confirmations"] >= 1 and seen["amount"] - counted >= self.price:
            # what came is counted once: the address can then serve the next asker
            self.state["addresses"][t["address"]] = seen["amount"]
            t["paid"], t["calls"] = True, self.calls * ((seen["amount"] - counted) // self.price)
            self._save()

    def status(self, token):
        with self.lock:
            t = self.state["tokens"].get(mark(token or ""))
            if t is None:
                raise Refused("NO_POSTAGE", "this token is not known here")
            self._settle(t)
            return {"paid": t["paid"], "calls_left": t["calls"]}

    def spend(self, token, source):
        """One call. True when it may pass."""
        with self.lock:
            t = self.state["tokens"].get(mark(token)) if token else None
            if t is not None:
                self._settle(t)
                if t["calls"] > 0:
                    t["calls"] -= 1
                    self._save()
                    return True
            if self.free and source is not None:
                day, key = int(self.clock() // 86400), hashlib.sha256(self.salt + source.encode()).hexdigest()[:16]
                was = self.free_used.get(key, (day, 0))
                used = was[1] if was[0] == day else 0
                if used < self.free:
                    self.free_used[key] = (day, used + 1)
                    return True
            return False

    def about(self):
        return {"error": "POSTAGE", "message": "this service asks for postage in NORTH",
                "price": north(self.price), "for_calls": self.calls, "free_calls_a_day": self.free,
                "how": ["POST /_postage/quote", "pay the price to the address it names", "send the token in the header X-Postage"],
                "note": "postage is counted in calls and is not given back; NORTH has no market price"}


def make_handler(gate, upstream, trust_proxy):
    host, port = upstream.rsplit(":", 1)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "polarka-postage"

        def source(self):
            if trust_proxy:
                f = self.headers.get("X-Forwarded-For", "")
                if f:
                    return f.split(",")[-1].strip()
            return self.client_address[0]

        def send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_one(self):
            what, outcome, t0 = "call", "passed", time.time()
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
                if n < 0 or n > MAX_BODY:
                    self.close_connection = True
                    outcome = "too large"
                    return self.send(413, {"error": "INPUT", "message": "request size"})
                body = self.rfile.read(n) if n else b""
                if self.path == "/_postage/quote" and self.command == "POST":
                    what, outcome = "quote", "ok"
                    return self.send(200, gate.quote())
                if self.path == "/_postage/status":
                    what, outcome = "status", "ok"
                    return self.send(200, gate.status(self.headers.get("X-Postage")))
                if not gate.spend(self.headers.get("X-Postage"), self.source()):
                    outcome = "402"
                    return self.send(402, gate.about())
                c = http.client.HTTPConnection(host, int(port), timeout=60)
                try:
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                    c.request(self.command, self.path, body or None, headers)
                    r = c.getresponse()
                    answer = r.read(64 << 20)
                    self.send_response(r.status)
                    for k, v in r.getheaders():
                        if k.lower() not in HOP:
                            self.send_header(k, v)
                    self.send_header("Content-Length", str(len(answer)))
                    self.end_headers()
                    self.wfile.write(answer)
                finally:
                    c.close()
            except Refused as e:
                outcome = e.kind
                self.send(503 if e.kind == "BUSY" else 400, {"error": e.kind, "message": str(e)})
            except ServerError:
                outcome = "SERVER"
                self.send(503, {"error": "NOT_NOW", "message": "the gate cannot look at its wallet just now"})
            except Exception as e:
                outcome = "failed %s" % type(e).__name__
                try:
                    self.send(502, {"error": "FAILED", "message": "the service behind the gate did not answer"})
                except Exception:
                    pass
            finally:
                sys.stderr.write("%s %s %s %.0f ms\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), what, outcome, (time.time() - t0) * 1000))

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = handle_one

        def log_message(self, *a):
            pass
    return H


def serve(wallet, upstream, price, calls, listen, free=0, trust_proxy=False):
    if price <= 0 or calls <= 0 or free < 0:
        raise Refused("INPUT", "a price and a number of calls above nothing")
    gate = Gate(wallet, price, calls, free)
    host, port = listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), make_handler(gate, upstream, trust_proxy))
    srv.daemon_threads = True
    sys.stderr.write("postage gate on %s before %s: %s NORTH for %d calls, %d free a day\n" % (listen, upstream, north(price), calls, free))
    srv.serve_forever()
