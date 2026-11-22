"""A TrueNorth wallet in a folder, for a program to use.

  wallet.json    the chain, the wallet server and the recovery words (only its owner may read the file)
  limits.json    what the wallet may pay by itself; written by its owner, never by this program; without it
                 the wallet pays nothing. {"per_payment_north": "5", "per_day_north": "20"}, and if wanted
                 "allow": [addresses that may be paid] and "fee_rate": base units per virtual byte
  journal.jsonl  every transaction this wallet signed and what became of it; lines are only ever added
  state.json     which receiving addresses were handed out

The rules for paying are those of the Polarka Wallet for Android, which an independent reviewer looked at.
A payment is written down before anything leaves the machine. Whoever was shown a signed transaction can
keep it and pass it on later, whatever they answered, so a payment that a server refused or lost is not
forgotten: it stays in the journal as one that did not go through, and the next payment is built on one of
its coins, so that only one of the two can ever be in a block. Nothing is concluded from a coin that a
server does not list.
"""
import fcntl
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation

from . import keys, tx
from .server import MOVED, Server, ServerError, reason_code

GAP = 20                # unused addresses in a row after which a wallet is taken to end
WINDOW = 40             # addresses of each kind that are looked at to begin with
MAX_WINDOW = 200
DAY = 24 * 3600
LONG_WAIT = 3600        # a payment that has waited this long for a block is named
STAMP = b"POLARKA\x01"  # what a stamp's data begins with; the SHA-256 that is stamped follows
OPEN = ("signed", "unknown", "kept")        # written down, and nobody can say that it is in a block or dead
UNITS = keys.UNIT


class Refused(Exception):
    """Something this wallet does not do. kind is a short word a program can act on."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def units(text):
    """An amount in NORTH, written as a decimal number ("1.5"), as base units. Exact, or refused."""
    if not isinstance(text, str):
        raise Refused("AMOUNT", "an amount is written as text, a decimal number of NORTH, for example \"1.5\"")
    text = text.strip()
    if not re.match(r"^[0-9]{1,12}(\.[0-9]{1,8})?$", text):
        raise Refused("AMOUNT", "an amount is a number of NORTH with at most 8 decimals, for example 1.5")
    try:
        value = int(Decimal(text) * UNITS)
    except InvalidOperation:
        raise Refused("AMOUNT", "this is not an amount")
    return value


def north(value):
    return "%d.%08d" % (value // UNITS, value % UNITS)


def _write_new(path, text, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


class Wallet:
    def __init__(self, folder, server=None, clock=time.time):
        self.folder, self._clock = os.path.abspath(folder), clock
        path = os.path.join(self.folder, "wallet.json")
        if not os.path.isfile(path):
            raise Refused("NO_WALLET", "there is no wallet in %s" % self.folder)
        if os.stat(path).st_mode & 0o077:
            raise Refused("OPEN_FILE", "wallet.json can be read by others than its owner; set its mode to 600 first")
        with open(path) as f:
            info = json.load(f)
        if info.get("version") != 1 or info.get("chain") not in keys.CHAINS:
            raise Refused("NO_WALLET", "wallet.json is not a wallet of this program")
        self.chain = info["chain"]
        self.account = keys.Account.from_words(info["words"], self.chain)
        try:
            self.server = server or Server(info["server"])
        except (ValueError, KeyError, TypeError) as e:
            raise Refused("SERVER", "wallet.json names no usable server: %s" % e)
        self._lock = None

    # ------------------------------------------------------------------------------------------ making one

    @classmethod
    def create(cls, folder, chain, server_url, words=None, count=24):
        """Makes the folder and the wallet in it. Returns the recovery words, the one time they are shown."""
        if chain not in keys.CHAINS:
            raise Refused("CHAIN", "the chain is main or regtest")
        try:
            Server(server_url).close()                  # refuses an address that is not one
        except ValueError as e:
            raise Refused("SERVER", str(e))
        words = keys.new_words(count) if words is None else " ".join(words.split())
        wrong = keys.check_words(words)
        if wrong:
            raise Refused("WORDS", wrong)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, "wallet.json")
        if os.path.exists(path):
            raise Refused("EXISTS", "there is a wallet in this folder already; it is not replaced")
        _write_new(path, json.dumps({"version": 1, "chain": chain, "server": server_url.strip().rstrip("/"), "words": words}, indent=1) + "\n")
        return words

    # ------------------------------------------------------------------------------------------ files

    def _path(self, name):
        return os.path.join(self.folder, name)

    def __enter__(self):
        """One program at a time works with a wallet folder."""
        self._lock = os.fdopen(os.open(self._path("lock"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()
        self._lock = None

    def _state(self):
        try:
            with open(self._path("state.json")) as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state):
        tmp = self._path("state.json.tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path("state.json"))

    def limits(self):
        """What the owner lets the wallet pay by itself, or None when there is no limits.json."""
        try:
            with open(self._path("limits.json")) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise Refused("LIMITS", "limits.json cannot be read; nothing is paid until it can")
        try:
            limits = {"per_payment": units(raw["per_payment_north"]), "per_day": units(raw["per_day_north"]),
                      "fee_rate": raw.get("fee_rate", tx.DEFAULT_FEE_RATE), "allow": raw.get("allow")}
        except (KeyError, TypeError, Refused):
            raise Refused("LIMITS", "limits.json needs per_payment_north and per_day_north, amounts of NORTH written as text (\"5\", \"0.5\")")
        if type(limits["fee_rate"]) is not int or not tx.MIN_FEE_RATE <= limits["fee_rate"] <= tx.MAX_FEE_RATE:
            raise Refused("LIMITS", "fee_rate in limits.json is a whole number from %d to %d" % (tx.MIN_FEE_RATE, tx.MAX_FEE_RATE))
        if limits["allow"] is not None:
            if type(limits["allow"]) is not list:
                raise Refused("LIMITS", "allow in limits.json is a list of addresses")
            try:
                limits["allow"] = {keys.script_for_address(self.chain, a) for a in limits["allow"]}
            except keys.BadInput as e:
                raise Refused("LIMITS", "an address under allow in limits.json is not one: %s" % e)
        return limits

    # ------------------------------------------------------------------------------------------ the journal

    def _journal(self):
        """The payments of this wallet, in the order they were made: id -> the record with its state."""
        payments = {}
        try:
            with open(self._path("journal.jsonl")) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return payments
        for n, text in enumerate(lines, 1):
            try:
                e = json.loads(text)
                if e["event"] == "signed":
                    payments[e["txid"]] = dict(e, state="signed", note=None, settled=None)
                else:
                    payments[e["txid"]].update(state=e["state"], note=e.get("note"), settled=e["t"])
                    if "counted" in e:          # shown again a day or more after it was made: it counts for that day
                        payments[e["txid"]]["t"] = int(e["counted"])
            except (ValueError, KeyError, TypeError):
                # a journal that cannot be read is not guessed at: what it holds decides what may be paid
                raise Refused("JOURNAL", "line %d of journal.jsonl cannot be read; nothing is paid until that is put right" % n)
        return payments

    def _note(self, event):
        with os.fdopen(os.open(self._path("journal.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
            f.flush()
            if hasattr(fcntl, "F_FULLFSYNC"):       # macOS: fsync alone leaves the line in the drive's cache
                try:
                    fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)
                except OSError:
                    os.fsync(f.fileno())
            else:
                os.fsync(f.fileno())

    def _count_today(self, payment, now):
        """A payment shown again a day or more after it was made can still arrive now: from here on it counts for
        this day (first audit, F6). The line keeps the shape older versions read: a state that does not change."""
        self._note({"t": int(now), "event": "state", "txid": payment["txid"], "state": payment["state"],
                    "note": payment.get("note"), "counted": int(now)})
        payment["t"] = int(now)

    def _set(self, payment, state, note=None):
        if payment["state"] != state or payment.get("note") != note:
            self._note({"t": int(self._clock()), "event": "state", "txid": payment["txid"], "state": state, "note": note})
            payment.update(state=state, note=note)

    # ------------------------------------------------------------------------------------------ looking

    def _entries(self, window):
        return [(b, i) for b in (0, 1) for i in range(window)]

    def look(self):
        """What the server shows for this wallet at one block: the height, the coins, the keys in use."""
        state = self._state()
        window = min(MAX_WINDOW, max(WINDOW, int(state.get("window", WINDOW))))
        handed = int(state.get("handed", 0))
        moved = 0
        while True:
            entries = self._entries(window)
            by_address = {self.account.address(b, i): (b, i) for b, i in entries}
            by_script = {self.account.script(b, i).hex(): (b, i) for b, i in entries}
            try:
                tip, block, used = self.server.activity(list(by_address))
                tip2, block2, listed = self.server.utxos(list(by_address))
                if (tip, block) != (tip2, block2):
                    raise ServerError(200, "a block arrived between two questions to the server", MOVED)
            except ServerError as e:
                moved += 1
                if e.kind == MOVED and moved <= 3:
                    continue
                raise
            last = {0: -1, 1: -1}
            for address in used:
                b, i = by_address[address]
                last[b] = max(last[b], i)
            need = max(last[0], last[1], handed - 1) + 1 + GAP
            if need > window and window < MAX_WINDOW:
                window = min(MAX_WINDOW, (need + GAP - 1) // GAP * GAP)
                continue
            coins = []
            for c in listed:
                where = by_script.get(c["script"])
                if where is None:
                    # the server is trusted for what exists, never for what is ours
                    raise ServerError(200, "the server listed a coin that does not pay to this wallet")
                if c["amount"] == 0:
                    continue    # an output of nothing can be in a block; it is no coin, and anybody can make one
                if c["amount"] < 0 or c["height"] < 1:
                    raise ServerError(200, "the server listed a coin that cannot exist")
                coins.append(tx.Coin(c["txid"], c["vout"], c["amount"], bytes.fromhex(c["script"]), c["height"], c["coinbase"], where[0], where[1]))
            if window != state.get("window"):
                state["window"] = window
                self._save_state(state)
            # beyond: the wallet uses keys so far out that this program does not look at all it may hold
            return {"tip": tip, "block": block, "coins": coins, "last": last, "window": window, "addresses": by_address, "beyond": need > window}

    def fresh_address(self):
        """A receiving address that was never handed out and never paid to."""
        with self:
            seen = self.look()
            state = self._state()
            index = max(seen["last"][0] + 1, int(state.get("handed", 0)))
            if index >= seen["window"]:
                raise Refused("TOO_MANY_ADDRESSES", "this wallet has used %d receiving addresses, as many as this program looks at; "
                                                    "a coin paid to a further one would not be seen. Use an address handed out before." % seen["window"])
            if index - (seen["last"][0] + 1) >= GAP:
                raise Refused("TOO_MANY_OPEN", "%d addresses in a row were handed out and none was paid to; a wallet brought back "
                                               "from its words would not find coins beyond them. Use one of them first." % GAP)
            state["handed"] = index + 1
            self._save_state(state)
            return self.account.address(0, index)

    def balance(self):
        with self:
            seen = self.look()
            payments = self._journal()
            self._settle(payments, seen)
            aside = {(t, n) for p in payments.values() if p["state"] in OPEN + ("handed",) for t, n, _ in p["inputs"]}
            ripe = [c for c in seen["coins"] if tx.spendable(c, seen["tip"])]
            free = [c for c in ripe if (c.txid, c.vout) not in aside]
            unripe = [c for c in seen["coins"] if not tx.spendable(c, seen["tip"])]
            return {"height": seen["tip"], "spendable": sum(c.amount for c in free), "set_aside": sum(c.amount for c in ripe) - sum(c.amount for c in free),
                    "unripe": sum(c.amount for c in unripe), "coins": len(seen["coins"]), "beyond": seen["beyond"],
                    "open_payments": [self._shown(p) for p in payments.values() if p["state"] in OPEN + ("handed",)]}

    def received(self, address):
        """What sits on one of the wallet's own addresses, in blocks: the amount and how deep its newest coin is.
        A coin that was spent since is no longer counted, and a payment that waits for a block is not yet."""
        with self:
            seen = self.look()
            if address not in seen["addresses"]:
                raise Refused("NOT_OURS", "this is not one of the addresses this wallet looks at")
            script = self.account.script(*seen["addresses"][address])
            coins = [c for c in seen["coins"] if c.script == script]
            return {"height": seen["tip"], "amount": sum(c.amount for c in coins), "coins": len(coins),
                    "confirmations": min((seen["tip"] - c.height + 1 for c in coins), default=0)}

    def limits_shown(self):
        limits = self.limits()
        if limits is None:
            return {"pays": False, "why": "the wallet has no limits.json; its owner writes that file"}
        return {"pays": True, "per_payment": north(limits["per_payment"]), "per_day": north(limits["per_day"]), "fee_rate": limits["fee_rate"],
                "only_to": None if limits["allow"] is None else sorted(keys.address_for_script(self.chain, s) for s in limits["allow"])}

    def history(self, most=200):
        """The wallet's movements as the server sums them up. Exact while the wallet's addresses fit one request."""
        with self:
            seen = self.look()
            if len(seen["addresses"]) > 100:
                raise Refused("TOO_MANY_ADDRESSES", "this wallet looks at more than a hundred addresses; a server cannot sum up its history exactly")
            tip, block, lines, complete = self.server.history(list(seen["addresses"]), most)
            return {"height": tip, "complete": complete, "lines": lines}

    # ------------------------------------------------------------------------------------------ what became of payments

    def _shown(self, p):
        return {"id": p["txid"], "state": p["state"], "sent": north(p["sent"]), "fee": north(p["fee"]), "to": p["to"], "memo": p.get("memo"),
                "made": p["t"], "waits_long": p["state"] == "handed" and self._clock() - p["t"] >= LONG_WAIT, "note": p.get("note")}

    def _settle(self, payments, seen):
        """Asks the server about every payment that is not known to be in a block or dead, and writes down
        what it shows. A payment is in a block when the server lists it there. It is dead when a block shows
        a later payment of this wallet that spends one of its coins. Nothing else settles it."""
        todo = [p for p in payments.values() if p["state"] in OPEN + ("handed",)]
        if not todo:
            return
        script_address = {self.account.script(b, i).hex(): a for a, (b, i) in seen["addresses"].items()}
        for p in todo:
            mine = [script_address[s] for s in p["input_scripts"] if s in script_address]
            if len(mine) != len(p["input_scripts"]):
                continue        # its coins sit on keys this look does not cover: nothing can be said, and nothing is
            try:
                # a payment is in the history of every address it spends from: one of them is enough, and the
                # reading stops at the page that holds it (human review, 22 November)
                tip, block, lines, complete = self.server.history([sorted(set(mine))[0]], 2000, until=p["txid"])
            except ServerError:
                continue
            line = next((m for m in lines if m["txid"] == p["txid"]), None)
            if line is not None and line["height"] is not None:
                self._set(p, "in_block", "block %d" % line["height"])
            elif line is not None:
                if p["state"] != "handed":
                    self._set(p, "handed", "the server shows it waiting for a block")
            elif p["state"] == "handed" and self._clock() - p["t"] >= 60:
                # It was handed over and is neither in a block nor waiting. If the server does not know it
                # either, it is open again: the next payment is built on one of its coins.
                try:
                    if not self.server.known(p["txid"], 20):
                        self._set(p, "unknown", "the server no longer shows it")
                except ServerError:
                    pass
        in_block = [p for p in payments.values() if p["state"] == "in_block"]
        for p in payments.values():
            if p["state"] in OPEN + ("handed",):
                coins = {(t, n) for t, n, _ in p["inputs"]}
                for q in in_block:
                    if q["txid"] != p["txid"] and coins & {(t, n) for t, n, _ in q["inputs"]}:
                        self._set(p, "overtaken", "another payment of this wallet that spends one of its coins is in a block")
                        break

    def payments(self):
        with self:
            payments = self._journal()
            if any(p["state"] in OPEN + ("handed",) for p in payments.values()):
                self._settle(payments, self.look())
            return [self._shown(p) for p in payments.values()]

    def forget(self, txid):
        """The owner's word that a payment which did not go through is to stand against nothing any more.
        Not offered to programs: after it, the same thing can be paid twice."""
        with self:
            payments = self._journal()
            p = payments.get(txid)
            if p is None or p["state"] not in OPEN:
                raise Refused("NOT_OPEN", "there is no payment with this id whose outcome is open")
            self._set(p, "forgotten", "forgotten by the wallet's owner")

    # ------------------------------------------------------------------------------------------ paying

    def hand_again(self, txid):
        """Shows a payment whose outcome is open to the server once more. Nothing new is signed: it is the
        transaction that was written down, or none."""
        with self:
            payments = self._journal()
            p = payments.get(txid)
            if p is None:
                raise Refused("NOT_OPEN", "there is no payment with this id")
            self._settle(payments, self.look())
            if p["state"] in OPEN and self._clock() - p["t"] >= DAY:
                self._count_today(p, self._clock())
            if p["state"] in OPEN or p["state"] == "in_block":
                # also one that a server called "in a block": whoever asks has a node that does not see it there
                self._hand_over(p)
            return self._shown(p)

    def inputs_of(self, txid):
        """The coins a payment of this wallet spends, as (txid, number)."""
        with self:
            p = self._journal().get(txid)
            return [] if p is None else [(t, n) for t, n, _ in p["inputs"]]

    def pay(self, outputs, memo=None, dry_run=False, hand=True):
        """Pays [(address, amount in base units)] in one transaction, within the owner's limits.
        Returns what was done. With dry_run nothing is signed and nothing leaves the machine. With hand=False the
        payment is signed and written down but not shown to the wallet server: the answer carries it ("raw") for
        whoever is to hand it over (the seller, in x402). Until a server shows it, it is a payment whose outcome
        is open, and the next payment stands against it, so that the two can never both arrive."""
        try:
            named = [(keys.script_for_address(self.chain, a), v, a) for a, v in outputs]
        except keys.BadInput as e:
            raise Refused("ADDRESS", str(e))
        return self._send(named, memo, dry_run, hand)

    def stamp(self, digest, memo=None, dry_run=False):
        """Writes a SHA-256 into the chain, in an output that carries data and nothing else."""
        if not isinstance(digest, (bytes, bytearray)) or len(digest) != 32:
            raise Refused("DIGEST", "a stamp takes a SHA-256, 32 bytes")
        return self._send([(tx.data_script(STAMP + bytes(digest)), 0, None)], memo, dry_run)

    def _send(self, outputs, memo, dry_run, hand=True):
        if memo is not None and (not isinstance(memo, str) or len(memo) > 200):
            raise Refused("MEMO", "a note is a text of at most 200 characters")
        with self:
            limits = self.limits()
            if limits is None:
                raise Refused("NO_LIMITS", "this wallet has no limits.json, so it pays nothing. Its owner writes that file.")
            payments = self._journal()
            seen = self.look()
            self._settle(payments, seen)
            now = self._clock()
            listed = {(c.txid, c.vout): c for c in seen["coins"]}
            # every payment whose outcome is open has to be stood against: one of its coins goes into this one
            must = []
            for p in payments.values():
                if p["state"] in OPEN:
                    held = [listed[(t, n)] for t, n, _ in p["inputs"] if (t, n) in listed and tx.spendable(listed[(t, n)], seen["tip"])]
                    if not held:
                        raise Refused("OPEN_PAYMENT", "the payment %s did not go through and the server shows none of its coins, so nothing is "
                                                      "paid: it could still arrive beside a new one. Look at the wallet through another server "
                                                      "or a node; its owner can then forget the payment by hand." % p["txid"])
                    must.append(held[0])
            aside = {(t, n) for p in payments.values() if p["state"] == "handed" for t, n, _ in p["inputs"]}
            free = [c for c in seen["coins"] if tx.spendable(c, seen["tip"]) and (c.txid, c.vout) not in aside]
            # change goes to the next unused key, but never to one this program does not look at: at the end of
            # its window the keys of the window are used again in turn, so that no one key gathers every payment
            # (human review, 22 November)
            change_index = seen["last"][1] + 1
            if change_index >= seen["window"]:
                change_index = len(payments) % seen["window"]
            try:
                the_plan = tx.plan(free, [(s, v) for s, v, _ in outputs], limits["fee_rate"], self.account.script(1, change_index), must)
            except tx.PlanError as e:
                raise Refused(e.reason, {"NOT_ENOUGH": "not enough coins that can be spent now (%s NORTH could be sent)" % north(e.available),
                                         "AMOUNT_TOO_SMALL": "the smallest payment is %s NORTH" % north(tx.MIN_OUTPUT),
                                         "NOTHING_TO_SPEND": "the wallet has no coin that can be spent now"}.get(e.reason, "this cannot be paid (%s)" % e.reason))
            sent = sum(v for _, v, _ in outputs)
            out = sent + the_plan.fee
            if limits["allow"] is not None and any(a is not None and s not in limits["allow"] for s, _, a in outputs):
                raise Refused("NOT_ALLOWED", "the owner's limits name the addresses this wallet may pay, and this is not one of them")
            if out > limits["per_payment"]:
                raise Refused("OVER_LIMIT", "this payment (%s NORTH with its fee) is over the owner's limit for one payment (%s)" % (north(out), north(limits["per_payment"])))
            # The same coins to the same outputs is the same transaction as before, with the same id: a payment
            # whose outcome is open and that is asked for again is that payment, shown to the server once more.
            again = payments.get(tx.txid_of(tx.VERSION, [(c.txid, c.vout, tx.SEQUENCE) for c in the_plan.coins],
                                            [(v, s) for s, v in the_plan.outputs], tx.LOCKTIME))
            if again is not None and again["state"] not in OPEN:
                raise Refused("ALREADY", "this very transaction was made before and is %s" % again["state"])
            # A payment counts for its day whatever became of it. "overtaken" rests on the server's word that
            # another payment is in a block, and a server must not be the one to free the owner's limit.
            spent = sum(p["out"] for p in payments.values() if now - p["t"] < DAY and p["state"] != "forgotten" and p is not again)
            if spent + out > limits["per_day"]:
                raise Refused("OVER_DAY", "with this payment the wallet would pay %s NORTH within a day; the owner's limit is %s" % (north(spent + out), north(limits["per_day"])))
            shown = {"sent": north(sent), "fee": north(the_plan.fee), "change": north(the_plan.change), "coins": len(the_plan.coins),
                     "to": [a for _, _, a in outputs if a is not None], "stands_against": [p["txid"] for p in payments.values() if p["state"] in OPEN]}
            if again is not None:
                shown["stands_against"] = [t for t in shown["stands_against"] if t != again["txid"]]
            if dry_run:
                return dict(shown, state="not made (a dry run)", same_as=None if again is None else again["txid"])
            if again is not None and now - again["t"] >= DAY:
                self._count_today(again, now)
            if again is not None:
                if not hand:            # the same payment, given once more to whoever hands it over
                    return dict(shown, id=again["txid"], state=again["state"], note=again["note"], same_as=again["txid"], raw=again["raw"])
                self._hand_over(again)
                return dict(shown, id=again["txid"], state=again["state"], note=again["note"], same_as=again["txid"])
            raw, txid = tx.sign(the_plan, lambda c: self.account.key(c.branch, c.index)[:2])
            record = {"t": int(now), "event": "signed", "txid": txid, "raw": raw, "sent": sent, "fee": the_plan.fee, "out": out,
                      "inputs": [[c.txid, c.vout, c.amount] for c in the_plan.coins], "input_scripts": [c.script.hex() for c in the_plan.coins],
                      "outputs": [[s.hex(), v] for s, v in the_plan.outputs], "to": shown["to"], "memo": memo}
            self._note(record)                          # written down before anything leaves this machine
            payment = dict(record, state="signed", note=None)
            if not hand:
                return dict(shown, id=txid, state="signed", note="signed and not handed over: whoever was given it hands it over",
                            same_as=None, raw=raw)
            self._hand_over(payment)
            return dict(shown, id=txid, state=payment["state"], note=payment["note"], same_as=None)

    def _hand_over(self, p):
        """Shows the transaction to the server and writes down what came of it. Whatever the server says, it
        has seen the transaction from the first question on."""
        try:
            allowed, reason = self.server.validate(p["raw"], 30)
        except ServerError as e:
            return self._set(p, "unknown", "no answer to the question whether the node would take it (%s)" % e)
        if not allowed:
            return self._set(p, "kept", "the server said that its node does not take it (%s)" % reason_code(reason))
        for attempt in range(3):
            try:
                answered = self.server.broadcast(p["raw"], 30)
                if answered != p["txid"]:
                    return self._set(p, "unknown", "the server answered with another transaction's id")
                return self._set(p, "handed", "handed over")
            except ServerError as e:
                last = str(e)
                try:
                    if self.server.known(p["txid"], 20):
                        return self._set(p, "handed", "the hand-over failed, but the server knows the transaction")
                except ServerError:
                    pass
                else:
                    if e.status in (400, 409):
                        # the node said no and the server does not know the transaction: asking again changes nothing
                        return self._set(p, "kept", "the server said that its node does not take it (%s)" % reason_code(e.said))
        return self._set(p, "unknown", "handed over three times and nobody knows it (%s)" % last)
