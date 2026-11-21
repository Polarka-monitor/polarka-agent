"""A faucet: a small web door through which a wallet of this program hands out a little NORTH to whoever asks.

It is meant for programs that have no coin yet. Whoever asks does a small piece of work first (hashing, a few
seconds of one processor in this program). The work is friction and not a wall: a program written for speed
does it many times faster. What bounds the faucet is what it counts: one address once a day, one place (an IPv4
address or an IPv6 /48) a few coins a day, the faucet as a whole so many an hour, and above all of that the
wallet's limits.json, written by its owner. The faucet holds no rule about the day's money of its own.

    python3 -m polarka_agent --dir FOLDER faucet-serve --amount 2 --listen 127.0.0.1:8660

    GET  /                    what this is and how to ask, as JSON
    POST /v1/challenge        {"address": "north1z..."}            -> {"challenge", "bits", "expires"}
    POST /v1/claim            {"address", "challenge", "answer"}   -> {"paid": "2.00000000", "transaction_id"}

The work: find an answer (text of up to 32 hex characters) such that SHA-256(challenge + ":" + answer) begins
with `bits` zero bits. The more challenges the faucet handed out in the last hour, the more bits it wants.

What it keeps: the addresses it paid and when, in faucet.jsonl beside the wallet (addresses are public in the
chain anyway; the wallet's own journal.jsonl holds them too). It keeps no internet address on disk; in memory
it counts requests by a salted hash of the place they came from, and the salt is new at every start. It asks
for no name, no e-mail and nothing else.
"""
import collections
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .httpguard import OneRequestWithBody

from . import keys
from .keys import BadInput
from .server import ServerError, reachable
from .wallet import Refused, north

MAX_BODY = 4096
CHALLENGE_LIFE = 600            # seconds a challenge may be answered
PERIOD = 24 * 3600              # one address is paid once in this time
BASE_BITS, MAX_BITS = 20, 30    # the work asked for: from about a second to about a quarter of an hour of one core
ANSWER = re.compile(r"[0-9a-f]{1,32}")
MAX_SOURCES = 100000            # places counted at one time; beyond that new ones wait
MAX_THREADS = 64                # requests answered at one time
SOCKET_TIMEOUT = 10             # seconds one read of a request may take
REQUEST_TIME = 20               # seconds the whole request has to arrive, however its bytes come
ASK_BITS = 24                   # the most work this program's own asker does unasked; no faucet asks more of anybody
PAY_WAIT = 20                   # seconds a claim waits for the payment before it
ERRORS = ("ADDRESS", "ALREADY", "CHALLENGE", "WORK", "TOO_OFTEN", "EMPTY", "LATER", "NOT_NOW", "INPUT", "FAILED")


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


def solve(challenge, bits, give_up_after=1 << 34, until=None):
    """The asker's side of the work. Returns the answer, or None when it was not found or the time ran out."""
    prefix = (challenge + ":").encode()
    for i in range(give_up_after):
        if until is not None and i % 65536 == 0 and time.time() > until:
            return None
        answer = "%x" % i
        if leading_zero_bits(hashlib.sha256(prefix + answer.encode()).digest()) >= bits:
            return answer
    return None


def _say(text):
    sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))


class Faucet:
    def __init__(self, wallet, amount, base_bits=BASE_BITS, clock=time.time, per_source=20, per_source_paid=2, per_hour=None):
        self.wallet, self.amount, self.base_bits, self.clock = wallet, amount, base_bits, clock
        self.per_source, self.per_source_paid, self.per_hour = per_source, per_source_paid, per_hour
        self.secret, self.salt = os.urandom(32), os.urandom(16)
        self.lock = threading.Lock()            # the books below; never held while the wallet server is asked
        self.paying = threading.Lock()          # one payment at a time
        self.book = os.path.join(wallet.folder, "faucet.jsonl")
        self.paid = {}                          # address -> when it was last paid
        self.pending = {}                       # address -> the place that asked, while its payment is made
        self.recent = collections.deque(maxlen=1 << 17)   # times of the challenges handed out in the last hour
        self.hour = collections.deque()         # times of the payments of the last hour
        self.sources = {}                       # salted hash of a place -> times of its requests in the last hour
        self.source_paid = {}                   # salted hash of a place -> times it was paid in the last day
        self.used = {}                          # challenges that reached the wallet -> when they run out
        self.empty_until = self.dry_until = self.trouble_until = self.cleaned = 0
        self.troubles = self.dries = 0
        now, skipped = clock(), 0
        try:
            with open(self.book) as f:
                for text in f:
                    try:
                        e = json.loads(text)
                        if e.get("state") != "kept":
                            self._close(e["address"].lower(), e["t"] + 0)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        skipped += 1            # a line cut short by a crash; the wallet's journal knows the payment
        except FileNotFoundError:
            pass
        if skipped:
            _say("faucet.jsonl: %d lines could not be read and were passed over" % skipped)
        # The wallet's journal is written before a payment leaves; the book above only after. What the journal
        # paid as a faucet counts as paid, so a crash between the two does not pay an address twice.
        recent = []
        for p in wallet._journal().values():
            if p.get("memo") == "faucet" and p["state"] not in ("kept", "forgotten", "overtaken"):
                for a in p.get("to") or ():
                    if isinstance(a, str):
                        self._close(a.lower(), p["t"])
                if now - p["t"] < 3600:
                    recent.append(p["t"])
        self.hour.extend(sorted(recent))

    def _close(self, address, t):
        self.paid[address] = max(self.paid.get(address, 0), t)

    # The more claims whose work was done in the last hour, the more work: one bit more for every doubling beyond
    # eight. A challenge that nobody answered raises nothing for others; it raises the work of the place that
    # fetched it (own). And a faucet never asks more than this program's own asker does unasked, unless its
    # owner set the quiet work higher than that.
    def bits(self, own=0):
        now = self.clock()
        while self.recent and now - self.recent[0] >= 3600:
            self.recent.popleft()
        most = min(MAX_BITS, max(self.base_bits, ASK_BITS))
        return min(most, self.base_bits + max(0, (len(self.recent) // 8).bit_length()) + max(0, own))

    def _clean(self, now):
        self.cleaned = now
        self.sources = {k: v for k, v in self.sources.items() if v and now - v[-1] < 3600}
        self.source_paid = {k: [t for t in v if now - t < PERIOD] for k, v in self.source_paid.items()}
        self.source_paid = {k: v for k, v in self.source_paid.items() if v}
        self.used = {c: t for c, t in self.used.items() if t >= now}
        self.paid = {a: t for a, t in self.paid.items() if now - t < PERIOD}

    def _key(self, source):
        # one place is one IPv4 address or one IPv6 /48: a customer of a provider often has a /56 or a /48 to himself
        source = str(source).strip()[:100]
        try:
            ip = ipaddress.ip_address(source)
            if ip.version == 6:
                ip = ip.ipv4_mapped or ipaddress.ip_address(int(ip) >> 80 << 80)
            source = str(ip)
        except ValueError:
            pass
        return hashlib.sha256(self.salt + source.encode()).hexdigest()[:16]

    def _source(self, source):
        """Counts a request of this place and returns the place's key and its requests of the last hour, or refuses."""
        now = self.clock()
        if now - self.cleaned >= 60:
            self._clean(now)
        key = self._key(source)
        times = self.sources.get(key)
        if times is None:
            if len(self.sources) >= MAX_SOURCES:
                raise Refused("LATER", "the faucet is asked from too many places at once; try again in an hour")
            times = self.sources[key] = collections.deque()
        while times and now - times[0] >= 3600:
            times.popleft()
        if len(times) >= self.per_source:
            raise Refused("TOO_OFTEN", "too many requests from where you are; come back in an hour")
        times.append(now)
        return key, len(times)

    def _sign(self, address, bits, expires, nonce):
        return hmac.new(self.secret, ("%s|%d|%d|%s" % (address, bits, expires, nonce)).encode(), hashlib.sha256).hexdigest()[:32]

    def _address(self, address):
        if not isinstance(address, str) or len(address) > 100:
            raise Refused("ADDRESS", "name the address to be paid")
        try:
            keys.script_for_address(self.wallet.chain, address)
        except BadInput as e:
            raise Refused("ADDRESS", "this is not an address of this network: %s" % e)
        return address.lower()      # the same address in capitals is the same address

    def _may_pay(self, key, address, now):
        """What the faucet knows without asking anybody. Asked before a challenge is given, so that nobody works
        for nothing, and again before a payment."""
        if now < self.trouble_until:
            raise Refused("NOT_NOW", "the faucet cannot pay just now; try again in a while")
        if now - self.paid.get(address, -PERIOD) < PERIOD or address in self.pending:
            raise Refused("ALREADY", "this address was paid within the last day")
        if now < self.empty_until:
            raise Refused("EMPTY", "the faucet has given what it may give in a day; come back tomorrow")
        if now < self.dry_until:
            raise Refused("LATER", "the faucet has no coin free just now; try again in a few minutes")
        while self.hour and now - self.hour[0] >= 3600:
            self.hour.popleft()
        if self.per_hour is not None and len(self.hour) + len(self.pending) >= self.per_hour:
            raise Refused("LATER", "the faucet has given what it gives in an hour; try again in an hour")
        got = sum(1 for t in self.source_paid.get(key, ()) if now - t < PERIOD) + sum(1 for k in self.pending.values() if k == key)
        if got >= self.per_source_paid:
            raise Refused("TOO_OFTEN", "the faucet gave to where you are %d times within a day, and that is all it gives to one place" % got)

    def _refusal(self, kind, now):
        """What the asker is told when the wallet would not pay, and what the faucet remembers of it."""
        if kind == "OVER_DAY":
            self.empty_until = now + 600        # the wallet is asked again in ten minutes, not at every claim
            return Refused("EMPTY", "the faucet has given what it may give in a day; come back tomorrow")
        if kind in ("NOT_ENOUGH", "NOTHING_TO_SPEND", "OPEN_PAYMENT"):
            # its coins wait for a block, or a payment the network did not take stands in the way until the next block
            self.dry_until = now + 120
            self.dries += 1
            if self.dries >= 5:                 # this often in a row it is not a block's wait: the wallet is empty or stuck
                self.dry_until = now + 600
                _say("ALARM the faucet had no coin to pay with %d times in a row (%s); its owner has to look"
                     % (self.dries, re.sub(r"[^A-Z_]", "", str(kind))[:30]))
            return Refused("LATER", "the faucet has no coin free just now; try again in a few minutes")
        self.trouble_until = now + 600
        _say("ALARM the wallet refuses to pay (%s); the faucet's owner has to look" % re.sub(r"[^A-Z_]", "", str(kind))[:30])
        return Refused("NOT_NOW", "the faucet is out of order just now; its owner has to look")

    def _trouble(self, now, what):
        self.troubles += 1
        if self.troubles >= 2:
            _say("ALARM %s, %d times in a row" % (what, self.troubles))
        if self.troubles >= 3:
            self.trouble_until = now + 600

    def _write(self, entry):
        fd = os.open(self.book, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def challenge(self, source, address):
        with self.lock:
            key, asked = self._source(source)
            address = self._address(address)
            now = self.clock()
            self._may_pay(key, address, now)
            # a place's first questions get the common work; more of them within the hour cost that place more
            bits, expires, nonce = self.bits(asked - 4), int(now) + CHALLENGE_LIFE, os.urandom(8).hex()
            return {"challenge": "%d.%d.%s.%s" % (bits, expires, nonce, self._sign(address, bits, expires, nonce)), "bits": bits, "expires": expires,
                    "work": "find an answer of up to 32 hex characters so that SHA-256(challenge + ':' + answer) begins with that many zero bits"}

    def claim(self, source, address, challenge, answer):
        with self.lock:
            key, _ = self._source(source)
            address = self._address(address)
            try:
                if len(challenge) > 200:
                    raise ValueError
                bits, expires, nonce, mark = challenge.split(".")
                bits, expires = int(bits), int(expires)
                # one challenge has one spelling: "+20." or "020." is not the challenge that was given
                if challenge != "%d.%d.%s.%s" % (bits, expires, nonce, mark) or not mark.isascii():
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise Refused("CHALLENGE", "this is not a challenge of this faucet")
            if not hmac.compare_digest(mark, self._sign(address, bits, expires, nonce)):
                raise Refused("CHALLENGE", "this challenge was not given for this address, or not by this faucet since it last started")
            now = self.clock()
            if now > expires:
                raise Refused("CHALLENGE", "this challenge is too old; ask for a new one")
            if not isinstance(answer, str) or not ANSWER.fullmatch(answer) or not work_done(challenge, answer, bits):
                raise Refused("WORK", "the answer does not do the work asked for")
            if challenge in self.used:
                raise Refused("ALREADY", "this challenge was answered before")
            self.recent.append(now)                     # work was done: that is what raises the work for all
            self._may_pay(key, address, now)
            # From here on the challenge is used and the address is taken, before the wallet is asked: a claim
            # that the wallet refuses cannot be sent again and again, and two claims at once pay once.
            self.used[challenge] = expires
            self.pending[address] = key
        if not self.paying.acquire(timeout=PAY_WAIT):
            with self.lock:
                self.pending.pop(address, None)
                self.used.pop(challenge, None)          # it never reached the wallet
            raise Refused("LATER", "the faucet is busy; try again in a minute")
        try:
            try:
                done = self.wallet.pay([(address, self.amount)], memo="faucet")
            except Refused as e:                        # refused before anything was signed
                with self.lock:
                    self.pending.pop(address, None)
                    raise self._refusal(e.kind, now)
            except ServerError:                         # the wallet server did not answer; nothing was signed
                with self.lock:
                    self.pending.pop(address, None)
                    self.used.pop(challenge, None)
                    self.trouble_until = now + 60       # and it is not asked again for a minute
                raise
            except BaseException:
                # anything else may have happened after the payment was signed and shown: the address stays closed
                with self.lock:
                    self.pending.pop(address, None)
                    self.paid[address] = now
                    self.hour.append(now)
                    self.source_paid.setdefault(key, []).append(now)
                raise
        finally:
            self.paying.release()
        state = done["state"]
        with self.lock:
            self.pending.pop(address, None)
            if state == "kept":
                self._trouble(now, "the network did not take a payment")
            else:
                self.paid[address] = now
                self.hour.append(now)
                self.source_paid.setdefault(key, []).append(now)
                if state == "handed":
                    self.troubles = self.dries = 0
                else:
                    self._trouble(now, "the outcome of a payment is not known")
            self._write({"t": int(now), "address": address, "txid": done["id"], "state": state})
        if state == "handed":
            return {"paid": north(self.amount), "transaction_id": done["id"]}
        if state == "kept":
            raise Refused("NOT_NOW", "the network did not take the faucet's payment, so nothing was paid; try again later")
        raise Refused("NOT_NOW", "the payment was made but its outcome is not known yet; do not ask again for this address today")

    def about(self):
        with self.lock:
            bits = self.bits()
        return {"service": "polarka-faucet", "network": self.wallet.chain, "gives": north(self.amount) + " NORTH to one address once a day",
                "how": ["POST /v1/challenge {\"address\": yours}", "do the work it names", "POST /v1/claim {\"address\", \"challenge\", \"answer\"}"],
                "bits_now": bits, "to_one_place_a_day": self.per_source_paid,
                "keeps": "the addresses it paid; no internet addresses on disk, no names",
                "note": "NORTH has no market price. This faucet is run by a community member who mines and holds NORTH."}


class Server(ThreadingHTTPServer):
    """Answers at most so many requests at one time and says nothing of who asked when a connection breaks."""
    daemon_threads = True

    def __init__(self, address, handler, most=MAX_THREADS):
        super().__init__(address, handler)
        self.slots = threading.BoundedSemaphore(most)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            return self.shutdown_request(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        _say("connection failed %s" % getattr(sys.exc_info()[0], "__name__", "error"))


def make_handler(faucet, trust_proxy):
    class H(OneRequestWithBody, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "polarka-faucet"
        sys_version = ""
        timeout = SOCKET_TIMEOUT        # one read that takes longer than this ends the request

        def setup(self):
            super().setup()
            # and the whole request has REQUEST_TIME to arrive: one byte every few seconds holds no thread for long
            self._late = threading.Timer(REQUEST_TIME, self._drop)
            self._late.daemon = True
            self._late.start()

        def _drop(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self._late.cancel()
            super().finish()

        def source(self):
            if not trust_proxy:
                return self.client_address[0]
            # the web server in front writes last: the last element of the last line is its word, the rest the asker's
            lines = self.headers.get_all("X-Forwarded-For") or [""]
            last = lines[-1].split(",")[-1].strip()
            if not last:
                raise Refused("PROXY", "no X-Forwarded-For")
            return last

        def send(self, code, obj):
            body = json.dumps(obj).encode()
            self.close_connection = True        # one request a connection: an idle one holds no thread
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._late.cancel()                 # it has arrived
            if self.path != "/":
                return self.send(404, {"error": "INPUT", "message": "there is /, /v1/challenge and /v1/claim"})
            self.send(200, faucet.about())

        def do_POST(self):
            t0, what, outcome = time.time(), "other", "ok"
            try:
                length = self.headers.get("Content-Length", "")
                if not re.fullmatch(r"[0-9]{1,6}", length) or not 2 <= int(length) <= MAX_BODY:
                    outcome = "INPUT"
                    return self.send(413, {"error": "INPUT", "message": "request size"})
                raw = self.rfile.read(int(length))
                self._late.cancel()             # it has arrived; a payment may take longer than a request may
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                if type(body) is not dict:
                    outcome = "INPUT"
                    return self.send(400, {"error": "INPUT", "message": "send a JSON object"})
                if self.path == "/v1/challenge":
                    what = "challenge"
                    self.send(200, faucet.challenge(self.source(), body.get("address")))
                elif self.path == "/v1/claim":
                    what = "claim"
                    self.send(200, faucet.claim(self.source(), body.get("address"), body.get("challenge"), body.get("answer")))
                else:
                    outcome = "INPUT"
                    self.send(404, {"error": "INPUT", "message": "there is /v1/challenge and /v1/claim"})
            except Refused as e:
                outcome = e.kind
                if e.kind == "PROXY":
                    self.send(503, {"error": "NOT_NOW", "message": "the faucet stands behind a web server that did not say where the request came from"})
                else:
                    self.send({"TOO_OFTEN": 429, "EMPTY": 429, "LATER": 429, "NOT_NOW": 503}.get(e.kind, 400), {"error": e.kind, "message": str(e)})
            except ServerError:
                outcome = "SERVER"
                self.send(503, {"error": "NOT_NOW", "message": "the faucet cannot reach its wallet server just now"})
            except Exception as e:
                outcome = "failed %s" % type(e).__name__
                try:
                    self.send(500, {"error": "FAILED", "message": "faucet error"})
                except Exception:
                    pass
            finally:
                _say("%s %s %.0f ms" % (what, outcome, (time.time() - t0) * 1000))

        def log_message(self, *a):
            pass
    return H


def serve(wallet, amount, listen, trust_proxy=False, base_bits=BASE_BITS, per_source_paid=2):
    limits = wallet.limits()
    if limits is None:
        raise Refused("NO_LIMITS", "the faucet's wallet has no limits.json; its owner writes what it may give in a day")
    room = (limits.get("fee_rate") or 2) * 2000         # the fee of a payment made of a dozen coins, with room to spare
    if amount <= 0 or amount + room > limits["per_payment"]:
        # a payment counts with its fee, so a limit equal to the amount, or a hair above it, would let nothing through
        raise Refused("LIMITS", "the amount given (%s NORTH) and its fee have to fit under per_payment_north in limits.json (%s); "
                      "leave at least %s NORTH above the amount" % (north(amount), north(limits["per_payment"]), north(room)))
    os.umask(0o077)                 # what the faucet writes is its owner's to read
    # the day's purse is not to be had in one burst: at most a twelfth of it in an hour
    per_hour = max(1, int(limits["per_day"] // amount) // 12)
    faucet = Faucet(wallet, amount, base_bits, per_source_paid=per_source_paid, per_hour=per_hour)
    host, port = listen.rsplit(":", 1)
    srv = Server((host, int(port)), make_handler(faucet, trust_proxy))
    _say("faucet on %s, %s NORTH to an address a day, at most %d payments an hour and %d a day to one place, work from %d bits"
         % (listen, north(amount), per_hour, per_source_paid, base_bits))
    srv.serve_forever()


def ask(url, address, timeout=30, most_bits=MAX_BITS):
    """The asker's side: challenge, work, claim. Returns what the faucet answered, as a dict. No more work
    is done than most_bits, no redirect is followed (the address asked is the one that answers), and an answer
    that trickles in is not waited for without end."""
    import http.client
    import urllib.error
    import urllib.request

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    def post(path, obj):
        # the whole exchange has a deadline of its own, whatever comes slowly: the header as well as the body
        box = {}

        def run():
            try:
                box["answer"] = fetch(path, obj)
            except BaseException as e:
                box["error"] = e
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(2 * timeout + 1)
        if worker.is_alive():
            raise Refused("FAUCET", "the faucet did not answer in time")
        if "error" in box:
            raise box["error"]
        return box["answer"]

    def fetch(path, obj):
        req = urllib.request.Request(url.rstrip("/") + path, json.dumps(obj).encode(), {"Content-Type": "application/json"})
        end, text = time.time() + 2 * timeout, b""
        try:
            try:
                r = urllib.request.build_opener(Stay).open(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                r = e
            with r:
                read = getattr(r, "read1", None) or r.read
                while len(text) < 65536:
                    if time.time() > end:
                        raise OSError("slow")
                    piece = read(4096)
                    if not piece:
                        break
                    text += piece
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise Refused("FAUCET", "the faucet cannot be reached (%s)" % type(e).__name__)
        try:
            answer = json.loads(text.decode("utf-8"))
        except ValueError:
            answer = None
        if type(answer) is not dict:
            raise Refused("FAUCET", "the faucet sent something that is not an answer")
        if "error" in answer:
            # only words this program knows are passed on: what a faucet says is not to steer whoever reads it
            raise Refused("FAUCET", "the faucet said no (%s)" % (answer["error"] if answer["error"] in ERRORS else "OTHER"))
        return answer

    if not reachable(url):
        raise Refused("FAUCET", "a faucet is reached over https")
    c = post("/v1/challenge", {"address": address})
    bits, challenge = c.get("bits"), c.get("challenge")
    if type(bits) is not int or not 0 <= bits <= min(most_bits, MAX_BITS) or not isinstance(challenge, str) or len(challenge) > 200:
        raise Refused("FAUCET", "the faucet asks for work this program does not do")
    until = c.get("expires")
    if type(until) is not int or until <= time.time():
        raise Refused("FAUCET", "the faucet gave a challenge that has run out already")
    answer = solve(challenge, bits, 1 << (bits + 4), until)     # sixteen times the work expected or the challenge's time, then no more
    if answer is None:
        raise Refused("FAUCET", "the work was not done")
    got = post("/v1/claim", {"address": address, "challenge": challenge, "answer": answer})
    txid = got.get("transaction_id")
    if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-f]{64}", txid):
        raise Refused("FAUCET", "the faucet did not name a transaction")
    return {"asked": address, "transaction_id": txid, "work_bits": bits,
            "then": "this is the faucet's word; the coin shows in the balance once it is in a block"}
