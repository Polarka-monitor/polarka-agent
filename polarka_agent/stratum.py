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

A header also belongs to exactly one job id, and so to exactly one difficulty: every job handed out, a new
difficulty for the same template included, gets an issue number of its own in the coinbase after MARK, and the time
of a share is its job's (IronMiner does not roll it). Without that, a miner could hold one template under several
ids at several difficulties and claim each hash at the hardest one it meets, about four times what its work is worth
(review 11, H1); and an honest miner would hash the same headers again after a change of difficulty (M1).

A connection that is not authorized within QUIET seconds, or sends no share the pool takes for IDLE seconds, is let
go; one place holds at most PER_PLACE connections, and when all are taken the place that holds the most gives one up
to a newcomer from elsewhere (review 11, H2).
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
from .httpguard import Limiter
from .keys import BadInput, script_for_address
from .pool import PoolError, dsha

EXTRANONCE1 = 4             # bytes the pool gives a connection
EXTRANONCE2 = 4             # bytes the miner counts through
MARK = b"S"                 # in the coinbase before the extranonces: a header made here is never one made over HTTP
ISSUE = 4                   # bytes of the issue number after MARK: one coinbase, and so one header, for one job id
TARGET_SECONDS = 15         # a share about this often from a connection
JOB_REFRESH = 60            # seconds between two jobs on the same tip (new transactions, a new time)
JOBS_KEPT = 8               # jobs of one connection that still take shares, all on the newest tip
MIN_ODDS = 16               # a share is never more than this part of a block
MOST_LINE = 4096            # bytes in one line from a miner
MOST_CONNECTIONS = 2000     # each one an open file: the pool's service has LimitNOFILE=8192
PER_PLACE = 32              # connections from one place (pool.place: an IPv4 address, or an IPv6 /48)
MOST_WAITING = 200          # connections not yet authorized, in all
WAITING_PER_PLACE = 8       # and from one place
MOST_WORKERS = 4            # names authorized on one connection
QUIET = 10                  # seconds a connection has to subscribe and authorize
IDLE = 3600                 # seconds an authorized connection may go without a share the pool takes
STALE_SLOT = 300            # when all are taken, one that went this long without a share makes room for a newcomer
CONNECTS_PER_MINUTE = 20    # new connections a minute from one place
RETUNE_SHARES = 12          # shares the difficulty is decided on (or half as many over RETUNE_SECONDS)
RETUNE_SECONDS = 300
RATE, BURST = 10.0, 20.0    # shares a second one connection may send, after a burst
OUT_MOST = 64 * 1024        # bytes waiting to go out to one miner before it is let go (2000 of them: 128 MB at most)
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
    while high < EASIEST and share_target(high) < network:
        high += 1           # never a share target below the network's: a block's hash would be refused as LOW (review 11, I3)
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


def inherited():
    """The socket systemd listens on for this service (socket activation), or None. Its own IP rules then hold for
    the miners' connections, and the service's (a pool that talks only to its own machine) for everything else."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or not os.environ.get("LISTEN_FDS", "").isdigit():
        return None
    if int(os.environ["LISTEN_FDS"]) < 1:
        return None
    return socket.socket(fileno=3)          # SD_LISTEN_FDS_START


def swapped(previous):
    """The previous block's hash as stratum sends it: as the header holds it, the bytes of every four turned around."""
    raw = bytes.fromhex(previous)[::-1]
    return b"".join(raw[i:i + 4][::-1] for i in range(0, 32, 4)).hex()


def _less(counter, key):
    counter[key] -= 1
    if counter[key] <= 0:
        del counter[key]


class Job:
    """One block template cut for stratum: the coinbase before the issue number and after the extranonces, the
    branch, and what a share of it is worth. The same for every connection and every job id given of it: the issue
    number (one for every job id) and the connection's extranonce1 make the headers differ."""

    def __init__(self, number, template, key, key_height, pay_script, made):
        self.number, self.template, self.key, self.key_height, self.made = number, template, key, key_height, made
        txs = template.get("transactions", [])
        probe = bytes(range(1, ISSUE + 9))          # the issue number, then the eight bytes of the extranonces
        plain = poolmod.coinbase(template["height"], template["coinbasevalue"], pay_script,
                                 int.from_bytes(probe[ISSUE:], "little"), poolmod.commitment_of(template), False,
                                 MARK + probe[:ISSUE])
        at = plain.index(bytes([len(MARK) + ISSUE + 8]) + MARK + probe) + 1 + len(MARK)
        self.head, self.tail = plain[:at], plain[at + ISSUE + 8:]
        self.branch = branch([bytes.fromhex(t["txid"])[::-1] for t in txs])
        self.previous, self.height = template["previousblockhash"], template["height"]
        self.version, self.bits, self.time = template["version"], template["bits"], template["curtime"]
        if len(self.bits) != 8 or not 0 <= self.version <= 0xFFFFFFFF:
            raise ValueError("the template does not make a header")
        self.network = rxmod.target_of(int(self.bits, 16))
        self.reward = template["coinbasevalue"] - sum(int(t.get("fee", 0)) for t in txs)

    def coinb1(self, issue):
        """The coinbase before the extranonces, for one issue (ISSUE bytes)."""
        return self.head + issue

    def notify(self, job_id, issue, clean):
        return [job_id, swapped(self.previous), self.coinb1(issue).hex(), self.tail.hex(), [b.hex() for b in self.branch],
                "%08x" % self.version, self.bits, "%08x" % self.time, clean]

    def header(self, issue, extranonce1, extranonce2, nonce):
        """The 80 bytes a miner hashes for one issue of this job; the time is always the job's."""
        coinbase = self.head + issue + extranonce1 + extranonce2 + self.tail
        return (self.version.to_bytes(4, "little") + bytes.fromhex(self.previous)[::-1] + root(dsha(coinbase), self.branch)
                + self.time.to_bytes(4, "little") + bytes.fromhex(self.bits)[::-1] + nonce.to_bytes(4, "little"))

    def block(self, issue, extranonce1, extranonce2, header, pay_script):
        """The whole block for a header of this job: the coinbase is the one the header's merkle root was made of."""
        extranonce = int.from_bytes(extranonce1 + extranonce2, "little")
        return header + poolmod.assemble(self.template, pay_script, extranonce, MARK + issue)[1]


class Miner:
    """One connection, and what the pool knows of it."""

    def __init__(self, writer, source, extranonce1, place=None, now=0.0):
        self.writer, self.source, self.extranonce1, self.place = writer, source, extranonce1, place
        self.subscribed = self.closed = False
        self.waiting = True                         # not authorized yet
        self.came = self.active = now               # when it came, and when the pool last took a share of it
        self.workers = {}                           # the name the miner gave -> the address its shares are paid to
        self.k = self.k_sent = self.seed_sent = self.last = None
        self.jobs = collections.OrderedDict()       # a job id given to this miner -> (Job, k, issue)
        self.given = 0
        self.since, self.found = None, []           # for the difficulty: since when, and what each share since stood for
        self.tokens, self.tokens_at = BURST, None


class Stratum:
    """Listens for miners in a thread of its own (asyncio) and asks the pool, its node and its proof of work in
    worker threads, so that a slow node or a slow check holds up no connection."""

    def __init__(self, pool, listen, target_seconds=TARGET_SECONDS, refresh=JOB_REFRESH, most=MOST_CONNECTIONS,
                 per_place=PER_PLACE, welcome=None, span=None, sock=None, quiet=QUIET, idle=IDLE):
        """`listen` is HOST:PORT (":3333" for every address), or "systemd" for the socket systemd hands over;
        `sock` a listening socket made already."""
        self.sock = sock
        if listen == "systemd" and sock is None:
            self.sock = inherited()
            if self.sock is None:
                raise ValueError("--stratum systemd needs the socket of a socket unit (LISTEN_FDS); there is none")
        if self.sock is not None:
            self.host, self.port = None, self.sock.getsockname()[1]
        else:
            host, _, port = listen.rpartition(":")
            self.host, self.port = host.strip("[]") or None, int(port)
        self.pool, self.ticks = pool, pool.ticks
        self.span = span                            # (hardest k, easiest k) in place of bounds(): only for trying a miner on regtest
        self.target_seconds, self.refresh_every, self.most, self.per_place = target_seconds, refresh, most, per_place
        self.most_waiting, self.waiting_per_place = MOST_WAITING, WAITING_PER_PLACE
        self.quiet, self.idle, self.stale_slot = quiet, idle, STALE_SLOT
        self.arrivals = Limiter(CONNECTS_PER_MINUTE, clock=pool.ticks, everyone=0)     # new connections of a place
        self.welcome = welcome                      # said to a miner once it is authorized; {address} is its address
        self.lock = threading.Lock()                # the newest job and the headers taken, also from the worker threads
        self.refreshing = threading.Lock()          # one new job at a time
        self.job, self.number = None, 0
        self.taken = collections.OrderedDict()      # a tip -> the headers taken on it
        self.miners = set()
        self.places, self.waiting = collections.Counter(), collections.Counter()    # connections, and those not authorized yet, of a place
        self.next_extranonce = int.from_bytes(os.urandom(EXTRANONCE1), "big")
        self.next_issue = int.from_bytes(os.urandom(ISSUE), "big")
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
            if self.sock is not None:
                self.server = await asyncio.start_server(self._client, sock=self.sock, limit=MOST_LINE)
            else:
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
        if self.pool.guard is not None:
            try:
                self.pool.guard.check(source)
            except PoolError:
                writer.close()
                return
        if not self._room(place):
            writer.close()
            return
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
        miner = Miner(writer, source, self._extranonce(), place, self.ticks())
        self._enter(miner)
        try:
            while not miner.closed:
                wait = None
                if miner.waiting:           # QUIET seconds from when it came, however much it says meanwhile
                    wait = miner.came + self.quiet - self.ticks()
                    if wait <= 0:
                        break
                try:
                    line = await asyncio.wait_for(reader.readline(), wait)
                except (asyncio.TimeoutError, ValueError, OSError):
                    break           # not authorized in time, a line too long, or the line is gone
                if not line.endswith(b"\n") or not await self._said(miner, line):
                    break
        finally:
            self._close(miner)

    def _enter(self, miner):
        self.miners.add(miner)
        self.places[miner.place] += 1
        if miner.waiting:
            self.waiting[miner.place] += 1

    def _forget(self, miner):
        """A connection that ended is no longer counted; once, whoever ends it."""
        if miner not in self.miners:
            return
        self.miners.discard(miner)
        _less(self.places, miner.place)
        if miner.waiting:
            _less(self.waiting, miner.place)

    def _authorized(self, miner):
        if miner.waiting and miner in self.miners:
            _less(self.waiting, miner.place)
        miner.waiting = False

    def _room(self, place):
        """Whether a new connection from `place` may come in (review 11, H2; review 11b, N7). A place opens at most
        CONNECTS_PER_MINUTE connections a minute, holds at most per_place, and at most waiting_per_place that are not
        authorized yet. When those not authorized fill their table, the one that came first goes: an honest miner
        authorizes in well under a second, so whoever holds that table must keep coming faster than that. When all
        connections are taken, the one that went longest without a share makes room once that is stale_slot or more;
        else the place that holds the most gives one up to a newcomer from a place that holds at least two fewer. The
        pool's own machine (place None) is nobody's place, as for the guard."""
        if place is not None:
            if not self.arrivals.take(place):
                return False
            if self.places[place] >= self.per_place or self.waiting[place] >= self.waiting_per_place:
                return False
        if sum(self.waiting.values()) >= self.most_waiting:
            first = min((m for m in self.miners if m.waiting and not m.closed), key=lambda m: m.came, default=None)
            if first is not None:
                self._let_go(first, "the pool is busy: a connection that did not authorize in time makes room")
        if len(self.miners) >= self.most and not self._make_room(place):
            return False
        return True

    def _make_room(self, place):
        """All connections are taken: lets one go for `place`. False: there is no room for it."""
        stalest = min((m for m in self.miners if not m.waiting and not m.closed), key=lambda m: m.active, default=None)
        if stalest is not None and self.ticks() - stalest.active >= self.stale_slot:
            self._let_go(stalest, "the pool is full and this connection sent no share for %d minutes: it makes room"
                         % (self.stale_slot // 60))
            return True
        held = self.places
        most = max((p for p in held if p is not None), key=held.__getitem__, default=None)
        if most is None or held[most] < held[place] + 2:
            return False
        idle = [m for m in self.miners if m.place == most and not m.closed]
        if not idle:
            return False
        self._let_go(min(idle, key=lambda m: m.active),
                     "the pool is full: a connection of a place that holds many makes room for a miner from elsewhere")
        return True

    def _let_go(self, miner, why):
        self._message(miner, why)
        self._close(miner)

    def _extranonce(self):
        self.next_extranonce = (self.next_extranonce + 1) % (1 << (8 * EXTRANONCE1))
        return self.next_extranonce.to_bytes(EXTRANONCE1, "big")

    def _issue(self):
        """A number of its own for every job id given: between MARK and the extranonces in the coinbase, so that no
        header belongs to two job ids (review 11, H1)."""
        self.next_issue = (self.next_issue + 1) % (1 << (8 * ISSUE))
        return self.next_issue.to_bytes(ISSUE, "big")

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
        try:
            address = await self._call(self._may_mine, miner.source, name.split(".", 1)[0].strip())
        except PoolError as e:
            self._message(miner, str(e))
            return self._error(miner, rid, 24, str(e))
        if miner.closed:
            return
        first = not miner.workers
        miner.workers[name] = address
        self._answer(miner, rid, True)
        if not first:
            return
        self._authorized(miner)
        miner.active = self.ticks()
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
        """In a worker thread: the pool's rules for whoever asks for work, as pool.work() keeps them: the address
        as it was given is checked, and then written small (review 11, I2). Returns the address written small."""
        p = self.pool
        try:
            script_for_address(p.chain, address)
        except BadInput as e:
            raise PoolError("ADDRESS", "the user name is your address of this network, if you like with .worker after it: %s" % e)
        address = address.lower()
        if p.miners is not None and address not in p.miners:
            raise PoolError("CLOSED", "this pool takes only its owner's miners for now; it will open to others later", 403)
        if p.cap is not None and p.cap.is_closed():
            raise PoolError("CAP", "the pool has found as large a part of the network's blocks as it allows itself and hands out "
                                   "no work until that part is smaller; mine alone or elsewhere meanwhile", 503)
        p.look()                    # the node is asked outside the pool's lock (review 11, L2)
        with p.lock:
            if p.template is None:
                raise PoolError("PAUSED", "the pool's node does not answer, so no work is handed out", 503)
            if p.guard is not None:
                p.guard.asked(source, address, p.template["previousblockhash"])
            p._may_work(address)
        return address

    def _suggest(self, miner, params):
        """A miner may say what difficulty it would like; the pool starts there, within its bounds."""
        try:
            wanted = float(params[0])
        except (IndexError, TypeError, ValueError, OverflowError):     # a number of 309 digits (review 11, L1)
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
        job, k, issue = held
        try:
            entry = await self.loop.run_in_executor(self.checks, functools.partial(
                self._check, miner.extranonce1, address, job, k, issue, params[2], params[3], params[4]))
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
        miner.active = self.ticks()
        if entry["accepted"]:
            self.wake.set()
        if self._retune(miner) and self.job is not None:
            self._give(miner, self.job)

    def _check(self, extranonce1, address, job, k, issue, extranonce2, ntime, nonce):
        """In a worker thread: what was written down for a share, or PoolError."""
        p = self.pool
        if not (HEX8.match(extranonce2) and HEX8.match(ntime) and HEX8.match(nonce)):
            raise PoolError("INPUT", "extranonce2, time and nonce are eight hexadecimal digits each")
        extranonce2, ntime, nonce = bytes.fromhex(extranonce2), int(ntime, 16), int(nonce, 16)
        if ntime != job.time:               # IronMiner never moves it; one header, one job id (review 11, H1)
            raise PoolError("INPUT", "the time of a share is its job's")
        tip = p.look(template=False)        # the node's tip, asked at most every TIP_AGE, outside the pool's lock
        header = job.header(issue, extranonce1, extranonce2, nonce)
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
                refused = p.node.call("submitblock", job.block(issue, extranonce1, extranonce2, header, p.pay_script).hex())
                entry["accepted"] = refused is None
                if refused is not None:
                    entry["refused"] = str(refused)[:60]
            except OSError as e:
                entry["accepted"], entry["refused"] = False, "the node did not take it: %s" % str(e)[:60]
        p._write(entry)                     # first on disk, then in the sums
        with p.lock:
            p._count(entry)
            if entry["accepted"]:
                p._found(entry["block"])    # the tip has moved: the next look asks the node
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
            p.look()                        # the node is asked outside the pool's lock (review 11, L2)
            with p.lock:
                template, key, key_height, best = p.template, p.key, p.key_height, p.best
            if template is None or (best is not None and template["previousblockhash"] != best):
                return None                 # the node is silent for a moment, or has not yet given work on its new tip
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
            self._let_go_idle(self.ticks())
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
                    self._give(miner, job)
                elif look and self._retune(miner, now):
                    self._give(miner, job)

    def _let_go_idle(self, now):
        """Connections that sent no share the pool took for `idle` seconds end: a slot is not held for nothing
        (review 11, H2). Those not authorized yet end after `quiet` seconds, in _client."""
        for miner in [m for m in self.miners if m.workers and not m.closed and now - m.active >= self.idle]:
            self._message(miner, "no share for %d minutes: the pool lets this connection go; connect again to mine"
                          % (self.idle // 60))
            self._close(miner)

    def _give(self, miner, job, clean=None):
        """Work for one miner: its difficulty and the key when they changed, then the job under an id of its own and
        with an issue number of its own in the coinbase, so that no header is valid under two ids and so at two
        difficulties (review 11, H1). Clean, unless the caller says, when the tip moved since the miner's last job
        (review 11, I4)."""
        if miner.closed:
            return
        if clean is None:
            clean = miner.last is None or miner.last.previous != job.previous
        low, high = self.span or bounds(job.network, self.pool.ratio)
        miner.k = high if miner.k is None else min(max(miner.k, low), high)
        if miner.k != miner.k_sent:
            self._notify(miner, "mining.set_difficulty", [difficulty(miner.k)])
            miner.k_sent, miner.since, miner.found = miner.k, self.ticks(), []
        if job.key != miner.seed_sent:
            self._notify(miner, "mining.set_randomx_seed", [job.key[::-1].hex(), job.key_height])
            miner.seed_sent = job.key
        for old in [i for i, (j, _, _) in miner.jobs.items() if j.previous != job.previous]:
            del miner.jobs[old]
        miner.given += 1
        job_id, issue = "%x" % miner.given, self._issue()
        miner.jobs[job_id] = (job, miner.k, issue)
        while len(miner.jobs) > JOBS_KEPT:
            miner.jobs.popitem(last=False)
        miner.last = job
        self._notify(miner, "mining.notify", job.notify(job_id, issue, clean))

    def _retune(self, miner, now=None):
        """Moves a miner's difficulty towards a share every target_seconds, on enough shares to tell and only by a
        whole step or more (review 11, M1): RETUNE_SHARES of them, or half as many over RETUNE_SECONDS; a miner
        with at most one share in four times target_seconds gets easier work. True when it moved."""
        job = self.job
        if miner.since is None or miner.k is None or job is None:
            return False
        now = self.ticks() if now is None else now
        elapsed, n = now - miner.since, len(miner.found)
        want = miner.k
        if n >= RETUNE_SHARES or (n >= RETUNE_SHARES // 2 and elapsed >= RETUNE_SECONDS):
            ideal = 32 - math.log2(max(sum(miner.found) / max(elapsed, 1.0) * self.target_seconds, 1.0))
            if abs(ideal - miner.k) >= 1:
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
        self._forget(miner)
        try:
            miner.writer.close()
        except RuntimeError:
            pass
