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

from .wallet import STAMP, Refused

DIGEST = re.compile(r"^[0-9a-f]{64}$")
VERSION = 1
DEEP = 6            # blocks after which a batch is no longer looked at for a reorganization


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
        raw = bytes.fromhex(tx_hex)
    except (TypeError, ValueError):
        raise ValueError("this is not a proof of a stamp")
    if b"\x6a" + bytes([len(STAMP) + 32]) + STAMP + root not in raw:
        raise ValueError("the transaction does not carry the root")
    return True


def verify(proof, node_rpc=None):
    """check(), and with a node (anything with call()): the transaction is the one named, is in the named
    block, and that block is in the node's chain. Returns what was checked."""
    check(proof)
    done = {"path": True, "root_in_transaction": True}
    if node_rpc is None:
        done["block"] = "not checked: give a node, or look up the transaction and its block in any explorer"
        return done
    got = node_rpc.call("getrawtransaction", proof["txid"], True, proof["block_hash"])
    if got.get("hex") != proof["transaction"] and got.get("hex", "").lower() != str(proof["transaction"]).lower():
        raise ValueError("the node has another transaction under this id")
    header = node_rpc.call("getblockheader", proof["block_hash"])
    if int(header.get("confirmations", -1)) < 1:
        raise ValueError("the block is not in the node's chain")
    if int(header["height"]) != int(proof["block_height"]):
        raise ValueError("the block is at another height than the proof says")
    done.update(block=True, confirmations=int(header["confirmations"]), time=int(header["time"]))
    return done


class Stamps:
    """The batches and what is waiting. `wallet` pays for the stamps (it needs coins and limits.json); `rpc` is the
    node (a walletd.Node) the service reads blocks from."""

    def __init__(self, wallet, rpc, folder, every=600, most_waiting=10000, clock=time.time):
        self.wallet, self.rpc, self.every, self.most_waiting, self.clock = wallet, rpc, every, most_waiting, clock
        self.lock = threading.Lock()
        self.book = os.path.join(folder, "stamps.json")
        try:
            with open(self.book) as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = {"waiting": [], "batches": [], "scanned": None, "last": 0}
        self.where = {}             # digest -> number of its batch
        for i, b in enumerate(self.state["batches"]):
            for d in b["digests"]:
                self.where.setdefault(d, i)
        self.waiting = set(self.state["waiting"])
        self.trees = {}             # number of a batch -> its paths, worked out once

    def _save(self):
        self.state["waiting"] = sorted(self.waiting)
        tmp = self.book + ".new"
        with open(tmp, "w") as f:
            json.dump(self.state, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.book)

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
                self.waiting.add(digest)
                self._save()
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
            paths = self.trees[i] = tree([bytes.fromhex(d) for d in sorted(b["digests"])])[1]
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
            except Refused as e:
                sys.stderr.write("%s batch refused: %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), e.kind, str(e)[:200]))
                with self.lock:
                    self.state["last"] = now                   # tried again after `every`
                    self._save()
            else:
                try:
                    raw = self.rpc.call("getrawtransaction", done["id"])
                except Exception:
                    raw = None                                 # read with the block later
                with self.lock:
                    self.state["batches"].append({"root": root.hex(), "digests": due, "txid": done["id"], "transaction": raw,
                                                  "made": int(now), "block": None})
                    i = len(self.state["batches"]) - 1
                    for d in due:
                        self.where.setdefault(d, i)
                        self.waiting.discard(d)
                    self.state["last"] = now
                    self._save()
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
            batches = [(i, dict(b)) for i, b in enumerate(self.state["batches"]) if b["block"] is None or tip - b["block"]["height"] < DEEP]
            scanned = self.state["scanned"]
        if scanned is None:
            scanned = tip - 1
        changes = {}
        try:
            for i, b in batches:                               # a block that left the chain takes its batches with it
                if b["block"] is not None and (b["block"]["height"] > tip
                                               or self.rpc.call("getblockhash", b["block"]["height"]) != b["block"]["hash"]):
                    changes[i] = {"block": None}
                    scanned = min(scanned, b["block"]["height"] - 1)
            wanted = {b["txid"]: i for i, b in batches if b["block"] is None or i in changes}
            top = min(tip, scanned + most)
            for h in range(scanned + 1, top + 1):
                if wanted:
                    bh = self.rpc.call("getblockhash", h)
                    for txid in self.rpc.call("getblock", bh, 1)["tx"]:
                        if txid in wanted:
                            i = wanted.pop(txid)
                            raw = self.rpc.call("getrawtransaction", txid, False, bh)
                            changes[i] = {"block": {"hash": bh, "height": h, "txoutproof": self.rpc.call("gettxoutproof", [txid], bh)},
                                          "transaction": raw}
                scanned = h
        except Exception as e:
            sys.stderr.write("%s scan stopped: %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), type(e).__name__, str(e)[:200]))
        with self.lock:
            for i, change in changes.items():
                self.state["batches"][i].update(change)
                self.trees.pop(i, None)
            self.state["scanned"] = scanned
            self._save()


ABOUT = {"service": "polarka-stamps", "what": "proof that a file existed by a given block of TrueNorth",
         "how": ["GET /v1/stamp/<sha256 of your file>", "ask again after the next batch and one block",
                 "keep the proof; check it with polarka-agent stamp-verify"],
         "note": "many SHA-256s share one transaction; only the SHA-256 is sent, never the file"}


def make_handler(stamps):
    class H(BaseHTTPRequestHandler):
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
