"""NORTH over x402 (version 2): a resource answers 402 with what it costs, the client pays in NORTH and asks
again, the resource takes the payment and answers.

x402 (github.com/x402-foundation/x402, specs/x402-specification-v2.md and specs/transports-v2/http.md) names its
networks in CAIP-2. NORTH is a chain of the Bitcoin kind, so it is named as such chains are (CAIP-4, namespace
bip122): the first 32 hexadecimal characters of the hash of its first block. x402 has no scheme for a chain of
coins yet; this module uses "exact" in the plain way such a chain allows:

  requirements  {"scheme": "exact", "network": NETWORK, "amount": base units as text, "asset": "NORTH",
                 "payTo": an address, "maxTimeoutSeconds": n}
  payload       {"transaction": a signed transaction in hexadecimal that pays at least amount to payTo}
  verify        the transaction pays enough to payTo, the node would take it, it was not used before
  settle        the node is given the transaction; its id is the "transaction" of the answer

A transaction can buy one request only: the seller keeps the ids it took. The seller takes the payment as soon
as its node takes the transaction, before any block (NORTH has no price; this is for keeping robots away, not
for trade). The headers: PAYMENT-REQUIRED (402), PAYMENT-SIGNATURE (the client's payment), PAYMENT-RESPONSE
(what came of it), each a JSON object in base64.

Not settled with x402's own people: that "exact" may be used so for a chain of coins, and that NORTH may be named
so. Until then only clients that know this module pay in NORTH.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .keys import BadInput, script_for_address
from .wallet import Refused, north

VERSION = 2
ASSET = "NORTH"
# the first 32 hexadecimal characters of each chain's first block (getblockhash 0)
NETWORKS = {"main": "bip122:470c8b8ae824c06575cfae0851282c49"}
MAX_HEADER = 400_000                # a transaction of 200 kB, in hexadecimal and base64, and the rest
MAX_TX_HEX = 200_000


def b64json(obj):
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def from_b64json(text):
    if not isinstance(text, str) or len(text) > MAX_HEADER:
        raise ValueError("too long")
    obj = json.loads(base64.b64decode(text.encode(), validate=True))
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


def network_of(node):
    """The CAIP-2 name of the chain the node runs."""
    return "bip122:" + node.call("getblockhash", 0)[:32]


def requirements(network, amount, pay_to, timeout=60):
    return {"scheme": "exact", "network": network, "amount": str(int(amount)), "asset": ASSET, "payTo": pay_to,
            "maxTimeoutSeconds": int(timeout)}


# ------------------------------------------------------------------------------------------------ the seller

class Facilitator:
    """Checks payments and gives them to the seller's own node (a walletd.Node, or anything with call()). The ids
    of the transactions it took are kept in `used` (a file, one id a line), so that one cannot buy twice."""

    def __init__(self, node, chain, used):
        self.node, self.chain, self.used_path = node, chain, used
        self.lock = threading.Lock()
        self.network = network_of(node)
        self.used = set()
        if os.path.exists(used):
            with open(used) as f:
                self.used = {line.strip() for line in f if line.strip()}

    def supported(self):
        return {"kinds": [{"x402Version": VERSION, "scheme": "exact", "network": self.network}], "extensions": [], "signers": {}}

    def _check(self, payment, wanted):
        """(txid, None) when the payment is good for the requirements, or (txid or None, reason)."""
        if not isinstance(payment, dict) or payment.get("x402Version") != VERSION:
            return None, "invalid_x402_version"
        accepted = payment.get("accepted")
        if not isinstance(accepted, dict) or any(accepted.get(k) != wanted[k] for k in ("scheme", "network", "amount", "asset", "payTo")):
            return None, "invalid_payment_requirements"
        body = payment.get("payload")
        raw = body.get("transaction") if isinstance(body, dict) else None
        if not isinstance(raw, str) or not raw or len(raw) > MAX_TX_HEX or len(raw) % 2 or any(c not in "0123456789abcdefABCDEF" for c in raw):
            return None, "invalid_payload"
        try:
            decoded = self.node.call("decoderawtransaction", raw)
        except Exception:
            return None, "invalid_transaction"
        txid = decoded["txid"]
        if txid in self.used:
            return txid, "transaction_already_used"
        from .walletd import units
        paid = sum(units(o["value"]) for o in decoded.get("vout", ()) if o.get("scriptPubKey", {}).get("address") == wanted["payTo"])
        if paid < int(wanted["amount"]):
            return txid, "insufficient_funds"
        verdict = self.node.call("testmempoolaccept", [raw])[0]
        if not verdict.get("allowed") and verdict.get("reject-reason") not in ("txn-already-in-mempool", "txn-already-known"):
            return txid, "transaction_refused_by_node"
        return txid, None

    def verify(self, payment, wanted):
        with self.lock:
            txid, why = self._check(payment, wanted)
        return {"isValid": why is None, "invalidReason": why, "payer": None}

    def settle(self, payment, wanted):
        """Takes the payment: the transaction goes to the node and its id is kept, so that it buys nothing more."""
        with self.lock:
            txid, why = self._check(payment, wanted)
            if why is None:
                with open(self.used_path, "a") as f:            # kept before the node has it: never sold twice
                    f.write(txid + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                self.used.add(txid)
                try:
                    self.node.call("sendrawtransaction", payment["payload"]["transaction"])
                except Exception as e:
                    if "already" not in str(e).lower():
                        return {"success": False, "errorReason": "settlement_failed", "transaction": txid, "network": self.network}
        if why is not None:
            return {"success": False, "errorReason": why, "transaction": "", "network": self.network}
        return {"success": True, "transaction": txid, "network": self.network, "payer": None}


def paywall(facilitator, price, pay_to, content, content_type="text/plain; charset=utf-8", description="", listen=("127.0.0.1", 0)):
    """A small HTTP server whose one resource (any path) costs `price` base units, paid to `pay_to`. For trying x402
    in NORTH and for the tests; a real seller puts the same steps in front of its own resource."""
    script_for_address(facilitator.chain, pay_to)
    wanted = requirements(facilitator.network, price, pay_to)

    class Handler(BaseHTTPRequestHandler):
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
            done = facilitator.settle(payment, wanted)
            if not done["success"]:
                return self.send(402, {}, {"Content-Type": "application/json", "PAYMENT-RESPONSE": b64json(done)})
            return self.send(200, content, {"Content-Type": content_type, "PAYMENT-RESPONSE": b64json(done)})

    server = ThreadingHTTPServer(listen, Handler)
    server.daemon_threads = True
    return server


# ------------------------------------------------------------------------------------------------ the buyer

def fetch(url, wallet, most, timeout=60, network=None):
    """GET url; when it answers 402 and takes NORTH on the wallet's chain for at most `most` base units, pays
    within the owner's limits and asks again. Returns what came of it. The payment is handed to the seller, not to
    a server: until a server shows it in a block or waiting, the wallet's next payment stands against it."""
    network = network or NETWORKS.get(wallet.chain)
    if network is None:
        raise Refused("X402", "x402 in NORTH on %s needs the network's name (network=)" % wallet.chain)

    def get(headers):
        req = urllib.request.Request(url, headers=dict(headers, **{"User-Agent": "polarka-agent"}))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers, r.read(2_000_000)          # headers are looked up whatever their case
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read(2_000_000)

    if not url.startswith("https://") and not url.split("//", 1)[-1].startswith(("127.0.0.1", "localhost", "[::1]")):
        raise Refused("X402", "x402 is used over https (or on this machine)")
    status, headers, body = get({})
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
        amount = int(offer["amount"])
        script_for_address(wallet.chain, offer["payTo"])
    except (KeyError, ValueError, TypeError, BadInput):
        raise Refused("X402", "the resource asks for a payment that cannot be read")
    if not 0 < amount <= most:
        raise Refused("X402_TOO_MUCH", "the resource asks %s NORTH, more than the %s allowed for it" % (north(amount), north(most)))
    made = wallet.pay([(offer["payTo"], amount)], "x402 " + url[:150], hand=False)
    payment = {"x402Version": VERSION, "resource": needed.get("resource"), "accepted": offer, "payload": {"transaction": made["raw"]}}
    status, headers, body = get({"PAYMENT-SIGNATURE": b64json(payment)})
    try:
        settled = from_b64json(headers.get("PAYMENT-RESPONSE"))
    except Exception:
        settled = None
    return {"status": status, "paid": north(amount), "txid": made["id"], "settlement": settled, "body": body}
