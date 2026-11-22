"""NORTH over x402 (version 2): a resource answers 402 with what it costs, the client pays in NORTH and asks
again, the resource takes the payment and answers.

x402 (github.com/x402-foundation/x402, specs/x402-specification-v2.md and specs/transports-v2/http.md) names its
networks in CAIP-2. NORTH is a chain of the Bitcoin kind, so it is named as such chains are (CAIP-4, namespace
bip122): the first 32 hexadecimal characters of the hash of its first block. x402 has no scheme for a chain of
coins yet; this module uses "exact" in the plain way such a chain allows (docs/x402-scheme-exact-bip122.md):

  requirements  {"scheme": "exact", "network": NETWORK, "amount": base units as text, "asset": "NORTH",
                 "payTo": an address, "maxTimeoutSeconds": n, "extra": {"confirmations": k}}
  payload       {"transaction": a signed transaction in hexadecimal with exactly one output to payTo, of at
                 least amount, "claim": 32 to 128 hexadecimal characters the client chose and keeps to itself}
  verify        the transaction pays enough to payTo, the node would take it, it was not used before
  settle        the node is given the transaction; once it is k blocks deep, the request is answered

Why a claim: a payment that has to wait for a block is public while it waits, and whoever copies it from the
network could ask with it first. Only who made the payment knows its claim, so only they get what it bought.
Showing the same payment and claim again is how the client asks while it waits, and how it gets an answer
again that was lost on the way (for `redeliver` seconds after it was given).

How deep: `confirmations` blocks (1 unless the seller says otherwise), or none for an amount under
`instant_below`, where the seller takes the payment as soon as its node does. With none, a payment can still be
replaced before a block; that is a price only for keeping robots away, not for anything of value.

The headers: PAYMENT-REQUIRED (402), PAYMENT-SIGNATURE (the client's payment), PAYMENT-RESPONSE (what came of it),
each a JSON object in base64. While the payment waits for its block the seller answers 202 with Retry-After and a
PAYMENT-RESPONSE whose errorReason is "awaiting_confirmation".

Not settled with x402's own people: that "exact" may be used so for a chain of coins, the claim, the 202, and that
NORTH may be named so. Until then only clients that know this module pay in NORTH.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .httpguard import Bounded, Limiter, OneRequestWithBody, forwarded, one_length, place  # noqa: F401 (Bounded, Limiter: also for others)

from .keys import BadInput, script_for_address
from .wallet import Refused, north

VERSION = 2
ASSET = "NORTH"
# the first 32 hexadecimal characters of each chain's first block (getblockhash 0)
NETWORKS = {"main": "bip122:470c8b8ae824c06575cfae0851282c49"}
MAX_HEADER = 400_000                # a transaction of 200 kB, in hexadecimal and base64, and the rest
MAX_TX_HEX = 200_000
MAX_BODY = 1 << 20                  # the largest request the facilitator service reads
CLAIM = re.compile(r"^[0-9a-f]{32,128}$")
KEYS = ("scheme", "network", "amount", "asset", "payTo")
WAITING, DELIVERED = "waiting", "delivered"
AWAITING = "awaiting_confirmation"
SCAN_MOST = 288                     # blocks looked through for a payment whose coin was spent since
AGAIN = ("unexpected_settle_error", "settlement_failed")       # the facilitator failed, not the payment: ask again later
LOCAL = ("127.0.0.1", "localhost", "::1")


def digits(text, most=18):
    """True for text of ASCII digits only, at most `most` of them. str.isdigit() also takes "\u00b2", which int() then
    refuses (human review, 22 November)."""
    return isinstance(text, str) and 0 < len(text) <= most and text.isascii() and text.isdigit()


def local_or_https(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL)
PENDING = "x402-pending.json"       # in the buyer's wallet folder: payments made and not yet answered, and lots of calls


def b64json(obj):
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def from_b64json(text):
    if not isinstance(text, str) or len(text) > MAX_HEADER:
        raise ValueError("too long")
    obj = json.loads(base64.b64decode(text.encode(), validate=True))
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


def mark(text):
    return hashlib.sha256(text.encode()).hexdigest()


def network_of(node):
    """The CAIP-2 name of the chain the node runs."""
    return "bip122:" + node.call("getblockhash", 0)[:32]


def chain_of(network):
    for chain, name in NETWORKS.items():
        if name == network:
            return chain
    return None


def requirements(network, amount, pay_to, timeout=60, confirmations=None):
    r = {"scheme": "exact", "network": network, "amount": str(int(amount)), "asset": ASSET, "payTo": pay_to,
         "maxTimeoutSeconds": int(timeout)}
    if confirmations is not None:
        r["extra"] = {"confirmations": int(confirmations)}
    return r


# ------------------------------------------------------------------------------------------------ the seller

class Blocks:
    """Which of the payments a facilitator waits for went into which of the last SCAN_MOST blocks. Each block is read
    once, when it comes (the first time, SCAN_MOST blocks back), and only the transactions waited for are written
    down. So a payment whose paying output the seller has spent since is still found, and one that never went into
    a block (its payer replaced it) costs two calls of the node however often it is shown, not a scan of hundreds of
    blocks each time (review 10, H2). A reorganization is noticed by the hash of the block read last."""

    def __init__(self, node, most=SCAN_MOST):
        self.node, self.most, self.lock = node, most, threading.Lock()
        self.read = {}              # height -> hash of the blocks read, the last `most` of them
        self.top = None             # the height read last
        self.found = {}             # txid -> height of the block it is in

    def _forget_above(self, height):
        for h in [h for h in self.read if h > height]:
            del self.read[h]
        self.found = {t: h for t, h in self.found.items() if h <= height}
        self.top = max(self.read) if self.read else None

    def depth(self, txid, waited):
        """Blocks the transaction is in (1 or more), or None when it is in none of the last `most`. waited() gives
        the txids to write down from blocks not read yet."""
        with self.lock:
            tip = int(self.node.call("getblockcount"))
            if self.top is not None and (self.top > tip or self.node.call("getblockhash", self.top) != self.read[self.top]):
                # another branch: back to the last block read that is still in the chain
                h = min(self.top, tip)
                while h in self.read and self.node.call("getblockhash", h) != self.read[h]:
                    h -= 1
                self._forget_above(h if h in self.read else -1)
            start = max(tip - self.most + 1, 0) if self.top is None else self.top + 1
            if start <= tip:
                looked_for = waited()
                for h in range(start, tip + 1):
                    block_hash = self.node.call("getblockhash", h)
                    for t in self.node.call("getblock", block_hash, 1).get("tx", ()):
                        if t in looked_for:
                            self.found[t] = h
                    self.read[h] = block_hash
                    self.top = h
                for h in [h for h in self.read if h <= tip - self.most]:
                    del self.read[h]
                self.found = {t: h for t, h in self.found.items() if h > tip - self.most}
            height = self.found.get(txid)
            return None if height is None else tip - height + 1


class Facilitator:
    """Checks payments and gives them to the seller's own node (a walletd.Node, or anything with call()). What it
    took is kept in `used` (a file, one JSON line a change), so that one payment cannot buy twice, not even after a
    start again."""

    def __init__(self, node, chain, used, confirmations=1, instant_below=0, redeliver=600, clock=time.time, catalog=None,
                 most_payments=None, new_per_place=None):
        if not 0 <= confirmations <= 100 or instant_below < 0 or redeliver < 0 or (most_payments is not None and most_payments < 1) \
                or (new_per_place is not None and new_per_place < 1):
            raise ValueError("confirmations 0 to 100, instant_below and redeliver 0 or more, most_payments and new_per_place 1 or more")
        self.node, self.chain, self.used_path = node, chain, used
        self.confirmations, self.instant_below, self.redeliver, self.clock = confirmations, instant_below, redeliver, clock
        self.most_payments = most_payments     # it keeps so many payments, then takes no new one (None: no bound)
        # new payments one place (an IPv4 address, an IPv6 /48) may bring in a day, so that one sender cannot fill
        # what is kept for all (review 10, M2); the places of this day only, in memory
        self.new_per_place, self._today, self._added = new_per_place, None, {}
        self.catalog = catalog      # a bazaar.Catalog: what is settled and asks to be found goes into it
        self.told = threading.local()   # what the last settle of this thread says about the extensions
        self.lock = threading.Lock()
        self.network = network_of(node)
        self.known = {}             # txid -> the last word about it
        if os.path.exists(used):
            with open(used) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if not line.startswith("{"):            # the first form of the file: one txid a line, given
                        self.known[line] = {"txid": line, "claim": None, "state": DELIVERED, "at": 0}
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue                            # a line cut short when the machine stopped; the one before stands
                    self.known[rec["txid"]] = rec
        self.blocks = Blocks(node)
        try:
            self.blocks.depth("", self._waited)     # the last blocks are read once now, not while a request waits
        except Exception:
            pass                                    # a node that does not answer yet: they are read when first needed

    def _waited(self):
        with self.lock:
            return {t for t, r in self.known.items() if r.get("state") == WAITING}

    def needed(self, amount):
        """Blocks a payment of this amount must be in before what it buys is given."""
        return 0 if int(amount) < self.instant_below else self.confirmations

    def supported(self):
        return {"kinds": [{"x402Version": VERSION, "scheme": "exact", "network": self.network,
                           "extra": {"confirmations": self.confirmations, "instantBelow": str(self.instant_below)}}],
                "extensions": ["bazaar"] if self.catalog is not None else [], "signers": {}}

    def _keep(self, rec):
        with open(self.used_path, "a+b") as f:
            f.seek(0, os.SEEK_END)
            if f.tell():
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":                      # a line cut short when the machine stopped
                    f.write(b"\n")
            f.write(json.dumps(rec, separators=(",", ":")).encode() + b"\n")
            f.flush()
            os.fsync(f.fileno())
        self.known[rec["txid"]] = rec

    def _read(self, payment):
        """(txid, raw, claim, decoded, None), or with a reason in place of None."""
        if not isinstance(payment, dict) or payment.get("x402Version") != VERSION:
            return None, None, None, None, "invalid_x402_version"
        body = payment.get("payload")
        raw = body.get("transaction") if isinstance(body, dict) else None
        claim = body.get("claim") if isinstance(body, dict) else None
        if not isinstance(raw, str) or not raw or len(raw) > MAX_TX_HEX or len(raw) % 2 or any(c not in "0123456789abcdefABCDEF" for c in raw):
            return None, None, None, None, "invalid_payload"
        if claim is not None and (not isinstance(claim, str) or not CLAIM.match(claim)):
            return None, None, None, None, "invalid_payload"
        try:
            decoded = self.node.call("decoderawtransaction", raw)
        except Exception:
            return None, None, None, None, "invalid_transaction"
        return decoded["txid"], raw, claim, decoded, None

    @staticmethod
    def _matches(payment, decoded, wanted):
        """(the output that pays, None), or (None, a reason): the client accepted these requirements, and the
        transaction has exactly one output to payTo, of at least the amount."""
        accepted = payment.get("accepted")
        if not isinstance(accepted, dict) or any(accepted.get(k) != wanted.get(k) for k in KEYS):
            return None, "invalid_payment_requirements"
        from .walletd import units
        hits = [(o["n"], units(o["value"])) for o in decoded.get("vout", ()) if o.get("scriptPubKey", {}).get("address") == wanted["payTo"]]
        if not hits:
            return None, "insufficient_funds"
        if len(hits) > 1:
            return None, "invalid_payload"
        if hits[0][1] < int(wanted["amount"]):
            return None, "insufficient_funds"
        return hits[0][0], None

    def _node_takes(self, raw):
        """Only a transaction the node does not have yet: one it has (waiting or in a block) was sent by somebody
        else, and whoever shows it now need not be who paid."""
        return self.node.call("testmempoolaccept", [raw])[0].get("allowed") is True

    def _need(self, wanted):
        """Blocks a payment for these requirements must be in: what the seller asks in extra.confirmations, and
        never fewer than this facilitator asks itself."""
        asked = (wanted.get("extra") or {}).get("confirmations", 0) if isinstance(wanted.get("extra"), (dict, type(None))) else 0
        asked = asked if type(asked) is int and 0 <= asked <= 100 else self.confirmations
        return max(self.needed(wanted["amount"]), asked)

    @staticmethod
    def _covers(rec, wanted):
        """A payment shown again pays for what is asked now: the same seller, chain and coin, and at least today's price."""
        had = rec.get("wanted") or {}
        if any(had.get(k) != wanted.get(k) for k in ("scheme", "network", "asset", "payTo")) or int(had.get("amount", 0)) < int(wanted["amount"]):
            return "invalid_payment_requirements"
        return None

    def _depth(self, txid, vout, since=None):
        """Blocks the payment is in (0: waiting in the node), or None when the node no longer knows it. A paying
        output the seller has spent since is not in the node's set of coins any more: then the payment is looked
        for in the last SCAN_MOST blocks (human review, 22 November), each of which is read once (review 10, H2)."""
        out = self.node.call("gettxout", txid, vout, True)
        if out is not None:
            return int(out.get("confirmations", 0))
        try:
            self.node.call("getmempoolentry", txid)
            return 0
        except Exception:
            pass
        if since is None:
            return None
        try:
            return self.blocks.depth(txid, self._waited)
        except Exception:
            return None                 # a node that does not let this user read blocks: as before, not found

    def _fail(self, why, txid=None, **more):
        return dict({"success": False, "errorReason": why, "transaction": txid or "", "network": self.network, "payer": None}, **more)

    def _ok(self, txid):
        return {"success": True, "transaction": txid, "network": self.network, "payer": None}

    @staticmethod
    def _resource(payment, wanted=None):
        """What the payment buys: the resource the seller names in extra.resource, else the one the buyer's payload
        names. Kept as a hash of the whole text."""
        named = (wanted or {}).get("extra", {}).get("resource") if isinstance((wanted or {}).get("extra"), dict) else None
        if not isinstance(named, str):
            r = payment.get("resource")
            named = r.get("url") if isinstance(r, dict) else None
        return mark(named) if isinstance(named, str) else None

    def _owner(self, rec, claim):
        return claim is not None and rec.get("claim") is not None and rec["claim"] == mark(claim)

    def _full(self):
        """It keeps as many payments as it may: a new one is refused, one it has is still settled."""
        return self.most_payments is not None and len(self.known) >= self.most_payments

    def _may_add(self, who, now):
        """Under the lock: a place that brought new_per_place new payments this day brings no more until the next."""
        if who is None or self.new_per_place is None:
            return True
        day = int(now // 86400)
        if self._today != day:
            self._today, self._added = day, {}
        if self._added.get(who, 0) >= self.new_per_place:
            return False
        self._added[who] = self._added.get(who, 0) + 1
        return True

    def verify(self, payment, wanted):
        # the node is asked outside the lock; only self.known is read under it (human review, 22 November)
        txid, raw, claim, decoded, why = self._read(payment)
        if why is None:
            with self.lock:
                rec = self.known.get(txid)
            if rec is not None:
                if not self._owner(rec, claim) or (rec["state"] == DELIVERED and self.clock() - rec["at"] > self.redeliver):
                    why = "transaction_already_used"
                elif rec.get("resource") != self._resource(payment, wanted):
                    why = "invalid_payment_requirements"
                else:
                    why = self._matches(payment, decoded, rec["wanted"])[1] or self._covers(rec, wanted)
            else:
                why = self._matches(payment, decoded, wanted)[1]
                if why is None and self._need(wanted) and claim is None:
                    why = "claim_required"
                if why is None and self._full():
                    why = "settlement_failed"                           # it takes no new payment now; ask again later
                if why is None and not self._node_takes(raw):
                    why = "transaction_refused_by_node"
        return {"isValid": why is None, "invalidReason": why, "payer": None}

    def settle(self, payment, wanted, who=None):
        """Takes the payment. Gives success once it is deep enough; until then "awaiting_confirmation" with how deep
        it is. The same payment with the same claim may be shown again as often as needed. A settled payment that
        carries the bazaar extension puts its resource into the catalog; what came of that is in self.told.bazaar.
        who: the place the request came from, for new_per_place."""
        self.told.bazaar = None
        done = self._settle(payment, wanted, who)
        if done.get("success") and not done.get("redelivered") and self.catalog is not None:
            try:
                self.told.bazaar = self.catalog.add(payment, wanted)
            except Exception:
                self.told.bazaar = {"status": "rejected", "rejectedReason": "the catalog could not be written"}
        return done

    def _settle(self, payment, wanted, who=None):
        # The node is asked outside the lock (human review, 22 November): under it only self.known and the file are
        # read and written, so that one caller who floods or a node that is slow holds up nobody else. A payment is
        # still written down before the node has it, and once: who comes second finds it written.
        txid, raw, claim, decoded, why = self._read(payment)
        if why is not None:
            return self._fail(why, txid)
        now = self.clock()
        with self.lock:
            rec = self.known.get(txid)
        made = False
        if rec is None:
            vout, why = self._matches(payment, decoded, wanted)
            if why is not None:
                return self._fail(why, txid)
            need = self._need(wanted)
            if need and claim is None:
                return self._fail("claim_required", txid)
            if self._full():
                return self._fail("settlement_failed", txid)            # it takes no new payment now; ask again later
            if not self._node_takes(raw):
                return self._fail("transaction_refused_by_node", txid)
            with self.lock:
                rec = self.known.get(txid)                              # another request may have written it meanwhile
                if rec is None:
                    if self._full() or not self._may_add(who, now):
                        return self._fail("settlement_failed", txid)
                    rec = {"txid": txid, "claim": mark(claim) if claim else None, "vout": vout, "need": need,
                           "resource": self._resource(payment, wanted),
                           "wanted": {k: wanted[k] for k in KEYS}, "state": WAITING, "at": int(now)}
                    self._keep(rec)                     # kept before the node has it: never sold twice
                    made = True
            if made:
                try:
                    self.node.call("sendrawtransaction", raw)
                except Exception as e:
                    if "already" not in str(e).lower():
                        return self._fail("settlement_failed", txid)
        if not made:
            if not self._owner(rec, claim):
                return self._fail("transaction_already_used", txid)
            # shown again: it is held to what was asked when it was made, not to today's price
            if (self._matches(payment, decoded, rec["wanted"])[1] is not None or self._covers(rec, wanted)
                    or rec.get("resource") != self._resource(payment, wanted)):
                return self._fail("invalid_payment_requirements", txid)
            if rec["state"] == DELIVERED:
                if now - rec["at"] <= self.redeliver:
                    return dict(self._ok(txid), redelivered=True)       # the same answer again, not a new sale
                return self._fail("transaction_already_used", txid)
        need = max(rec.get("need", 0), self._need(wanted))
        if need:
            depth = self._depth(txid, rec["vout"], since=rec.get("at"))
            if depth is None:
                try:
                    self.node.call("sendrawtransaction", raw)       # the node may have let it go; it is given again
                except Exception:
                    pass
                depth = self._depth(txid, rec["vout"], since=rec.get("at"))
            if depth is None:
                return self._fail("payment_not_found", txid)        # another transaction took its coins
            if depth < need:
                return self._fail(AWAITING, txid, confirmations=depth, required=need)
        with self.lock:
            last = self.known.get(txid)
            if last is not None and last["state"] == DELIVERED:
                return dict(self._ok(txid), redelivered=True)       # another request with the same claim was first
            self._keep(dict(rec, state=DELIVERED, at=int(now)))
        return self._ok(txid)


class RemoteFacilitator:
    """A facilitator reached over HTTP (/supported, /verify, /settle), for a seller without a node of its own."""

    def __init__(self, url, timeout=30):
        self.url, self.timeout = url.rstrip("/"), timeout
        if not local_or_https(self.url):
            raise Refused("X402", "a facilitator is reached over https (or on this machine)")
        kinds = [k for k in self._call("/supported").get("kinds", []) if isinstance(k, dict) and k.get("scheme") == "exact"
                 and str(k.get("network", "")).startswith("bip122:")]
        if not kinds:
            raise Refused("X402", "the facilitator does not settle NORTH")
        extra = kinds[0].get("extra") or {}
        self.network = kinds[0]["network"]
        self.chain = chain_of(self.network)
        self.confirmations, self.instant_below = int(extra.get("confirmations", 1)), int(extra.get("instantBelow", "0"))

    def needed(self, amount):
        return 0 if int(amount) < self.instant_below else self.confirmations

    def _call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data, {"Content-Type": "application/json", "User-Agent": "polarka-agent"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                got = json.loads(r.read(MAX_BODY).decode())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Refused("X402", "the facilitator cannot be reached (%s)" % type(e).__name__)
        if not isinstance(got, dict):
            raise Refused("X402", "the facilitator sent something that is not an object")
        return got

    def verify(self, payment, wanted):
        return self._call("/verify", {"x402Version": VERSION, "paymentPayload": payment, "paymentRequirements": wanted})

    def settle(self, payment, wanted):
        try:
            return self._call("/settle", {"x402Version": VERSION, "paymentPayload": payment, "paymentRequirements": wanted})
        except Refused:
            return {"success": False, "errorReason": "unexpected_settle_error", "transaction": "", "network": self.network, "payer": None}


def caller(address):
    """Whom a request to a facilitator for sellers from outside counts against: its address, and for IPv6 the /64
    it is in, which one machine may hold whole."""
    return place(address, 64)


def wide(address):
    """The wider place a caller counts against too: the IPv6 /48 its /64 is in, which one site may hold whole; none
    for an IPv4 address (review 10, M2)."""
    p = place(address, 48)
    return p if "/" in p else None


def facilitator_server(facilitator, listen=("127.0.0.1", 0), most=64, public=False, per_minute=60, clock=time.monotonic):
    """The facilitator as a service, as x402 names it: GET /supported, POST /verify and POST /settle with
    {"x402Version": 2, "paymentPayload": ..., "paymentRequirements": ...}. It keeps no coins and signs nothing: it
    checks a payment and hands it to its node. For sellers that have no node of their own.

    With `public` it serves sellers from outside, behind a web server that names the caller in X-Forwarded-For:
    it takes a payment to /verify or /settle at most `per_minute` times a minute from one caller (an IPv6 address
    by its /64) and ten times that from all of them together, and it keeps no catalog, so that nothing others sell
    is listed in our name. Run it apart from the facilitator of one's own gate: its payments, its lock and its
    memory are then its own."""
    if public and getattr(facilitator, "catalog", None) is not None:
        raise ValueError("a facilitator for sellers from outside keeps no catalog")
    limiter = Limiter(per_minute, clock) if public else None

    class Handler(OneRequestWithBody, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "polarka-x402"
        timeout = 30

        def log_message(self, *a):
            pass

        def send(self, status, obj, more=None):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (more or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            if parts.path == "/supported":
                return self.send(200, facilitator.supported())
            catalog = getattr(facilitator, "catalog", None)
            if catalog is not None and parts.path in ("/discovery/resources", "/discovery/search"):
                params = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items() if v}
                if parts.path == "/discovery/search" and not params.get("query"):
                    return self.send(400, {"error": "query is required"})
                return self.send(200, catalog.resources(params) if parts.path == "/discovery/resources" else catalog.search(params))
            self.send(404, {"error": "there is /supported, /verify and /settle here"
                                     + (", and /discovery/resources and /discovery/search" if catalog is not None else "")})

        def client(self):
            # behind the web server the last entry of the last X-Forwarded-For is the address it saw; whatever the
            # caller wrote before it counts for nothing. The caller, and the wider place it counts against too.
            address = forwarded(self) or self.client_address[0]
            return caller(address), wide(address)

        def do_POST(self):
            settle = self.path == "/settle"
            if self.path not in ("/verify", "/settle"):
                self.close_connection = True
                return self.send(404, {"error": "there is /supported, /verify and /settle here"})
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return self.send(411, {"error": "send Content-Length"})
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.close_connection = True
                return self.send(411, {"error": "send Content-Length"})
            if not 0 < n <= MAX_BODY:
                self.close_connection = True
                return self.send(413, {"error": "the request is too large"})
            try:
                body = json.loads(self.rfile.read(n).decode())
                payment, wanted = body["paymentPayload"], body["paymentRequirements"]
                if not isinstance(payment, dict) or not isinstance(wanted, dict):
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeDecodeError, RecursionError):
                return self.send(400, {"error": "a JSON object with paymentPayload and paymentRequirements"})
            why = None
            if wanted.get("scheme") != "exact" or wanted.get("asset") != ASSET:
                why = "unsupported_scheme"
            elif wanted.get("network") != facilitator.network:
                why = "invalid_network"
            else:
                amount, pay_to = wanted.get("amount"), wanted.get("payTo")
                try:
                    if not digits(amount) or not 0 < int(amount) < 10 ** 17 or not isinstance(pay_to, str):
                        raise BadInput("amount")
                    script_for_address(facilitator.chain, pay_to)
                except BadInput:
                    why = "invalid_payment_requirements"
            if why is not None:
                answer = ({"success": False, "errorReason": why, "transaction": "", "network": facilitator.network, "payer": None}
                          if settle else {"isValid": False, "invalidReason": why, "payer": None})
                return self.send(200, answer)
            place_of = {}
            if limiter is not None:
                who, wider = self.client()
                if not limiter.take(who, wider):
                    # counted only here, before the node is asked: the body is read, so the web server gets the answer
                    self.close_connection = True
                    return self.send(429, {"error": "too many requests from one address; try again in a minute"}, {"Retry-After": "60"})
                place_of = {"who": wider or who}       # new payments a day count against the wider place
            more = {}
            try:
                answer = facilitator.settle(payment, wanted, **place_of) if settle else facilitator.verify(payment, wanted)
                told = getattr(getattr(facilitator, "told", None), "bazaar", None) if settle else None
                if told:
                    more["EXTENSION-RESPONSES"] = b64json({"bazaar": told})
            except Exception:
                answer = ({"success": False, "errorReason": "unexpected_settle_error", "transaction": "", "network": facilitator.network,
                           "payer": None} if settle else {"isValid": False, "invalidReason": "unexpected_verify_error", "payer": None})
            self.send(200, answer, more)

    return Bounded(listen, Handler, most)


def paywall(facilitator, price, pay_to, content, content_type="text/plain; charset=utf-8", description="", listen=("127.0.0.1", 0)):
    """A small HTTP server whose one resource (any path) costs `price` base units, paid to `pay_to`. For trying x402
    in NORTH and for the tests; a real seller puts the same steps in front of its own resource."""
    if getattr(facilitator, "chain", None):
        script_for_address(facilitator.chain, pay_to)
    wanted = requirements(facilitator.network, price, pay_to, confirmations=facilitator.needed(price))

    class Handler(OneRequestWithBody, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send(self, status, body, headers):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            asked = self.headers.get("PAYMENT-SIGNATURE")
            if asked is None:
                needed = {"x402Version": VERSION, "error": "PAYMENT-SIGNATURE header is required",
                          "resource": {"url": self.path, "description": description, "mimeType": content_type}, "accepts": [wanted]}
                return self.send(402, {}, {"Content-Type": "application/json", "PAYMENT-REQUIRED": b64json(needed)})
            try:
                payment = from_b64json(asked)
            except Exception:
                return self.send(400, {"error": "the payment cannot be read"}, {"Content-Type": "application/json"})
            asked_for = payment.get("resource") if isinstance(payment.get("resource"), dict) else {}
            if asked_for.get("url") != self.path:
                done = {"success": False, "errorReason": "invalid_payment_requirements", "transaction": "", "network": facilitator.network,
                        "payer": None}
                return self.send(402, {}, {"Content-Type": "application/json", "PAYMENT-RESPONSE": b64json(done)})
            done = facilitator.settle(payment, dict(wanted, extra=dict(wanted.get("extra") or {}, resource=self.path)))
            told = {"Content-Type": "application/json", "PAYMENT-RESPONSE": b64json(done)}
            if done.get("success"):
                return self.send(200, content, dict(told, **{"Content-Type": content_type}))
            if done.get("errorReason") == AWAITING:
                return self.send(202, {"waiting": "the payment is not deep enough in the chain yet; send the same PAYMENT-SIGNATURE again"},
                                 dict(told, **{"Retry-After": "20"}))
            if done.get("errorReason") in AGAIN:
                return self.send(503, {"error": "the payment could not be settled just now; send the same PAYMENT-SIGNATURE again"},
                                 dict(told, **{"Retry-After": "60"}))
            return self.send(402, {}, told)

    server = ThreadingHTTPServer(listen, Handler)
    server.daemon_threads = True
    return server


# ------------------------------------------------------------------------------------------------ the buyer

_BOOK_LOCK = threading.Lock()


class BOOK:
    """with BOOK(wallet): the buyer's book is changed by one at a time: one thread of this process (a lock) and one
    process (a lock on a file beside the book), so that the command line and an MCP server of the same wallet never
    write over each other's lots (human review, 22 November). Where there is no fcntl, the lock of this process only."""

    def __init__(self, wallet):
        self.path, self.fd = os.path.join(wallet.folder, PENDING + ".lock"), None

    def __enter__(self):
        _BOOK_LOCK.acquire()
        try:
            import fcntl
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except ImportError:
            self.fd = None
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            _BOOK_LOCK.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                os.close(self.fd)               # closing lets the lock go
                self.fd = None
        finally:
            _BOOK_LOCK.release()


def _book(wallet):
    try:
        with open(os.path.join(wallet.folder, PENDING)) as f:
            book = json.load(f)
    except (FileNotFoundError, ValueError):
        book = {}
    if not isinstance(book, dict):
        book = {}
    book.setdefault("pending", {})
    book.setdefault("lots", {})
    return book


def _keep_book(wallet, book):
    path = os.path.join(wallet.folder, PENDING)
    tmp = path + ".new"
    # 600 from the first byte, whatever the umask: it holds claims and signed payments (review 10, L5)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(book, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)                    # one left by an older version keeps its mode through O_TRUNC
    os.replace(tmp, path)


def _next_lot(book, origin):
    spare = book.setdefault("spare", {}).get(origin) or []
    if spare:
        book["lots"][origin] = spare.pop(0)
        book["spare"][origin] = spare
    else:
        book["lots"].pop(origin, None)


def fetch(url, wallet, most, timeout=60, network=None, wait=600, sleep=time.sleep, clock=time.time):
    """GET url; when it answers 402 and takes NORTH on the wallet's chain for at most `most` base units, pays
    within the owner's limits and asks again, while the seller waits for a block for up to `wait` seconds.

    The payment is kept in the wallet's folder before anyone sees it, with its claim: a later fetch of the same url
    shows the same payment again and pays nothing more, until the seller answers or refuses. The payment is handed to
    the seller, not to a server: until a server shows it in a block or waiting, the wallet's next payment stands
    against it. When a gate of postage answers that the payment bought a lot of calls (X-Postage-Calls-Left), the
    rest of the lot is used for the next calls to the same place before anything is paid again."""
    network = network or NETWORKS.get(wallet.chain)
    if network is None:
        raise Refused("X402", "x402 in NORTH on %s needs the network's name (network=)" % wallet.chain)
    if not local_or_https(url):
        raise Refused("X402", "x402 is used over https (or on this machine)")
    parts = urllib.parse.urlsplit(url)
    origin = "%s://%s" % (parts.scheme, parts.netloc)

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):        # a payment and a lot token go to this url and nowhere else
            return None
    opener = urllib.request.build_opener(Stay)

    def read(r):
        # at most 2 MB, and within the time: a server that sends a byte now and then cannot hold the buyer (human review)
        until, got = clock() + 2 * timeout, b""
        while len(got) < 2_000_000:
            chunk = r.read1(65536)
            if not chunk:
                break
            got += chunk
            if clock() > until:
                raise Refused("X402", "the answer took longer than %d seconds" % (2 * timeout))
        return got[:2_000_000]

    def get(headers):
        req = urllib.request.Request(url, headers=dict(headers, **{"User-Agent": "polarka-agent"}))
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.headers, read(r)            # headers are looked up whatever their case
        except urllib.error.HTTPError as e:
            return e.code, e.headers, read(e)

    def told(headers):
        try:
            return from_b64json(headers.get("PAYMENT-RESPONSE"))
        except Exception:
            return None

    def calls_left(headers):
        left = headers.get("X-Postage-Calls-Left")
        return int(left) if digits(left, 12) else None

    with BOOK(wallet):
        book = _book(wallet)
    held, lot = book["pending"].get(url), book["lots"].get(origin)
    if held is None:
        status, headers, body = get({"X-Postage": lot["claim"]} if lot else {})
        if lot:
            left = calls_left(headers)
            with BOOK(wallet):
                book = _book(wallet)
                if (status == 402 and left is None) or left == 0:
                    _next_lot(book, origin)                 # used up, or the gate no longer knows it
                elif left:
                    book["lots"][origin] = dict(lot, left=left)
                _keep_book(wallet, book)
            if status != 402 or left is not None:           # the gate let the call through, whatever the service said
                return {"status": status, "paid": None, "calls_left": left, "body": body,
                        "note": "a call of a lot paid for before"}
        if status != 402:
            return {"status": status, "paid": None, "body": body}
        try:
            needed = from_b64json(headers.get("PAYMENT-REQUIRED"))
        except Exception:
            raise Refused("X402", "the answer 402 has no PAYMENT-REQUIRED that can be read")
        offers = [o for o in needed.get("accepts", []) if isinstance(o, dict) and o.get("scheme") == "exact"
                  and o.get("network") == network and o.get("asset") == ASSET]
        if not offers:
            raise Refused("X402", "the resource does not take NORTH on this chain (%s)" % network)
        offer = offers[0]
        try:
            if not digits(offer["amount"]):         # the amount is text of digits, as x402 writes it, never a number
                raise ValueError("amount")
            amount = int(offer["amount"])
            script_for_address(wallet.chain, offer["payTo"])
        except (KeyError, ValueError, TypeError, BadInput):
            raise Refused("X402", "the resource asks for a payment that cannot be read")
        if not 0 < amount <= most:
            raise Refused("X402_TOO_MUCH", "the resource asks %s NORTH, more than the %s allowed for it" % (north(amount), north(most)))
        made = wallet.pay([(offer["payTo"], amount)], "x402 " + url[:150], hand=False)
        if not isinstance(made.get("raw"), str) or not isinstance(made.get("id"), str):
            raise Refused("X402", "the wallet gave no signed payment to hand over")     # a signing service of another kind
        claim = os.urandom(16).hex()
        payment = {"x402Version": VERSION, "resource": needed.get("resource"), "accepted": offer,
                   "payload": {"transaction": made["raw"], "claim": claim}}
        ext = needed.get("extensions")
        if isinstance(ext, dict) and len(json.dumps(ext)) <= 16384:
            payment["extensions"] = ext                 # echoed, as x402 asks of a buyer (the bazaar catalogs from it)
        held = {"header": b64json(payment), "txid": made["id"], "amount": amount, "claim": claim, "made": int(clock())}
        with BOOK(wallet):                          # kept before the seller sees it: one payment for one resource
            book = _book(wallet)
            book["pending"][url] = held
            _keep_book(wallet, book)
        again = False
    else:
        again = True
    start = clock()
    while True:
        status, headers, body = get({"PAYMENT-SIGNATURE": held["header"]})
        settled = told(headers)
        # 202: the seller waits for a block; 503: the seller's facilitator could not settle just now. Both are asked
        # again, with the same payment, within `wait` (human review, 22 November)
        if not ((status == 202 and settled and settled.get("errorReason") == AWAITING) or status == 503):
            break
        left = wait - (clock() - start)
        if left <= 0:
            return {"status": status, "paid": north(held["amount"]), "txid": held["txid"], "settlement": settled, "body": b"",
                    "pending": True, "then": "the seller waits for the payment to be in a block; ask for the same url again "
                                             "later, nothing is paid twice"}
        try:
            retry = int(headers.get("Retry-After", "20"))
        except ValueError:
            retry = 20
        sleep(max(0, min(max(retry, 5), 60, left)))
    with BOOK(wallet):
        book = _book(wallet)
        paid = bool(settled and settled.get("success"))
        final = paid or status == 400 or (status == 402 and (settled is None or settled.get("errorReason") not in AGAIN + (AWAITING,)))
        if final:                                   # settled, or refused for good; anything else is asked again later
            book["pending"].pop(url, None)
        left = calls_left(headers)
        if paid and left:                           # the lot is paid whatever the service behind the gate answered
            new, had = {"claim": held["claim"], "left": left}, book["lots"].get(origin)
            if had and had.get("claim") != held["claim"]:  # a lot still in use: this one waits behind it
                spare = book.setdefault("spare", {}).setdefault(origin, [])
                if all(s.get("claim") != held["claim"] for s in spare):
                    spare.append(new)
            else:
                book["lots"][origin] = new
        _keep_book(wallet, book)
    got = {"status": status, "paid": north(held["amount"]), "txid": held["txid"], "settlement": settled, "body": body}
    if again:
        got["note"] = "the payment made before was shown again; nothing more was paid"
    if calls_left(headers):
        got["calls_left"] = calls_left(headers)
    return got
