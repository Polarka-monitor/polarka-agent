"""Postage: a small gate that stands in front of a web service and lets through who paid a little NORTH.

Whoever wants to keep robots from wearing a service out makes them burn processor time for nothing today.
With postage the caller pays instead, and the one who runs the service gets the coin.

    python3 -m polarka_agent --dir FOLDER postage-serve --upstream 127.0.0.1:9000 --price 0.01 --calls 100
    python3 -m polarka_agent --dir FOLDER postage-serve --upstream 127.0.0.1:9000 --price-hashes 600 --calls 100

A price can be a fixed amount of NORTH, or an amount of mining work in hashes. The second is turned into
NORTH by what the network needs for a block and what a block pays, read from the wallet server's block
template every ten minutes. When more machines mine, the same work is fewer NORTH. A quote keeps the price
it was given.

    any request without paid postage  -> 402 with what to do, as JSON
    POST /_postage/quote              -> {"token", "pay_to", "price", "calls", "expires"}
    GET  /_postage/status             -> {"calls_left", "paid"}          (header X-Postage: token)
    any request with X-Postage: token -> passed on to the service while calls are left

With a facilitator (--x402-facilitator), the same lot can be bought with x402 instead: the 402 also carries
PAYMENT-REQUIRED, a request with PAYMENT-SIGNATURE pays for one lot, and once the payment is in a block that request
passes and its claim (the secret in the payment's payload) is the token for the rest of the lot. Every call that
passes on such a lot tells how many are left in X-Postage-Calls-Left.

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
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .keys import UNIT
from .server import ServerError, reachable
from .wallet import Refused, north, units as units_of
from .x402 import AGAIN as X402_AGAIN, AWAITING, CLAIM, VERSION as X402_VERSION, b64json, from_b64json, requirements

QUOTE_LIFE = 1800               # seconds an address waits for its payment
PRICE_LIFE = 600                # seconds a price worked out from the network is kept
PRICE_STALE = 6 * 3600          # when the server gives no template: a price older than this is not quoted
MAX_BODY = 1 << 20              # the largest request passed on
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
       "x-postage", "host", "content-length", "payment-signature"}


def mark(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Gate:
    def __init__(self, wallet, price, calls, free=0, clock=time.time, look_every=20, hashes=0, x402=None):
        self.wallet, self.price, self.calls, self.free, self.clock, self.look_every = wallet, price, calls, free, clock, look_every
        self.x402 = x402            # a facilitator: the lot may then be bought with x402 too
        self.offered = {}           # a price asked over x402 -> until when a payment of it is taken
        self.hashes = hashes        # above nothing: the price of a lot is this much mining work, not a fixed amount
        self.priced = None          # (when, the price in base units, hashes for one NORTH)
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
        if x402 is not None and not self.state.get("x402_address"):
            # one address for every payment over x402: such a payment is told apart by its transaction, not its address
            self.state["x402_address"] = wallet.fresh_address()
            self._save()

    def _save(self):
        tmp = self.book + ".new"
        with open(tmp, "w") as f:
            json.dump(self.state, f, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.book)

    def _price(self):
        """The price of one lot in base units: the fixed one, or so many hashes' worth of NORTH at what the
        network needs for a block now. Called with the lock held."""
        if not self.hashes:
            return self.price
        now = self.clock()
        if self.priced is None or now - self.priced[0] >= PRICE_LIFE:
            try:
                work, pays = self.wallet.server.work()
                self.priced = (now, max(1, self.hashes * pays // work), work * UNIT // pays)
            except ServerError:
                # a server that is away for a while does not stop the gate; one that stays away does
                if self.priced is None or now - self.priced[0] > PRICE_STALE:
                    raise
        return self.priced[1]

    def quote(self):
        with self.lock:
            now = self.clock()
            price = self._price()
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
            self.state["tokens"][mark(token)] = {"address": address, "expires": int(now) + QUOTE_LIFE, "paid": False, "calls": 0, "price": price}
            # tokens that were never paid and whose time is long over are dropped
            self.state["tokens"] = {k: t for k, t in self.state["tokens"].items() if t["paid"] or now < t["expires"] + 3600}
            self._save()
            return {"token": token, "pay_to": address, "price": north(price), "calls": self.calls, "expires": int(now) + QUOTE_LIFE,
                    "then": "pay the price to pay_to; when it is in a block, send the token in the header X-Postage"}

    def _settle(self, t):
        """Looks whether the payment for a token has arrived. Called with the lock held."""
        now = self.clock()
        if t["paid"] or now > t["expires"] + 600 or now - self.looked.get(t["address"], 0) < self.look_every:
            return
        self.looked[t["address"]] = now
        seen = self.wallet.received(t["address"])
        counted = self.state["addresses"].get(t["address"], 0)
        price = t.get("price") or self._price()         # a token keeps the price it was quoted
        if seen["confirmations"] >= 1 and seen["amount"] - counted >= price:
            # what came is counted once: the address can then serve the next asker
            self.state["addresses"][t["address"]] = seen["amount"]
            t["paid"], t["calls"] = True, self.calls * ((seen["amount"] - counted) // price)
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

    def x402_wanted(self, amount=None):
        """The requirements of x402 for one lot. A price asked in the last hour is still taken, so that a
        price that moved with the network does not refuse who paid the one before."""
        with self.lock:
            now = self.clock()
            price = self._price()
            self.offered = {p: until for p, until in self.offered.items() if until > now}
            self.offered[price] = now + 6 * PRICE_LIFE
            try:
                asked = int(amount) if amount is not None else None
            except (TypeError, ValueError):
                asked = None
            if asked in self.offered:
                price = asked
        return requirements(self.x402.network, price, self.state["x402_address"], confirmations=self.x402.needed(price))

    def x402_needed(self, path):
        return {"x402Version": X402_VERSION, "error": "PAYMENT-SIGNATURE header is required",
                "resource": {"url": path, "description": "postage for %d calls" % self.calls, "mimeType": "application/json"},
                "accepts": [self.x402_wanted()]}

    def x402_pay(self, payment):
        """A payment of x402 for one lot. (what came of it, True when this call may pass)."""
        body = payment.get("payload") if isinstance(payment, dict) else None
        claim = body.get("claim") if isinstance(body, dict) else None
        if not isinstance(claim, str) or not CLAIM.match(claim):
            return {"success": False, "errorReason": "claim_required", "transaction": "", "network": self.x402.network, "payer": None}, False
        with self.lock:
            t = self.state["tokens"].get(mark(claim))
        if t is not None:                   # the lot is paid already: the payment shown again is one more call of it
            done = {"success": True, "transaction": t.get("txid", ""), "network": self.x402.network, "payer": None}
            return done, self.spend(claim, None)
        accepted = payment.get("accepted")
        done = self.x402.settle(payment, self.x402_wanted(accepted.get("amount") if isinstance(accepted, dict) else None))
        if not done.get("success"):
            return done, False
        with self.lock:
            key = mark(claim)
            if key not in self.state["tokens"]:
                self.state["tokens"][key] = {"address": self.state["x402_address"], "expires": int(self.clock()) + QUOTE_LIFE, "paid": True,
                                             "calls": self.calls, "price": int(accepted["amount"]), "txid": done.get("transaction", "")}
                self._save()
        return done, self.spend(claim, None)

    def left(self, token):
        with self.lock:
            t = self.state["tokens"].get(mark(token or ""))
            return t["calls"] if t is not None else None

    def about(self):
        with self.lock:
            price = self._price()
            priced = self.priced
        a = {"error": "POSTAGE", "message": "this service asks for postage in NORTH",
             "price": north(price), "for_calls": self.calls, "free_calls_a_day": self.free,
             "how": ["POST /_postage/quote", "pay the price to the address it names", "send the token in the header X-Postage"],
             "note": "postage is counted in calls and is not given back; NORTH has no market price"}
        if self.x402 is not None:
            a["x402"] = {"network": self.x402.network, "how": "or pay one lot with x402 (PAYMENT-REQUIRED is in this answer's headers); "
                         "the claim in your payment's payload is then the token for the rest of the lot",
                         "confirmations": self.x402.needed(price)}
        if self.hashes:
            a["price_in_hashes"], a["hashes_for_one_north"] = self.hashes, priced[2]
            a["note"] = ("the price is so many hashes of mining work, in NORTH at what the network needs for a block now, and moves with it; "
                         + a["note"])
        return a


def make_handler(gate, upstream, trust_proxy, routes=()):
    """`routes`: (path prefix, host:port) pairs; a request whose path starts with a prefix goes there, the longest
    prefix first, and anything else to `upstream`."""
    targets = sorted(((prefix, where.rsplit(":", 1)) for prefix, where in routes), key=lambda r: -len(r[0]))
    default = upstream.rsplit(":", 1)

    def target(path):
        for prefix, where in targets:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/") or path.startswith(prefix + "?"):
                return where
        return default

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "polarka-postage"

        def source(self):
            if trust_proxy:
                f = self.headers.get("X-Forwarded-For", "")
                if f:
                    return f.split(",")[-1].strip()
            return self.client_address[0]

        def send(self, code, obj, more=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (more or {}).items():
                self.send_header(k, v)
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
                token, signed, more = self.headers.get("X-Postage"), self.headers.get("PAYMENT-SIGNATURE"), {}
                if gate.x402 is not None and signed and not token:
                    what = "x402"
                    try:
                        payment = from_b64json(signed)
                    except Exception:
                        outcome = "unreadable"
                        return self.send(400, {"error": "INPUT", "message": "the payment cannot be read"})
                    done, may = gate.x402_pay(payment)
                    more["PAYMENT-RESPONSE"] = b64json(done)
                    if not may:
                        if done.get("errorReason") == AWAITING:
                            outcome = "waiting"
                            return self.send(202, {"waiting": "the payment is not deep enough in the chain yet; send the same "
                                                              "PAYMENT-SIGNATURE again"}, dict(more, **{"Retry-After": "20"}))
                        if done.get("errorReason") in X402_AGAIN:
                            outcome = "x402 not now"
                            return self.send(503, {"error": "NOT_NOW", "message": "the payment could not be settled just now; send the "
                                                   "same PAYMENT-SIGNATURE again"}, dict(more, **{"Retry-After": "60"}))
                        outcome = "x402 %s" % str(done.get("errorReason"))[:40]
                        more["PAYMENT-REQUIRED"] = b64json(gate.x402_needed(self.path))
                        return self.send(402, gate.about(), more)
                    token = payment["payload"]["claim"]
                elif not gate.spend(token, self.source()):
                    outcome = "402"
                    if gate.x402 is not None:
                        more["PAYMENT-REQUIRED"] = b64json(gate.x402_needed(self.path))
                    return self.send(402, gate.about(), more)
                if token:
                    left = gate.left(token)
                    if left is not None:
                        more["X-Postage-Calls-Left"] = str(left)
                host, port = target(self.path)
                c = http.client.HTTPConnection(host, int(port), timeout=60)
                try:
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                    c.request(self.command, self.path, body or None, headers)
                    r = c.getresponse()
                    answer = r.read(64 << 20)
                    self.send_response(r.status)
                    for k, v in r.getheaders():
                        if k.lower() not in HOP and k.lower() not in ("payment-response", "x-postage-calls-left"):
                            self.send_header(k, v)
                    for k, v in more.items():
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


TOKEN_SHAPE = re.compile(r"^[0-9a-f]{16,128}$")


def buy(wallet, url, most, timeout=30):
    """The asker's side: a quote from a gate, and the payment for it from this wallet. The owner's limits
    hold as for any payment, and nothing above `most` base units is paid whatever the gate asks. The
    token gives its calls once the payment is in a block."""
    import urllib.error
    import urllib.request

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    if not reachable(url):
        raise Refused("GATE", "a gate is reached over https")
    req = urllib.request.Request(url.rstrip("/") + "/_postage/quote", b"{}", {"Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(Stay).open(req, timeout=timeout) as r:
            text = r.read(65536)
    except urllib.error.HTTPError as e:
        raise Refused("GATE", "the gate answered HTTP %d" % e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Refused("GATE", "the gate cannot be reached (%s)" % type(e).__name__)
    try:
        q = json.loads(text.decode("utf-8"))
    except ValueError:
        q = None
    if type(q) is not dict:
        raise Refused("GATE", "the gate sent something that is not a quote")
    token, pay_to, calls, asked = q.get("token"), q.get("pay_to"), q.get("calls"), q.get("price")
    if not isinstance(token, str) or not TOKEN_SHAPE.match(token) or not isinstance(pay_to, str) or len(pay_to) > 100 \
            or type(calls) is not int or not 1 <= calls <= 10 ** 9 or not isinstance(asked, str):
        raise Refused("GATE", "the gate sent a quote this program does not understand")
    try:
        price = units_of(asked)
    except Refused:
        raise Refused("GATE", "the gate sent a quote this program does not understand")
    if price <= 0:
        raise Refused("GATE", "the gate sent a quote this program does not understand")
    if price > most:
        raise Refused("TOO_DEAR", "the gate asks %s NORTH for %d calls, and %s is the most to be paid" % (north(price), calls, north(most)))
    done = wallet.pay([(pay_to, price)], memo="postage")
    return {"token": token, "paid": north(price), "calls": calls, "pay_to": pay_to, "payment": done["id"], "state": done["state"],
            "then": "when the payment is in a block, send the token in the header X-Postage with each call"}


def serve(wallet, upstream, price, calls, listen, free=0, trust_proxy=False, hashes=0, x402=None, routes=()):
    if (price <= 0) == (hashes <= 0) or calls <= 0 or free < 0:
        raise Refused("INPUT", "a price in NORTH or a price in hashes, one of the two, and a number of calls above nothing")
    if x402 is not None and getattr(x402, "chain", None) and x402.chain != wallet.chain:
        raise Refused("CHAIN", "the facilitator settles another chain than the gate's wallet")
    gate = Gate(wallet, price, calls, free, hashes=hashes, x402=x402)
    if hashes:
        try:
            price = units_of(gate.about()["price"])
        except ServerError as e:
            raise Refused("NO_WORK", "the wallet server gives no block template (HTTP %s); a price in hashes needs a server with the mining routes of Northbound" % e.status)
    host, port = listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), make_handler(gate, upstream, trust_proxy, routes))
    srv.daemon_threads = True
    sys.stderr.write("postage gate on %s before %s: %s NORTH for %d calls%s, %d free a day%s\n"
                     % (listen, upstream, north(price), calls, " (%d hashes of work, at the network of this moment)" % hashes if hashes else "", free,
                        ", x402 on %s" % x402.network if x402 is not None else ""))
    srv.serve_forever()
