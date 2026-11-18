"""A faucet: a small web door through which a wallet of this program hands out a little NORTH to whoever asks.

It is meant for programs that have no coin yet. Whoever asks does a small piece of work first (hashing, a few
seconds of one processor), so that asking a thousand times costs a thousand times as much. The faucet holds
no rule about money of its own: what it may pay in a day is the wallet's limits.json, written by its owner.

    python3 -m polarka_agent --dir FOLDER faucet-serve --amount 2 --listen 127.0.0.1:8660

    GET  /                    what this is and how to ask, as JSON
    POST /v1/challenge        {"address": "north1z..."}            -> {"challenge", "bits", "expires"}
    POST /v1/claim            {"address", "challenge", "answer"}   -> {"paid": "2.00000000", "transaction_id"}

The work: find an answer (text of up to 32 hex characters) such that SHA-256(challenge + ":" + answer) begins
with `bits` zero bits. The more the faucet is asked, the more bits it wants.

What it keeps: the addresses it paid and when, in faucet.jsonl beside the wallet (addresses are public in the
chain anyway). It keeps no internet address on disk; in memory it counts requests by a salted hash of the
address they came from, and the salt is new at every start. It asks for no name, no e-mail and nothing else.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import keys
from .keys import BadInput
from .server import ServerError, reachable
from .wallet import Refused, north

MAX_BODY = 4096
CHALLENGE_LIFE = 600            # seconds a challenge may be answered
PERIOD = 24 * 3600              # one address is paid once in this time
BASE_BITS, MAX_BITS = 20, 30    # the work asked for: from about a second to about a quarter of an hour of one core
ANSWER = re.compile(r"^[0-9a-f]{1,32}$")


def leading_zero_bits(digest):
    n = 0
    for byte in digest:
        if byte == 0:
            n += 8
            continue
        return n + 8 - byte.bit_length()
    return n


def work_done(challenge, answer, bits):
    return leading_zero_bits(hashlib.sha256(("%s:%s" % (challenge, answer)).encode()).digest()) >= bits


def solve(challenge, bits, give_up_after=1 << 34):
    """The asker's side of the work. Returns the answer, or None when it was not found."""
    prefix = (challenge + ":").encode()
    for i in range(give_up_after):
        answer = "%x" % i
        if leading_zero_bits(hashlib.sha256(prefix + answer.encode()).digest()) >= bits:
            return answer
    return None


class Faucet:
    def __init__(self, wallet, amount, base_bits=BASE_BITS, clock=time.time, per_source=20):
        self.wallet, self.amount, self.base_bits, self.clock, self.per_source = wallet, amount, base_bits, clock, per_source
        self.secret, self.salt = os.urandom(32), os.urandom(16)
        self.lock = threading.Lock()
        self.book = os.path.join(wallet.folder, "faucet.jsonl")
        self.paid = {}              # address -> when it was last paid
        self.recent = []            # times of the claims of the last hour
        self.sources = {}           # salted hash of where a request came from -> times of its requests
        self.used = set()           # challenges that were answered
        try:
            with open(self.book) as f:
                for text in f:
                    e = json.loads(text)
                    self.paid[e["address"]] = max(self.paid.get(e["address"], 0), e["t"])
        except FileNotFoundError:
            pass

    # the more claims in the last hour, the more work: one bit more for every doubling beyond eight
    def bits(self):
        now = self.clock()
        self.recent = [t for t in self.recent if now - t < 3600]
        return min(MAX_BITS, self.base_bits + max(0, (len(self.recent) // 8).bit_length()))

    def _source_ok(self, source):
        now = self.clock()
        key = hashlib.sha256(self.salt + source.encode()).hexdigest()[:16]
        times = [t for t in self.sources.get(key, []) if now - t < 3600]
        if len(times) >= self.per_source:
            self.sources[key] = times
            return False
        self.sources[key] = times + [now]
        if len(self.sources) > 100000:
            self.sources = {k: v for k, v in self.sources.items() if v and now - v[-1] < 3600}
        return True

    def _sign(self, address, bits, expires, nonce):
        return hmac.new(self.secret, ("%s|%d|%d|%s" % (address, bits, expires, nonce)).encode(), hashlib.sha256).hexdigest()[:32]

    def _address(self, address):
        if not isinstance(address, str) or len(address) > 100:
            raise Refused("ADDRESS", "name the address to be paid")
        try:
            keys.script_for_address(self.wallet.chain, address)
        except BadInput as e:
            raise Refused("ADDRESS", "this is not an address of this network: %s" % e)
        return address

    def challenge(self, source, address):
        with self.lock:
            if not self._source_ok(source):
                raise Refused("TOO_OFTEN", "too many requests from where you are; come back in an hour")
            address = self._address(address)
            now = self.clock()
            if now - self.paid.get(address, -PERIOD) < PERIOD:
                raise Refused("ALREADY", "this address was paid within the last day")
            bits, expires, nonce = self.bits(), int(now) + CHALLENGE_LIFE, os.urandom(8).hex()
            return {"challenge": "%d.%d.%s.%s" % (bits, expires, nonce, self._sign(address, bits, expires, nonce)), "bits": bits, "expires": expires,
                    "work": "find an answer of up to 32 hex characters so that SHA-256(challenge + ':' + answer) begins with that many zero bits"}

    def claim(self, source, address, challenge, answer):
        with self.lock:
            if not self._source_ok(source):
                raise Refused("TOO_OFTEN", "too many requests from where you are; come back in an hour")
            address = self._address(address)
            try:
                bits, expires, nonce, mark = str(challenge).split(".")
                bits, expires = int(bits), int(expires)
            except ValueError:
                raise Refused("CHALLENGE", "this is not a challenge of this faucet")
            if not hmac.compare_digest(mark, self._sign(address, bits, expires, nonce)):
                raise Refused("CHALLENGE", "this challenge was not given for this address, or not by this faucet since it last started")
            now = self.clock()
            if now > expires:
                raise Refused("CHALLENGE", "this challenge is too old; ask for a new one")
            if not isinstance(answer, str) or not ANSWER.match(answer) or not work_done(challenge, answer, bits):
                raise Refused("WORK", "the answer does not do the work asked for")
            if challenge in self.used:
                raise Refused("ALREADY", "this challenge was answered before")
            if now - self.paid.get(address, -PERIOD) < PERIOD:
                raise Refused("ALREADY", "this address was paid within the last day")
            try:
                done = self.wallet.pay([(address, self.amount)], memo="faucet")
            except Refused as e:
                if e.kind in ("OVER_DAY", "NOT_ENOUGH", "NOTHING_TO_SPEND", "NO_LIMITS", "OVER_LIMIT"):
                    raise Refused("EMPTY", "the faucet has given what it may give for now; come back tomorrow")
                raise Refused("NOT_NOW", "the faucet cannot pay just now (%s)" % e.kind)
            # whatever became of the payment, the wallet made and counted it: the address and the challenge are used
            self.used.add(challenge)
            self.paid[address] = now
            self.recent.append(now)
            with open(self.book, "a") as f:
                f.write(json.dumps({"t": int(now), "address": address, "txid": done["id"], "state": done["state"]}, separators=(",", ":")) + "\n")
            if len(self.used) > 200000:
                self.used.clear()       # all of them are past their time long since
            if done["state"] != "handed":
                raise Refused("NOT_NOW", "the payment was made but the network has not taken it yet; do not ask again for this address today")
            return {"paid": north(self.amount), "transaction_id": done["id"]}

    def about(self):
        return {"service": "polarka-faucet", "network": self.wallet.chain, "gives": north(self.amount) + " NORTH to one address once a day",
                "how": ["POST /v1/challenge {\"address\": yours}", "do the work it names", "POST /v1/claim {\"address\", \"challenge\", \"answer\"}"],
                "bits_now": self.bits(), "keeps": "the addresses it paid; no internet addresses on disk, no names",
                "note": "NORTH has no market price. This faucet is run by a community member who mines and holds NORTH."}


def make_handler(faucet, trust_proxy):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "polarka-faucet"

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

        def do_GET(self):
            self.send(200, faucet.about())

        def do_POST(self):
            t0, what, outcome = time.time(), "other", "ok"
            try:
                n = int(self.headers.get("Content-Length", "-1"))
                if n < 2 or n > MAX_BODY:
                    self.close_connection = True
                    return self.send(413, {"error": "INPUT", "message": "request size"})
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
                except ValueError:
                    body = None
                if type(body) is not dict:
                    return self.send(400, {"error": "INPUT", "message": "send a JSON object"})
                if self.path == "/v1/challenge":
                    what = "challenge"
                    self.send(200, faucet.challenge(self.source(), body.get("address")))
                elif self.path == "/v1/claim":
                    what = "claim"
                    self.send(200, faucet.claim(self.source(), body.get("address"), body.get("challenge"), body.get("answer")))
                else:
                    self.send(404, {"error": "INPUT", "message": "there is /v1/challenge and /v1/claim"})
            except Refused as e:
                outcome = e.kind
                self.send(429 if e.kind in ("TOO_OFTEN", "EMPTY") else 400, {"error": e.kind, "message": str(e)})
            except ServerError as e:
                outcome = "SERVER"
                self.send(503, {"error": "NOT_NOW", "message": "the faucet cannot reach its wallet server just now"})
            except Exception as e:
                outcome = "failed %s" % type(e).__name__
                try:
                    self.send(500, {"error": "FAILED", "message": "faucet error"})
                except Exception:
                    pass
            sys.stderr.write("%s %s %s %.0f ms\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), what, outcome, (time.time() - t0) * 1000))

        def log_message(self, *a):
            pass
    return H


def serve(wallet, amount, listen, trust_proxy=False, base_bits=BASE_BITS):
    limits = wallet.limits()
    if limits is None:
        raise Refused("NO_LIMITS", "the faucet's wallet has no limits.json; its owner writes what it may give in a day")
    if amount <= 0 or amount >= limits["per_payment"]:
        # a payment counts with its fee, so a limit equal to the amount would let nothing through
        raise Refused("LIMITS", "the amount given (%s NORTH) and its fee have to fit under per_payment_north in limits.json (%s)"
                      % (north(amount), north(limits["per_payment"])))
    faucet = Faucet(wallet, amount, base_bits)
    host, port = listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), make_handler(faucet, trust_proxy))
    srv.daemon_threads = True
    sys.stderr.write("faucet on %s, %s NORTH to an address a day, work from %d bits\n" % (listen, north(amount), base_bits))
    srv.serve_forever()


def ask(url, address, timeout=30, most_bits=MAX_BITS):
    """The asker's side: challenge, work, claim. Returns what the faucet answered, as a dict. No more work
    is done than most_bits, and no redirect is followed: the address asked is the one that answers."""
    import urllib.error
    import urllib.request

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    def post(path, obj):
        req = urllib.request.Request(url.rstrip("/") + path, json.dumps(obj).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(Stay).open(req, timeout=timeout) as r:
                text = r.read(65536)
        except urllib.error.HTTPError as e:
            text = e.read(65536)
        except (urllib.error.URLError, OSError) as e:
            raise Refused("FAUCET", "the faucet cannot be reached (%s)" % type(e).__name__)
        try:
            answer = json.loads(text.decode("utf-8"))
        except ValueError:
            answer = None
        if type(answer) is not dict:
            raise Refused("FAUCET", "the faucet sent something that is not an answer")
        if "error" in answer:
            raise Refused("FAUCET", "the faucet said no (%s)" % re.sub(r"[^A-Z_]", "", str(answer["error"]))[:20])
        return answer

    if not reachable(url):
        raise Refused("FAUCET", "a faucet is reached over https")
    c = post("/v1/challenge", {"address": address})
    bits, challenge = c.get("bits"), c.get("challenge")
    if type(bits) is not int or not 0 <= bits <= min(most_bits, MAX_BITS) or not isinstance(challenge, str) or len(challenge) > 200:
        raise Refused("FAUCET", "the faucet asks for work this program does not do")
    answer = solve(challenge, bits)
    if answer is None:
        raise Refused("FAUCET", "the work was not done")
    got = post("/v1/claim", {"address": address, "challenge": challenge, "answer": answer})
    txid = got.get("transaction_id")
    if not isinstance(txid, str) or not re.match(r"^[0-9a-f]{64}$", txid):
        raise Refused("FAUCET", "the faucet did not name a transaction")
    return {"asked": address, "transaction_id": txid, "work_bits": bits}
