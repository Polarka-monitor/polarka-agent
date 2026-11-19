"""Paying out what a pool owes, in rounds, from a wallet of this program and within that wallet's limits.json.

One round is one transaction to many addresses. The rules that keep an address from being paid twice:

- A round is written into pool-payouts.jsonl before the wallet is asked to pay, and what it holds counts as
  on the way from that line on, so it is not put into another round.
- Only one round is on the way at a time. The next one starts when the last one is in a block or dead.
- After a fall in the middle of a round, the wallet's own journal says what happened: the wallet writes a
  payment down before anything leaves the machine, under a note that names the round. A round with no such
  payment was never signed and is taken back; one with a payment goes on from where it stood.
- A round whose payment did not go through is tried again through the wallet, which builds every new payment
  on a coin of the one before, so that only one of them can ever be in a block. A payment that waits for a
  block is left alone.
- The two books must agree before anything is paid: every payment the ledger names must be in the wallet's
  journal, and every payment the wallet made for a round must be in the ledger. A ledger that was lost or
  put back from an older copy, or a wallet without its journal, stops the payouts; a person has to look.
- One process at a time works over a folder (hold_folder).

A reward of a block can be spent after 100 blocks; the wallet does not count such coins before, so a round
that the ripe coins do not cover in full pays what they cover.
"""
import fcntl
import json
import os
import threading
import time

from .wallet import OPEN, Refused, north

UNITS = 100_000_000
MOST_PAYEES = 200
MARGIN = UNITS // 100           # left in the wallet for the round's fee when the round is cut to what is there
NOTE = "polarka-pool round "

_held = {}
_held_lock = threading.Lock()


class BooksDisagree(ValueError):
    """The payout ledger and the wallet's journal tell different stories. Nothing is paid until a person has looked."""


def hold_folder(folder):
    """Takes the folder for this process, for as long as it lives. A second process over the same folder could
    pay the same round a second time, so it is refused."""
    real = os.path.realpath(folder)
    with _held_lock:
        if real in _held:
            return
        os.makedirs(real, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(real, "pool.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ValueError("another pool process works over %s; two would pay the same shares twice" % real)
        _held[real] = fd


def sync(f):
    """On disk before the next step. On macOS fsync alone leaves the data in the drive's cache."""
    f.flush()
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(f.fileno())


def sync_folder(folder):
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class Payouts:
    def __init__(self, wallet, folder, minimum=UNITS, most_payees=MOST_PAYEES, clock=time.time, depth=None, deep=6):
        hold_folder(folder)
        self.wallet, self.minimum, self.most_payees, self.clock = wallet, max(int(minimum), 1000), most_payees, clock
        self.folder, self.path = folder, os.path.join(folder, "pool-payouts.jsonl")
        self.depth, self.deep = depth, deep     # depth(txid): blocks on top of it by the pool's own node
        self.lock = threading.RLock()
        self.rounds = {}            # number -> {"pay": {address: units}, "txids": [...], "state": "open" | "paid" | "void", "mark"}
        if not os.path.exists(self.path):
            shares = os.path.join(folder, "pool-shares.jsonl")
            if os.path.exists(shares) and os.path.getsize(shares):
                raise BooksDisagree("%s is missing while shares are written down; if nothing was ever paid for them, "
                                    "make it by hand with the one line {\"event\": \"begin\"}" % self.path)
            with open(self.path, "a") as f:
                f.write(json.dumps({"event": "begin", "t": int(self.clock())}) + "\n")
                sync(f)
            sync_folder(folder)
        with open(self.path) as f:
            for number, line in enumerate(f, 1):
                try:
                    self._apply(json.loads(line))
                except (ValueError, KeyError, TypeError):
                    raise ValueError("line %d of %s cannot be read; nothing is paid out over a damaged ledger" % (number, self.path))

    def _apply(self, e):
        if e["event"] == "begin":
            return
        if e["event"] == "round":
            if e["round"] in self.rounds:
                raise ValueError("round %d is written down twice" % e["round"])
            self.rounds[e["round"]] = {"pay": {a: int(v) for a, v in e["pay"]}, "txids": [], "state": "open", "t": e["t"],
                                       "mark": str(e["mark"])}
        elif e["event"] == "sent":
            if e["txid"] not in self.rounds[e["round"]]["txids"]:
                self.rounds[e["round"]]["txids"].append(e["txid"])
        elif e["event"] in ("paid", "void"):
            self.rounds[e["round"]]["state"] = e["event"]
        else:
            raise ValueError

    def _write(self, entry):
        entry = dict(entry, t=int(self.clock()))
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
            sync(f)
        self._apply(entry)

    def memo(self, number):
        """The note under which the wallet keeps the round's payments."""
        return "%s%d %s" % (NOTE, number, self.rounds[number]["mark"])

    def sums(self):
        """{address: (paid, on the way)} in base units."""
        with self.lock:
            out = {}
            for r in self.rounds.values():
                if r["state"] == "void":
                    continue
                for address, value in r["pay"].items():
                    paid, flying = out.get(address, (0, 0))
                    out[address] = (paid + value, flying) if r["state"] == "paid" else (paid, flying + value)
            return out

    def _books(self):
        """The wallet's payments, after the two books were held against each other."""
        made = self.wallet.payments()
        ids = {p["id"] for p in made}
        named = {t for r in self.rounds.values() for t in r["txids"]}
        if named - ids:
            raise BooksDisagree("the ledger names %d payment(s) that the wallet's journal does not know; the journal was lost or "
                                "is another wallet's" % len(named - ids))
        notes = {self.memo(n) for n, r in self.rounds.items() if r["state"] == "open"}
        strangers = [p["id"] for p in made if str(p.get("memo") or "").startswith(NOTE) and p["id"] not in named
                     and p.get("memo") not in notes]
        if strangers:
            raise BooksDisagree("the wallet made %d payment(s) for rounds that the ledger does not know; the ledger was lost or "
                                "put back from an older copy" % len(strangers))
        return made

    def _settle(self):
        """Looks at the round that is on the way, if there is one. Returns it while it still is on the way."""
        made = self._books()
        number = next((n for n, r in self.rounds.items() if r["state"] == "open"), None)
        if number is None:
            return None
        r = self.rounds[number]
        mine = [p for p in made if p["memo"] == self.memo(number)]
        for p in mine:
            if p["id"] not in r["txids"]:
                self._write({"event": "sent", "round": number, "txid": p["id"]})
        if not mine:
            # Written down here and never signed there: nothing left this machine. (_books has made sure that
            # the wallet's journal is the one that knows the earlier rounds.)
            self._write({"event": "void", "round": number, "why": "never signed"})
            return None
        if any(p["state"] == "in_block" for p in mine):
            # The wallet has a server's word for it. With a node of the pool's own, the round is paid when that
            # node sees the payment `deep` blocks down; until then it is on the way, also if a block was undone.
            if self.depth is None or max(self.depth(p["id"]) for p in mine if p["state"] == "in_block") >= self.deep:
                self._write({"event": "paid", "round": number})
                return None
            return number
        if any(p["state"] == "handed" for p in mine):
            return number                                   # it waits for a block: left alone
        if any(p["state"] in OPEN for p in mine):
            try:
                again = self.wallet.pay(sorted(r["pay"].items()), self.memo(number))
                if again["id"] not in r["txids"]:
                    self._write({"event": "sent", "round": number, "txid": again["id"]})
            except Refused:
                pass
            return number
        # every payment of the round is overtaken or forgotten: none of them can be in a block any more
        self._write({"event": "void", "round": number, "why": "its payments are dead"})
        return None

    def run(self, earned):
        """One step: settles the round on the way, and if none is left on the way, makes the next one from
        {address: earned units}. Returns what it did. One caller at a time."""
        with self.lock:
            waiting = self._settle()
            if waiting is not None:
                return {"round": waiting, "state": "on the way", "paid_now": north(0)}
            sums = self.sums()
            owed = {a: e - sum(sums.get(a, (0, 0))) for a, e in earned.items()}
            if any(v < 0 for v in owed.values()):
                raise BooksDisagree("more was paid than was earned; the ledgers do not agree and nothing is paid")
            due = sorted(((v, a) for a, v in owed.items() if v >= self.minimum), reverse=True)[:self.most_payees]
            if not due:
                return {"round": None, "state": "nothing is due", "paid_now": north(0)}
            try:
                limits, balance = self.wallet.limits(), self.wallet.balance()
            except Refused as e:
                return {"round": None, "state": "refused", "why": e.kind, "paid_now": north(0)}
            if limits is None:
                return {"round": None, "state": "refused", "why": "NO_LIMITS", "paid_now": north(0)}
            budget = min(balance["spendable"], limits["per_payment"]) - MARGIN
            pay, total = [], 0
            for value, address in due:
                part = min(value, budget - total)       # who is owed more than there is gets what there is
                if part >= self.minimum:
                    pay.append((address, part))
                    total += part
            if not pay:
                return {"round": None, "state": "neither the ripe coins nor the limit for one payment cover anybody yet", "paid_now": north(0)}
            pay.sort()
            number = max(self.rounds, default=0) + 1
            try:
                self.wallet.pay(pay, "polarka-pool dry run", dry_run=True)
            except Refused as e:
                return {"round": None, "state": "refused", "why": e.kind, "message": str(e)[:200], "paid_now": north(0)}
            self._write({"event": "round", "round": number, "pay": pay, "mark": os.urandom(4).hex()})
            try:
                made = self.wallet.pay(pay, self.memo(number))
            except Refused as e:
                self._write({"event": "void", "round": number, "why": "refused: %s" % e.kind})
                return {"round": number, "state": "refused", "why": e.kind, "paid_now": north(0)}
            self._write({"event": "sent", "round": number, "txid": made["id"]})
            return {"round": number, "state": made["state"], "id": made["id"], "payees": len(pay), "paid_now": north(total)}
