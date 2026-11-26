"""What every HTTP server of this package shares.

One rule: a request that carries a body ends its connection. Python's BaseHTTPRequestHandler does not drain a body
that a handler did not read (a GET with a body, a POST whose path is refused before it is read, a chunked body that
no handler here decodes). On a kept-alive connection the bytes left over are read as the next request: one that the
web server in front never saw, with headers of the sender's choosing (X-Forwarded-For among them). Review 8 showed
it through a proxy (H1, M1). So whenever a request carries a body, says it does with Transfer-Encoding, or names its
length more than once, the answer says Connection: close and the connection ends after it; requests without a body
keep their connection. It also takes the Python version out of the Server header.

And, since the human review of 22 November, the pieces the services had each written for themselves: a server that
takes so many connections at once (Bounded), the caller's address behind a web server (forwarded), one place on
the internet for it (place: an IPv4 address or an IPv6 network) and a bound on requests a minute (Limiter)."""
import collections
import ipaddress
import threading
import time
from http.server import ThreadingHTTPServer

# How the postage gate asks a service behind it only to look (a proof of a SHA-256 already paid for, which costs
# nothing), never to take anything: GET LOOK_PATH + the path. A service that can look answers with LOOK: known; the
# gate gives nothing for free without it, and drops LOOK from every request that comes from outside and from what it
# passes back. A path that starts with LOOK_PATH is refused when it comes from outside.
LOOK = "X-Polarka-Look"
LOOK_PATH = "/_look"


class OneRequestWithBody:
    """Mix in before BaseHTTPRequestHandler: class H(OneRequestWithBody, BaseHTTPRequestHandler)."""

    sys_version = ""

    def parse_request(self):
        self._body_closes = False
        self._connection_said = False
        if not super().parse_request():
            return False
        lengths = [v.strip() for v in (self.headers.get_all("Content-Length") or [])]
        # two lengths are read differently by different readers (human review, 22 November): such a request ends
        # its connection whatever the first one says
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1 or (lengths and lengths[0] not in ("", "0")):
            self.close_connection = True
            self._body_closes = True
        return True

    def send_header(self, keyword, value):
        if keyword.lower() == "connection":
            self._connection_said = True
        super().send_header(keyword, value)

    def end_headers(self):
        if getattr(self, "_body_closes", False):
            self._body_closes = False
            if not getattr(self, "_connection_said", False):       # a handler that said it already: once is enough
                self.send_header("Connection", "close")
        self._connection_said = False
        super().end_headers()


def one_length(headers):
    """The body's length as a number, or None when it is absent, named twice, or not a plain number of digits."""
    lengths = headers.get_all("Content-Length") or []
    if len(lengths) != 1:
        return None if lengths else 0
    text = lengths[0].strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 12:
        return None
    return int(text)


class Bounded(ThreadingHTTPServer):
    """An HTTP server that takes a set number of connections at once; one more is closed without an answer."""

    daemon_threads = True

    def __init__(self, address, handler, most=64):
        self.slots = threading.BoundedSemaphore(most)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def forwarded(handler):
    """The caller's address as the web server in front wrote it: the last element of the last X-Forwarded-For
    line (what the caller wrote before it counts for nothing), or "" when there is none."""
    lines = handler.headers.get_all("X-Forwarded-For") or []
    return lines[-1].split(",")[-1].strip()[:100] if lines else ""


def place(address, v6_bits=48):
    """One place on the internet: an IPv4 address, or the IPv6 network of `v6_bits` an address is in (a customer of
    a provider often has a /56 or a /48 to himself, a machine a /64). Anything else is kept as text, cut short."""
    address = str(address).strip()
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address[:64]
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network((ip, v6_bits), strict=False))
    return str(ip)


class Limiter:
    """At most `per_minute` requests a minute from one caller, and ten times that from all of them together: a
    bucket each, refilled evenly. A caller may count against a wider place too (the IPv6 /48 its /64 is in), which
    has `wide` times the share of one caller: one sender with many /64 then has no more than that, and cannot empty
    the bucket of all (review 10, M2). A request that is refused takes nothing from any bucket. A caller is forgotten
    a minute after its last request, when its bucket is full again; none is written down. When `most_callers` are
    held all the same, the one heard from longest ago makes room: a newcomer is never turned away because the table
    is full (review 10, M2). `everyone` sizes the bucket of all, a minute (ten times per_minute unless said; 0: none),
    where all callers together are bounded only by what the machine can do, so that a few cannot shut the door for
    the rest (review 11, H4)."""

    def __init__(self, per_minute, clock=time.monotonic, most_callers=10000, wide=2, everyone=None):
        if not 1 <= per_minute <= 600:
            raise ValueError("per_minute is 1 to 600")
        self.per_minute, self.clock, self.most, self.wide = per_minute, clock, most_callers, wide
        self.everyone = per_minute * 10 if everyone is None else everyone
        if self.everyone < 0:
            raise ValueError("everyone is 0 or more")
        self.buckets = collections.OrderedDict()        # caller -> (tokens, when); the one heard from last at the end
        self.all = (float(self.everyone), clock())
        self.swept = clock()
        self.lock = threading.Lock()

    @staticmethod
    def _refill(held, size, now):
        return min(float(size), held[0] + (now - held[1]) * size / 60.0), now

    def take(self, who, wider=None):
        with self.lock:
            now = self.clock()
            if now - self.swept >= 60:
                for key in [k for k, v in self.buckets.items() if now - v[1] >= 60]:
                    del self.buckets[key]               # full again: nothing is lost by forgetting it
                self.swept = now
            asked = [(who, self.per_minute)]
            if wider is not None and wider != who:
                asked.append((("wider", wider), self.per_minute * self.wide))
            held = [(key, size, self._refill(self.buckets.pop(key, (float(size), now)), size, now)) for key, size in asked]
            everyone = self._refill(self.all, self.everyone, now) if self.everyone else (1.0, now)
            ok = everyone[0] >= 1 and all(tokens >= 1 for _, _, (tokens, _) in held)
            for key, size, (tokens, when) in held:
                self.buckets[key] = (tokens - 1 if ok else tokens, when)
            while len(self.buckets) > self.most:
                self.buckets.popitem(last=False)
            if self.everyone:
                self.all = (everyone[0] - 1, now) if ok else everyone
            return ok
