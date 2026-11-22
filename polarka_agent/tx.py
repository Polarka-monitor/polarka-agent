"""Transactions that spend P2QRH coins: sizes, coin choice, the signature hash and signing.

Nothing here touches the network or a file. The serialization and the BIP-341 signature hash are those of the
cold address tool's test vectors, which TrueNorth Core v30.5.4 accepted on regtest. The rules for choosing
coins are those of the Polarka Wallet for Android (largest coins first, a remainder under 1000 units goes to
the fee), so that both can be compared.
"""
import struct
from collections import namedtuple

from .keys import BadInput, SCHNORR, qrh_script, schnorr_sign, sha, tagged

VERSION, LOCKTIME, SEQUENCE = 2, 0, 0xFFFFFFFD
MATURITY = 100              # a block reward can be spent once it is 100 blocks deep
MIN_OUTPUT = 1000           # smallest output this wallet makes; the node's own dust limit for these is 330
MIN_FEE_RATE, DEFAULT_FEE_RATE, MAX_FEE_RATE = 1, 2, 1000       # base units per virtual byte
MAX_INPUTS = 1000
MAX_OUTPUTS = 500
MAX_DATA = 80               # bytes of data this wallet puts into one output (the node relays far more)
MAX_MONEY = 21_000_000_000 * 100_000_000   # no amount in this wallet's sums may pass this

# One unspent output: where it is, what it holds, and which key of the account it belongs to.
Coin = namedtuple("Coin", "txid vout amount script height coinbase branch index")
Plan = namedtuple("Plan", "coins outputs fee vsize change")


class PlanError(BadInput):
    def __init__(self, reason, available=0):
        super().__init__(reason)
        self.reason, self.available = reason, available


def cs(n):
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    return b"\xfe" + struct.pack("<I", n)


def ser_out(amount, script):
    return struct.pack("<q", amount) + cs(len(script)) + script


def ser_tx(version, vin, vout, locktime, witnesses=None):
    b = struct.pack("<i", version) + (b"\x00\x01" if witnesses is not None else b"") + cs(len(vin))
    for txid, n, seq in vin:
        b += bytes.fromhex(txid)[::-1] + struct.pack("<I", n) + b"\x00" + struct.pack("<I", seq)
    b += cs(len(vout)) + b"".join(ser_out(a, s) for a, s in vout)
    for w in witnesses or []:
        b += cs(len(w)) + b"".join(cs(len(i)) + i for i in w)
    return b + struct.pack("<I", locktime)


def sighash(version, locktime, vin, prevouts, vout, index):
    """BIP-341 signature hash of a key-path spend with SIGHASH_DEFAULT and no annex: what P2QRH signs."""
    msg = b"\x00" + struct.pack("<i", version) + struct.pack("<I", locktime)
    msg += sha(b"".join(bytes.fromhex(t)[::-1] + struct.pack("<I", n) for t, n, _ in vin))
    msg += sha(b"".join(struct.pack("<q", a) for a, _ in prevouts))
    msg += sha(b"".join(cs(len(s)) + s for _, s in prevouts))
    msg += sha(b"".join(struct.pack("<I", s) for _, _, s in vin))
    msg += sha(b"".join(ser_out(a, s) for a, s in vout))
    msg += b"\x00" + struct.pack("<I", index)
    return tagged("TapSighash", b"\x00" + msg)


def txid_of(version, vin, vout, locktime):
    return sha(sha(ser_tx(version, vin, vout, locktime)))[::-1].hex()


def weight(n_in, out_scripts):
    """Weight of a transaction with n_in P2QRH inputs. A witness is a signature (64), a key (32) and one byte."""
    base = 4 + len(cs(n_in)) + 41 * n_in + len(cs(len(out_scripts))) + 4
    base += sum(8 + len(cs(len(s))) + len(s) for s in out_scripts)
    return 4 * base + 2 + 101 * n_in


def vsize(n_in, out_scripts):
    return (weight(n_in, out_scripts) + 3) // 4


def data_script(data):
    """An output that carries data and can never be spent (OP_RETURN and one push)."""
    if not isinstance(data, (bytes, bytearray)) or not 1 <= len(data) <= MAX_DATA:
        raise BadInput("data of 1 to %d bytes" % MAX_DATA)
    push = bytes([len(data)]) if len(data) <= 75 else b"\x4c" + bytes([len(data)])
    return b"\x6a" + push + bytes(data)


def is_data(script):
    return len(script) > 0 and script[0] == 0x6A


def spendable(coin, tip):
    return 1 <= coin.height <= tip and (not coin.coinbase or coin.height <= tip - MATURITY)


def plan(coins, outputs, fee_rate, change_script, must=()):
    """Chooses coins for the outputs [(script, amount)] and works out the fee.

    The coins in [must] are spent in any case: that is how a payment is made to stand against an earlier one
    that spends the same coin, so that only one of the two can ever be in a block. Then the largest first.
    What is left returns to change_script; a remainder under MIN_OUTPUT is not worth an output and goes to the fee.
    """
    if not (isinstance(fee_rate, int) and not isinstance(fee_rate, bool)) or not MIN_FEE_RATE <= fee_rate <= MAX_FEE_RATE:
        raise PlanError("FEE_RATE")
    if not outputs or len(outputs) > MAX_OUTPUTS:
        raise PlanError("OUTPUTS")
    total = 0
    for script, amount in outputs:
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise PlanError("AMOUNT")
        if is_data(script):
            if amount != 0:
                raise PlanError("AMOUNT")
        elif amount < MIN_OUTPUT:
            raise PlanError("AMOUNT_TOO_SMALL")
        total += amount
        if total > MAX_MONEY:
            raise PlanError("AMOUNT")
    if all(is_data(s) for s, _ in outputs) and len(outputs) > 1:
        raise PlanError("OUTPUTS")
    key = lambda c: (c.txid, c.vout)
    first, seen = [], set()
    for c in must:
        if key(c) not in seen:
            seen.add(key(c))
            first.append(c)
    if len(first) > MAX_INPUTS:
        raise PlanError("TOO_MANY_INPUTS", sum(c.amount for c in first))
    rest = {}
    for c in coins:
        if key(c) not in seen:
            rest.setdefault(key(c), c)
    ordered = first + sorted(rest.values(), key=lambda c: (-c.amount, c.txid, c.vout))
    if not ordered:
        raise PlanError("NOTHING_TO_SPEND")
    scripts = [s for s, _ in outputs]
    least = max(1, len(first))
    have = sum(c.amount for c in ordered[:least - 1])
    for n in range(least, min(len(ordered), MAX_INPUTS) + 1):
        have += ordered[n - 1].amount
        with_change = vsize(n, scripts + [change_script])
        left = have - total - with_change * fee_rate
        if left >= MIN_OUTPUT:
            return Plan(ordered[:n], list(outputs) + [(change_script, left)], with_change * fee_rate, with_change, left)
        alone = vsize(n, scripts)
        if have - total >= alone * fee_rate:
            return Plan(ordered[:n], list(outputs), have - total, alone, 0)
    if len(ordered) > MAX_INPUTS:
        raise PlanError("TOO_MANY_INPUTS", have)
    raise PlanError("NOT_ENOUGH", max(0, have - vsize(len(ordered), scripts) * fee_rate))


def sign(the_plan, key_of, aux=None):
    """Signs a plan. key_of(coin) gives (private key, x-only public key) of the key the coin sits on.

    Returns (transaction in hex, its id). A coin whose script is not that of its key is refused: this wallet
    signs for its own outputs only.
    """
    vin = [(c.txid, c.vout, SEQUENCE) for c in the_plan.coins]
    if len(set((t, n) for t, n, _ in vin)) != len(vin):
        raise BadInput("a coin is named twice")
    prevouts = [(c.amount, c.script) for c in the_plan.coins]
    vout = [(a, s) for s, a in the_plan.outputs]
    if sum(a for a, _ in prevouts) - sum(a for a, _ in vout) != the_plan.fee or the_plan.fee < 0:
        raise BadInput("the plan does not add up")
    witnesses = []
    for i, coin in enumerate(the_plan.coins):
        private, xonly = key_of(coin)
        if qrh_script(xonly) != coin.script:
            raise BadInput("a coin does not sit on a key of this wallet")
        sig = schnorr_sign(sighash(VERSION, LOCKTIME, vin, prevouts, vout, i), private, aux)
        witnesses.append([sig, xonly, bytes([SCHNORR])])
    raw = ser_tx(VERSION, vin, vout, LOCKTIME, witnesses)
    if (len(ser_tx(VERSION, vin, vout, LOCKTIME)) * 3 + len(raw) + 3) // 4 != the_plan.vsize:
        raise BadInput("the transaction is not of the size the fee was worked out for")
    return raw.hex(), txid_of(VERSION, vin, vout, LOCKTIME)
