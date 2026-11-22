"""A mining pool for TrueNorth that pays for each share.

The pool takes a block template from a node of its own, gives every asker a header of its own (a number in
the coinbase that nobody else gets), checks the proof of work of what comes back, writes down what it owes
for it and hands a share that is also a block to the node. What it owes is paid out in rounds (payout.py).
Cap keeps the pool under a part of the network, Guard bounds what one place may ask and send.

It runs on main since 20 November 2026, open to everybody since 21 November; a programmer read it on 22 November 2026,
a security expert has not yet. The speed it shows (`speed`, GET /v1/stats) is estimated from the shares it took. Miners
that speak stratum (IronMiner) come in through stratum.py, into the same ledger; there the share target follows the
miner's speed. Not here: anything against a miner who keeps back the blocks he finds, and taking back what was
credited for a block that later left the chain.

A share is a header whose hash is at or below the share target, which is `ratio` times the network's target
(2048: a block is worth 2048 shares on average). The pool owes the reward of a block without the fees,
divided by that ratio, less its fee, for every share, whether or not the pool finds blocks.
"""
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .httpguard import OneRequestWithBody
from urllib.parse import parse_qs, urlparse

from . import rx as rxmod
from .keys import BadInput, script_for_address
from .payout import hold_folder, sync

UNITS = 100_000_000
TAG = b"/Polarka/"          # written into the coinbase, so that anybody can count the pool's blocks
RATIO = 2048
TEMPLATE_AGE = 5            # seconds a template is used before the node is asked again
TIP_AGE = 1                 # seconds the node's word about its tip is believed when a share comes in
MOST_CALLS = 200            # askers served at once; the rest are turned away at the door
JOB_AGE = 120               # seconds an asker gets the same header before it is given a fresh one
MOST_JOBS = 50_000          # headers remembered for one tip; beyond that no new ones until the next block
MOST = (1 << 256) - 1
NODE_SILENCE = 30           # seconds without a template after which shares are no longer taken
UNLUCKY = 0.001             # an address whose shares would have found more blocks with this certainty gets no more work
UNLUCKY_POOL = 0.000001     # and the same for all the pool's shares together: then nobody gets work
SPEED_WINDOWS = (600, 3600, 86400)  # seconds over which the pool's speed is shown, from the shares it took


class PoolError(Exception):
    def __init__(self, kind, message, status=400):
        Exception.__init__(self, message)
        self.kind, self.status = kind, status


def at_most(blocks, expected):
    """The chance of finding this few blocks or fewer when `expected` were due (Poisson)."""
    if expected <= 0:
        return 1.0
    log = math.log(expected)
    return min(1.0, sum(math.exp(k * log - expected - math.lgamma(k + 1)) for k in range(blocks + 1)))


def north(value):
    return "%d.%08d" % (value // UNITS, value % UNITS)


def dsha(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def varint(n):
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xffffffff:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def push(data):
    if len(data) >= 76:
        raise ValueError("only short pushes are made here")
    return bytes([len(data)]) + data


def height_push(height):
    """The height as the node's CScript() << int64 writes it (BIP 34)."""
    if height == 0:
        return b"\x00"
    if 1 <= height <= 16:
        return bytes([0x50 + height])
    out = bytearray()
    while height:
        out.append(height & 0xff)
        height >>= 8
    if out[-1] & 0x80:
        out.append(0)
    return push(bytes(out))


def merkle_root(hashes):
    layer = list(hashes)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [dsha(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def coinbase(height, value, pay_script, extranonce, commitment, with_witness, mark=b""):
    """`mark` goes in front of the extranonce: stratum.MARK for a coinbase made for stratum, so that no header made
    there can be one handed out here."""
    sig = height_push(height) + push(mark + extranonce.to_bytes(8, "little")) + push(TAG)
    outs = [value.to_bytes(8, "little") + varint(len(pay_script)) + pay_script]
    if commitment is not None:
        script = bytes([0x6a, 0x24, 0xaa, 0x21, 0xa9, 0xed]) + commitment
        outs.append(bytes(8) + varint(len(script)) + script)
    body = b"\x01" + bytes(32) + b"\xff\xff\xff\xff" + varint(len(sig)) + sig + b"\xff\xff\xff\xff"
    body += varint(len(outs)) + b"".join(outs)
    version, locktime = (1).to_bytes(4, "little"), bytes(4)
    if with_witness and commitment is not None:
        return version + b"\x00\x01" + body + b"\x01\x20" + bytes(32) + locktime
    return version + body + locktime


def commitment_of(template):
    """The witness commitment of a template's transactions (the coinbase's own counts as zeros), or None."""
    if "default_witness_commitment" not in template:
        return None
    wroot = merkle_root([bytes(32)] + [bytes.fromhex(t["hash"])[::-1] for t in template.get("transactions", [])])
    return dsha(wroot + bytes(32))


def assemble(template, pay_script, extranonce, mark=b""):
    """(the 76 bytes of the header before the nonce, the block after the header, the network's target)."""
    txs = template.get("transactions", [])
    commitment = commitment_of(template)
    plain = coinbase(template["height"], template["coinbasevalue"], pay_script, extranonce, commitment, False, mark)
    full = coinbase(template["height"], template["coinbasevalue"], pay_script, extranonce, commitment, True, mark)
    root = merkle_root([dsha(plain)] + [bytes.fromhex(t["txid"])[::-1] for t in txs])
    header = (template["version"].to_bytes(4, "little") + bytes.fromhex(template["previousblockhash"])[::-1] + root
              + template["curtime"].to_bytes(4, "little") + bytes.fromhex(template["bits"])[::-1])
    if len(header) != 76:
        raise ValueError("the template does not make a header")
    rest = varint(1 + len(txs)) + full + b"".join(bytes.fromhex(t["data"]) for t in txs)
    return header, rest, rxmod.target_of(int(template["bits"], 16))


class Node:
    """The pool's own node, over JSON-RPC. The cookie is read anew for every call: it changes when the node
    starts again."""

    def __init__(self, url, cookie=None, userpass=None, timeout=30):
        self.url, self.cookie, self.userpass, self.timeout = url, cookie, userpass, timeout

    def call(self, method, *params):
        if self.cookie:
            with open(self.cookie) as f:
                userpass = f.read().strip()
        else:
            userpass = self.userpass or ""
        body = json.dumps({"jsonrpc": "1.0", "id": "pool", "method": method, "params": list(params)}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Basic " + base64.b64encode(userpass.encode()).decode()})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                answer = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                answer = json.loads(e.read())
            except ValueError:
                raise OSError("the node answered %d" % e.code)
        if answer.get("error"):
            raise OSError("the node refused %s: %s" % (method, str(answer["error"].get("message"))[:200]))
        return answer.get("result")


def depth_by(node, most=20000):
    """A function that says how many blocks deep a transaction is by this node's own chain, or 0. The node needs
    no index for it: the blocks are read from the tip back, each one once, as far as two hours before `since`
    (the time the payment was made), or 300 blocks when no time is given."""
    seen = {}

    def depth(txid, since=None):
        at, steps = node.call("getbestblockhash"), 0
        while at and steps < most:
            block = seen.get(at)
            if block is None:
                got = node.call("getblock", at, 1)
                block = seen[at] = (set(got["tx"]), got.get("previousblockhash"), got["time"])
                if len(seen) > most + 100:
                    seen.pop(next(iter(seen)))
            if txid in block[0]:
                return steps + 1
            if (since is None and steps >= 300) or (since is not None and block[2] < since - 7200):
                break
            at, steps = block[1], steps + 1
        return 0

    depth.unspent = lambda txid, n: node.call("gettxout", txid, n, False) is not None      # by the chain, not the mempool
    depth.height = lambda: node.call("getblockcount")

    return depth


class RxHasher:
    """Checks a header with the real proof of work, the slow way (no 2 GiB table), one at a time."""

    def __init__(self, engine=None):
        self.engine = engine or rxmod.engine()
        self.lock = threading.Lock()
        self.vm, self.generation = None, None

    def hash(self, key, header80):
        with self.lock:
            generation = self.engine.ensure(key)
            if generation != self.generation:
                if self.vm:
                    self.engine.rx.free(self.vm)
                self.vm, self.generation = self.engine.rx.vm(), generation
            return self.engine.rx.hash(self.vm, header80)


def place(source):
    """One place on the internet: an IPv4 address, or the /48 of an IPv6 address (one customer of a provider)."""
    source = str(source).strip()
    if source.startswith("[") and "]" in source:
        source = source[1:source.index("]")]            # [2001:db8::1]:4711
    elif source.count(":") == 1:
        source = source.split(":")[0]                   # 203.0.113.9:4711
    try:
        ip = ipaddress.ip_address(source)
    except ValueError:
        return "?"
    if ip.is_loopback:
        return None         # the pool's own machine, or a proxy that was not named with --trust-proxy: nobody's place
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 6:
        return str(ipaddress.ip_network((int(ip) >> 80 << 80, 48)))
    return str(ip)


class Cap:
    """Keeps the pool, counted together with the mining of whoever runs it, under a part of the network. `stops`
    are (seconds of a window, percent at which no work is handed out, percent below which it is again): by
    default 32 % of the blocks of a day, and 40 % of the blocks of an hour as a stop for a sudden rise (an
    hour is some thirty blocks, and a pool with a quarter of the network has a third of them by chance in
    one hour of five). The pool's blocks are the ones its own ledger says the node took, never the ones that
    merely carry its tag, which anybody can write; the owner's are known by the scripts they pay. A window
    with fewer than `least` blocks says nothing."""

    def __init__(self, node, own_scripts=(), stops=((86400, 32, 30), (3600, 40, 38)), least=10, every=60, clock=time.time, is_ours=None):
        self.node, self.own, self.is_ours = node, {s.hex() for s in own_scripts}, is_ours
        self.stops, self.windows, self.least, self.every, self.clock = stops, [w for w, _, _ in stops], least, every, clock
        self.blocks, self.closed, self.checked, self.last = {}, False, None, {}
        self.lock = threading.Lock()

    def measure(self):
        """{seconds of the window: (blocks of ours, blocks)} from the node, walking back from the tip."""
        now = self.clock()
        horizon = now - max(self.windows)
        at, chain = self.node.call("getbestblockhash"), []
        while at:
            info = self.blocks.get(at)
            if info is None:
                block = self.node.call("getblock", at, 2)
                first = block["tx"][0]
                # the owner's blocks are known by who gets most of the reward; anybody can add an output of nothing
                most = max(first["vout"], key=lambda o: o.get("value", 0))
                ours = (self.is_ours is not None and self.is_ours(at)) or most["scriptPubKey"].get("hex") in self.own
                info = (block["time"], ours, block.get("previousblockhash"))
                if ours or now - block["time"] > 300:
                    self.blocks[at] = info      # "not ours" is kept only for a block old enough for the ledger to know it
            if info[0] < horizon:
                break
            chain.append(info)
            at = info[2]
        self.blocks = {h: i for h, i in self.blocks.items() if i[0] >= horizon - 3600}
        return {w: (sum(1 for t, ours, _ in chain if ours and t >= now - w), sum(1 for t, _, _ in chain if t >= now - w)) for w in self.windows}

    def is_closed(self):
        with self.lock:
            now = self.clock()
            if self.checked is None or now - self.checked >= self.every:
                try:
                    self.last = self.measure()
                except (OSError, KeyError, TypeError, IndexError):
                    return self.closed          # no answer: what was decided last stands
                self.checked = now
                parts = [(100.0 * self.last[w][0] / self.last[w][1], stop, resume) for w, stop, resume in self.stops
                         if self.last[w][1] >= self.least]
                if any(part >= stop for part, stop, _ in parts):
                    self.closed = True
                elif all(part < resume for part, _, resume in parts):
                    self.closed = False
            return self.closed

    def shown(self):
        return {"closed": self.closed, "windows": {str(w): dict(zip(("ours", "blocks"), self.last.get(w, (None, None))),
                                                                stop_percent=stop, resume_percent=resume) for w, stop, resume in self.stops}}


class Guard:
    """Bounds on one place: how often it asks for work, for how many addresses in one block, and how many
    wrong shares it may send before it is not listened to for a while."""

    def __init__(self, work_a_minute=120, addresses_a_block=50, bad_shares=20, ban=600, most_places=100_000, clock=time.time):
        self.work_a_minute, self.addresses_a_block, self.bad_shares, self.ban = work_a_minute, addresses_a_block, bad_shares, ban
        self.most_places, self.clock = most_places, clock
        self.lock = threading.Lock()
        self.asks, self.addresses, self.bad, self.banned, self.tip = {}, {}, {}, {}, None

    def _recent(self, table, key, window, now):
        times = table.get(key)
        if times is None:
            if len(table) >= self.most_places:
                raise PoolError("BUSY", "the pool is asked from too many places at once", 503)
            times = table[key] = collections.deque()
        while times and now - times[0] >= window:
            times.popleft()
        return times

    def check(self, source):
        """Refuses a place that is not listened to at the moment."""
        key, now = None if source is None else place(source), self.clock()
        if key is None:
            return
        with self.lock:
            until = self.banned.get(key)
            if until is not None and now < until:
                raise PoolError("BANNED", "too many wrong shares came from this place; it is listened to again in %d s" % (until - now), 429)
            self.banned.pop(key, None)

    def asked(self, source, address, tip):
        key, now = None if source is None else place(source), self.clock()
        if key is None:
            return
        self.check(source)
        with self.lock:
            if tip != self.tip:
                self.tip, self.addresses = tip, {}
                self.banned = {k: until for k, until in self.banned.items() if until > now}
                self.bad = {k: v for k, v in self.bad.items() if v and now - v[-1] < self.ban}
                if len(self.asks) > self.most_places // 2:
                    self.asks = {k: v for k, v in self.asks.items() if v and now - v[-1] < 60}
            times = self._recent(self.asks, key, 60, now)
            if len(times) >= self.work_a_minute:
                raise PoolError("SLOW", "this place asks for work too often; a header is good for two minutes", 429)
            times.append(now)
            seen = self.addresses.setdefault(key, set())
            if address not in seen and len(seen) >= self.addresses_a_block:
                raise PoolError("SLOW", "this place asked for work for too many addresses in one block", 429)
            seen.add(address)

    def wrong(self, source):
        key, now = None if source is None else place(source), self.clock()
        if key is None:
            return
        with self.lock:
            times = self._recent(self.bad, key, self.ban, now)
            times.append(now)
            if len(times) >= self.bad_shares:
                self.banned[key] = now + self.ban
                times.clear()


class Pool:
    def __init__(self, node, chain, pay_to, folder, ratio=RATIO, fee_percent=5, hasher=None, clock=time.time,
                 cap=None, guard=None, payouts=None, most_owed=None, miners=None):
        if not 1 <= ratio <= 1 << 20 or not 0 <= fee_percent < 100:
            raise ValueError("ratio or fee out of range")
        self.node, self.chain, self.pay_to = node, chain, pay_to
        self.pay_script = script_for_address(chain, pay_to)
        self.ratio, self.fee, self.clock = ratio, fee_percent, clock
        self.ticks = time.monotonic if clock is time.time else clock       # ages, on a clock nobody can set back
        self.tip_seen = None
        self.hasher = hasher or RxHasher()
        self.cap, self.guard, self.payouts = cap, guard, payouts
        self.folder, self.most_owed = folder, most_owed
        # None: anybody may mine. A set of addresses: only these get work (a pool whose wallet may pay only some)
        self.miners = None if miners is None else {a.strip().lower() for a in miners if a.strip()}
        hold_folder(folder)
        self.earned_total, self.paid_total, self.flying_total, self.assets, self.template_good_at = 0, 0, 0, None, None
        self.expected_total, self.found_total, self.last_payout = 0.0, 0, None
        self.cleared = set()        # addresses the owner let back in by hand: luck-cleared.txt, one a line
        if os.path.exists(os.path.join(folder, "luck-cleared.txt")):
            with open(os.path.join(folder, "luck-cleared.txt")) as f:
                self.cleared = {line.strip().lower() for line in f if line.strip()}
        self.secret = self._secret()
        self.ledger_path = os.path.join(folder, "pool-shares.jsonl")
        self.lock = threading.Lock()
        self.ledger_lock = threading.Lock()
        self.template, self.template_at, self.key, self.key_height = None, 0, None, None
        self.jobs, self.job_of, self.seen = {}, {}, set()
        self.accounts, self.blocks, self.taken = {}, [], set()
        self.recent = collections.deque()   # (time, address, hashes or None) of the shares of the last day, for the speed shown
        self.share_target = None            # the share target of the newest job: what one share stands for
        self.stratum = None                 # stratum.Stratum, when the pool also takes miners over stratum
        if cap is not None and cap.is_ours is None:
            cap.is_ours = self.taken.__contains__
        self.boot, self.counter = int.from_bytes(os.urandom(4), "little"), 0
        self._replay()
        if payouts is not None:     # what was paid is known from the ledger at once, not only after the first round
            sums = payouts.sums().values()
            self.paid_total, self.flying_total = sum(p for p, _ in sums), sum(f for _, f in sums)

    def _secret(self):
        path = os.path.join(self.folder, "pool.secret")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(path, "rb") as f:
                secret = f.read()
            if len(secret) != 32:
                raise ValueError("pool.secret is not 32 bytes")
            return secret
        secret = os.urandom(32)
        with os.fdopen(fd, "wb") as f:
            f.write(secret)
        return secret

    def _replay(self):
        """What the pool owes is what the ledger says: every line is read again when the pool starts."""
        if not os.path.exists(self.ledger_path):
            return
        with open(self.ledger_path) as f:
            for number, line in enumerate(f, 1):
                try:
                    entry = json.loads(line)
                    self._count(entry)
                except (ValueError, KeyError, TypeError):
                    raise ValueError("line %d of %s cannot be read; the pool does not start over a damaged ledger"
                                     % (number, self.ledger_path))

    def _count(self, entry):
        account = self.accounts.setdefault(entry["address"], {"shares": 0, "earned": 0, "blocks": 0, "expected": 0.0})
        account["shares"] += 1
        account["earned"] += int(entry["value"])
        account["expected"] += 1.0 / max(int(entry.get("odds", self.ratio)), 1)
        account["blocks"] += 1 if entry.get("accepted") else 0
        self.expected_total += 1.0 / max(int(entry.get("odds", self.ratio)), 1)
        self.found_total += 1 if entry.get("accepted") else 0
        self.earned_total += int(entry["value"])
        self.recent.append((int(entry["t"]), entry["address"], entry.get("work")))
        while self.recent[0][0] < self.recent[-1][0] - SPEED_WINDOWS[-1]:
            self.recent.popleft()
        if entry.get("block"):
            self.blocks.append({"height": entry["height"], "hash": entry["block"], "accepted": entry.get("accepted"),
                                "time": entry["t"]})
            if entry.get("accepted"):
                self.taken.add(entry["block"])

    def _write(self, entry):
        with self.ledger_lock:
            with open(self.ledger_path, "a") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
                sync(f)

    def _fresh_template(self):
        """Called with the lock held. A node that does not answer stops the pool from handing out work."""
        now = self.ticks()
        if self.template is not None and now - self.template_at < TEMPLATE_AGE:
            return self.template
        try:
            template = self.node.call("getblocktemplate", {"rules": ["segwit"]})
            seed = rxmod.seed_height(template["height"])
            if seed != self.key_height:
                self.key = bytes(32) if seed == 0 else bytes.fromhex(self.node.call("getblockhash", seed))[::-1]
                self.key_height = seed
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.template = None
            raise PoolError("PAUSED", "the pool's node does not answer, so no work is handed out (%s)" % type(e).__name__, 503)
        if self.template is None or template["previousblockhash"] != self.template["previousblockhash"]:
            self.jobs, self.job_of, self.seen = {}, {}, set()       # a new tip: what was handed out is stale
        self.template, self.template_at, self.template_good_at = template, now, now
        return template

    def _may_work(self, address):
        """Called with the lock held. The pool pays for shares whether or not blocks come of them, so it stops
        handing out work when it owes more than it may or more than it has, and to an address whose shares
        should long have found a block and did not."""
        owed = self.earned_total - self.paid_total - self.flying_total      # a round on the way has left the wallet already
        if at_most(self.found_total, self.expected_total) < UNLUCKY_POOL:
            raise PoolError("LUCK", "the pool's shares should have found %.1f blocks and found %d; somebody keeps blocks back, "
                                    "and no work is handed out until a person has looked" % (self.expected_total, self.found_total), 503)
        if self.most_owed is not None and owed > self.most_owed:
            raise PoolError("FUNDS", "the pool owes as much as it allows itself to owe; no work until it has paid out", 503)
        if self.assets is not None and owed > self.assets:
            raise PoolError("FUNDS", "the pool owes more than its wallet holds; no work until blocks or coins come in", 503)
        account = self.accounts.get(address)
        if account is not None and address not in self.cleared and at_most(account["blocks"], account["expected"]) < UNLUCKY:
            raise PoolError("LUCK", "the shares of this address should have found %.1f blocks and found %d; it gets no more work "
                                    "until a person has looked" % (account["expected"], account["blocks"]), 403)

    def _mark(self, job_id, address, header):
        return hmac.new(self.secret, ("%s|%s|" % (job_id, address)).encode() + header, hashlib.sha256).hexdigest()[:24]

    def work(self, address, source=None):
        """A header for this address to look for nonces on."""
        try:
            script_for_address(self.chain, address)
        except BadInput as e:
            raise PoolError("ADDRESS", "that is not an address of this network that can be paid: %s" % e)
        address = address.lower()
        if self.miners is not None and address not in self.miners:
            raise PoolError("CLOSED", "this pool takes only its owner's miners for now; it will open to others later", 403)
        if self.cap is not None and self.cap.is_closed():
            raise PoolError("CAP", "the pool has found as large a part of the network's blocks as it allows itself and hands out "
                                   "no work until that part is smaller; mine alone or elsewhere meanwhile", 503)
        with self.lock:
            template = self._fresh_template()
            if self.guard is not None:
                self.guard.asked(source, address, template["previousblockhash"])
            self._may_work(address)
            now = self.ticks()
            job_id = self.job_of.get(address)
            job = self.jobs.get(job_id)
            if job is None or now - job["made"] >= JOB_AGE:
                if len(self.jobs) >= MOST_JOBS:
                    raise PoolError("BUSY", "too many askers for this block; ask again after the next one", 503)
                self.counter += 1
                extranonce = (self.boot << 32) | (self.counter & 0xFFFFFFFF)
                header, _, network = assemble(template, self.pay_script, extranonce)
                share = min(network * self.ratio, MOST)
                self.share_target = share
                fees = sum(int(t.get("fee", 0)) for t in template.get("transactions", []))
                value = (template["coinbasevalue"] - fees) * network // share * (100 - self.fee) // 100
                job_id = os.urandom(8).hex()
                job = {"address": address, "header": header, "template": template, "extranonce": extranonce, "network": network,
                       "share": share, "odds": share // network,
                       "value": value, "height": template["height"], "key": self.key, "made": now,
                       "template_at": self.template_at}
                self.jobs[job_id] = job
                self.job_of[address] = job_id
            return {"job": job_id + "." + self._mark(job_id, address, job["header"]), "header": job["header"].hex(),
                    "share_target": "%064x" % job["share"], "seed": job["key"].hex(), "height": job["height"],
                    "share_value": north(job["value"]), "network": self.chain}

    def share(self, token, nonce, source=None):
        """Takes a nonce for a header the pool handed out. Returns what was written down for it."""
        if self.guard is not None:
            self.guard.check(source)
        try:
            return self._share(token, nonce)
        except PoolError as e:
            if self.guard is not None and e.kind in ("LOW", "INPUT"):
                self.guard.wrong(source)
            raise

    def _check_tip(self):
        """Called with the lock held, when a share comes in: a share for a block that is no longer the tip is worth
        nothing, so the node is asked (at most every TIP_AGE), not the clock."""
        try:
            now = self.ticks()
            if self.template is not None and (self.tip_seen is None or now - self.tip_seen >= TIP_AGE):
                if self.node.call("getbestblockhash") != self.template["previousblockhash"]:
                    self.template_at = now - TEMPLATE_AGE
                    self.jobs, self.job_of, self.seen = {}, {}, set()       # stale at once, whatever the node says next
                self.tip_seen = now
            self._fresh_template()
        except (PoolError, OSError):
            # a share for the tip the pool last knew still counts, but not for long: a pool that cannot see
            # the chain cannot know what its shares are worth
            if self.template_good_at is None or self.ticks() - self.template_good_at > NODE_SILENCE:
                raise PoolError("PAUSED", "the pool's node does not answer, so no shares are taken", 503)

    def _share(self, token, nonce):
        if not isinstance(token, str) or token.count(".") != 1 or type(nonce) is not int or not 0 <= nonce <= 0xFFFFFFFF:
            raise PoolError("INPUT", 'a share is {"job": the job as it was given, "nonce": a number below 2^32}')
        job_id, mark = token.split(".")
        with self.lock:
            self._check_tip()
            job = self.jobs.get(job_id)
            if job is None:
                raise PoolError("STALE", "that job is not for the block being looked for now; ask for work again", 409)
            if not hmac.compare_digest(mark, self._mark(job_id, job["address"], job["header"])):
                raise PoolError("INPUT", "that job was not handed out by this pool")
            if (job_id, nonce) in self.seen:
                raise PoolError("DUPLICATE", "that nonce was sent before", 409)
            self.seen.add((job_id, nonce))
        header = job["header"] + nonce.to_bytes(4, "little")
        proof = int.from_bytes(self.hasher.hash(job["key"], header), "little")
        if proof > job["share"]:
            raise PoolError("LOW", "the hash of that header is above the share target")
        entry = {"t": int(self.clock()), "address": job["address"], "height": job["height"], "job": job_id,
                 "nonce": nonce, "value": job["value"], "odds": job["odds"], "block": None, "accepted": None}
        if proof <= job["network"]:
            entry["block"] = dsha(header)[::-1].hex()
            try:
                rest = assemble(job["template"], self.pay_script, job["extranonce"])[1]
                refused = self.node.call("submitblock", (header + rest).hex())
                entry["accepted"] = refused is None
                if refused is not None:
                    entry["refused"] = str(refused)[:60]
            except OSError as e:
                entry["accepted"], entry["refused"] = False, "the node did not take it: %s" % str(e)[:60]
        self._write(entry)              # first on disk, then in the sums
        with self.lock:
            self._count(entry)
            if entry["accepted"]:
                self.template_at = 0    # the tip has moved: the next asker gets a new template
        return {"ok": True, "credited": north(entry["value"]), "block": entry["block"], "accepted": entry["accepted"]}

    def account(self, address):
        address = str(address).lower()
        with self.lock:
            account = dict(self.accounts.get(address, {"shares": 0, "earned": 0, "blocks": 0}))
        paid, flying = (0, 0) if self.payouts is None else self.payouts.sums().get(address, (0, 0))
        return {"address": address, "shares": account["shares"], "earned": north(account["earned"]), "paid": north(paid),
                "on_the_way": north(flying), "owed": north(account["earned"] - paid - flying),
                "pays_from": None if self.payouts is None else north(self.payouts.minimum)}

    def pay_round(self):
        """One step of paying out (see payout.py). Nothing happens in a pool that was started without payouts."""
        if self.payouts is None:
            return {"round": None, "state": "this pool does not pay out"}
        with self.lock:
            earned = {a: account["earned"] for a, account in self.accounts.items()}
            held = {a for a, account in self.accounts.items()
                    if a not in self.cleared and at_most(account["blocks"], account["expected"]) < UNLUCKY}
        try:
            self.last_payout = self.payouts.run(earned, held)
            if self.payouts.surplus is not None and self.last_payout.get("state") != "refused":
                # what the pool holds beyond what it owes and its reserve may go to its owner; not when a round
                # was refused (by the limits, for coins), so that the surplus does not take what a round needs
                sums = self.payouts.sums().values()
                owed = sum(earned.values()) - sum(paid + flying for paid, flying in sums)
                self.last_payout = dict(self.last_payout, surplus=self.payouts.sweep(owed))
            return self.last_payout
        except Exception as e:
            self.last_payout = {"state": "stopped", "why": "%s: %s" % (type(e).__name__, str(e)[:200])}
            raise
        finally:
            sums = self.payouts.sums().values()
            paid, flying = sum(p for p, _ in sums), sum(f for _, f in sums)
            try:
                held = self.payouts.wallet.balance()
                assets = sum(int(held.get(k, 0)) for k in ("spendable", "set_aside", "unripe"))
            except Exception:
                assets = self.assets        # no answer about the wallet: what was known stands
            with self.lock:
                self.paid_total, self.flying_total, self.assets = paid, flying, assets

    def tip(self):
        with self.lock:
            template = self._fresh_template()
            return {"height": template["height"], "previous": template["previousblockhash"]}

    def speed(self):
        """How fast the pool's miners were, estimated from the shares it took: a share stands for
        2^256 / (share target + 1) hashes on average, at the share target of the newest job. An estimate: over ten
        minutes a few shares more or less move it a lot; an hour or a day says more."""
        now = int(self.clock())
        with self.lock:
            while self.recent and self.recent[0][0] < now - SPEED_WINDOWS[-1]:
                self.recent.popleft()
            recent = list(self.recent)
            target = self.share_target
            if target is None and self.template is not None and self.template.get("bits"):
                target = min(rxmod.target_of(int(self.template["bits"], 16)) * self.ratio, MOST)
        per_share = (1 << 256) / (target + 1) if target else None
        windows = {}
        for window in SPEED_WINDOWS:
            shares = [(a, w) for t, a, w in recent if t > now - window]
            # a share over stratum says itself what it stands for (its difficulty follows the miner); one over
            # HTTP stands for what a share of the newest job does
            plain = sum(1 for _, w in shares if w is None)
            hashrate = None if per_share is None and plain else round((plain * (per_share or 0) + sum(w for _, w in shares if w)) / window)
            windows[str(window)] = {"shares": len(shares), "miners": len({a for a, _ in shares}), "hashrate": hashrate}
        return {"hashes_a_share": None if per_share is None else round(per_share), "windows": windows,
                "how": "shares taken in the window times the hashes a share stands for, divided by the window: an estimate"}

    def stats(self):
        """What listings of pools ask for, in one small answer (GET /v1/stats)."""
        height = None
        try:
            height = self.tip()["height"]       # first: after a restart the template tells what a share stands for
        except PoolError:
            pass
        speed = self.speed()
        hour, day = speed["windows"]["3600"], speed["windows"]["86400"]
        blocks = (self.cap.last.get(86400) or (None, None))[1] if self.cap is not None else None
        chain_speed = None
        if blocks and speed["hashes_a_share"] and self.share_target is not None and self.share_target < MOST:
            chain_speed = round(blocks * speed["hashes_a_share"] * self.ratio / 86400)
        elif blocks and self.template is not None and self.template.get("bits"):
            chain_speed = round(blocks * (1 << 256) / (rxmod.target_of(int(self.template["bits"], 16)) + 1) / 86400)
        with self.lock:
            found = [b for b in self.blocks if b.get("accepted")]
        last = found[-1] if found else None
        return {"coin": "NORTH", "network": self.chain, "algorithm": "RandomX", "t": int(self.clock()),
                "pool": {"hashrate": hour["hashrate"], "miners": hour["miners"], "hashrate_10m": speed["windows"]["600"]["hashrate"],
                         "hashrate_24h": day["hashrate"], "miners_24h": day["miners"], "fee_percent": self.fee, "payout_scheme": "PPS",
                         "min_payout": None if self.payouts is None else north(self.payouts.minimum), "blocks_found": len(found),
                         "last_block": None if last is None else {"height": last["height"], "hash": last["hash"], "time": last["time"]},
                         "stratum": None if self.stratum is None else self.stratum.shown()},
                "chain": {"height": height, "blocks_24h": blocks, "hashrate": chain_speed},
                "how": "hashrates are hashes a second over the last hour unless named otherwise, estimated from the shares the pool "
                       "took; miners are the addresses that sent a share in the window; the chain's hashrate is its blocks of the "
                       "last day times the hashes a block stands for now"}

    def info(self):
        paused, height = False, None
        try:
            height = self.tip()["height"]
        except PoolError:
            paused = True
        speed = self.speed()
        with self.lock:
            shares = sum(a["shares"] for a in self.accounts.values())
            earned = sum(a["earned"] for a in self.accounts.values())
            # what it owes is what was earned and is neither paid nor on the way (it read "earned" before 20 Nov 2026)
            paid, flying = (self.paid_total, self.flying_total) if self.payouts is not None else (0, 0)
        return {"service": "polarka-pool", "network": self.chain, "pays_out": self.payouts is not None, "last_payout": self.last_payout,
                "cap": None if self.cap is None else self.cap.shown(),
                "pays": "for each share, the block reward without fees divided by share_ratio, less the fee",
                "share_ratio": self.ratio, "fee_percent": self.fee, "coinbase_tag": TAG.decode(), "pays_blocks_to": self.pay_to,
                "height": height, "paused": paused, "shares": shares, "earned": north(earned), "paid": north(paid),
                "on_the_way": north(flying), "owed": north(earned - paid - flying), "blocks": len(self.blocks), "speed": speed,
                "how": ["GET /v1/work?address=YOURS", "look for a nonce whose hash is at or below share_target",
                        'POST /v1/share {"job", "nonce"}', "GET /v1/account?address=YOURS"],
                "stratum": None if self.stratum is None else {
                    "port": self.stratum.port, "user": "your address, if you like with .worker after it", "password": "anything",
                    "miner": "IronMiner (truenorth-ironminer) 1.0.8 or later: -o stratum+tcp://HOST:%d -u YOURS -p x" % self.stratum.port}}


def _say(text):
    import sys
    sys.stderr.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    sys.stderr.flush()


def handler_for(pool, trust_proxy=False):
    class Handler(OneRequestWithBody, BaseHTTPRequestHandler):
        server_version = "polarka-pool"
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, *a):
            pass

        def send(self, status, result):
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def source(self):
            if not trust_proxy:
                return self.client_address[0]
            # the web server in front writes last: the last element of the last line is its word
            last = (self.headers.get_all("X-Forwarded-For") or [""])[-1].split(",")[-1].strip()
            if not last:
                raise PoolError("PROXY", "no X-Forwarded-For", 500)
            return last

        def answer(self, make):
            try:
                self.send(200, make())
            except PoolError as e:
                self.send(e.status, {"error": e.kind, "message": str(e)})
            except Exception as e:      # never a traceback to an asker
                _say("fault %s" % type(e).__name__)
                self.send(500, {"error": "INTERNAL", "message": type(e).__name__})

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            address = (query.get("address") or [""])[0]
            if url.path in ("/", "/v1/pool"):
                return self.answer(pool.info)
            if url.path == "/v1/work":
                return self.answer(lambda: pool.work(address, self.source()))
            if url.path == "/v1/tip":
                return self.answer(pool.tip)
            if url.path == "/v1/account":
                return self.answer(lambda: pool.account(address))
            if url.path == "/v1/blocks":
                return self.answer(lambda: {"blocks": pool.blocks[-50:]})
            if url.path == "/v1/stats":
                return self.answer(pool.stats)
            self.send(404, {"error": "INPUT", "message": "there is /v1/pool, /v1/stats, /v1/work, /v1/tip, /v1/share, /v1/account and /v1/blocks"})

        def do_POST(self):
            if urlparse(self.path).path != "/v1/share":
                return self.send(404, {"error": "INPUT", "message": "shares go to /v1/share"})
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= 1024:
                    raise ValueError
                said = json.loads(self.rfile.read(length))
                if type(said) is not dict:
                    raise ValueError
            except ValueError:
                self.close_connection = True
                return self.send(400, {"error": "INPUT", "message": "a share is a small JSON object"})
            self.answer(lambda: pool.share(said.get("job"), said.get("nonce"), self.source()))

    return Handler


class Server(ThreadingHTTPServer):
    """Serves MOST_CALLS askers at once and turns the rest away, so that silent connections cannot use up
    the threads and the open files. In front of the internet it still belongs behind a web server."""
    request_queue_size = 64

    def __init__(self, *a, **k):
        ThreadingHTTPServer.__init__(self, *a, **k)
        self.slots = threading.BoundedSemaphore(MOST_CALLS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            ThreadingHTTPServer.process_request(self, request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            ThreadingHTTPServer.process_request_thread(self, request, client_address)
        finally:
            self.slots.release()


def make_server(pool, listen, trust_proxy=False):
    host, _, port = listen.rpartition(":")
    server = Server((host or "127.0.0.1", int(port)), handler_for(pool, trust_proxy))
    server.daemon_threads = True
    return server


def pay_for_ever(pool, every, sleep=time.sleep):
    while True:
        try:
            done = pool.pay_round()
            _say("payout: %s" % json.dumps(done, sort_keys=True))
        except Exception as e:          # the next step looks at the ledgers again; nothing is assumed from this one
            _say("payout failed: %s: %s" % (type(e).__name__, str(e)[:300]))
        sleep(every)


def serve(pool, listen, trust_proxy=False, pay_every=0, stratum=None, welcome=None):
    server = make_server(pool, listen, trust_proxy)
    if pool.payouts is not None and pay_every > 0:
        threading.Thread(target=pay_for_ever, args=(pool, pay_every), daemon=True).start()
    _say("pool on %s:%d, chain %s, a share is 1/%d of a block, fee %d %%, blocks pay %s"
         % (server.server_address[0], server.server_address[1], pool.chain, pool.ratio, pool.fee, pool.pay_to))
    if stratum:
        from .stratum import Stratum
        _say("stratum on port %d" % Stratum(pool, stratum, welcome=welcome).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
