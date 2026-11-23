"""Stamps as a service: many SHA-256s in one transaction, and a proof for each.

Whoever wants to show that a file existed by a given block sends its SHA-256. Every few minutes the service
puts what came in into a tree of hashes and writes the root of the tree into the chain with one stamp of its
wallet (an output that carries "POLARKA\\x01" and the root, the same as `polarka-agent stamp`). Each SHA-256 then
gets its proof: the path from it to the root, the transaction, the block, and the node's proof that the
transaction is in that block. Checking a proof needs no trust in this service: `polarka-agent stamp-verify`.

    GET or POST /v1/stamp/<sha256>   takes the SHA-256 if it is new, and tells where it stands:
                                     waiting (with the time of the next batch), written (in a transaction that
                                     waits for a block), or stamped (with the proof)
    GET /v1/stamp                    what this is

Meant to stand behind the postage gate (`postage-serve --route /v1/stamp=127.0.0.1:8681`), which takes the
payment: the service itself asks nothing. Its wallet pays only the network fee of one transaction a batch.

The tree: leaf = SHA-256(0x00 || digest), node = SHA-256(0x01 || left || right), the leaves sorted, an odd one at
the end of a level goes up as it is. The prefixes keep a leaf from being taken for a node.
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .httpguard import OneRequestWithBody

from .wallet import STAMP, Refused

DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
VERSION = 1
DEEP = 30           # blocks after which a batch is no longer looked at for a reorganization (as many as RECENT)
RECENT = 30         # blocks whose hashes are kept, to find where a reorganization began
DEAD = 3600         # seconds after which a batch whose transaction the node no longer has goes back to waiting


def leaf(digest):
    return hashlib.sha256(b"\x00" + digest).digest()


def node(left, right):
    return hashlib.sha256(b"\x01" + left + right).digest()


def tree(digests):
    """(root, {digest: path}) for sorted unique digests (bytes); a path is a list of (side, hash), side being
    "left" or "right": where the sibling stands."""
    level = [leaf(d) for d in digests]
    paths = {d: [] for d in digests}
    members = [[d] for d in digests]           # which digests sit under each hash of the level
    while len(level) > 1:
        up, up_members = [], []
        for i in range(0, len(level), 2):
            if i + 1 == len(level):
                up.append(level[i])
                up_members.append(members[i])
                continue
            a, b = level[i], level[i + 1]
            for d in members[i]:
                paths[d].append(("right", b))
            for d in members[i + 1]:
                paths[d].append(("left", a))
            up.append(node(a, b))
            up_members.append(members[i] + members[i + 1])
        level, members = up, up_members
    return level[0], paths


def climb(digest, path):
    h = leaf(digest)
    for side, sibling in path:
        h = node(sibling, h) if side == "left" else node(h, sibling)
    return h


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


class Reader:
    def __init__(self, raw):
        self.raw, self.at = raw, 0

    def take(self, n):
        if n < 0 or self.at + n > len(self.raw):
            raise ValueError("cut short")
        part = self.raw[self.at:self.at + n]
        self.at += n
        return part

    def varint(self):
        first = self.take(1)[0]
        if first < 0xfd:
            return first
        return int.from_bytes(self.take({0xfd: 2, 0xfe: 4, 0xff: 8}[first]), "little")


def parse_tx(raw):
    """(txid as hex, [output scripts]) of a serialized transaction; the txid leaves the witness out."""
    r = Reader(raw)
    version = r.take(4)
    segwit = raw[4:6] == b"\x00\x01"
    if segwit:
        r.take(2)
    start = r.at
    n_in = r.varint()
    for _ in range(n_in):
        r.take(36)
        r.take(r.varint())
        r.take(4)
    scripts = []
    for _ in range(r.varint()):
        r.take(8)
        scripts.append(r.take(r.varint()))
    middle = raw[start:r.at]
    if segwit:
        for _ in range(n_in):
            for _ in range(r.varint()):
                r.take(r.varint())
    locktime = r.take(4)
    if r.at != len(raw):
        raise ValueError("bytes after the transaction")
    return dsha(version + middle + locktime)[::-1].hex(), scripts


def parse_txoutproof(raw):
    """(block hash as hex, [txids it proves, as hex]) of a merkle block (gettxoutproof): the partial tree must lead
    to the merkle root of the header, or ValueError."""
    r = Reader(raw)
    header = r.take(80)
    total = int.from_bytes(r.take(4), "little")
    hashes = [r.take(32) for _ in range(r.varint())]
    flags = r.take(r.varint())
    if r.at != len(raw) or not 0 < total < 1 << 24:
        raise ValueError("this is not a merkle block")
    bits = len(flags) * 8           # read one at a time: a list of them would take ~300 MB for 4 MB of flags (human review)
    used = {"bits": 0, "hashes": 0}
    matched = []

    def width(h):
        return (total + (1 << h) - 1) >> h

    def walk(h, pos):
        if used["bits"] >= bits:
            raise ValueError("this is not a merkle block")
        flag = (flags[used["bits"] >> 3] >> (used["bits"] & 7)) & 1
        used["bits"] += 1
        if h == 0 or not flag:
            if used["hashes"] >= len(hashes):
                raise ValueError("this is not a merkle block")
            got = hashes[used["hashes"]]
            used["hashes"] += 1
            if h == 0 and flag:
                matched.append(got)
            return got
        left = walk(h - 1, pos * 2)
        right = walk(h - 1, pos * 2 + 1) if pos * 2 + 1 < width(h - 1) else left
        if right is not left and right == left:
            raise ValueError("this is not a merkle block")
        return dsha(left + right)

    height = 0
    while width(height) > 1:
        height += 1
    if walk(height, 0) != header[36:68] or used["hashes"] != len(hashes):
        raise ValueError("the merkle block does not lead to the root of its header")
    return dsha(header)[::-1].hex(), [m[::-1].hex() for m in matched]


def check(proof):
    """The parts of a proof that need nothing but the proof: the path leads to the root, and the transaction
    carries the root. Raises ValueError with what is wrong."""
    try:
        digest, root = bytes.fromhex(proof["digest"]), bytes.fromhex(proof["root"])
        path = [(p["side"], bytes.fromhex(p["hash"])) for p in proof["path"]]
        tx_hex = proof["transaction"]
    except (KeyError, TypeError, ValueError):
        raise ValueError("this is not a proof of a stamp")
    if len(digest) != 32 or len(root) != 32 or any(side not in ("left", "right") or len(h) != 32 for side, h in path):
        raise ValueError("this is not a proof of a stamp")
    if climb(digest, path) != root:
        raise ValueError("the path does not lead from the SHA-256 to the root")
    try:
        txid, scripts = parse_tx(bytes.fromhex(tx_hex))
        block, proven = parse_txoutproof(bytes.fromhex(proof["txoutproof"]))
    except (KeyError, TypeError, ValueError):
        raise ValueError("the transaction or the proof of its block cannot be read")
    if txid != proof.get("txid"):
        raise ValueError("the transaction is not the one named")
    if b"\x6a" + bytes([len(STAMP) + 32]) + STAMP + root not in scripts:
        raise ValueError("the transaction does not carry the root")
    if txid not in proven or block != proof.get("block_hash"):
        raise ValueError("the proof of the block does not hold this transaction, or names another block")
    return True


class CannotAsk(Exception):
    """The node could not judge a proof now: it does not answer, it is starting, it no longer holds the block. Says
    nothing about the proof (review 12, L1)."""


def verify(proof, node_rpc=None):
    """check(), and with a node (anything with call()): the transaction is the one named, is in the named
    block, and that block is in the node's chain. Returns what was checked. Raises ValueError when the proof does not
    hold, CannotAsk when the node could not say."""
    check(proof)
    done = {"path": True, "root_in_transaction": True, "transaction_in_block": True}
    if node_rpc is None:
        # all of the above can be made up together with a block that was never mined: only the chain says it was
        done["block"] = ("not checked: give a node, or look up block %s at height %s in an explorer"
                         % (proof.get("block_hash"), proof.get("block_height")))
        return done
    from .walletd import NodeDown, Refused as NodeRefused
    try:
        got = node_rpc.call("getrawtransaction", proof["txid"], True, proof["block_hash"])
        if got.get("hex") != proof["transaction"] and got.get("hex", "").lower() != str(proof["transaction"]).lower():
            raise ValueError("the node has another transaction under this id")
        header = node_rpc.call("getblockheader", proof["block_hash"])
        if int(header.get("confirmations", -1)) < 1:
            raise ValueError("the block is not in the node's chain")
        if int(header["height"]) != int(proof["block_height"]):
            raise ValueError("the block is at another height than the proof says")
    except NodeRefused as e:
        # The node's own no about this block or this transaction (-5: not found; -8: not a hash it can read) is an
        # answer: the proof does not hold. Any other error (-28 while it starts, -1 for a block it pruned) is not.
        if e.code in (-5, -8):
            raise ValueError("the node does not know this transaction in this block (%s)" % str(e)[:120])
        raise CannotAsk("the node cannot judge the proof now (%s)" % str(e)[:120])
    except NodeDown as e:
        raise CannotAsk("the node does not answer (%s)" % str(e)[:120])
    except (KeyError, TypeError) as e:
        raise ValueError("the proof lacks what the node is asked about (%s)" % e)
    done.update(block=True, confirmations=int(header["confirmations"]), time=int(header["time"]))
    return done


class Stamps:
    """The batches and what is waiting. `wallet` pays for the stamps (it needs coins and limits.json); `rpc` is the
    node (a walletd.Node) the service reads blocks from."""

    def __init__(self, wallet, rpc, folder, every=600, most_waiting=2000, clock=time.time):
        self.wallet, self.rpc, self.every, self.most_waiting, self.clock = wallet, rpc, every, most_waiting, clock
        self.lock = threading.Lock()
        # stamps.json keeps only what changes (the batches without their digests, the blocks read); the digests of a
        # batch go once into batches/<number> when it is made, and what waits is appended to waiting.log, one line
        # each, so that taking a digest no longer rewrites everything taken before it
        self.book = os.path.join(folder, "stamps.json")
        self.shelf = os.path.join(folder, "batches")
        self.log = os.path.join(folder, "waiting.log")
        os.makedirs(self.shelf, mode=0o700, exist_ok=True)
        try:
            with open(self.book) as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = {"waiting": [], "batches": [], "scanned": None, "last": 0}
        moved = False
        for i, b in enumerate(self.state["batches"]):          # the format before: digests inside stamps.json
            if "digests" in b:
                self._shelve(i, b.pop("digests"))
                moved = True
        self.where = {}             # digest -> number of its batch
        for i, b in enumerate(self.state["batches"]):
            if not b.get("dead"):
                for d in self._digests(i):
                    self.where[d] = i
        self.waiting = set(self.state.get("waiting", []))
        try:
            with open(self.log) as f:
                for line in f:
                    d = line.strip()
                    if DIGEST.match(d):                        # a line torn by a crash is not a digest
                        self.waiting.add(d)
        except FileNotFoundError:
            pass
        self.waiting -= set(self.where)                        # a crash between a batch and the log's rewrite
        self.trees = {}             # number of a batch -> its paths, worked out once
        if moved or self.state.get("waiting"):
            self._rewrite_log()
            self._save()

    @staticmethod
    def _write(path, text):
        tmp = path + ".new"
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _save(self):
        self.state.pop("waiting", None)                        # kept in waiting.log
        self._write(self.book, json.dumps(self.state, separators=(",", ":")))

    def _shelve(self, i, digests):
        self._write(os.path.join(self.shelf, str(i)), "".join(d + "\n" for d in digests))

    def _digests(self, i):
        with open(os.path.join(self.shelf, str(i))) as f:
            return [line.strip() for line in f if line.strip()]

    def _append(self, digest):
        with open(self.log, "a") as f:
            f.write(digest + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(self.log, 0o600)

    def _rewrite_log(self):
        self._write(self.log, "".join(d + "\n" for d in sorted(self.waiting)))

    def next_batch(self):
        return int(max(self.state["last"] + self.every, self.clock()))

    def take(self, digest):
        """Takes a SHA-256 (64 hexadecimal characters) and tells where it stands."""
        digest = digest.lower()
        if not DIGEST.match(digest):
            raise Refused("DIGEST", "a SHA-256 is 64 hexadecimal characters")
        with self.lock:
            if digest not in self.where and digest not in self.waiting:
                if len(self.waiting) >= self.most_waiting:
                    raise Refused("BUSY", "the next batch is full; ask again after %d" % self.next_batch())
                self._append(digest)
                self.waiting.add(digest)
            return self._status(digest)

    def _status(self, digest):
        if digest in self.waiting:
            return {"digest": digest, "state": "waiting", "next_batch": self.next_batch(),
                    "then": "ask again after the next batch and one block"}
        i = self.where[digest]
        b = self.state["batches"][i]
        if b.get("block") is None:
            return {"digest": digest, "state": "written", "txid": b["txid"], "root": b["root"],
                    "then": "the transaction waits for a block; ask again in a few minutes"}
        paths = self.trees.get(i)
        if paths is None:
            if len(self.trees) >= 32:
                self.trees.pop(next(iter(self.trees)))
            paths = self.trees[i] = tree([bytes.fromhex(d) for d in sorted(self._digests(i))])[1]
        tip = self.state["scanned"] or b["block"]["height"]
        return {"version": VERSION, "digest": digest, "state": "stamped", "root": b["root"],
                "path": [{"side": s, "hash": h.hex()} for s, h in paths[bytes.fromhex(digest)]],
                "txid": b["txid"], "transaction": b["transaction"], "block_hash": b["block"]["hash"],
                "block_height": b["block"]["height"], "confirmations": max(tip - b["block"]["height"] + 1, 1),
                "txoutproof": b["block"].get("txoutproof"),
                "rules": "leaf = SHA-256(00 || digest), node = SHA-256(01 || left || right); the transaction carries "
                         "OP_RETURN 'POLARKA' 01 || root; check with polarka-agent stamp-verify"}

    def tick(self):
        """One step: a batch when it is time and something waits, then the new blocks for the batches. The wallet
        and the node are asked without the lock held, so that asking for a stamp never waits on them."""
        made = None
        with self.lock:
            now = self.clock()
            due = sorted(self.waiting) if self.waiting and now >= self.state["last"] + self.every else None
            number = len(self.state["batches"])
        if due:
            root = tree([bytes.fromhex(d) for d in due])[0]
            try:
                done = self.wallet.stamp(root, memo="stamps batch %d (%d)" % (number, len(due)))
            except Exception as e:
                sys.stderr.write("%s batch refused: %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), getattr(e, "kind", type(e).__name__), str(e)[:200]))
                with self.lock:
                    self.state["last"] = now                   # tried again after `every`
                    self._save()
            else:
                if done.get("state") == "kept":
                    # the node said no: no transaction is on its way, so no batch is written down; its stamps wait for
                    # the next one, and the wallet's next payment stands against this one (human review, 22 November)
                    sys.stderr.write("%s batch refused by the node: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), str(done.get("note"))[:200]))
                    with self.lock:
                        self.state["last"] = now
                        self._save()
                    self._scan()
                    return None
                try:
                    raw = self.rpc.call("getrawtransaction", done["id"])
                except Exception:
                    raw = None                                 # read with the block later
                with self.lock:
                    i = len(self.state["batches"])
                    self._shelve(i, due)
                    self.state["batches"].append({"root": root.hex(), "count": len(due), "txid": done["id"],
                                                  "transaction": raw, "made": int(now), "block": None})
                    for d in due:
                        self.where.setdefault(d, i)
                        self.waiting.discard(d)
                    self.state["last"] = now
                    self._save()
                    self._rewrite_log()
                made = {"batch": i, "stamps": len(due), "txid": done["id"], "root": root.hex()}
                sys.stderr.write("%s batch %d: %d stamps, %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), i, len(due), done["id"]))
        self._scan()
        return made

    def _scan(self, most=200):
        try:
            tip = int(self.rpc.call("getblockcount"))
        except Exception:
            return
        with self.lock:
            batches = [(i, dict(b)) for i, b in enumerate(self.state["batches"])
                       if not b.get("dead") and (b["block"] is None or tip - b["block"]["height"] < DEEP)]
            scanned = self.state["scanned"]
            recent = [list(x) for x in self.state.get("recent", [])]
        if scanned is None:
            scanned = tip - 1
        changes, dead = {}, []
        try:
            # where the chain we read and the node's chain part: from there on everything is read again
            common = None
            for h, hh in reversed(recent):
                if h <= tip and self.rpc.call("getblockhash", h) == hh:
                    common = h
                    break
            if recent and common is None:
                common = recent[0][0] - 1
            if common is not None and common < scanned:
                scanned = common
            recent = [x for x in recent if common is None or x[0] <= common]
            for i, b in batches:                               # a block that left the chain takes its batches with it
                if b["block"] is not None and (b["block"]["height"] > tip
                                               or self.rpc.call("getblockhash", b["block"]["height"]) != b["block"]["hash"]):
                    # its time starts again: the node may take a while to have it waiting again (human review)
                    changes[i] = {"block": None, "made": int(self.clock())}
                    scanned = min(scanned, b["block"]["height"] - 1)
            wanted = {b["txid"]: i for i, b in batches if b["block"] is None or i in changes}
            top = min(tip, scanned + most)
            for h in range(scanned + 1, top + 1):
                bh = self.rpc.call("getblockhash", h)
                recent = (recent + [[h, bh]])[-RECENT:]
                if wanted:
                    for txid in self.rpc.call("getblock", bh, 1)["tx"]:
                        if txid in wanted:
                            i = wanted.pop(txid)
                            raw = self.rpc.call("getrawtransaction", txid, False, bh)
                            changes[i] = {"block": {"hash": bh, "height": h, "txoutproof": self.rpc.call("gettxoutproof", [txid], bh)},
                                          "transaction": raw}
                scanned = h
            # a transaction the node has lost for long is not waited for any more: its stamps go into the next batch
            if scanned >= tip:
                for txid, i in wanted.items():
                    b = dict(batches)[i]
                    if self.clock() - changes.get(i, b).get("made", b["made"]) >= DEAD:
                        try:
                            self.rpc.call("getmempoolentry", txid)
                        except Exception as e:
                            # only the node's word that it does not have it: a node that did not answer says nothing;
                            # and only while no block came since the count above, or it may be in that block (human review)
                            if "not in mempool" in str(e).lower() and int(self.rpc.call("getblockcount")) == tip:
                                dead.append(i)
        except Exception as e:
            sys.stderr.write("%s scan stopped: %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), type(e).__name__, str(e)[:200]))
        with self.lock:
            # written only when something changed: not every half minute for nothing (human review, 22 November)
            changed = bool(changes or dead) or self.state["scanned"] != scanned or self.state.get("recent") != recent
            for i, change in changes.items():
                self.state["batches"][i].update(change)
                self.trees.pop(i, None)
            for i in dead:
                b = self.state["batches"][i]
                b["dead"] = True
                digests = self._digests(i)
                for d in digests:
                    if self.where.get(d) == i:
                        del self.where[d]
                        self.waiting.add(d)
                sys.stderr.write("%s batch %d: its transaction is gone, %d stamps wait again\n"
                                 % (time.strftime("%Y-%m-%d %H:%M:%S"), i, len(digests)))
            self.state["scanned"] = scanned
            self.state["recent"] = recent
            if dead:
                self._rewrite_log()
            if changed:
                self._save()


ABOUT = {"service": "polarka-stamps", "what": "proof that a file existed by a given block of TrueNorth",
         "how": ["GET /v1/stamp/<sha256 of your file>", "ask again after the next batch and one block",
                 "keep the proof; check it with polarka-agent stamp-verify"],
         "note": "many SHA-256s share one transaction; only the SHA-256 is sent, never the file"}


def make_handler(stamps):
    class H(OneRequestWithBody, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "polarka-stamps"
        timeout = 30

        def log_message(self, *a):
            pass

        def send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def handle_one(self):
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
                if not 0 <= n <= 4096:
                    self.close_connection = True
                    return self.send(413, {"error": "INPUT", "message": "request size"})
                if n:
                    self.rfile.read(n)
                path = self.path.split("?", 1)[0]
                if path in ("/v1/stamp", "/v1/stamp/"):
                    return self.send(200, dict(ABOUT, every_seconds=stamps.every, next_batch=stamps.next_batch()))
                if path.startswith("/v1/stamp/"):
                    return self.send(200, stamps.take(path[len("/v1/stamp/"):]))
                self.send(404, {"error": "NOT_HERE", "message": "there is /v1/stamp/<sha256> here"})
            except Refused as e:
                self.send(503 if e.kind == "BUSY" else 400, {"error": e.kind, "message": str(e)})
            except Exception as e:
                self.send(500, {"error": "FAILED", "message": type(e).__name__})

        do_GET = do_POST = handle_one
    return H


def serve(wallet, rpc, folder, listen, every=600, look=30):
    stamps = Stamps(wallet, rpc, folder, every)
    host, port = listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), make_handler(stamps))
    srv.daemon_threads = True

    def loop():
        while True:
            try:
                stamps.tick()
            except Exception as e:
                sys.stderr.write("%s tick failed: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), type(e).__name__))
            time.sleep(look)
    threading.Thread(target=loop, daemon=True).start()
    sys.stderr.write("stamps on %s: a batch every %d s at most\n" % (listen, every))
    srv.serve_forever()
