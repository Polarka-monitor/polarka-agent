"""A client for a wallet server that speaks the Northbound API, version 1 (github.com/nova9/northbound).

Such a server stands in front of a TrueNorth Core node and keeps an index of addresses. The wallet names its
addresses to it and gets coins and history back; it hands over signed transactions. No secret goes to the
server. What the server learns is every address it is asked about and where the questions come from.

The rules are those of the Polarka Wallet for Android (ServerClient.kt), which an independent reviewer looked
at: https only, or plain http to this machine or a private address; no redirects; whole numbers that can
exist; ids that look like ids; a largest size for every list and answer; a coin's depth must fit its block;
an index that is not at the tip of its node is not believed; parts of one answer must come from one block;
and no question is waited for without end.

What a server says in words (an error message, a reason) is data. It is cut short and stripped of anything
but plain characters before it is shown, and a program that reads it must not take it for an instruction.
"""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

MAX_ADDRESSES = 100         # the most addresses the server takes in one request
MAX_ADDRESS = 100           # the longest text that can be an address
PAGE = 200                  # lines of history asked for at a time
MAX_UTXOS = 10_000          # the server refuses to list more coins than this for one request
MAX_HEX = 800_000
MAX_CURSOR = 1024
MAX_HEIGHT = 500_000_000
MAX_RATE = 1_000_000
MAX_TIME = 32_503_680_000   # seconds since 1970 up to the year 3000
MAX_AMOUNT = 21_000_000_000 * 100_000_000
MAX_VOUT = 0xFFFFFFFF
MAX_RESTARTS = 2
MAX_ANSWER_BYTES = 8 * 1024 * 1024
MAX_WORDS = 300             # of a server's own words, this many characters are kept

LATE, MOVED, UNREACHABLE = "late", "moved", "unreachable"
_ID = re.compile(r"^[0-9a-f]{64}$")
_HEX = re.compile(r"^(?:[0-9a-f]{2})+$")
_ADDRESS = re.compile(r"^[0-9A-Za-z]{8,%d}$" % MAX_ADDRESS)


class ServerError(Exception):
    """status is the HTTP status (0 when there was no answer, 200 for an answer that cannot be believed);
    kind is LATE, MOVED, UNREACHABLE or None."""

    def __init__(self, status, message, kind=None, said=None):
        super().__init__(message)
        # said: the server's own words, cleaned. They are for the owner's eyes at the command line and are
        # never part of the message, which is what a program or an agent is shown.
        self.status, self.kind, self.said = status, kind, said


# what an address can look like at all: one word of small letters and digits. Anything else is not passed on.
ADDRESS_SHAPE = re.compile(r"^[a-z]{1,10}1[a-z0-9]{6,90}$")

# Why a node did not take a transaction, as one of a few fixed words. The node's own text is free text from
# the server's side and is not passed on to a program.
REASONS = (("ALREADY_KNOWN", ("already",)), ("INPUTS_GONE", ("missingorspent", "missing-inputs", "missing inputs")),
           ("CONFLICT", ("conflict",)), ("FEE_TOO_LOW", ("fee",)), ("DUST", ("dust",)), ("NOT_FINAL", ("non-final", "nonfinal")),
           ("TOO_LONG_CHAIN", ("too-long-mempool-chain", "too many")), ("NOT_STANDARD", ("nonstandard", "non-standard", "scriptpubkey")))


def reason_code(reason):
    low = (reason or "").lower()
    if not low:
        return "NO_REASON"
    for code, marks in REASONS:
        if any(m in low for m in marks):
            return code
    return "OTHER"


def clean(text, most=MAX_WORDS):
    """A server's words, made harmless to show: printable ASCII only, one line, cut short."""
    text = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in str(text))
    return " ".join(text.split())[:most]


def is_private(host):
    """True for this machine and for addresses of private networks (also the shared range 100.64.0.0/10)."""
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))


def _unknown(what):
    return ServerError(200, "the server sent an answer in an unknown form (%s)" % what)


def _whole(obj, name, most, optional=False):
    value = obj.get(name)
    if value is None and optional:
        return None
    if type(value) is not int or value < 0:
        raise _unknown(name)
    if value > most:
        raise ServerError(200, "the server sent a number that cannot exist (%s)" % name)
    return value


def _flag(obj, name):
    value = obj.get(name)
    if type(value) is not bool:
        raise _unknown(name)
    return value


def _text(obj, name, most, optional=False):
    value = obj.get(name)
    if value is None and optional:
        return None
    if type(value) is not str or len(value) > most:
        raise _unknown(name)
    return value


def _list(obj, name, most):
    value = obj.get(name)
    if type(value) is not list:
        raise _unknown(name)
    if len(value) > most:
        raise ServerError(200, "the server sent a list that is too long (%s)" % name)
    return value


def _id(text):
    if type(text) is not str or not _ID.match(text):
        raise ServerError(200, "the server sent something that is not an id")
    return text


class Server:
    def __init__(self, url, timeout=30.0, max_wait=20.0, deadline=90.0, pause=time.sleep, clock=time.monotonic):
        url = url.strip()
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("the server address must start with https:// or http:// and name a host")
        if parts.scheme == "http" and not is_private(parts.hostname):
            raise ValueError("plain http is only allowed to a private address; use https for %s" % parts.hostname)
        if parts.query or parts.fragment or parts.username or parts.password or "@" in parts.netloc:
            raise ValueError("the server address must be an address and nothing else")
        if parts.path not in ("", "/"):
            raise ValueError("the server address must not have a path")
        try:
            self._port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise ValueError("the server address has a port that is not a number")
        self.url = url.rstrip("/")
        self._tls, self._host = parts.scheme == "https", parts.hostname
        self._timeout, self._max_wait, self._deadline = timeout, max_wait, deadline
        self._pause, self._clock = pause, clock
        self._conn = None

    # ------------------------------------------------------------------------------------------ one question

    def close(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _late(self):
        return ServerError(0, "the server did not answer in time", LATE)

    def _once(self, path, body, within):
        """One request and its whole answer: (status, text, seconds the server asks to wait or None)."""
        if within <= 0:
            raise self._late()
        until = self._clock() + within
        for attempt in (0, 1):
            reused = self._conn is not None
            if not reused:
                wait = min(self._timeout, max(0.001, until - self._clock()))
                if self._tls:
                    self._conn = http.client.HTTPSConnection(self._host, self._port, timeout=wait, context=ssl.create_default_context())
                else:
                    self._conn = http.client.HTTPConnection(self._host, self._port, timeout=wait)
            conn, asked = self._conn, False
            try:
                if conn.sock is not None:
                    conn.sock.settimeout(min(self._timeout, max(0.001, until - self._clock())))
                headers = {"Accept": "application/json"}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                conn.request("GET" if body is None else "POST", path, body=None if body is None else body.encode(), headers=headers)
                asked = True
                if conn.sock is not None:
                    conn.sock.settimeout(min(self._timeout, max(0.001, until - self._clock())))
                answer = conn.getresponse()
                data = bytearray()
                while True:
                    left = until - self._clock()
                    if left <= 0:
                        raise socket.timeout()
                    if conn.sock is not None:
                        conn.sock.settimeout(min(self._timeout, left))
                    piece = answer.read1(64 * 1024)
                    if not piece:
                        break
                    data += piece
                    if len(data) > MAX_ANSWER_BYTES:
                        self.close()
                        raise ServerError(200, "the server sent an answer that is too long")
                retry = answer.getheader("Retry-After")
                retry = int(retry.strip()) if retry is not None and retry.strip().isdigit() and len(retry.strip()) < 6 else None
                if answer.will_close:
                    self.close()
                return answer.status, bytes(data).decode("utf-8", "replace"), retry
            except ServerError:
                raise
            except socket.timeout:
                self.close()
                if asked or self._clock() >= until:
                    raise self._late()
                raise ServerError(0, "the server cannot be reached (timeout)", UNREACHABLE)
            except (OSError, http.client.HTTPException) as e:
                self.close()
                if self._clock() >= until:
                    raise self._late()
                # a connection that was kept from the question before may have been closed by the server meanwhile
                if reused and attempt == 0:
                    continue
                raise ServerError(0, "the server cannot be reached (%s)" % type(e).__name__, UNREACHABLE)
        raise ServerError(0, "the server cannot be reached", UNREACHABLE)

    def _call(self, path, body=None, within=None):
        """Asks, waits for a server that says it is busy (max_wait at most), and reads the answer as JSON."""
        until = self._clock() + (self._deadline if within is None else within)
        waited = 0.0
        while True:
            status, text, retry = self._once(path, body, until - self._clock())
            busy = status in (429, 503)
            if not busy or waited >= self._max_wait:
                return self._parsed(status, text)
            seconds = min(5, max(1, retry if retry is not None else (1 if status == 429 else 2))) + waited / 4
            if self._clock() + seconds >= until:
                return self._parsed(status, text)
            self._pause(seconds)
            waited += seconds

    @staticmethod
    def _parsed(status, text):
        def constant(name):
            raise ValueError(name)
        try:
            obj = json.loads(text, parse_constant=constant)
        except (ValueError, RecursionError):
            obj = None
        if type(obj) is not dict:
            obj = None
        if 200 <= status <= 299:
            if obj is None:
                raise ServerError(status, "the server sent an answer that is not JSON (HTTP %d)" % status)
            return obj
        said = obj.get("message") if obj else None
        said = clean(said) if type(said) is str and said.strip() else None
        raise ServerError(status, "the server is busy" if status in (429, 503) else "the server answered HTTP %d" % status, said=said)

    # ------------------------------------------------------------------------------------------ reading an answer

    @staticmethod
    def _moment(answer):
        """The block the answer was read at. An index that is not ready, does not know the waiting transactions,
        or is not at the tip its own node reports, is refused: what it shows may be out of date."""
        index = answer.get("index")
        if type(index) is not dict:
            raise _unknown("index")
        height = _whole(index, "indexed_height", MAX_HEIGHT, True)
        block = _text(index, "indexed_hash", 64, True)
        node_height = _whole(index, "observed_node_height", MAX_HEIGHT, True)
        node_block = _text(index, "observed_node_hash", 64, True)
        if block is not None:
            _id(block)
        if node_block is not None:
            _id(node_block)
        current = _flag(index, "ready") and _flag(index, "pending_available") and height is not None and block is not None \
            and height == node_height and block == node_block
        if not current:
            raise ServerError(503, "the server's index is not up to date")
        return height, block

    @staticmethod
    def _same(before, now):
        if before is not None and before != now:
            raise ServerError(200, "a block arrived between two parts of one question to the server", MOVED)
        return now

    @staticmethod
    def _parts(addresses):
        addresses = list(addresses)
        if not addresses:
            raise ValueError("no address to ask about")
        if not all(type(a) is str and _ADDRESS.match(a) for a in addresses):
            raise ValueError("this is not an address")
        distinct = list(dict.fromkeys(addresses))
        return [distinct[i:i + MAX_ADDRESSES] for i in range(0, len(distinct), MAX_ADDRESSES)]

    @staticmethod
    def _carrying(tx_hex):
        if type(tx_hex) is not str or not 2 <= len(tx_hex) <= MAX_HEX or not _HEX.match(tx_hex):
            raise ValueError("this is not a transaction")
        return json.dumps({"transaction_hex": tx_hex})

    # ------------------------------------------------------------------------------------------ asking

    def tip_height(self):
        """The height of the node behind the server. Good for seeing whether a server answers at all."""
        return _whole(self._call("/v1/chain/tip"), "height", MAX_HEIGHT)

    def activity(self, addresses):
        """(height, block, the addresses that were ever paid to). Any number of addresses; one block."""
        used, at = set(), None
        for part in self._parts(addresses):
            answer = self._call("/v1/addresses/activity", json.dumps({"addresses": part}))
            at = self._same(at, self._moment(answer))
            named = set(part)
            for address in _list(answer, "used_addresses", len(part)):
                if type(address) is not str:
                    raise _unknown("used_addresses")
                if address not in named:
                    raise ServerError(200, "the server named an address it was not asked about")
                used.add(address)
        return at[0], at[1], used

    def utxos(self, addresses):
        """(height, block, coins): the outputs on these addresses that are in a block and that nothing spends,
        not even a transaction that waits for a block. Whose the scripts are is for the caller to check: the
        server is trusted for what exists, not for what belongs to the wallet."""
        coins, seen, at, total = [], set(), None, 0
        for part in self._parts(addresses):
            answer = self._call("/v1/utxos", json.dumps({"addresses": part}))
            now = self._moment(answer)
            at = self._same(at, now)
            for u in _list(answer, "utxos", MAX_UTXOS):
                if type(u) is not dict:
                    raise _unknown("utxos")
                txid, vout = _id(_text(u, "transaction_id", 64)), _whole(u, "output_index", MAX_VOUT)
                if (txid, vout) in seen:
                    raise ServerError(200, "the server listed the same output twice")
                seen.add((txid, vout))
                script = _text(u, "script_pubkey", 200)
                if not _HEX.match(script):
                    raise _unknown("script_pubkey")
                height, deep = _whole(u, "block_height", MAX_HEIGHT), _whole(u, "confirmations", MAX_HEIGHT + 1)
                if height > now[0] or deep != now[0] - height + 1:
                    raise ServerError(200, "the server sent a coin whose depth does not fit its block")
                amount = _whole(u, "amount", MAX_AMOUNT)
                total += amount
                if total > MAX_AMOUNT:
                    raise ServerError(200, "the server sent amounts that cannot exist together")
                coins.append({"txid": txid, "vout": vout, "amount": amount, "script": script, "height": height, "coinbase": _flag(u, "coinbase")})
        return at[0], at[1], coins

    @staticmethod
    def _move(t, tip, named):
        if type(t) is not dict:
            raise _unknown("transactions")
        txid = _id(_text(t, "transaction_id", 64))
        received_at = _text(t, "received_at", MAX_ADDRESS, True)
        if received_at is not None and received_at not in named:
            raise ServerError(200, "the server named an address it was not asked about")
        sent_to = _text(t, "sent_to", MAX_ADDRESS, True)
        if sent_to is not None and not ADDRESS_SHAPE.match(sent_to):
            raise ServerError(200, "the server named a receiver that is not an address")
        height, deep = _whole(t, "block_height", MAX_HEIGHT, True), _whole(t, "confirmations", MAX_HEIGHT + 1)
        if not (deep == 0 if height is None else height <= tip and deep == tip - height + 1):
            raise ServerError(200, "the server sent a transaction whose depth does not fit its block")
        return {"txid": txid, "received_at": received_at, "sent_to": None if sent_to is None else clean(sent_to, MAX_ADDRESS),
                "received": _whole(t, "received_amount", MAX_AMOUNT), "sent": _whole(t, "sent_amount", MAX_AMOUNT),
                "fee": _whole(t, "fee", MAX_AMOUNT, True), "height": height, "confirmations": deep,
                "time": _whole(t, "timestamp", MAX_TIME, True)}

    def history(self, addresses, most=2000):
        """(height, block, lines, complete): the history of at most a hundred addresses, newest first, the
        waiting transactions before those in blocks. Each line is summed up against the addresses of this
        request. When a block or a waiting transaction arrives while it is read, the reading starts again."""
        asked = self._parts(addresses)
        if len(asked) != 1:
            raise ValueError("history is exact for at most %d addresses in one request" % MAX_ADDRESSES)
        if most <= 0:
            raise ValueError("nothing to read")
        named, restarts = set(asked[0]), 0
        while True:
            moves, seen, at, cursor, changed, complete = [], set(), None, None, False, True
            while True:
                body = {"addresses": asked[0], "limit": PAGE}
                if cursor is not None:
                    body["cursor"] = cursor
                try:
                    answer = self._call("/v1/transactions", json.dumps(body))
                except ServerError as e:
                    if e.status != 409:
                        raise
                    changed = True
                    break
                now = self._moment(answer)
                if at is not None and at != now:
                    changed = True
                    break
                at = now
                page = _list(answer, "transactions", PAGE)
                for t in page:
                    line = self._move(t, now[0], named)
                    if line["txid"] in seen:
                        raise ServerError(200, "the server listed the same transaction twice")
                    seen.add(line["txid"])
                    moves.append(line)
                cursor = _text(answer, "next_cursor", MAX_CURSOR, True)
                if cursor is None:
                    break
                if not page:
                    raise _unknown("next_cursor")       # a page with nothing on it and another one after it would never end
                if len(moves) >= most:
                    complete = False
                    break
            if not changed:
                return at[0], at[1], moves, complete
            restarts += 1
            if restarts > MAX_RESTARTS:
                raise ServerError(409, "the server's history kept changing while it was read")

    def validate(self, tx_hex, within=None):
        """Asks whether the node behind the server would take this transaction: (allowed, reason).
        The server sees the whole signed transaction and could keep it, whatever it answers."""
        answer = self._call("/v1/transactions/validate", self._carrying(tx_hex), within)
        reason = _text(answer, "rejection_reason", 2000, True)
        return _flag(answer, "allowed"), None if reason is None else clean(reason)

    def broadcast(self, tx_hex, within=None):
        """Hands a signed transaction over and returns the id the server answers with. Every error of the node
        behind the server comes back as 502, a refusal as well as a node that is down: an error here says
        nothing about what became of the transaction, and known() has to be asked."""
        return _id(_text(self._call("/v1/transactions/broadcast", self._carrying(tx_hex), within), "transaction_id", 64))

    def known(self, txid, within=None):
        """True if the server or its node knows this transaction, in a block or waiting for one."""
        if type(txid) is not str or not _ID.match(txid):
            raise ValueError("this is not a transaction id")
        return _flag(self._call("/v1/transactions/status", json.dumps({"transaction_id": txid}), within), "known")
