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
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

from .httpguard import Bounded, Limiter, OneRequestWithBody, forwarded, one_length, place

from .keys import UNIT
from .server import ServerError, reachable
from .wallet import GAP, Refused, north, units as units_of
from .x402 import AGAIN as X402_AGAIN, AWAITING, CLAIM, VERSION as X402_VERSION, b64json, from_b64json, requirements

QUOTE_LIFE = 1800               # seconds an address waits for its payment
PRICE_LIFE = 600                # seconds a price worked out from the network is kept
PRICE_STALE = 6 * 3600          # when the server gives no template: a price older than this is not quoted
MAX_BODY = 1 << 20              # the largest request passed on
MAX_ANSWER = 8 << 20            # the largest answer of the service behind that is passed back
GIVE_BACK = (429, 503, 507)     # answers of the service behind for which the call of a lot is given back: it could not take it
X402_PER_MINUTE = 30            # x402 payments that reach the facilitator, a minute from one place (ten times that from all)
QUOTES_PER_MINUTE = 3          # quotes a minute from one place (review 11c, T4)
UNPAID_PER_PLACE = 2           # quotes of one place that wait for their payment at one time
KEEP_FOR_NEWCOMERS = 5         # of the wallet's GAP addresses nobody paid, those kept for places that hold no quote
KEEP_SPENT = 30 * 86400         # a lot whose calls are used up is forgotten this long after its quote ran out
SAVE_EVERY = 5                  # seconds: a call taken from a lot is written down with the next save, at most this late
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
       "x-postage", "host", "content-length", "payment-signature"}


TOO_MANY = "too_many_requests"


def mark(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Gate:
    def __init__(self, wallet, price, calls, free=0, clock=time.time, look_every=20, hashes=0, x402=None, public_url=None, offers=()):
        self.wallet, self.price, self.calls, self.free, self.clock, self.look_every = wallet, price, calls, free, clock, look_every
        self.x402 = x402            # a facilitator: the lot may then be bought with x402 too
        # where the gate is seen from outside (https://...), and what it lets be found in a bazaar: a list of
        # {"prefix", "description", "method", "route", "pathParams", "queryParams", "output", "serviceName", "tags", "iconUrl"}
        self.public_url = public_url.rstrip("/") if public_url else None
        self.offers = sorted((dict(o) for o in offers), key=lambda o: -len(o["prefix"]))
        self.hashes = hashes        # above nothing: the price of a lot is this much mining work, not a fixed amount
        self.priced = None          # (when, the price in base units, hashes for one NORTH)
        self.lock = threading.Lock()
        self.book = os.path.join(wallet.folder, "postage.json")
        self.salt = os.urandom(16)
        self.free_used = {}         # salted hash of a place -> its calls today; emptied when the day changes
        self.free_day = None
        self.x402_limiter = Limiter(X402_PER_MINUTE) if x402 is not None else None
        self.quote_limiter = Limiter(QUOTES_PER_MINUTE, everyone=60)
        self.quoted = {}            # mark of an unpaid token -> salted hash of the place that asked (memory only)
        self.saved, self.dirty = 0, False
        self.reserved = {}          # mark of a token -> calls taken by requests not yet answered
        self.looked = {}            # address -> when the wallet was last asked about it
        try:
            with open(self.book) as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = {"tokens": {}, "addresses": {}}     # addresses: address -> the amount on it already counted
        # a price asked over x402 -> until when a payment of it is taken. Kept in the book, so that a payment made
        # before a restart of the gate and shown again after it is held to the price it was asked, not to today's
        # (review 8, L1). Written at most once a PRICE_LIFE unless the prices change, so the kept time may be up
        # to that much shorter than the one in memory.
        self.offered, self.offered_kept = {}, 0
        for p, until in (self.state.get("offered") or {}).items():
            try:
                self.offered[int(p)] = int(until)
            except (TypeError, ValueError):
                pass
        if x402 is not None and not self.state.get("x402_address"):
            # one address for every payment over x402: such a payment is told apart by its transaction, not its address
            self.state["x402_address"] = wallet.fresh_address()
            self._save()

    def _save(self, lazy=False):
        """Writes the book (with the lock held). A call taken from a lot is written lazily: with the next save, at
        most SAVE_EVERY seconds late, so that the whole book is not written at every call (human review, 22 November).
        A crash in that time gives those calls back to the buyer, never takes any. Everything else is written at
        once, with fsync."""
        now = self.clock()
        if lazy and now - self.saved < SAVE_EVERY:
            self.dirty = True
            return
        state = self.state
        if self.reserved:           # calls of requests still to be answered are written as not spent
            tokens = dict(state["tokens"])
            for key, n in self.reserved.items():
                if key in tokens:
                    tokens[key] = dict(tokens[key], calls=tokens[key]["calls"] + n)
            state = dict(state, tokens=tokens)
        tmp = self.book + ".new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.book)
        self.saved, self.dirty = now, False

    def _prune(self, now):
        """Tokens never paid whose time is long over, and lots whose calls are used up a month after their quote
        ran out, are dropped. A payment over x402 shown again after that buys nothing: its facilitator knows it."""
        self.state["tokens"] = {k: t for k, t in self.state["tokens"].items()
                                if (t["paid"] and (t["calls"] > 0 or now < t["expires"] + KEEP_SPENT)) or now < t["expires"] + 3600
                                or k in self.reserved}

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

    def quote(self, source=None):
        """A quote: an address to pay and a token. Each holds one of the wallet's addresses for QUOTE_LIFE and ten minutes
        more, and the wallet hands out GAP of them in a row that nobody paid; so a place asks QUOTES_PER_MINUTE a minute,
        holds UNPAID_PER_PLACE unpaid at a time, and the last KEEP_FOR_NEWCOMERS are for places that hold none
        (review 11c, T4). `source`: the asker's address, None for the gate's own uses."""
        who = None if source is None else hashlib.sha256(self.salt + place(source).encode()).hexdigest()
        if who is not None and not self.quote_limiter.take(who):
            raise Refused("SLOW", "too many quotes from this place in a minute; ask again in a minute, or pay over x402")
        with self.lock:
            now = self.clock()
            price = self._price()
            # an address stays with its token until the time for a late payment is over too
            open_marks = {k for k, t in self.state["tokens"].items() if not t["paid"] and now < t["expires"] + 600}
            self.quoted = {k: w for k, w in self.quoted.items() if k in open_marks}
            waiting = {self.state["tokens"][k]["address"] for k in open_marks}
            if who is not None:
                mine = sum(1 for w in self.quoted.values() if w == who)
                if mine >= UNPAID_PER_PLACE or (mine and len(waiting) >= GAP - KEEP_FOR_NEWCOMERS):
                    raise Refused("BUSY", "quotes of this place wait for their payment: pay one of them, or ask again when "
                                          "they run out (%d minutes), or pay over x402" % ((QUOTE_LIFE + 600) // 60))
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
            if who is not None:
                self.quoted[mark(token)] = who
            self._prune(now)
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
        """One call, spent at once. True when it may pass."""
        how = self.reserve(token, source)
        if how == "lot":
            self.done(token)
        return how is not None

    def reserve(self, token, source):
        """One call for a request that is still to be answered: "lot" when it was taken from the token's lot (it is
        spent with done(), or goes back with give_back()), "free" when from the free calls of the place, None when
        there is none. A call taken and not yet done is written to the book as not spent, so that a fall of the gate
        in between gives it back to the buyer and never takes it (review 11b, N3)."""
        with self.lock:
            key = mark(token) if token else None
            t = self.state["tokens"].get(key) if token else None
            if t is not None:
                self._settle(t)
                if t["calls"] > 0:
                    t["calls"] -= 1
                    self.reserved[key] = self.reserved.get(key, 0) + 1
                    return "lot"
            if self.free and source is not None:
                # one place is an IPv4 address or an IPv6 /48, as at the faucet and the pool (human review, 22 November)
                day, key = int(self.clock() // 86400), hashlib.sha256(self.salt + place(source).encode()).hexdigest()[:16]
                if day != self.free_day:
                    self.free_used, self.free_day = {}, day
                used = self.free_used.get(key, 0)
                if used < self.free:
                    self.free_used[key] = used + 1
                    return "free"
            return None

    def _unreserve(self, key):
        """Called with the lock held. False when no call of that lot was waiting."""
        n = self.reserved.get(key, 0)
        if n <= 0:
            return False
        if n == 1:
            del self.reserved[key]
        else:
            self.reserved[key] = n - 1
        return True

    def done(self, token):
        """The call reserved for a request is spent: written with the next save, at most SAVE_EVERY late."""
        with self.lock:
            if token and self._unreserve(mark(token)):
                self._save(lazy=True)

    def give_back(self, token):
        """The call reserved for a request goes back to its lot, when the service behind the gate could not take it
        (GIVE_BACK, or no answer): a letter the post office refuses for a limit or a full inbox costs no postage
        (review 11, H5). Nothing is written: in the book that call was never spent (review 11b, N3)."""
        with self.lock:
            key = mark(token) if token else None
            t = self.state["tokens"].get(key) if key else None
            if t is not None and self._unreserve(key):
                t["calls"] += 1

    def flush(self):
        """Writes what a lazy save left unwritten."""
        with self.lock:
            if self.dirty:
                self._save()

    def x402_wanted(self, amount=None):
        """The requirements of x402 for one lot. A price asked in the last hour is still taken, so that a
        price that moved with the network does not refuse who paid the one before."""
        with self.lock:
            now = self.clock()
            price = self._price()
            before = set(self.offered)
            self.offered = {p: until for p, until in self.offered.items() if until > now}
            self.offered[price] = now + 6 * PRICE_LIFE
            if set(self.offered) != before or now - self.offered_kept >= PRICE_LIFE:
                self.state["offered"] = {str(p): int(until) for p, until in self.offered.items()}
                self._save()
                self.offered_kept = now
            try:
                asked = int(amount) if amount is not None else None
            except (TypeError, ValueError):
                asked = None
            if asked in self.offered:
                price = asked
        return requirements(self.x402.network, price, self.state["x402_address"], confirmations=self.x402.needed(price))

    def _offer(self, path):
        for o in self.offers:
            if path == o["prefix"] or path.startswith(o["prefix"].rstrip("/") + "/") or path.startswith(o["prefix"] + "?"):
                return o
        return None

    def described(self, path):
        """(resource, extensions) as the gate itself states them for a call to `path`: the seller says what it sells."""
        resource = {"url": (self.public_url or "") + path, "description": "postage for %d calls" % self.calls,
                    "mimeType": "application/json"}
        o = self._offer(path)
        if o is None:
            return resource, None
        resource["description"] = "%s (one payment buys %d calls)" % (o["description"], self.calls)
        resource["mimeType"] = o.get("mimeType", "application/json")
        for k in ("serviceName", "tags", "iconUrl"):
            if o.get(k):
                resource[k] = o[k]
        if not self.public_url:
            return resource, None               # a catalog takes only absolute URLs
        from .bazaar import declaration
        ext = declaration(o.get("method", "GET"), o.get("pathParams"), o.get("queryParams"), o.get("output"), o.get("route"))
        return resource, {"bazaar": ext}

    def x402_needed(self, path):
        resource, ext = self.described(path)
        needed = {"x402Version": X402_VERSION, "error": "PAYMENT-SIGNATURE header is required", "resource": resource,
                  "accepts": [self.x402_wanted()]}
        if ext:
            needed["extensions"] = ext
        return needed

    def x402_pay(self, payment, path=None, source=None):
        """A payment of x402 for one lot. (what came of it, True when this call may pass: a call of the lot is then
        reserved, and the caller says done() or give_back() when the service has answered). What the payment says it
        buys (resource, extensions) is put back to what the gate states for `path`, so that a buyer cannot have the
        facilitator write something else into its catalog. A payment that is not one of a paid lot goes to the
        facilitator at most X402_PER_MINUTE times a minute from one place (`source`; human review, 22 November)."""
        if isinstance(payment, dict) and path is not None:
            resource, ext = self.described(path)
            payment = dict(payment, resource=resource)
            payment.pop("extensions", None)
            if ext:
                payment["extensions"] = ext
        body = payment.get("payload") if isinstance(payment, dict) else None
        claim = body.get("claim") if isinstance(body, dict) else None
        if not isinstance(claim, str) or not CLAIM.match(claim):
            return {"success": False, "errorReason": "claim_required", "transaction": "", "network": self.x402.network, "payer": None}, False
        with self.lock:
            t = self.state["tokens"].get(mark(claim))
        if t is not None:                   # the lot is paid already: the payment shown again is one more call of it
            done = {"success": True, "transaction": t.get("txid", ""), "network": self.x402.network, "payer": None}
            return done, self.reserve(claim, None) is not None     # the caller then says done() or give_back()
        if source is not None and self.x402_limiter is not None and not self.x402_limiter.take(place(source)):
            return {"success": False, "errorReason": TOO_MANY, "transaction": "", "network": self.x402.network, "payer": None}, False
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
        return done, self.reserve(claim, None) is not None

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
             "note": "postage is counted in calls and is not paid back; a call the service behind could not take (429, 503, "
                     "507) goes back to the lot; NORTH has no market price"}
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

    class H(OneRequestWithBody, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "polarka-postage"
        timeout = 60                # a client that announces a body and sends nothing does not hold a thread for ever

        def source(self):
            # behind the web server: the last element of the last X-Forwarded-For line (human review, 22 November)
            return (forwarded(self) if trust_proxy else "") or self.client_address[0]

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
            token = spent = None        # spent "lot": a call reserved, to be done or given back before the end
            try:
                if self.headers.get("Transfer-Encoding") is not None:
                    # a body in pieces is not read here: without this answer the service behind would get an empty one
                    self.close_connection = True
                    outcome = "chunked"
                    return self.send(411, {"error": "INPUT", "message": "send the length of the body (Content-Length)"})
                n = one_length(self.headers)
                if n is None:
                    self.close_connection = True
                    outcome = "bad length"
                    return self.send(400, {"error": "INPUT", "message": "Content-Length is one number"})
                if n > MAX_BODY:
                    self.close_connection = True
                    outcome = "too large"
                    return self.send(413, {"error": "INPUT", "message": "request size"})
                body = self.rfile.read(n) if n else b""
                if self.path == "/_postage/quote" and self.command == "POST":
                    what, outcome = "quote", "ok"
                    return self.send(200, gate.quote(self.source()))
                if self.path == "/_postage/status":
                    what, outcome = "status", "ok"
                    return self.send(200, gate.status(self.headers.get("X-Postage")))
                token, signed, more, spent = self.headers.get("X-Postage"), self.headers.get("PAYMENT-SIGNATURE"), {}, None
                if gate.x402 is not None and signed and not token:
                    what = "x402"
                    try:
                        payment = from_b64json(signed)
                    except Exception:
                        outcome = "unreadable"
                        return self.send(400, {"error": "INPUT", "message": "the payment cannot be read"})
                    done, may = gate.x402_pay(payment, self.path, self.source())
                    if may:                 # a call of the lot is reserved now: it is done or given back below
                        token, spent = payment["payload"]["claim"], "lot"
                    more["PAYMENT-RESPONSE"] = b64json(done)
                    if not may and done.get("errorReason") == TOO_MANY:
                        outcome = "x402 too many"
                        return self.send(429, {"error": "TOO_MANY", "message": "too many payments from one place; try again "
                                                                                "in a minute"}, dict(more, **{"Retry-After": "60"}))
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
                else:
                    # calls for nothing are for the service before the gate; what it routes elsewhere costs postage
                    spent = gate.reserve(token, self.source() if target(self.path) is default else None)
                    if spent is None:
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
                    answer = r.read(MAX_ANSWER + 1)
                    if len(answer) > MAX_ANSWER:
                        raise ValueError("the service behind the gate answered more than %d bytes" % MAX_ANSWER)
                    if spent == "lot" and r.status in GIVE_BACK:
                        gate.give_back(token)       # the service could not take the call: it costs nothing (review 11, H5)
                        outcome = "given back %d" % r.status
                        left = gate.left(token)
                        if left is not None:
                            more["X-Postage-Calls-Left"] = str(left)
                    elif spent == "lot":
                        gate.done(token)
                    spent = None
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
                self.send({"BUSY": 503, "SLOW": 429}.get(e.kind, 400), {"error": e.kind, "message": str(e)})
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
                if spent == "lot":
                    gate.give_back(token)       # the service behind did not answer: the call goes back to the lot
                    outcome += ", given back"
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


def load_offers(path):
    """What the gate lets be found in a bazaar, from a JSON file: a list of objects with at least prefix and
    description (see Gate)."""
    with open(path) as f:
        offers = json.load(f)
    if not isinstance(offers, list) or not all(isinstance(o, dict) and isinstance(o.get("prefix"), str) and o["prefix"].startswith("/")
                                               and isinstance(o.get("description"), str) for o in offers):
        raise Refused("INPUT", "the offers file is a list of objects, each with a prefix (/...) and a description")
    from .bazaar import route_template
    for o in offers:
        # without a route the payer's own path and query would go into a catalog, one entry for each (review 8, L2)
        if not route_template(o.get("route")) or not o["route"].startswith(o["prefix"]):
            raise Refused("INPUT", "every offer needs a route under its prefix, such as /v1/stamp/:sha256")
    return offers


def serve(wallet, upstream, price, calls, listen, free=0, trust_proxy=False, hashes=0, x402=None, routes=(), public_url=None, offers=()):
    if (price <= 0) == (hashes <= 0) or calls <= 0 or free < 0:
        raise Refused("INPUT", "a price in NORTH or a price in hashes, one of the two, and a number of calls above nothing")
    if x402 is not None and getattr(x402, "chain", None) and x402.chain != wallet.chain:
        raise Refused("CHAIN", "the facilitator settles another chain than the gate's wallet")
    if public_url is not None and not public_url.startswith("https://"):
        raise Refused("INPUT", "--public-url is the https:// address the gate is seen at")
    gate = Gate(wallet, price, calls, free, hashes=hashes, x402=x402, public_url=public_url, offers=offers)
    if hashes:
        try:
            price = units_of(gate.about()["price"])
        except ServerError as e:
            raise Refused("NO_WORK", "the wallet server gives no block template (HTTP %s); a price in hashes needs a server with the mining routes of Northbound" % e.status)
    host, port = listen.rsplit(":", 1)
    # so many connections at once and no more (human review, 22 November)
    srv = Bounded((host, int(port)), make_handler(gate, upstream, trust_proxy, routes), most=64)

    def flushing():
        while True:
            time.sleep(SAVE_EVERY)
            try:
                gate.flush()
            except OSError as e:
                sys.stderr.write("%s ALARM the book could not be written (%s)\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
    threading.Thread(target=flushing, daemon=True).start()

    def stop(*_):                   # a restart writes down the calls taken since the last save
        try:
            gate.flush()
        finally:
            sys.exit(0)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop)
    sys.stderr.write("postage gate on %s before %s: %s NORTH for %d calls%s, %d free a day%s\n"
                     % (listen, upstream, north(price), calls, " (%d hashes of work, at the network of this moment)" % hashes if hashes else "", free,
                        ", x402 on %s" % x402.network if x402 is not None else ""))
    srv.serve_forever()
