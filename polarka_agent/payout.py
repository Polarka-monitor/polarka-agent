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

A reward of a block can be spent after 100 blocks; the wallet does not count such coins before, so a round
that the ripe coins do not cover is simply not made yet.
"""
import json
import os
import threading
import time

from .wallet import OPEN, Refused, north

UNITS = 100_000_000
MOST_PAYEES = 200
MARGIN = UNITS // 100           # left in the wallet for the round's fee when the round is cut to what is there


class Payouts:
    def __init__(self, wallet, folder, minimum=UNITS, most_payees=MOST_PAYEES, clock=time.time):
        self.wallet, self.minimum, self.most_payees, self.clock = wallet, minimum, most_payees, clock
        self.path = os.path.join(folder, "pool-payouts.jsonl")
        self.lock = threading.Lock()
        self.rounds = {}            # number -> {"pay": {address: units}, "txids": [...], "state": "open" | "paid" | "void"}
        if os.path.exists(self.path):
            with open(self.path) as f:
                for number, line in enumerate(f, 1):
                    try:
                        self._apply(json.loads(line))
                    except (ValueError, KeyError, TypeError):
                        raise ValueError("line %d of %s cannot be read; nothing is paid out over a damaged ledger" % (number, self.path))

    def _apply(self, e):
        if e["event"] == "round":
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
            f.flush()
            os.fsync(f.fileno())
        self._apply(entry)

    def memo(self, number):
        """The note under which the wallet keeps the round's payments. The mark is made anew for every round, so
        that a ledger begun again never takes an old payment of the wallet for one of its own."""
        return "polarka-pool round %d %s" % (number, self.rounds[number]["mark"])

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

    def _settle(self):
        """Looks at the round that is on the way, if there is one. Returns it while it still is on the way."""
        number = next((n for n, r in self.rounds.items() if r["state"] == "open"), None)
        if number is None:
            return None
        r = self.rounds[number]
        mine = [p for p in self.wallet.payments() if p["memo"] == self.memo(number)]
        for p in mine:
            if p["id"] not in r["txids"]:
                self._write({"event": "sent", "round": number, "txid": p["id"]})
        if not mine:
            # written down here and never signed there: nothing left this machine
            self._write({"event": "void", "round": number, "why": "never signed"})
            return None
        if any(p["state"] == "in_block" for p in mine):
            self._write({"event": "paid", "round": number})
            return None
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
        {address: earned units}. Returns what it did."""
        with self.lock:
            waiting = self._settle()
        if waiting is not None:
            return {"round": waiting, "state": "on the way", "paid_now": north(0)}
        sums = self.sums()
        owed = {a: e - sum(sums.get(a, (0, 0))) for a, e in earned.items()}
        if any(v < 0 for v in owed.values()):
            raise ValueError("more was paid than was earned; the ledgers do not agree and nothing is paid")
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
            if total + value <= budget:
                pay.append((address, value))
                total += value
        if not pay:
            return {"round": None, "state": "neither the ripe coins nor the limit for one payment cover anybody yet", "paid_now": north(0)}
        pay.sort()
        with self.lock:
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
