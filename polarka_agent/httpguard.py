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
import ipaddress
import threading
import time
from http.server import ThreadingHTTPServer


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
    bucket each, refilled evenly. A caller is forgotten a minute after its last request, when its bucket is full
    again; none is written down."""

    def __init__(self, per_minute, clock=time.monotonic, most_callers=10000):
        if not 1 <= per_minute <= 600:
            raise ValueError("per_minute is 1 to 600")
        self.per_minute, self.clock, self.most = per_minute, clock, most_callers
        self.buckets = {}               # caller -> (tokens, when)
        self.all = (float(per_minute * 10), clock())
        self.swept = clock()
        self.lock = threading.Lock()

    @staticmethod
    def _take(held, rate, size, now):
        tokens = min(float(size), held[0] + (now - held[1]) * rate)
        if tokens < 1:
            return False, (tokens, now)
        return True, (tokens - 1, now)

    def take(self, who):
        with self.lock:
            now, rate = self.clock(), self.per_minute / 60.0
            if now - self.swept >= 60 or (who not in self.buckets and len(self.buckets) >= self.most):
                self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < 60}   # full again: nothing lost
                self.swept = now
                if who not in self.buckets and len(self.buckets) >= self.most:
                    return False
            ok, self.buckets[who] = self._take(self.buckets.get(who, (float(self.per_minute), now)), rate, self.per_minute, now)
            if not ok:
                return False
            ok, self.all = self._take(self.all, rate * 10, self.per_minute * 10, now)
            return ok
