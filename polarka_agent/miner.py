"""Mining for a pool that speaks the protocol of pool.py: ask for a header, look for nonces, send them.

Nothing of the wallet is touched: the miner knows an address and nothing else. The reward for a share is
what the pool writes down for that address; whether and when it pays is the pool's word, not this program's.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from . import rx as rxmod


class PoolSaid(Exception):
    def __init__(self, status, kind, message):
        Exception.__init__(self, message)
        self.status, self.kind = status, kind


def _call(url, body=None, timeout=20):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "polarka-agent"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            answer = json.loads(r.read(65536))
    except urllib.error.HTTPError as e:
        try:
            said = json.loads(e.read(4096))
            kind, message = str(said.get("error"))[:20], str(said.get("message"))[:200]
        except (ValueError, AttributeError):
            kind, message = "HTTP", "the pool answered %d" % e.code
        raise PoolSaid(e.code, kind, message)
    if type(answer) is not dict:
        raise PoolSaid(200, "ANSWER", "the pool's answer is not a JSON object")
    return answer


def _units(text):
    """What the pool says it wrote down, as base units; 0 when it cannot be read."""
    try:
        whole, _, part = str(text).partition(".")
        return int(whole) * 10 ** 8 + int((part + "0" * 8)[:8])
    except ValueError:
        return 0


def _work(url, address):
    work = _call(url + "/v1/work?address=" + quote(address))
    try:
        header, key = bytes.fromhex(work["header"]), bytes.fromhex(work["seed"])
        target, job, height = int(work["share_target"], 16), work["job"], work["height"]
        if len(header) != 76 or len(key) != 32 or not isinstance(job, str) or type(height) is not int or target <= 0:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise PoolSaid(200, "ANSWER", "the pool's work cannot be read")
    return {"job": job, "header": header, "key": key, "target": target, "height": height}


def mine(url, address, threads=1, fast=True, shares=None, seconds=None, say=lambda event: None, engine=None,
         look_every=2.0, clock=time.monotonic):
    """Mines for the pool at `url` to `address` until `shares` were taken or `seconds` have passed (for ever
    without both). Returns the sums. `fast` builds the 2 GiB table; without it a thread does tens of hashes
    a second, which is for trying things out and not for mining."""
    url = url.rstrip("/")
    engine = engine or rxmod.engine()
    threads = max(1, int(threads))
    chunk = 512 if fast else 4
    sums = {"shares": 0, "blocks": 0, "refused": 0, "hashes": 0, "credited": 0}
    lock, started = threading.Lock(), clock()

    def over():
        return (shares is not None and sums["shares"] >= shares) or (seconds is not None and clock() - started >= seconds)

    def look(work, index, stop):
        vm = engine.rx.vm()
        try:
            at = index
            while not stop.is_set() and at < 1 << 32:
                found = engine.rx.scan(vm, work["header"], work["target"], at, threads, chunk)
                if found is None:
                    with lock:
                        sums["hashes"] += chunk
                    at += chunk * threads
                    continue
                with lock:
                    sums["hashes"] += (found[0] - at) // threads + 1
                at = found[0] + threads
                try:
                    took = _call(url + "/v1/share", {"job": work["job"], "nonce": found[0]})
                except PoolSaid as e:
                    with lock:
                        sums["refused"] += 1
                    say({"event": "refused", "kind": e.kind, "message": str(e)})
                    if e.kind == "STALE":
                        stop.set()
                    continue
                except OSError as e:
                    say({"event": "lost", "message": "the share did not reach the pool: %s" % type(e).__name__})
                    stop.set()
                    continue
                with lock:
                    sums["shares"] += 1
                    sums["blocks"] += 1 if took.get("accepted") else 0
                    sums["credited"] += _units(took.get("credited"))
                say({"event": "share", "height": work["height"], "credited": took.get("credited"), "block": took.get("block"),
                     "accepted": took.get("accepted")})
                if over():
                    stop.set()
        finally:
            engine.rx.free(vm)

    while not over():
        try:
            work = _work(url, address)
        except PoolSaid as e:
            if e.status not in (429, 503):
                raise
            say({"event": "waiting", "kind": e.kind, "message": str(e)})
            time.sleep(look_every)
            continue
        except OSError as e:
            say({"event": "waiting", "kind": "NETWORK", "message": type(e).__name__})
            time.sleep(look_every)
            continue
        engine.ensure(work["key"], fast, threads)
        stop = threading.Event()
        crew = [threading.Thread(target=look, args=(work, i, stop), daemon=True) for i in range(threads)]
        for t in crew:
            t.start()
        say({"event": "work", "height": work["height"], "threads": threads})
        asked = clock()
        while not stop.is_set() and any(t.is_alive() for t in crew):
            stop.wait(look_every)
            if over() or clock() - asked >= 110:
                stop.set()
                break
            try:
                if _call(url + "/v1/tip")["height"] != work["height"]:
                    stop.set()
            except (PoolSaid, OSError, KeyError):
                stop.set()
        stop.set()
        for t in crew:
            t.join()
    sums["seconds"] = round(clock() - started, 1)
    sums["credited"] = "%d.%08d" % (sums["credited"] // 10 ** 8, sums["credited"] % 10 ** 8)
    return sums
