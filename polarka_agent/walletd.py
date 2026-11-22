#!/usr/bin/env python3
"""walletd: a Northbound wallet server (API version 1) in front of one's own TrueNorth Core node.

It takes the requests of the server of github.com/nova9/northbound and answers in the same shapes, from the
blocks and waiting transactions of a node that the same people run. It was written from that server's
sources (tag v0.1.0-rc.5 and the commits up to 07523b6 of 15 November 2026). None of that code is used or
run here, and where this program and the real server differ the real server is right.

What it is for: a pool or a faucet pays through a wallet server, and that server should stand on the payer's
own node, not on somebody else's. Strangers must not reach that instance, or they could take all its
connections while a payout waits; to serve the public as well, run a second instance with a user, a port and
an RPC user of its own (deploy/walletd/README.md). It is not a block explorer.

  python3 -m polarka_agent.walletd --chain main --rpc-url http://127.0.0.1:9554 --cookie /var/lib/truenorth/.cookie
                                   [--listen 127.0.0.1:9560]

It needs nothing but the standard library of Python 3.9 or later, so this one file can be copied on its own.
It prints "listening PORT" when it is ready; until then it has read the whole chain once.

What it does that a test double does not (the tests' stand-in, tests/standin/northbound_standin.py, is this
program with test switches on top, so every test of the wallet and the pool runs this code):
- it listens on a loopback address only, and will not start on any other;
- it starts only when the node runs the chain it was told (--chain);
- it answers wallet data with 503 while the node is catching up, and on main also when the node has no
  peers or has not seen a block for --max-tip-age seconds: a payout must not rest on an old picture;
- a request body is read only with a valid Content-Length of at most 1 MB, a connection that sends nothing
  for --idle seconds is closed, and at most --most-connections connections are served at once;
- of the waiting transactions it remembers only those that still wait.

It keeps no database: every answer is worked out from the node at that moment. The blocks it has read are
kept in memory, which is fine for a chain as small as TrueNorth's was in November 2026 (22 MB of blocks).
"""
import argparse
import base64
import ipaddress
import json
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    # within the package an address is read here, as the wallet reads it (human review, 22 November); run as one
    # file, the node is asked
    from .keys import BadInput as _NotAnAddress, script_for_address as _script_for
except ImportError:
    _NotAnAddress, _script_for = ValueError, None

KEEP_BLOCKS = 200           # blocks kept as read below the tip; a deeper change of branch reads them from the node again


class OneRequestWithBody:
    """The same as polarka_agent/httpguard.py, written out here because walletd runs as one file: a request that
    carries a body ends its connection, so that bytes left unread are never taken for a next request."""

    sys_version = ""

    def parse_request(self):
        self._body_closes = False
        if not super().parse_request():
            return False
        lengths = [v.strip() for v in (self.headers.get_all("Content-Length") or [])]
        # two lengths are read differently by different readers (human review, 22 November)
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1 or (lengths and lengths[0] not in ("", "0")):
            self.close_connection = True
            self._body_closes = True
        return True

    def end_headers(self):
        if getattr(self, "_body_closes", False):
            self._body_closes = False
            self.send_header("Connection", "close")
        super().end_headers()


COIN = 100_000_000
MAX_ADDRESSES = 100
UTXO_PAGE = 1_000                 # coins on a page when the request names no limit
MAX_UTXO_PAGE = 10_000
MAX_CURSOR = 1_024
MAX_BODY = 1_000_000
MAX_HEX = 800_000
MAX_AGE_SECONDS = 30
NO_BLOCK = (1 << 63) - 1          # where the real server sorts a waiting transaction: above every block
NODE_ERROR = "The blockchain node is unavailable or returned an invalid response."
CHANGED = "The index changed; restart pagination."
BAD_JSON = "Request must contain valid JSON with the required fields."
STALE = "index is synchronizing or stale; retry shortly"
NOT_SERVED = "Request could not be served."
GETS = ("/ready", "/v1/chain/tip", "/v1/fees/estimate", "/v2/fees/estimate")
DEFAULT_LISTEN = "127.0.0.1:9560"


class Answer(Exception):
    """An answer that ends the request: an HTTP status and a message, as the real server words it."""

    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


class Refused(Exception):
    """The node answered a call with an error of its own."""

    def __init__(self, code, message):
        super().__init__("%s (%s)" % (message, code))
        self.code = code


class NodeDown(Exception):
    pass


def units(value):
    """An amount as the node prints it (coins, a decimal number) in base units, exactly."""
    scaled = Decimal(str(value)).scaleb(8)
    whole = int(scaled)
    if scaled != whole or whole < 0:
        raise NodeDown("an amount that is not a whole number of base units: %s" % value)
    return whole


class Node:
    def __init__(self, url, cookie, timeout=120):
        self.url = url
        self.cookie = cookie
        self.timeout = timeout

    def call(self, method, *params):
        with open(self.cookie, encoding="utf-8") as f:      # read every time: the node writes a new one whenever it starts
            login = f.read().strip()
        body = json.dumps({"jsonrpc": "1.0", "id": "walletd", "method": method, "params": list(params)}).encode()
        request = urllib.request.Request(self.url, body, {
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(login.encode()).decode(),
        })
        try:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    text = response.read().decode()
            except urllib.error.HTTPError as e:
                text = e.read().decode()
            answer = json.loads(text, parse_float=Decimal)
        except Exception as e:                                # nothing listens, a timeout, no JSON
            raise NodeDown("%s: %s" % (method, e))
        if answer.get("error"):
            raise Refused(answer["error"].get("code"), answer["error"].get("message"))
        return answer["result"]


class Chain:
    """What is in blocks: every output, what spends it, and where each transaction is."""

    def __init__(self):
        self.hashes = []            # the block hash at every height
        self.outputs = {}           # (txid, n) -> (amount, script, height)
        self.where = {}             # txid -> (height, block time, is a block reward)
        self.spends = {}            # (txid, n) -> the transaction that spends it
        self.inputs = {}            # txid -> [(txid, n), ...]
        self.count = {}             # txid -> number of outputs
        self.by_script = {}         # script -> [(txid, n), ...]

    def add(self, block):
        height = len(self.hashes)
        self.hashes.append(block["hash"])
        for tx in block["txs"]:
            txid = tx["txid"]
            self.where[txid] = (height, block["time"], tx["coinbase"])
            self.inputs[txid] = tx["inputs"]
            for spent in tx["inputs"]:
                self.spends[spent] = txid
            self.count[txid] = len(tx["outputs"])
            for n, (amount, script) in enumerate(tx["outputs"]):
                self.outputs[(txid, n)] = (amount, script, height)
                self.by_script.setdefault(script, []).append((txid, n))

    def pop(self, block):
        """Takes the top block off again, when another branch has won: what it made goes, what it spent is unspent."""
        if not self.hashes or self.hashes[-1] != block["hash"]:
            raise ValueError("not the top block")
        height = len(self.hashes) - 1
        for tx in reversed(block["txs"]):
            txid = tx["txid"]
            for n, (amount, script) in enumerate(tx["outputs"]):
                if self.outputs.get((txid, n), (None, None, None))[2] == height:
                    del self.outputs[(txid, n)]
                    keys = self.by_script.get(script)
                    if keys and (txid, n) in keys:
                        keys.remove((txid, n))
                        if not keys:
                            del self.by_script[script]
            for spent in tx["inputs"]:
                if self.spends.get(spent) == txid:
                    del self.spends[spent]
            if self.where.get(txid, (None,))[0] == height:
                del self.where[txid]
                self.inputs.pop(txid, None)
                self.count.pop(txid, None)
        self.hashes.pop()


def is_id(text):
    return isinstance(text, str) and len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text)


HRPS = {"main": "north", "regtest": "rnorth"}
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_checksum_holds(address):
    """True if the text is a Bech32 or Bech32m string with a right checksum (BIP-173, BIP-350). What it encodes is not looked at."""
    text = address.lower()
    if address not in (text, address.upper()) or "1" not in text:
        return False
    hrp, _, data = text.rpartition("1")
    if not hrp or len(data) < 6 or any(c not in CHARSET for c in data):
        return False
    check = 1
    for value in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + [CHARSET.find(c) for c in data]:
        top = check >> 25
        check = (check & 0x1ffffff) << 5 ^ value
        for i, generator in enumerate((0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)):
            if (top >> i) & 1:
                check ^= generator
    return check in (1, 0x2bc830a3)


def parsed(tx):
    """A transaction as the node shows it (getblock 2, getrawtransaction true), reduced to what is needed here."""
    coinbase = any("coinbase" in i for i in tx["vin"])
    inputs = [] if coinbase else [(i["txid"], i["vout"]) for i in tx["vin"]]
    outputs = [(units(o["value"]), o["scriptPubKey"]["hex"]) for o in sorted(tx["vout"], key=lambda o: o["n"])]
    return {"txid": tx["txid"], "coinbase": coinbase, "inputs": inputs, "outputs": outputs}


def whole(value, low, high):
    """True for a whole number in a range. JSON's true and false are not numbers here."""
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def cursor_of(text, what, numbers, ids):
    """
    A cursor as the real server reads it: hexadecimal text of a JSON object. What does not parse into the
    fields it must have is "invalid"; fields that parse and cannot be are "invalid ... fields".
    """
    if len(text) > MAX_CURSOR:
        raise Answer(400, "%s cursor is too long" % what)
    try:
        if len(text) % 2 or any(c not in "0123456789abcdefABCDEF" for c in text):
            raise ValueError("not hexadecimal")
        cursor = json.loads(bytes.fromhex(text).decode())
        typed = isinstance(cursor, dict) and all(whole(cursor[name], low, high) for name, (low, high) in numbers.items()) \
            and all(isinstance(cursor[name], str) for name in ids)
    except Exception:
        typed = False
    if not typed:
        raise Answer(400, "invalid %s cursor" % what)
    return cursor


def read_cursor(text):
    """The cursor of a page of history: two signed numbers of 64 bits, and two ids."""
    span = (-(1 << 63), (1 << 63) - 1)
    cursor = cursor_of(text, "history", {"pending_revision": span, "height": span}, ("txid", "tip_hash"))
    if cursor["height"] < 0 or not is_id(cursor["txid"]) or not is_id(cursor["tip_hash"]):
        raise Answer(400, "invalid history cursor fields")
    return cursor


def read_coin_cursor(text):
    """The cursor of a page of coins: the last coin of the page before, and the moment it was read at."""
    numbers = {"pending_revision": (0, (1 << 64) - 1), "block_height": (0, (1 << 64) - 1), "output_index": (0, (1 << 32) - 1)}
    cursor = cursor_of(text, "UTXO", numbers, ("transaction_id", "tip_hash"))
    if cursor["block_height"] > NO_BLOCK or not is_id(cursor["transaction_id"]) or not is_id(cursor["tip_hash"]):
        raise Answer(400, "invalid UTXO cursor fields")
    return cursor


class Picture:
    """The chain and the waiting transactions at one moment; every answer about addresses is read from one of these."""

    def __init__(self, chain, height, tip_hash, revision, waiting, first_seen, now):
        self.chain = chain
        self.height = height
        self.tip_hash = tip_hash
        self.revision = revision
        self.now = now
        self.w_where = {}           # txid -> when it was first seen waiting
        self.w_outputs = {}         # (txid, n) -> (amount, script)
        self.w_spends = {}          # (txid, n) -> the waiting transaction that spends it
        self.w_inputs = {}
        self.w_count = {}
        self.w_by_script = {}
        for tx in waiting:
            txid = tx["txid"]
            self.w_where[txid] = first_seen.get(txid, now)
            self.w_inputs[txid] = tx["inputs"]
            for spent in tx["inputs"]:
                self.w_spends[spent] = txid
            self.w_count[txid] = len(tx["outputs"])
            for n, (amount, script) in enumerate(tx["outputs"]):
                self.w_outputs[(txid, n)] = (amount, script)
                self.w_by_script.setdefault(script, []).append((txid, n))

    def status(self, ready=True):
        return {
            "ready": ready, "indexed_height": self.height, "indexed_hash": self.tip_hash, "observed_node_height": self.height,
            "observed_node_hash": self.tip_hash, "last_node_contact": self.now, "pending_available": ready,
            "pending_observed_at": self.now, "pending_revision": self.revision, "max_age_seconds": MAX_AGE_SECONDS,
        }

    def output(self, key):
        found = self.chain.outputs.get(key)
        return (found[0], found[1]) if found else self.w_outputs.get(key)

    def coins(self, scripts, cursor, limit, hidden=()):
        """
        Outputs in blocks that nothing spends, not even a waiting transaction, a page at a time: ordered by
        height, id and number, at most [limit] of them, and a cursor when there are more. A cursor holds for
        the block and the picture of the waiting transactions it was made at, and for nothing later.
        """
        after = None
        if cursor is not None:
            after = read_coin_cursor(cursor)
            if after["tip_hash"] != self.tip_hash or after["pending_revision"] != self.revision:
                raise Answer(409, CHANGED)
        rows = []
        for _, script in scripts:
            for key in self.chain.by_script.get(script, ()):
                amount, _, height = self.chain.outputs[key]
                # left out as the node's own set of unspent outputs leaves them out: the first block, data outputs, oversized scripts
                if height == 0 or script.startswith("6a") or len(script) > 20_000:
                    continue
                if key in self.chain.spends or key in self.w_spends or key in hidden:
                    continue
                rows.append((height, key[0], key[1], amount, script))
        rows.sort()
        if after is not None:
            last = (after["block_height"], after["transaction_id"].lower(), after["output_index"])
            rows = [row for row in rows if row[:3] > last]
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            height, txid, n = page[-1][:3]
            last = {"tip_hash": self.tip_hash, "pending_revision": self.revision, "block_height": height,
                    "transaction_id": txid, "output_index": n}
            next_cursor = json.dumps(last, separators=(",", ":")).encode().hex()
        return [{
            "transaction_id": txid, "output_index": n, "amount": amount, "script_pubkey": script, "block_height": height,
            "confirmations": self.height - height + 1, "coinbase": self.chain.where[txid][2],
        } for height, txid, n, amount, script in page], next_cursor

    def used(self, scripts):
        return sorted(address for address, script in scripts if script in self.chain.by_script or script in self.w_by_script)

    def known(self, txid):
        return txid in self.chain.where or txid in self.w_where

    def history(self, scripts, cursor, limit):
        """The transactions that pay to the scripts or spend from them: the waiting ones first, then by height downwards, then by id."""
        mine = {script for _, script in scripts}
        after = None
        if cursor is not None:
            after = read_cursor(cursor)
            if after["tip_hash"] != self.tip_hash or after["pending_revision"] != self.revision:
                raise Answer(409, CHANGED)
        touched = set()
        for script in mine:
            for key in list(self.chain.by_script.get(script, ())) + list(self.w_by_script.get(script, ())):
                touched.add(key[0])
                for spender in (self.chain.spends.get(key), self.w_spends.get(key)):
                    if spender is not None:
                        touched.add(spender)
        rows = []
        for txid in touched:
            if txid in self.chain.where:
                rows.append((self.chain.where[txid][0], txid, self.chain.where[txid][1]))
            if txid in self.w_where:
                rows.append((NO_BLOCK, txid, self.w_where[txid]))
        rows.sort(key=lambda row: (-row[0], row[1]))
        if after is not None:
            rows = [row for row in rows if row[0] < after["height"] or (row[0] == after["height"] and row[1] > after["txid"])]
        page = rows[:limit]
        lines = [self.line(txid, None if height == NO_BLOCK else height, stamp, mine) for height, txid, stamp in page]
        next_cursor = None
        if len(rows) > limit:
            height, txid, _ = page[-1]
            last = {"tip_hash": self.tip_hash, "pending_revision": self.revision, "height": height, "txid": txid}
            next_cursor = json.dumps(last, separators=(",", ":")).encode().hex()
        return lines, next_cursor

    def line(self, txid, height, stamp, mine):
        """
        One transaction summed up against the scripts that were asked about, the way the real server does it:
        what its outputs pay to them less what its inputs take from them, and the fee of the whole transaction,
        whoever paid it. A block reward has no fee.
        """
        in_block = txid in self.chain.where
        count = self.chain.count[txid] if in_block else self.w_count[txid]
        inputs = self.chain.inputs[txid] if in_block else self.w_inputs[txid]
        received = paid_out = paid_in = own_in = 0
        for n in range(count):
            amount, script = self.output((txid, n))
            paid_out += amount
            if script in mine:
                received += amount
        for key in inputs:
            spent = self.output(key)
            if spent is None:
                raise RuntimeError("an input of %s is not among the outputs read" % txid)
            paid_in += spent[0]
            if spent[1] in mine:
                own_in += spent[0]
        reward = in_block and self.chain.where[txid][2]
        return {
            "transaction_id": txid, "net_amount": received - own_in, "network_fee": None if reward else paid_in - paid_out,
            "coinbase": bool(reward),       # the public server has given this since after 07523b6 (seen 20 November 2026)
            "block_height": height, "confirmations": 0 if height is None else self.height - height + 1, "timestamp": stamp,
        }


class Index:
    """Reads the node and remembers the blocks it has read, so that a look costs only what is new."""

    def __init__(self, node):
        self.node = node
        self.blocks = {}            # block hash -> the block, reduced
        self.chain = Chain()
        self.pending = {}           # txid -> a waiting transaction read earlier
        self.first_seen = {}        # txid -> when it was first seen waiting
        self.moment = None          # the tip, the node's count of changes to its waiting transactions, and their ids
        self.revision = 0           # goes up whenever the moment changes; a history cursor holds for one revision

    def block(self, block_hash):
        block = self.blocks.get(block_hash)
        if block is None:
            raw = self.node.call("getblock", block_hash, 2)
            block = {"hash": raw["hash"], "prev": raw.get("previousblockhash"), "time": raw["time"], "txs": [parsed(t) for t in raw["tx"]]}
            self.blocks[block_hash] = block
        return block

    def follow(self, tip_hash):
        """Brings the chain up to the node's tip. When another branch has won, the blocks of the old one come off the
        top one by one and those of the new one go on. The new branch is found by its headers, and its blocks are read
        and added one at a time; only the last KEEP_BLOCKS stay as read, so that the chain is not held twice, not even
        at the first reading (human review, 22 November)."""
        if self.chain.hashes and self.chain.hashes[-1] == tip_hash:
            return
        known = {block_hash: height for height, block_hash in enumerate(self.chain.hashes)}
        fresh = []                                        # the hashes of the new branch, newest first
        cursor = tip_hash
        while cursor is not None and cursor not in known:
            fresh.append(cursor)
            kept = self.blocks.get(cursor)
            cursor = kept["prev"] if kept is not None else self.node.call("getblockheader", cursor).get("previousblockhash")
        if cursor is None:
            self.chain = Chain()                          # nothing in common: everything again
        else:
            while len(self.chain.hashes) - 1 > known[cursor]:
                self.chain.pop(self.block(self.chain.hashes[-1]))
        for n, block_hash in enumerate(reversed(fresh), 1):
            block = self.block(block_hash)
            if self.chain.hashes and block["prev"] != self.chain.hashes[-1]:
                # the node changed branch while it was read: what is on the chain holds, the look begins again
                raise Refused(0, "the node changed branch while it was read")
            self.chain.add(block)
            if n % KEEP_BLOCKS == 0:
                self._prune()
        self._prune()

    def _prune(self):
        near = set(self.chain.hashes[-KEEP_BLOCKS:])
        if len(self.blocks) > len(near):
            self.blocks = {h: b for h, b in self.blocks.items() if h in near}

    def look(self):
        """One picture of the node in which the tip and the waiting transactions belong together."""
        for _ in range(5):
            info = self.node.call("getblockchaininfo")
            waiting = self.node.call("getrawmempool", False, True)
            try:
                self.follow(info["bestblockhash"])
                txs = []
                for txid in waiting["txids"]:
                    if txid not in self.pending:
                        self.pending[txid] = parsed(self.node.call("getrawtransaction", txid, True))
                    txs.append(self.pending[txid])
            except Refused:
                continue                                      # a transaction left the waiting ones while it was being read
            after = self.node.call("getblockchaininfo")
            if after["bestblockhash"] != info["bestblockhash"] or self.node.call("getrawmempool", False, True) != waiting:
                continue
            moment = (info["bestblockhash"], waiting["mempool_sequence"], tuple(sorted(waiting["txids"])))
            if moment != self.moment:
                self.moment = moment
                self.revision += 1
            now = int(time.time())
            for txid in waiting["txids"]:
                self.first_seen.setdefault(txid, now)
            # only what still waits is remembered: a server that runs for months would otherwise keep every
            # transaction it ever saw waiting
            still = set(waiting["txids"])
            self.pending = {txid: tx for txid, tx in self.pending.items() if txid in still}
            self.first_seen = {txid: seen for txid, seen in self.first_seen.items() if txid in still}
            picture = Picture(self.chain, info["blocks"], info["bestblockhash"], self.revision, txs, self.first_seen, now)
            picture.info = info
            return picture
        raise Answer(503, STALE)




class App:
    """The API. One request at a time: every answer comes from one picture of a node that is near."""

    def __init__(self, node, chain=None, max_tip_age=7200):
        self.node = node
        self.index = Index(node)
        self.lock = threading.Lock()
        self.network = chain                # "main" or "regtest", as the node names it; asked of the node when not given
        self.max_tip_age = max_tip_age
        self.posts = {
            "/v1/addresses/activity": self.activity, "/v1/utxos": self.utxos, "/v1/transactions": self.transactions,
            "/v1/transactions/validate": self.validate, "/v1/transactions/broadcast": self.broadcast,
            "/v1/transactions/status": self.status,
        }

    def handle(self, method, path, content_type, raw):
        with self.lock:
            return self.route(method, path, content_type, raw)

    def route(self, method, path, content_type, raw):
        if method == "GET" and path == "/ready":
            picture = self.index.look()
            ready = self.node_ready(picture.info)
            return (200 if ready else 503), picture.status(ready)
        if method == "GET" and path == "/v1/chain/tip":
            return 200, {"height": self.node.call("getblockchaininfo")["blocks"]}
        if method == "GET" and path == "/v1/fees/estimate":
            return 200, {"atomic_units_per_vbyte": -(-self.fee_rate() // 1000), "target_blocks": 6}
        if method == "GET" and path == "/v2/fees/estimate":
            return 200, {"atomic_units_per_kvbyte": self.fee_rate(), "target_blocks": 6}
        if method == "POST" and path in self.posts:
            if (content_type or "").split(";")[0].strip().lower() != "application/json":
                raise Answer(415, "Request must use application/json.")
            try:
                body = json.loads(raw.decode())
            except Exception:
                raise Answer(400, BAD_JSON)
            if not isinstance(body, dict):
                raise Answer(422, BAD_JSON)
            return 200, self.posts[path](body)
        raise Answer(405 if path in self.posts or path in GETS else 404, NOT_SERVED)

    def node_ready(self, info):
        """
        True when what the node shows can be paid from: it is not catching up with the headers it knows. On
        main it must also have a peer and a block younger than max_tip_age seconds; a node cut off from the
        network still believes it is up to date, and a payout read from it would rest on an old picture.
        """
        if info.get("initialblockdownload") or info.get("headers", 0) - info.get("blocks", 0) > 1:
            return False
        if self.network == "main":
            if self.node.call("getconnectioncount") < 1:
                return False
            if time.time() - int(self.node.call("getblockheader", info["bestblockhash"])["time"]) > self.max_tip_age:
                return False
        return True

    def fee_rate(self):
        """Base units for 1,000 virtual bytes: the highest of the node's estimate for six blocks and its two minimums."""
        rates = [units(self.node.call("getmempoolinfo")["mempoolminfee"]), units(self.node.call("getnetworkinfo")["relayfee"])]
        estimate = self.node.call("estimatesmartfee", 6)
        if estimate.get("feerate") is not None:
            rates.append(units(estimate["feerate"]))
        if max(rates) == 0:
            raise NodeDown("the node gave a fee rate of zero")
        return max(rates)

    def scripts(self, body):
        """The addresses of a request as (address, script), each script once. The node itself says what an address is."""
        addresses = body.get("addresses")
        if not isinstance(addresses, list) or not all(isinstance(a, str) for a in addresses):
            raise Answer(422, BAD_JSON)
        if len(addresses) > MAX_ADDRESSES:
            raise Answer(400, "wallet data requests accept at most 100 addresses")
        found, seen = [], set()
        for address in addresses:
            if len(address) > 128:
                raise Answer(400, "address is too long")
            if self.network is None:
                self.network = self.node.call("getblockchaininfo")["chain"]
            if _script_for is not None:
                try:
                    script, shown = _script_for(self.network, address).hex(), address.lower()
                except _NotAnAddress:
                    script = None
            else:
                checked = self.node.call("validateaddress", address)
                script, shown = (checked["scriptPubKey"], checked.get("address", address)) if checked.get("isvalid") else (None, None)
            if script is None:
                # the real server tells a good address of another network from something that is no address
                foreign = bech32_checksum_holds(address) and address.lower().rpartition("1")[0] != HRPS.get(self.network)
                raise Answer(400, "address belongs to a different network" if foreign else "invalid address or checksum")
            if script not in seen:
                seen.add(script)
                found.append((shown, script))
        return found

    def picture(self):
        picture = self.index.look()
        if not self.node_ready(picture.info):
            raise Answer(503, STALE)
        return picture

    def hidden(self):
        """Coins to leave out of /v1/utxos. None here; the tests' stand-in can play a server that hides some."""
        return ()

    def activity(self, body):
        scripts = self.scripts(body)
        picture = self.picture()
        return {"used_addresses": picture.used(scripts), "index": picture.status()}

    def paging(self, body, usual):
        """The cursor and the limit of a request as the real server reads them: a text or nothing, and a whole number of sixteen bits or nothing."""
        cursor, limit = body.get("cursor"), body.get("limit")
        if cursor is not None and not isinstance(cursor, str):
            raise Answer(422, BAD_JSON)
        if limit is None:
            limit = usual
        if not whole(limit, 0, 65535):
            raise Answer(422, BAD_JSON)
        return cursor, limit

    def utxo_page(self):
        return UTXO_PAGE

    def utxos(self, body):
        cursor, limit = self.paging(body, self.utxo_page())
        scripts = self.scripts(body)
        picture = self.picture()
        if not 1 <= limit <= MAX_UTXO_PAGE:
            raise Answer(400, "UTXO limit must be between 1 and 10,000")
        coins, next_cursor = picture.coins(scripts, cursor, limit, self.hidden())
        return {"utxos": coins, "next_cursor": next_cursor, "index": picture.status()}

    def transactions(self, body):
        cursor, limit = self.paging(body, 100)
        scripts = self.scripts(body)
        picture = self.picture()
        if not 1 <= limit <= 200:
            raise Answer(400, "history limit must be between 1 and 200")
        lines, next_cursor = picture.history(scripts, cursor, limit)
        return {"transactions": lines, "next_cursor": next_cursor, "index": picture.status()}

    def signed(self, body):
        text = body.get("transaction_hex")
        if not isinstance(text, str):
            raise Answer(422, BAD_JSON)
        if not text or len(text) > MAX_HEX or len(text) % 2 or any(c not in "0123456789abcdefABCDEF" for c in text):
            raise Answer(400, "invalid RPC data: signed transaction must be valid hexadecimal data of at most 800,000 characters")
        return text

    def validate(self, body):
        text = self.signed(body)
        verdict = self.node.call("testmempoolaccept", [text])
        if not isinstance(verdict, list) or len(verdict) != 1:
            raise NodeDown("testmempoolaccept answered in an unknown form")
        return {"allowed": bool(verdict[0]["allowed"]), "rejection_reason": verdict[0].get("reject-reason")}

    def broadcast(self, body):
        text = self.signed(body)
        self.node.call("getblockchaininfo")
        return {"transaction_id": self.node.call("sendrawtransaction", text)}

    def status(self, body):
        txid = body.get("transaction_id")
        if not isinstance(txid, str):
            raise Answer(422, BAD_JSON)
        if not is_id(txid):
            raise Answer(400, "transaction ID must be 64 hexadecimal characters")
        txid = txid.lower()
        if self.picture().known(txid):
            return {"known": True}
        try:
            self.node.call("getrawtransaction", txid, False)
            return {"known": True}
        except Refused as e:
            if e.code == -5:
                return {"known": False}
            raise


class Handler(OneRequestWithBody, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "walletd"
    sys_version = ""

    def setup(self):
        self.timeout = self.server.idle         # a connection that sends nothing for so long is closed
        super().setup()

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def reply(self, status, body, headers=None):
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        """The body of a POST, read only when its length is given and whole: nothing is read past it or waited for in vain."""
        if self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
            raise Answer(411, "Request must give its length (Content-Length) and no Transfer-Encoding.")
        if len(self.headers.get_all("Content-Length") or []) > 1:
            self.close_connection = True                      # two lengths: which one holds is not ours to guess
            raise Answer(400, NOT_SERVED)
        text = self.headers.get("Content-Length")
        if text is None:
            return b""                                        # nothing: the empty body is not JSON, which is said further on
        if not (text.strip().isascii() and text.strip().isdigit()) or len(text.strip()) > 12:
            self.close_connection = True                      # the body is not read
            raise Answer(400, NOT_SERVED)
        length = int(text)
        if length > MAX_BODY:
            self.close_connection = True                      # the body is not read
            raise Answer(413, "Request exceeds the 1 MB body limit.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            self.close_connection = True
            raise Answer(400, NOT_SERVED)
        return raw

    def serve(self, method):
        started = time.monotonic()
        status = 500
        try:
            raw = self.body() if method == "POST" else b""
            status, answer = self.server.app.handle(method, self.path, self.headers.get("Content-Type"), raw)
            self.reply(status, answer)
        except Answer as answer:
            status = answer.status
            self.reply(answer.status, {"message": answer.message}, answer.headers)
        except (NodeDown, Refused) as e:
            status = 502
            if self.server.verbose or self.server.report:
                sys.stderr.write("node: %s\n" % e)
            self.reply(502, {"message": NODE_ERROR})
        except OSError:                                       # the caller went away or fell silent in the middle
            self.close_connection = True
            return
        except Exception:                                     # a fault of this program: say so loudly, answer like the real server
            sys.stderr.write("walletd fault:\n%s" % traceback.format_exc())
            self.reply(500, {"message": "Could not read wallet data."})
        took = time.monotonic() - started
        if self.server.report and not self.server.verbose and (status >= 500 or took > 5):
            path = self.path[:120].encode("unicode_escape").decode()
            sys.stderr.write("%s %s %d %.1fs\n" % (method, path, status, took))

    def do_GET(self):
        self.serve("GET")

    def do_POST(self):
        self.serve("POST")


class Listener(ThreadingHTTPServer):
    """An HTTP server that takes a set number of connections at once; one more is closed without an answer."""

    daemon_threads = True

    def __init__(self, address, app, most=32, idle=30.0, verbose=False, report=True):
        self.app, self.idle, self.verbose, self.report = app, idle, verbose, report
        self.slots = threading.BoundedSemaphore(most)
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, Handler)

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


def loopback(listen):
    """host and port of --listen, for a loopback address only (127.0.0.1, [::1] and the like); ValueError otherwise."""
    host, colon, port = listen.rpartition(":")
    host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if not colon or not (port.isascii() and port.isdigit()) or not ipaddress.ip_address(host).is_loopback:
        raise ValueError(listen)
    port = int(port)
    if port > 65535:
        raise ValueError(listen)
    return host, port


def main(argv=None):
    parser = argparse.ArgumentParser(prog="walletd", description="A Northbound wallet server (API version 1) in front of one's own TrueNorth Core node.")
    parser.add_argument("--chain", required=True, choices=("main", "regtest"), help="the chain the node must run")
    parser.add_argument("--rpc-url", required=True, help="the node's RPC address, for example http://127.0.0.1:9554")
    parser.add_argument("--cookie", required=True, help="the node's cookie file, or a file with user:password of an RPC user allowed only the calls walletd makes")
    parser.add_argument("--listen", default=DEFAULT_LISTEN, help="a loopback address and port (default %s; port 0 takes a free one)" % DEFAULT_LISTEN)
    parser.add_argument("--max-tip-age", type=int, default=7200, help="on main: seconds without a new block after which wallet data is answered 503 (default 7200)")
    parser.add_argument("--idle", type=float, default=30.0, help="seconds a connection may send nothing before it is closed (default 30)")
    parser.add_argument("--most-connections", type=int, default=32, help="connections served at once (default 32)")
    parser.add_argument("--rpc-timeout", type=float, default=60.0, help="seconds to wait for the node (default 60)")
    parser.add_argument("--verbose", action="store_true", help="write every request to standard error")
    a = parser.parse_args(argv)
    try:
        host, port = loopback(a.listen)
    except ValueError:
        parser.error("--listen must be a loopback address and a port, such as 127.0.0.1:9560 or [::1]:9560")
    if a.most_connections < 1 or a.idle <= 0 or a.rpc_timeout <= 0 or a.max_tip_age < 60:
        parser.error("--most-connections must be at least 1, --idle and --rpc-timeout above 0, --max-tip-age at least 60")
    node = Node(a.rpc_url, a.cookie, a.rpc_timeout)
    try:
        chain = node.call("getblockchaininfo")["chain"]
    except (NodeDown, Refused, OSError) as e:
        sys.stderr.write("walletd: the node does not answer: %s\n" % e)
        return 75
    if chain != a.chain:
        sys.stderr.write("walletd: the node runs %s, not %s; nothing is served\n" % (chain, a.chain))
        return 2
    app = App(node, a.chain, a.max_tip_age)
    started = time.monotonic()
    try:
        picture = app.index.look()                            # the whole chain is read before anybody is answered
        ready = app.node_ready(picture.info)
    except (NodeDown, Refused, Answer) as e:
        sys.stderr.write("walletd: could not read the chain: %s\n" % e)
        return 75
    server = Listener((host, port), app, a.most_connections, a.idle, a.verbose)
    sys.stderr.write("walletd: %s, block %d read in %.0f s, %s; listening on %s port %d\n" % (
        a.chain, picture.height, time.monotonic() - started, "ready" if ready else "NOT READY (the node is catching up or cut off)",
        host, server.server_address[1]))
    print("listening %d" % server.server_address[1], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
