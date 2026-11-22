"""Stratum for the pool: the protocol the network's miners already speak, next to the pool's own over HTTP.

IronMiner (truenorth-ironminer, a cpuminer-opt for TrueNorth's rx/north) mines for a pool over stratum v1, so a
miner who has it mines here with nothing new: -o stratum+tcp://HOST:PORT -u ADDRESS -p x. The shares go into the
pool's one ledger and are paid like every other share (the reward of a block without the fees, times the part of
a block a share is, less the fee); the cap and the guard are the same.

What goes over the line, as the miner reads and writes it (util.c, cpu-miner.c and algo/randomx/randomx-miner.cpp
of truenorth-ironminer at 78f3afd, read on 22 November 2026):

    -> mining.subscribe [agent]           <- [[["mining.set_difficulty", id], ["mining.notify", id]], extranonce1, 4]
    -> mining.authorize [address, x]      <- true; then set_difficulty, set_randomx_seed and notify
    <- mining.set_difficulty [d]
    <- mining.set_randomx_seed [key, h]   TrueNorth's own: the key of the proof of work as getblockhash shows a
                                          hash, and the height of the block it is the hash of
    <- mining.notify [job, previous, coinbase1, coinbase2, branch, version, bits, time, clean]
    -> mining.submit [worker, job, extranonce2, time, nonce]    <- true, or an error [code, why, null]

The miner hashes the plain 80 bytes of the header. Version, bits and time go as numbers in hexadecimal, the
previous block's hash as the header holds it with the bytes of every four turned around, the branch as the hashes
are, and the nonce comes back as a number in hexadecimal. The hash is read as a little-endian number and is a share
at or below the target.

A difficulty here is always a power of two, 2^-k. The target the miner works out from it (diff_to_hash in its
util.c) is then exactly 2^(224+k) + 2^128 - 1, and the pool takes what the miner sends, no more and no less. k
follows the miner's speed, towards a share every TARGET_SECONDS, within the same bounds as over HTTP: a share is at
most 1/ratio of a block, and at least 1/MIN_ODDS.

Every connection gets an extranonce1 of its own, and the coinbase carries MARK in front of the extranonces, so a
header made here is neither one made for another connection nor one handed out over HTTP; a header taken once is
not taken again. A share is paid once.
"""
import asyncio
import collections
import functools
import json
import math
import os
import re
import socket
import threading

from . import pool as poolmod
from . import rx as rxmod
from .keys import BadInput, script_for_address
from .pool import PoolError, dsha

EXTRANONCE1 = 4             # bytes the pool gives a connection
EXTRANONCE2 = 4             # bytes the miner counts through
MARK = b"S"                 # in the coinbase before the extranonces: a header made here is never one made over HTTP
TARGET_SECONDS = 15         # a share about this often from a connection
JOB_REFRESH = 60            # seconds between two jobs on the same tip (new transactions, a new time)
JOBS_KEPT = 8               # jobs of one connection that still take shares, all on the newest tip
MIN_ODDS = 16               # a share is never more than this part of a block
TIME_AHEAD = 600            # seconds the time in a share may be after its job's
MOST_LINE = 4096            # bytes in one line from a miner
MOST_CONNECTIONS = 1000
PER_PLACE = 64              # connections from one place (pool.place: an IPv4 address, or an IPv6 /48)
MOST_WORKERS = 4            # names authorized on one connection
QUIET = 30                  # seconds a connection has to subscribe and authorize
RATE, BURST = 10.0, 20.0    # shares a second one connection may send, after a burst
OUT_MOST = 256 * 1024       # bytes waiting to go out to one miner before it is let go
EASIEST, HARDEST = 31, -16  # the k of a difficulty 2^-k
TIPS_KEPT = 4               # tips for which the headers taken are remembered
HEX8 = re.compile(r"\A[0-9a-fA-F]{8}\Z")
CODES = {"STALE": 21, "DUPLICATE": 22, "LOW": 23}


def share_target(k):
    """The target a miner told the difficulty 2^-k works with: diff_to_hash() in IronMiner's util.c makes the
    upper 128 bits 2^(96+k) and sets every one of the lower 128."""
    return (1 << (224 + k)) + (1 << 128) - 1


def difficulty(k):
    return 2.0 ** -k        # a power of two is exact in a double, and json writes it so that it reads back the same


def hashes(k):
    """What a share at 2^-k stands for: the hashes it takes on average to find one."""
    return round((1 << 256) / (share_target(k) + 1))


def bounds(network, ratio):
    """(hardest k, easiest k) for a network target: a share at most 1/ratio of a block and at least 1/MIN_ODDS. On
    a chain as easy as regtest only the easiest is left."""
    easiest = min(network * ratio, poolmod.MOST)
    high = next((k for k in range(EASIEST, HARDEST - 1, -1) if share_target(k) <= easiest), HARDEST)
    low = next((k for k in range(HARDEST, EASIEST + 1) if share_target(k) >= network * MIN_ODDS), high)
    return min(low, high), high


def branch(txids):
    """The hashes that the coinbase's hash is put together with, one after the other, to make the merkle root:
    the same tree pool.merkle_root builds from below."""
    steps, layer = [], [None] + list(txids)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        steps.append(layer[1])
        layer = [None] + [dsha(layer[i] + layer[i + 1]) for i in range(2, len(layer), 2)]
    return steps


def root(coinbase_hash, steps):
    for step in steps:
        coinbase_hash = dsha(coinbase_hash + step)
    return coinbase_hash


def swapped(previous):
    """The previous block's hash as stratum sends it: as the header holds it, the bytes of every four turned around."""
    raw = bytes.fromhex(previous)[::-1]
    return b"".join(raw[i:i + 4][::-1] for i in range(0, 32, 4)).hex()


class Job:
    """One block template cut for stratum: the coinbase before and after the extranonces, the branch, and what a
    share of it is worth. The same for every connection; the extranonce1 makes the headers differ."""

    def __init__(self, number, template, key, key_height, pay_script, made):
        self.number, self.template, self.key, self.key_height, self.made = number, template, key, key_height, made
        txs = template.get("transactions", [])
        probe = bytes(range(1, 9))
        plain = poolmod.coinbase(template["height"], template["coinbasevalue"], pay_script, int.from_bytes(probe, "little"),
                                 poolmod.commitment_of(template), False, MARK)
        at = plain.index(bytes([len(MARK) + 8]) + MARK + probe) + 1 + len(MARK)
        self.coinb1, self.coinb2 = plain[:at], plain[at + 8:]
        self.branch = branch([bytes.fromhex(t["txid"])[::-1] for t in txs])
        self.previous, self.height = template["previousblockhash"], template["height"]
        self.version, self.bits, self.time = template["version"], template["bits"], template["curtime"]
        if len(self.bits) != 8 or not 0 <= self.version <= 0xFFFFFFFF:
            raise ValueError("the template does not make a header")
        self.network = rxmod.target_of(int(self.bits, 16))
        self.reward = template["coinbasevalue"] - sum(int(t.get("fee", 0)) for t in txs)

    def notify(self, job_id, clean):
        return [job_id, swapped(self.previous), self.coinb1.hex(), self.coinb2.hex(), [b.hex() for b in self.branch],
                "%08x" % self.version, self.bits, "%08x" % self.time, clean]

    def header(self, extranonce1, extranonce2, ntime, nonce):
        coinbase = self.coinb1 + extranonce1 + extranonce2 + self.coinb2
        return (self.version.to_bytes(4, "little") + bytes.fromhex(self.previous)[::-1] + root(dsha(coinbase), self.branch)
                + ntime.to_bytes(4, "little") + bytes.fromhex(self.bits)[::-1] + nonce.to_bytes(4, "little"))

    def block(self, extranonce1, extranonce2, header, pay_script):
        """The whole block for a header of this job: the coinbase is the one the header's merkle root was made of."""
        extranonce = int.from_bytes(extranonce1 + extranonce2, "little")
        return header + poolmod.assemble(self.template, pay_script, extranonce, MARK)[1]


class Miner:
    """One connection, and what the pool knows of it."""

    def __init__(self, writer, source, extranonce1):
        self.writer, self.source, self.extranonce1 = writer, source, extranonce1
        self.subscribed = self.closed = False
        self.workers = {}                           # the name the miner gave -> the address its shares are paid to
        self.k = self.k_sent = self.seed_sent = self.last = None
        self.jobs = collections.OrderedDict()       # a job id given to this miner -> (Job, k)
        self.given = 0
        self.since, self.found = None, []           # for the difficulty: since when, and what each share since stood for
        self.tokens, self.tokens_at = BURST, None


class Stratum:
    """Listens for miners in a thread of its own (asyncio) and asks the pool, its node and its proof of work in
    worker threads, so that a slow node or a slow check holds up no connection."""

    def __init__(self, pool, listen, target_seconds=TARGET_SECONDS, refresh=JOB_REFRESH, most=MOST_CONNECTIONS,
                 per_place=PER_PLACE, welcome=None, span=None):
        host, _, port = listen.rpartition(":")
        self.host, self.port = host.strip("[]") or None, int(port)
        self.pool, self.ticks = pool, pool.ticks
        self.span = span                            # (hardest k, easiest k) in place of bounds(): only for trying a miner on regtest
        self.target_seconds, self.refresh_every, self.most, self.per_place = target_seconds, refresh, most, per_place
        self.welcome = welcome                      # said to a miner once it is authorized; {address} is its address
        self.lock = threading.Lock()                # the newest job and the headers taken, also from the worker threads
        self.refreshing = threading.Lock()          # one new job at a time
        self.job, self.number = None, 0
        self.taken = collections.OrderedDict()      # a tip -> the headers taken on it
        self.miners, self.places = set(), collections.Counter()
        self.next_extranonce = int.from_bytes(os.urandom(EXTRANONCE1), "big")
        self.checks = None                          # worker threads for shares, and for the node, made in start()
        self.calls = None
        self.loop = self.server = self.wake = self.task = self.thread = None
        self.ready, self.failed = threading.Event(), None
        pool.stratum = self

    # -- running -------------------------------------------------------------------------------------------------

    def start(self):
        """Starts listening; returns the port."""
        from concurrent.futures import ThreadPoolExecutor
        self.checks = ThreadPoolExecutor(2, "stratum-share")
        self.calls = ThreadPoolExecutor(2, "stratum-node")
        self.thread = threading.Thread(target=self._run, name="stratum", daemon=True)
        self.thread.start()
        self.ready.wait(30)
        if self.failed is not None:
            raise self.failed
        if not self.ready.is_set():
            raise OSError("stratum did not start")
        return self.port

    def stop(self):
        loop, task = self.loop, self.task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass        # the loop is closed already
        if self.thread is not None:
            self.thread.join(10)
        for executor in (self.checks, self.calls):
            if executor is not None:
                executor.shutdown(wait=False)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.task = self.loop.create_task(self._main())
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.failed = e
        finally:
            try:
                rest = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
                for t in rest:
                    t.cancel()
                if rest:
                    self.loop.run_until_complete(asyncio.gather(*rest, return_exceptions=True))
            finally:
                self.ready.set()
                self.loop.close()

    async def _main(self):
        self.wake = asyncio.Event()
        try:
            self.server = await asyncio.start_server(self._client, self.host, self.port, limit=MOST_LINE, reuse_address=True)
        except OSError as e:
            self.failed = e
            return
        self.port = self.server.sockets[0].getsockname()[1]
        self.ready.set()
        try:
            await self._broadcast()
        finally:
            self.server.close()
            for miner in list(self.miners):
                self._close(miner)

    async def _call(self, fn, *args):
        return await self.loop.run_in_executor(self.calls, functools.partial(fn, *args))

    def shown(self):
        """For the pool's stats: who mines here now."""
        working = [m for m in list(self.miners) if m.workers]
        return {"port": self.port, "connections": len(working),
                "addresses": len({a for m in working for a in list(m.workers.values())})}

    # -- one connection ------------------------------------------------------------------------------------------

    async def _client(self, reader, writer):
        source = (writer.get_extra_info("peername") or ("?",))[0]
        place = poolmod.place(source)
        if len(self.miners) >= self.most or (place is not None and self.places[place] >= self.per_place):
            writer.close()
            return
        if self.pool.guard is not None:
            try:
                self.pool.guard.check(source)
            except PoolError:
                writer.close()
                return
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
        miner = Miner(writer, source, self._extranonce())
        self.miners.add(miner)
        self.places[place] += 1
        try:
            while not miner.closed:
                try:
                    line = await asyncio.wait_for(reader.readline(), None if miner.workers else QUIET)
                except (asyncio.TimeoutError, ValueError, OSError):
                    break           # silent too long before it was authorized, a line too long, or the line is gone
                if not line.endswith(b"\n") or not await self._said(miner, line):
                    break
        finally:
            self.miners.discard(miner)
            self.places[place] -= 1
            if self.places[place] <= 0:
                del self.places[place]
            self._close(miner)

    def _extranonce(self):
        self.next_extranonce = (self.next_extranonce + 1) % (1 << (8 * EXTRANONCE1))
        return self.next_extranonce.to_bytes(EXTRANONCE1, "big")

    async def _said(self, miner, line):
        """One line from a miner. False: the connection ends."""
        try:
            said = json.loads(line)
        except (ValueError, RecursionError):
            return False
        if type(said) is not dict:
            return False
        method, params, rid = said.get("method"), said.get("params"), said.get("id")
        if method is None:
            return True             # an answer to nothing the pool asked
        if type(params) is not list:
            params = []
        if method == "mining.subscribe":
            miner.subscribed = True
            session = miner.extranonce1.hex()
            self._answer(miner, rid, [[["mining.set_difficulty", session], ["mining.notify", session]], session, EXTRANONCE2])
        elif method == "mining.authorize":
            await self._authorize(miner, rid, params)
        elif method == "mining.submit":
            await self._submit(miner, rid, params)
        elif method == "mining.extranonce.subscribe":
            self._answer(miner, rid, False)         # the extranonce of a connection never changes
        elif method == "mining.suggest_difficulty":
            self._suggest(miner, params)
            self._answer(miner, rid, True)
        else:
            self._error(miner, rid, 20, "unknown method")
        return not miner.closed

    async def _authorize(self, miner, rid, params):
        if not miner.subscribed:
            return self._error(miner, rid, 25, "not subscribed")
        name = params[0] if params and type(params[0]) is str else ""
        if name in miner.workers:
            return self._answer(miner, rid, True)
        if len(miner.workers) >= MOST_WORKERS or len(name) > 200:
            return self._error(miner, rid, 24, "too many names, or too long a one, on one connection")
        address = name.split(".", 1)[0].strip().lower()
        try:
            await self._call(self._may_mine, miner.source, address)
        except PoolError as e:
            self._message(miner, str(e))
            return self._error(miner, rid, 24, str(e))
        first = not miner.workers
        miner.workers[name] = address
        self._answer(miner, rid, True)
        if not first:
            return
        if self.welcome:
            self._message(miner, self.welcome.replace("{address}", address))
        if self.job is None:
            try:
                await self._call(self._refresh, set())
            except PoolError as e:
                self._message(miner, str(e))
                return self._close(miner)
        if self.job is not None:
            self._give(miner, self.job, True)

    def _may_mine(self, source, address):
        """In a worker thread: the pool's rules for whoever asks for work, as pool.work() keeps them."""
        p = self.pool
        try:
            script_for_address(p.chain, address)
        except BadInput as e:
            raise PoolError("ADDRESS", "the user name is your address of this network, if you like with .worker after it: %s" % e)
        if p.miners is not None and address not in p.miners:
            raise PoolError("CLOSED", "this pool takes only its owner's miners for now; it will open to others later", 403)
        if p.cap is not None and p.cap.is_closed():
            raise PoolError("CAP", "the pool has found as large a part of the network's blocks as it allows itself and hands out "
                                   "no work until that part is smaller; mine alone or elsewhere meanwhile", 503)
        with p.lock:
            template = p._fresh_template()
            if p.guard is not None:
                p.guard.asked(source, address, template["previousblockhash"])
            p._may_work(address)

    def _suggest(self, miner, params):
        """A miner may say what difficulty it would like; the pool starts there, within its bounds."""
        try:
            wanted = float(params[0])
        except (IndexError, TypeError, ValueError):
            return
        if wanted > 0 and math.isfinite(wanted):
            miner.k = max(HARDEST, min(EASIEST, int(round(-math.log2(wanted)))))

    # -- shares --------------------------------------------------------------------------------------------------

    async def _submit(self, miner, rid, params):
        guard = self.pool.guard
        if guard is not None:
            try:
                guard.check(miner.source)
            except PoolError as e:
                self._error(miner, rid, 20, str(e))
                return self._close(miner)
        if len(params) < 5 or not all(type(x) is str for x in params[:5]):
            return self._wrong(miner, rid, 20, "a share is [worker, job, extranonce2, time, nonce]")
        address = miner.workers.get(params[0])
        if address is None:
            return self._error(miner, rid, 24, "unauthorized worker")
        if not self._spend(miner):
            return self._error(miner, rid, 20, "too many shares at once; the pool raises the difficulty")
        held = miner.jobs.get(params[1])
        if held is None:
            return self._error(miner, rid, 21, "stale: that job is not for the newest block")
        job, k = held
        try:
            entry = await self.loop.run_in_executor(self.checks, functools.partial(
                self._check, miner.extranonce1, address, job, k, params[2], params[3], params[4]))
        except PoolError as e:
            if e.kind in ("LOW", "INPUT"):
                return self._wrong(miner, rid, CODES.get(e.kind, 20), str(e))
            if e.kind == "STALE":
                self.wake.set()             # the tip moved: new work for everybody now, not at the next look
            return self._error(miner, rid, CODES.get(e.kind, 20), str(e))
        except Exception as e:              # never a traceback to a miner
            poolmod._say("stratum: a share could not be checked: %s" % type(e).__name__)
            return self._error(miner, rid, 20, "the pool could not check that share")
        self._answer(miner, rid, True)
        miner.found.append(hashes(k))
        if entry["accepted"]:
            self.wake.set()
        if self._retune(miner) and self.job is not None:
            self._give(miner, self.job, False)

    def _check(self, extranonce1, address, job, k, extranonce2, ntime, nonce):
        """In a worker thread: what was written down for a share, or PoolError."""
        p = self.pool
        if not (HEX8.match(extranonce2) and HEX8.match(ntime) and HEX8.match(nonce)):
            raise PoolError("INPUT", "extranonce2, time and nonce are eight hexadecimal digits each")
        extranonce2, ntime, nonce = bytes.fromhex(extranonce2), int(ntime, 16), int(nonce, 16)
        if not job.time <= ntime <= job.time + TIME_AHEAD:
            raise PoolError("INPUT", "the time of a share is its job's, or at most %d seconds later" % TIME_AHEAD)
        with p.lock:
            p._check_tip()
            tip = p.template["previousblockhash"] if p.template is not None else None
        header = job.header(extranonce1, extranonce2, ntime, nonce)
        with self.lock:
            if tip is None and self.job is not None:
                tip = self.job.previous     # the node was silent for a moment: the last tip known stands
            if job.previous != tip:
                raise PoolError("STALE", "that job is for a block that is no longer the newest", 409)
            taken = self.taken.setdefault(job.previous, set())
            if header in taken:
                raise PoolError("DUPLICATE", "that share was sent before", 409)
            taken.add(header)
        proof = int.from_bytes(p.hasher.hash(job.key, header), "little")
        target = share_target(k)
        if proof > target:
            raise PoolError("LOW", "the hash of that share is above its target")
        share = max(target, job.network)
        entry = {"t": int(p.clock()), "address": address, "height": job.height, "job": "s%d" % job.number, "nonce": nonce,
                 "value": job.reward * job.network // share * (100 - p.fee) // 100, "odds": share // job.network,
                 "work": hashes(k), "via": "stratum", "block": None, "accepted": None}
        if proof <= job.network:
            entry["block"] = dsha(header)[::-1].hex()
            try:
                refused = p.node.call("submitblock", job.block(extranonce1, extranonce2, header, p.pay_script).hex())
                entry["accepted"] = refused is None
                if refused is not None:
                    entry["refused"] = str(refused)[:60]
            except OSError as e:
                entry["accepted"], entry["refused"] = False, "the node did not take it: %s" % str(e)[:60]
        p._write(entry)                     # first on disk, then in the sums
        with p.lock:
            p._count(entry)
            if entry["accepted"]:
                p.template_at = 0           # the tip has moved: the next look asks the node again
        return entry

    def _wrong(self, miner, rid, code, why):
        """A share that is no share: answered, and counted against the miner's place by the guard."""
        self._error(miner, rid, code, why)
        guard = self.pool.guard
        if guard is None:
            return
        guard.wrong(miner.source)
        try:
            guard.check(miner.source)
        except PoolError as e:
            self._message(miner, str(e))
            self._close(miner)

    def _spend(self, miner):
        now = self.ticks()
        if miner.tokens_at is not None:
            miner.tokens = min(BURST, miner.tokens + max(now - miner.tokens_at, 0) * RATE)
        miner.tokens_at = now
        if miner.tokens < 1:
            return False
        miner.tokens -= 1
        return True

    # -- jobs ----------------------------------------------------------------------------------------------------

    def _refresh(self, addresses):
        """In a worker thread: a new job when the tip moved or the newest job is old. Returns None when no job was
        made, else what the pool now refuses: {"*": why} for everybody, {address: why} for some."""
        p = self.pool
        with self.refreshing:
            with p.lock:
                p._check_tip()
                template, key, key_height = p.template, p.key, p.key_height
            if template is None:
                return None                 # the node was silent for a moment: the job there is stands
            old = self.job
            if old is not None and template["previousblockhash"] == old.previous and self.ticks() - old.made < self.refresh_every:
                return None
            refused = {}
            if p.cap is not None and p.cap.is_closed():
                refused["*"] = ("the pool has found as large a part of the network's blocks as it allows itself and hands out "
                                "no work until that part is smaller; mine alone or elsewhere meanwhile")
            with p.lock:
                for address in addresses:
                    try:
                        p._may_work(address)
                    except PoolError as e:
                        refused[address] = str(e)
            self.number += 1
            job = Job(self.number, template, key, key_height, p.pay_script, self.ticks())
            with self.lock:
                self.job = job
                self.taken.setdefault(job.previous, set())
                self.taken.move_to_end(job.previous)
                while len(self.taken) > TIPS_KEPT:
                    self.taken.popitem(last=False)
            return refused

    async def _broadcast(self):
        """Every second, and at once when a share says the tip moved: new work to whoever needs it, and the
        difficulty of the quiet ones looked at."""
        tuned = None
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            working = [m for m in self.miners if m.workers and not m.closed]
            if not working:
                continue
            try:
                refused = await self._call(self._refresh, {a for m in working for a in m.workers.values()})
            except PoolError:
                refused = None              # the node is silent: shares are refused as PAUSED until it speaks
            except Exception as e:
                poolmod._say("stratum: no new job: %s: %s" % (type(e).__name__, str(e)[:200]))
                refused = None
            job = self.job
            if job is None:
                continue
            now = self.ticks()
            look = tuned is None or now - tuned >= 5
            if look:
                tuned = now
            for miner in working:
                if miner.closed:
                    continue
                why = None
                if refused:
                    why = refused.get("*") or next((refused[a] for a in miner.workers.values() if a in refused), None)
                if why:
                    self._message(miner, why)
                    self._close(miner)
                elif miner.last is not job:
                    self._give(miner, job, miner.last is None or miner.last.previous != job.previous)
                elif look and self._retune(miner, now):
                    self._give(miner, job, False)

    def _give(self, miner, job, clean):
        """Work for one miner: its difficulty and the key when they changed, then the job under an id of its own."""
        if miner.closed:
            return
        low, high = self.span or bounds(job.network, self.pool.ratio)
        miner.k = high if miner.k is None else min(max(miner.k, low), high)
        if miner.k != miner.k_sent:
            self._notify(miner, "mining.set_difficulty", [difficulty(miner.k)])
            miner.k_sent, miner.since, miner.found = miner.k, self.ticks(), []
        if job.key != miner.seed_sent:
            self._notify(miner, "mining.set_randomx_seed", [job.key[::-1].hex(), job.key_height])
            miner.seed_sent = job.key
        for old in [i for i, (j, _) in miner.jobs.items() if j.previous != job.previous]:
            del miner.jobs[old]
        miner.given += 1
        job_id = "%x" % miner.given
        miner.jobs[job_id] = (job, miner.k)
        while len(miner.jobs) > JOBS_KEPT:
            miner.jobs.popitem(last=False)
        miner.last = job
        self._notify(miner, "mining.notify", job.notify(job_id, clean))

    def _retune(self, miner, now=None):
        """Moves a miner's difficulty towards a share every target_seconds. True when it moved."""
        job = self.job
        if miner.since is None or miner.k is None or job is None:
            return False
        now = self.ticks() if now is None else now
        elapsed, n = now - miner.since, len(miner.found)
        want = miner.k
        if n >= 6:
            ideal = 32 - math.log2(max(sum(miner.found) / max(elapsed, 1.0) * self.target_seconds, 1.0))
            want = max(miner.k - 4, min(miner.k + 2, int(round(ideal))))
        elif n <= 1 and elapsed >= 4 * self.target_seconds:
            want = miner.k + 1
        low, high = self.span or bounds(job.network, self.pool.ratio)
        want = min(max(want, low), high)
        if want == miner.k:
            if elapsed >= 600:
                miner.since, miner.found = now, []      # look at the last ten minutes, not at the whole night
            return False
        miner.k = want
        return True

    # -- the line ------------------------------------------------------------------------------------------------

    def _send(self, miner, message):
        if miner.closed:
            return
        try:
            miner.writer.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        except (OSError, RuntimeError):
            return self._close(miner)
        if miner.writer.transport.get_write_buffer_size() > OUT_MOST:
            self._close(miner)              # it does not read what it is sent

    def _answer(self, miner, rid, result):
        self._send(miner, {"id": rid, "result": result, "error": None})

    def _error(self, miner, rid, code, why):
        self._send(miner, {"id": rid, "result": None, "error": [code, why, None]})

    def _notify(self, miner, method, params):
        self._send(miner, {"id": None, "method": method, "params": params})

    def _message(self, miner, text):
        self._notify(miner, "client.show_message", [text])

    def _close(self, miner):
        miner.closed = True
        try:
            miner.writer.close()
        except RuntimeError:
            pass
