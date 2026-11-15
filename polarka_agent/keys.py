"""Keys, addresses and signatures for TrueNorth (NORTH). Python standard library only.

The arithmetic, the derivation and the signature code are those of the cold address tool
(github.com/severka-monitor/truenorth-cold-address), where they were checked against TrueNorth Core v30.5.4
and against the published P2QRH test vectors. They are slow and not constant in time: fine for a wallet that
signs a few times a minute on a machine its owner controls, not for a server that signs for strangers.

A P2QRH address commits to SHA256("TrueNorth/QRH/v1" || 0x01 || x-only public key || 32 zero bytes); the keys
come from m/88h/COINh/0h/branch/index (doc/p2qrh.md of TrueNorth Core).
"""
import hashlib
import hmac
import os
import unicodedata

P = 2 ** 256 - 2 ** 32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
H = 0x80000000

# chain -> (bech32 prefix, SLIP-44 coin type in the derivation path)
CHAINS = {"main": ("north", 1867), "regtest": ("rnorth", 1)}
TAG = b"TrueNorth/QRH/v1"
SCHNORR = 0x01
WORDLIST_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
UNIT = 100_000_000          # base units in one NORTH


class BadInput(ValueError):
    """Words, an address or a key that cannot be used. The message says what is wrong and never holds a secret."""


def sha(b):
    return hashlib.sha256(b).digest()


def tagged(tag, msg):
    t = sha(tag.encode())
    return sha(t + t + msg)


def b32(i):
    return i.to_bytes(32, "big")


# ---------------------------------------------------------------- secp256k1

def add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0]:
        if (a[1] + b[1]) % P == 0:
            return None
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def mul(k, pt=G):
    r = None
    while k:
        if k & 1:
            r = add(r, pt)
        pt = add(pt, pt)
        k >>= 1
    return r


def lift_x(x):
    if x >= P:
        return None
    y = pow((pow(x, 3, P) + 7) % P, (P + 1) // 4, P)
    if pow(y, 2, P) != (pow(x, 3, P) + 7) % P:
        return None
    return x, y if y % 2 == 0 else P - y


# ---------------------------------------------------------------- BIP-340

def schnorr_sign(msg, seckey, aux=None):
    if len(msg) != 32 or not 0 < seckey < N:
        raise BadInput("cannot sign: wrong message length or key out of range")
    aux = os.urandom(32) if aux is None else aux
    pt = mul(seckey)
    d = seckey if pt[1] % 2 == 0 else N - seckey
    t = b32(d ^ int.from_bytes(tagged("BIP0340/aux", aux), "big"))
    k0 = int.from_bytes(tagged("BIP0340/nonce", t + b32(pt[0]) + msg), "big") % N
    if k0 == 0:
        raise BadInput("cannot sign: unusable nonce")
    r = mul(k0)
    k = k0 if r[1] % 2 == 0 else N - k0
    e = int.from_bytes(tagged("BIP0340/challenge", b32(r[0]) + b32(pt[0]) + msg), "big") % N
    sig = b32(r[0]) + b32((k + e * d) % N)
    if not schnorr_verify(msg, b32(pt[0]), sig):
        raise BadInput("cannot sign: the signature made did not verify")
    return sig


def schnorr_verify(msg, pubkey, sig):
    if len(msg) != 32 or len(pubkey) != 32 or len(sig) != 64:
        return False
    pt = lift_x(int.from_bytes(pubkey, "big"))
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if pt is None or r >= P or s >= N:
        return False
    e = int.from_bytes(tagged("BIP0340/challenge", sig[:32] + pubkey + msg), "big") % N
    rr = add(mul(s), mul(e, (pt[0], P - pt[1])))
    return rr is not None and rr[1] % 2 == 0 and rr[0] == r


# ---------------------------------------------------------------- bech32 and bech32m (BIP-173, BIP-350)

_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32, _BECH32M = 1, 0x2BC830A3


def _polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            if top >> i & 1:
                chk ^= gen[i]
    return chk


def _expand(hrp):
    return [ord(ch) >> 5 for ch in hrp] + [0] + [ord(ch) & 31 for ch in hrp]


def encode_address(hrp, version, program):
    data, acc, bits = [version], 0, 0
    for b in program:
        acc, bits = acc << 8 | b, bits + 8
        while bits >= 5:
            bits -= 5
            data.append(acc >> bits & 31)
    if bits:
        data.append(acc << (5 - bits) & 31)
    pm = _polymod(_expand(hrp) + data + [0] * 6) ^ (_BECH32 if version == 0 else _BECH32M)
    return hrp + "1" + "".join(_B32[d] for d in data + [pm >> 5 * (5 - i) & 31 for i in range(6)])


def script_for_address(chain, address):
    """The output script for an address of this chain. Accepts what TrueNorth Core accepts (witness version 0
    with 20 or 32 bytes, versions 1 and 2 with 32 bytes) and nothing else, so that a typing mistake or an
    address of another network never becomes a payment."""
    if not isinstance(address, str) or not 8 <= len(address) <= 90:
        raise BadInput("not an address")
    if address != address.lower() and address != address.upper():
        raise BadInput("the address mixes upper and lower case")
    address = address.lower()
    pos = address.rfind("1")
    if pos < 1 or pos + 7 > len(address):
        raise BadInput("not an address")
    hrp, rest = address[:pos], address[pos + 1:]
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in hrp) or any(ch not in _B32 for ch in rest):
        raise BadInput("the address has a character that cannot be there")
    data = [_B32.index(ch) for ch in rest]
    check = _polymod(_expand(hrp) + data)
    if check not in (_BECH32, _BECH32M):
        raise BadInput("the address is mistyped (its checksum does not hold)")
    if hrp != CHAINS[chain][0]:
        raise BadInput("the address belongs to another network (prefix %s, expected %s)" % (hrp, CHAINS[chain][0]))
    data = data[:-6]
    if not data:
        raise BadInput("the address is empty")
    version, acc, bits, program = data[0], 0, 0, bytearray()
    for g in data[1:]:
        acc, bits = acc << 5 | g, bits + 5
        if bits >= 8:
            bits -= 8
            program.append(acc >> bits & 0xFF)
    if bits >= 5 or acc & ((1 << bits) - 1):
        raise BadInput("the address has invalid padding")
    if check != (_BECH32 if version == 0 else _BECH32M):
        raise BadInput("wrong checksum type for this address version")
    if not ((version == 0 and len(program) in (20, 32)) or (version in (1, 2) and len(program) == 32)):
        raise BadInput("unsupported address type (witness version %d, %d bytes)" % (version, len(program)))
    return bytes([0x00 if version == 0 else 0x50 + version, len(program)]) + bytes(program)


def address_for_script(chain, script):
    """The address of an output script of the kinds above, or None for any other script."""
    if len(script) < 4 or script[1] != len(script) - 2:
        return None
    version = 0 if script[0] == 0 else script[0] - 0x50
    program = script[2:]
    if not ((version == 0 and len(program) in (20, 32)) or (version in (1, 2) and len(program) == 32)):
        return None
    return encode_address(CHAINS[chain][0], version, program)


def commitment(xonly, scheme_id=SCHNORR, script_root=bytes(32)):
    return sha(TAG + bytes([scheme_id]) + xonly + script_root)


def qrh_script(xonly):
    return b"\x52\x20" + commitment(xonly)


# ---------------------------------------------------------------- BIP39

_words = None


def wordlist():
    global _words
    if _words is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "english.txt"), "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != WORDLIST_SHA256:
            raise BadInput("english.txt is not the standard BIP39 word list")
        _words = data.decode().split()
    return _words


def new_words(count=24, entropy=None):
    if count not in (12, 24):
        raise BadInput("a wallet has 12 or 24 words")
    entropy = os.urandom(count * 4 // 3) if entropy is None else entropy
    bits = len(entropy) * 8
    n = (int.from_bytes(entropy, "big") << (bits // 32)) | (sha(entropy)[0] >> (8 - bits // 32))
    words = wordlist()
    return " ".join(words[(n >> (11 * (count - 1 - i))) & 2047] for i in range(count))


def check_words(mnemonic):
    """None when the words are a valid phrase, otherwise what is wrong. The answer names no word but by its place."""
    parts = mnemonic.split()
    if len(parts) not in (12, 24):
        return "expected 12 or 24 words, got %d" % len(parts)
    index = {w: i for i, w in enumerate(wordlist())}
    for place, p in enumerate(parts, 1):
        if p not in index:
            return "word %d is not on the list" % place
    n = 0
    for p in parts:
        n = (n << 11) | index[p]
    cs_bits = len(parts) * 11 // 33
    entropy = (n >> cs_bits).to_bytes(len(parts) * 11 * 32 // 33 // 8, "big")
    if sha(entropy)[0] >> (8 - cs_bits) != n & ((1 << cs_bits) - 1):
        return "the words do not fit each other (one is wrong or out of order)"
    return None


def seed_from_words(mnemonic):
    wrong = check_words(mnemonic)
    if wrong:
        raise BadInput(wrong)
    norm = unicodedata.normalize("NFKD", " ".join(mnemonic.split()))
    return hashlib.pbkdf2_hmac("sha512", norm.encode(), b"mnemonic", 2048, 64)


# ---------------------------------------------------------------- BIP32 and the account

def ckd(k, c, i, pub=None):
    """BIP32 private child derivation. [pub] may carry the parent's public point to save one multiplication."""
    if i >= H:
        data = b"\0" + b32(k) + i.to_bytes(4, "big")
    else:
        x, y = pub or mul(k)
        data = bytes([2 + (y & 1)]) + b32(x) + i.to_bytes(4, "big")
    d = hmac.new(c, data, hashlib.sha512).digest()
    il = int.from_bytes(d[:32], "big")
    child = (il + k) % N
    if il >= N or child == 0:
        raise BadInput("unusable child key")
    return child, d[32:]


class Account:
    """The keys of one wallet: m/88h/COINh/0h, branch 0 for receiving and branch 1 for change."""

    def __init__(self, seed, chain):
        if chain not in CHAINS:
            raise BadInput("unknown chain")
        self.chain = chain
        d = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        k, c = int.from_bytes(d[:32], "big"), d[32:]
        if not 0 < k < N:
            raise BadInput("unusable seed")
        for i in (88 | H, CHAINS[chain][1] | H, H):
            k, c = ckd(k, c, i)
        self._branch = {}
        for b in (0, 1):
            bk, bc = ckd(k, c, b)
            self._branch[b] = (bk, bc, mul(bk))
        self._keys = {}

    @classmethod
    def from_words(cls, mnemonic, chain):
        return cls(seed_from_words(mnemonic), chain)

    def key(self, branch, index):
        """(private key, x-only public key, output script, address) of one key of the account."""
        if branch not in (0, 1) or not 0 <= index < H:
            raise BadInput("no such key")
        if (branch, index) not in self._keys:
            bk, bc, bpub = self._branch[branch]
            k, _ = ckd(bk, bc, index, bpub)
            x = b32(mul(k)[0])
            script = qrh_script(x)
            self._keys[(branch, index)] = (k, x, script, encode_address(CHAINS[self.chain][0], 2, script[2:]))
        return self._keys[(branch, index)]

    def script(self, branch, index):
        return self.key(branch, index)[2]

    def address(self, branch, index):
        return self.key(branch, index)[3]
