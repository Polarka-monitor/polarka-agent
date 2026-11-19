"""A mining pool for TrueNorth that pays for each share. Stage 1: the work, the shares and their ledger.

What is here: the pool takes a block template from a node of its own, gives every asker a header of its own
(a number in the coinbase that nobody else gets), checks the proof of work of what comes back, writes down
what it owes for it and hands a share that is also a block to the node.

What is not here yet: payouts, the bound on the pool's part of the network, and anything against an asker who
means harm beyond what is said below. It has run on regtest only, and `pool-serve` refuses any other chain.

A share is a header whose hash is at or below the share target, which is `ratio` times the network's target
(2048: a block is worth 2048 shares on average). The pool owes the reward of a block without the fees,
divided by that ratio, less its fee, for every share, whether or not the pool finds blocks.
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import rx as rxmod
from .keys import BadInput, script_for_address

UNITS = 100_000_000
TAG = b"/Polarka/"          # written into the coinbase, so that anybody can count the pool's blocks
RATIO = 2048
TEMPLATE_AGE = 5            # seconds a template is used before the node is asked again
JOB_AGE = 120               # seconds an asker gets the same header before it is given a fresh one
MOST_JOBS = 50_000          # headers remembered for one tip; beyond that no new ones until the next block
MOST = (1 << 256) - 1


class PoolError(Exception):
    def __init__(self, kind, message, status=400):
        Exception.__init__(self, message)
        self.kind, self.status = kind, status


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


def coinbase(height, value, pay_script, extranonce, commitment, with_witness):
    sig = height_push(height) + push(extranonce.to_bytes(8, "little")) + push(TAG)
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


def assemble(template, pay_script, extranonce):
    """(the 76 bytes of the header before the nonce, the block after the header, the network's target)."""
    txs = template.get("transactions", [])
    commitment = None
    if "default_witness_commitment" in template:
        wroot = merkle_root([bytes(32)] + [bytes.fromhex(t["hash"])[::-1] for t in txs])
        commitment = dsha(wroot + bytes(32))
    plain = coinbase(template["height"], template["coinbasevalue"], pay_script, extranonce, commitment, False)
    full = coinbase(template["height"], template["coinbasevalue"], pay_script, extranonce, commitment, True)
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


class Pool:
    def __init__(self, node, chain, pay_to, folder, ratio=RATIO, fee_percent=5, hasher=None, clock=time.time):
        if not 1 <= ratio <= 1 << 20 or not 0 <= fee_percent < 100:
            raise ValueError("ratio or fee out of range")
        self.node, self.chain, self.pay_to = node, chain, pay_to
        self.pay_script = script_for_address(chain, pay_to)
        self.ratio, self.fee, self.clock = ratio, fee_percent, clock
        self.hasher = hasher or RxHasher()
        self.folder = folder
        os.makedirs(folder, mode=0o700, exist_ok=True)
        self.secret = self._secret()
        self.ledger_path = os.path.join(folder, "pool-shares.jsonl")
        self.lock = threading.Lock()
        self.ledger_lock = threading.Lock()
        self.template, self.template_at, self.key, self.key_height = None, 0, None, None
        self.jobs, self.job_of, self.seen = {}, {}, set()
        self.accounts, self.blocks = {}, []
        self.boot, self.counter = int.from_bytes(os.urandom(4), "little"), 0
        self._replay()

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
        account = self.accounts.setdefault(entry["address"], {"shares": 0, "earned": 0})
        account["shares"] += 1
        account["earned"] += int(entry["value"])
        if entry.get("block"):
            self.blocks.append({"height": entry["height"], "hash": entry["block"], "accepted": entry.get("accepted"),
                                "time": entry["t"]})

    def _write(self, entry):
        with self.ledger_lock:
            with open(self.ledger_path, "a") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def _fresh_template(self):
        """Called with the lock held. A node that does not answer stops the pool from handing out work."""
        now = self.clock()
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
        self.template, self.template_at = template, now
        return template

    def _mark(self, job_id, address, header):
        return hmac.new(self.secret, ("%s|%s|" % (job_id, address)).encode() + header, hashlib.sha256).hexdigest()[:24]

    def work(self, address):
        """A header for this address to look for nonces on."""
        try:
            script_for_address(self.chain, address)
        except BadInput as e:
            raise PoolError("ADDRESS", "that is not an address of this network that can be paid: %s" % e)
        address = address.lower()
        with self.lock:
            template = self._fresh_template()
            now = self.clock()
            job_id = self.job_of.get(address)
            job = self.jobs.get(job_id)
            if job is None or now - job["made"] >= JOB_AGE:
                if len(self.jobs) >= MOST_JOBS:
                    raise PoolError("BUSY", "too many askers for this block; ask again after the next one", 503)
                self.counter += 1
                extranonce = (self.boot << 32) | (self.counter & 0xFFFFFFFF)
                header, rest, network = assemble(template, self.pay_script, extranonce)
                share = min(network * self.ratio, MOST)
                fees = sum(int(t.get("fee", 0)) for t in template.get("transactions", []))
                value = (template["coinbasevalue"] - fees) * network // share * (100 - self.fee) // 100
                job_id = os.urandom(8).hex()
                job = {"address": address, "header": header, "rest": rest, "network": network, "share": share,
                       "value": value, "height": template["height"], "key": self.key, "made": now,
                       "template_at": self.template_at}
                self.jobs[job_id] = job
                self.job_of[address] = job_id
            return {"job": job_id + "." + self._mark(job_id, address, job["header"]), "header": job["header"].hex(),
                    "share_target": "%064x" % job["share"], "seed": job["key"].hex(), "height": job["height"],
                    "share_value": north(job["value"]), "network": self.chain}

    def share(self, token, nonce):
        """Takes a nonce for a header the pool handed out. Returns what was written down for it."""
        if not isinstance(token, str) or token.count(".") != 1 or type(nonce) is not int or not 0 <= nonce <= 0xFFFFFFFF:
            raise PoolError("INPUT", 'a share is {"job": the job as it was given, "nonce": a number below 2^32}')
        job_id, mark = token.split(".")
        with self.lock:
            try:
                self._fresh_template()
            except PoolError:
                pass                    # a share for the tip the pool last knew still counts
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
                 "nonce": nonce, "value": job["value"], "block": None, "accepted": None}
        if proof <= job["network"]:
            entry["block"] = dsha(header)[::-1].hex()
            try:
                refused = self.node.call("submitblock", (header + job["rest"]).hex())
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
            account = dict(self.accounts.get(address, {"shares": 0, "earned": 0}))
        return {"address": address, "shares": account["shares"], "earned": north(account["earned"]),
                "paid": north(0), "owed": north(account["earned"]), "note": "stage 1: nothing is paid out yet"}

    def tip(self):
        with self.lock:
            template = self._fresh_template()
            return {"height": template["height"], "previous": template["previousblockhash"]}

    def info(self):
        paused, height = False, None
        try:
            height = self.tip()["height"]
        except PoolError:
            paused = True
        with self.lock:
            shares = sum(a["shares"] for a in self.accounts.values())
            owed = sum(a["earned"] for a in self.accounts.values())
        return {"service": "polarka-pool", "network": self.chain, "stage": "1: shares are written down, nothing is paid out yet",
                "pays": "for each share, the block reward without fees divided by share_ratio, less the fee",
                "share_ratio": self.ratio, "fee_percent": self.fee, "coinbase_tag": TAG.decode(), "pays_blocks_to": self.pay_to,
                "height": height, "paused": paused, "shares": shares, "owed": north(owed), "blocks": len(self.blocks),
                "how": ["GET /v1/work?address=YOURS", "look for a nonce whose hash is at or below share_target",
                        'POST /v1/share {"job", "nonce"}', "GET /v1/account?address=YOURS"]}


def _say(text):
    import sys
    sys.stderr.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    sys.stderr.flush()


def handler_for(pool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "polarka-pool"
        protocol_version = "HTTP/1.1"
        timeout = 30

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
                return self.answer(lambda: pool.work(address))
            if url.path == "/v1/tip":
                return self.answer(pool.tip)
            if url.path == "/v1/account":
                return self.answer(lambda: pool.account(address))
            if url.path == "/v1/blocks":
                return self.answer(lambda: {"blocks": pool.blocks[-50:]})
            self.send(404, {"error": "INPUT", "message": "there is /v1/pool, /v1/work, /v1/tip, /v1/share, /v1/account and /v1/blocks"})

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
            self.answer(lambda: pool.share(said.get("job"), said.get("nonce")))

    return Handler


def make_server(pool, listen):
    host, _, port = listen.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), handler_for(pool))
    server.daemon_threads = True
    return server


def serve(pool, listen):
    server = make_server(pool, listen)
    _say("pool on %s:%d, chain %s, a share is 1/%d of a block, fee %d %%, blocks pay %s"
         % (server.server_address[0], server.server_address[1], pool.chain, pool.ratio, pool.fee, pool.pay_to))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
