#!/usr/bin/env python3
"""USDC facilitator for polarka.org: x402 v2, scheme "exact" with EIP-3009, one network, on this machine only.

It sits beside the polarka-agent gate and answers GET /supported, GET /health, POST /verify and POST /settle with
{"x402Version": 2, "paymentPayload": ..., "paymentRequirements": ...}, as the x402 facilitator API names them.

What it takes: x402 v2, the payment exactly as the requirements ask (scheme exact, this network, its USDC, our own
--pay-to, an amount up to --max-amount), an EIP-3009 authorization and nothing else, signed by an account with no
code (an EOA: the signer is recovered here from the EIP-712 message, and the chain must hold no code for it). A
payment seen for the first time must be valid now, for at least a minute more and for at most an hour. The official
x402 SDK (x402 on PyPI, Apache-2.0) then checks it once more and simulates the transfer. The transfer itself is sent
from here (transferWithAuthorization with v, r, s), so that every payment is written to a journal before anything
goes to the chain: by payer and nonce, with the authorization, the signed transaction and its outcome.

A payment already in the journal is answered from it, whatever the time: the same payment gets the same answer and
is never sent twice (paid: success with its transfer, until an hour after its authorization ran out); another
authorization under a payer and nonce already seen is refused. A transfer the chain lost is sent again only for a
request that shows the payment again while its authorization holds. Payments sent and not yet decided are looked at
every half minute without anyone asking, and only decided there, never sent: paid (a Transfer of the whole value to
us, logged right after the use of this very authorization), failed, or not paid once the chain's head is two minutes
past the authorization's end, as read at that head. A payer's payments not yet decided must fit its balance together.
Below --gas-floor-wei nothing is sent.

Work with the chain: the checks that only read run side by side (at most eight at once); the gas key's nonce, the
signature, the journal and the broadcast one payment at a time; the wait for a block without any lock. A payment
that could not be sent within --send-within seconds of its request is refused, with nothing sent, so that nothing
goes to the chain after the gate has stopped waiting.

Every answer of /settle says what the gate needs to know: "sent": false when this payment did not and will not move
any money (the gate may then forget it), true when a transfer of ours may be on the chain or was decided, and no
"sent" when that cannot be told yet. Refusals of the HTTP kind (size, length, JSON) are answered the same way.
The gas key and the RPC address (--rpc-file) are read from files only their owner may read; neither is printed or
logged.
"""
import argparse
import fcntl
import json
import logging
import os
import re
import stat
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound
from x402 import x402FacilitatorSync
from x402.mechanisms.evm import FacilitatorWeb3Signer
from x402.mechanisms.evm.default_assets import get_default_asset
from x402.mechanisms.evm.exact import ExactEvmFacilitatorScheme, ExactEvmSchemeConfig
from x402.schemas import PaymentPayload, PaymentRequirements

NETWORKS = ("eip155:8453", "eip155:84532")  # Base, Base Sepolia
MAX_BODY = 64 * 1024
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
DIGITS = re.compile(r"[0-9]{1,30}")
NONCE = re.compile(r"0x[0-9a-fA-F]{64}")
SIGNATURE = re.compile(r"0x[0-9a-fA-F]{130}")
CURVE_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
AUTH = ("from", "to", "value", "validAfter", "validBefore", "nonce")
WANTED = {"scheme", "network", "asset", "amount", "payTo", "maxTimeoutSeconds", "extra"}
LONGEST = 3600              # a payment seen for the first time and valid for longer than this from now is refused
EARLIEST = 60               # nor one that runs out within this many seconds (it could not be in a block in time)
REPLAY_FOR = 3600           # a payment paid is answered as paid until this long after its authorization ran out
EXPIRY_MARGIN = 120         # seconds after validBefore before an authorization the token never took is given up
KEEP = 2 * 86400            # what the journal remembers of a payment once it is decided
RECONCILE_EVERY = 30        # seconds between looks at the payments sent and not yet decided
CHECKS_AT_ONCE = 8          # reading checks of new payments side by side
SEND_WITHIN = 20.0          # seconds from a request's arrival within which a new payment must be sent, or refused
SEND_WAIT = 10              # seconds a request that sends a lost transfer again waits for the sending lock (N1)
BROADCAST_TRIES = 3         # tries of one signed transfer within its request, a second apart (third reading, G1)
LOST_AFTER = 90             # seconds after which a transfer of ours in no block and unknown to the RPC is lost (G4)
FILL_MOST = 4               # a filler bids at most this many times today's fee, or that of the transfer it fills (R1)
GAP_HOLD = 60               # seconds a lost nonce may stay open before new payments are refused, nothing sent
ALARM_EVERY = 600           # seconds between two ALARM lines for the same open nonce
ANSWER_WITHIN = 40          # seconds from a request's arrival by which /settle answers, the RPC willing (N2)
UNKNOWN_FOR = 6 * 3600      # seconds after validBefore after which a payment whose outcome cannot be found is given up
LOG_SPAN = 3000             # blocks looked through for the transaction that used an authorization
LOG_CHUNK = 500             # of them asked for at once: providers cap the range of eth_getLogs (N3)
GAS_MOST = 200_000
PROTOCOL = "polarka-usdc/2"  # what /health says this facilitator answers like; the gate asks for it
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"   # Transfer(address,address,uint256)
AUTH_USED = Web3.to_hex(Web3.keccak(text="AuthorizationUsed(address,bytes32)"))   # EIP-3009, both indexed
AUTH_CANCELED = Web3.to_hex(Web3.keccak(text="AuthorizationCanceled(address,bytes32)"))
ABI = [{"name": "transferWithAuthorization", "type": "function", "stateMutability": "nonpayable", "outputs": [],
        "inputs": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
                   {"name": "validAfter", "type": "uint256"}, {"name": "validBefore", "type": "uint256"},
                   {"name": "nonce", "type": "bytes32"}, {"name": "v", "type": "uint8"}, {"name": "r", "type": "bytes32"},
                   {"name": "s", "type": "bytes32"}]},
       {"name": "authorizationState", "type": "function", "stateMutability": "view", "outputs": [{"name": "", "type": "bool"}],
        "inputs": [{"name": "authorizer", "type": "address"}, {"name": "nonce", "type": "bytes32"}]},
       {"name": "balanceOf", "type": "function", "stateMutability": "view", "outputs": [{"name": "", "type": "uint256"}],
        "inputs": [{"name": "account", "type": "address"}]}]
log = logging.getLogger("usdc-facilitator")


def topic(address):
    """An address as an indexed topic of a log."""
    return "0x" + address[2:].lower().rjust(64, "0")


def hexed(value):
    return value.lower() if isinstance(value, str) else Web3.to_hex(value)


def topics(lg):
    return [hexed(t) for t in lg.get("topics") or []]


def amount(lg):
    """The uint256 of a log's data, or -1."""
    try:
        return int(hexed(lg.get("data") or b""), 16)
    except (TypeError, ValueError):
        return -1


def read_secret(path):
    """What is in a file that belongs to this user and that nobody else may read (never through a link)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        sys.exit("%s cannot be opened (%s)" % (path, type(e).__name__))
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            sys.exit("%s must be a file of yours that only you may read (chmod 600)" % path)
        text = f.read().strip()
    if not text:
        sys.exit("%s is empty" % path)
    return text


read_key = read_secret      # the name the tests use


def canonical(payload):
    """(the EIP-3009 authorization and its signature in one form, checked for shape, or None, why not)."""
    if not isinstance(payload, dict) or set(payload) != {"signature", "authorization"}:
        return None, "only_eip3009_authorizations"
    a, sig = payload["authorization"], payload["signature"]
    if not isinstance(a, dict) or set(a) != set(AUTH):
        return None, "invalid_authorization"
    if not isinstance(sig, str) or not SIGNATURE.fullmatch(sig):
        return None, "invalid_signature_form"
    if not all(isinstance(a[k], str) for k in ("from", "to", "nonce")) or not ADDRESS.fullmatch(a["from"]) \
            or not ADDRESS.fullmatch(a["to"]) or not NONCE.fullmatch(a["nonce"]):
        return None, "invalid_authorization"
    c = {"from": Web3.to_checksum_address(a["from"]), "to": Web3.to_checksum_address(a["to"]), "nonce": a["nonce"].lower()}
    for k in ("value", "validAfter", "validBefore"):
        v = a[k]
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            v = str(v)
        if not isinstance(v, str) or not DIGITS.fullmatch(v):
            return None, "invalid_authorization"
        c[k] = str(int(v))
    raw = bytes.fromhex(sig[2:])
    r, s, v = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:64], "big"), raw[64]
    if v not in (27, 28) or not 0 < r < CURVE_N or not 0 < s <= CURVE_N // 2:
        return None, "invalid_signature_form"
    c["signature"] = sig.lower()
    return c, None


def signer_of(c, chain_id, asset, name, version):
    """The account that signed the authorization `c`, recovered from its EIP-712 message; None when no account can
    have signed it (an r that is no point of the curve, for one)."""
    try:
        return Account.recover_message(typed(c, chain_id, asset, name, version), signature=bytes.fromhex(c["signature"][2:]))
    except Exception:
        return None


def typed(c, chain_id, asset, name, version):
    """The EIP-712 message of the authorization `c` (TransferWithAuthorization of USDC), ready to sign or recover."""
    full = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                        {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                       "TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                                     {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                                                     {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
             "primaryType": "TransferWithAuthorization",
             "domain": {"name": name, "version": version, "chainId": chain_id, "verifyingContract": asset},
             "message": {"from": c["from"], "to": c["to"], "value": int(c["value"]), "validAfter": int(c["validAfter"]),
                         "validBefore": int(c["validBefore"]), "nonce": bytes.fromhex(c["nonce"][2:])}}
    return encode_typed_data(full_message=full)


class Journal:
    """Every payment this facilitator has sent or decided, by payer and nonce, in one file that is written before
    anything goes to the chain and kept over restarts. One facilitator to a folder (a lock on it). A file that cannot
    be read stops the start: nothing is settled until a person has looked, so that nothing is ever sent twice."""

    def __init__(self, folder):
        os.makedirs(folder, mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        self.folder, self.path = folder, os.path.join(folder, "journal.json")
        self.held = os.open(os.path.join(folder, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("another facilitator uses %s" % folder)
        try:
            with open(self.path) as f:
                got = json.load(f)
        except FileNotFoundError:
            got = {}
        except ValueError:
            sys.exit("%s cannot be read: look at it before starting again" % self.path)
        if not isinstance(got, dict) or not all(isinstance(e, dict) for e in got.values()):
            sys.exit("%s is not a journal: look at it before starting again" % self.path)
        self.entries = got

    def get(self, key):
        return self.entries.get(key)

    def put(self, key, entry):
        """Written through, with fsync, before the caller goes on (the caller holds the facilitator's state lock). A
        payment still sent and not decided is kept; a decided one for KEEP after it was decided."""
        now = time.time()
        entries = {k: e for k, e in self.entries.items()
                   if e.get("state") == "sent" or now - e.get("decided", e.get("at", 0)) < KEEP}
        entries[key] = entry
        tmp = self.path + ".tmp"
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(entries, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        fd = os.open(self.folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.entries = entries

    def waiting(self):
        return sum(1 for e in self.entries.values() if e.get("state") == "sent")

    def undecided(self):
        """The keys of payments sent and not yet decided, the oldest first."""
        return [k for k, e in sorted(self.entries.items(), key=lambda kv: kv[1].get("at", 0)) if e.get("state") == "sent"]

    def owed(self, payer):
        """What the payments of `payer` sent and not yet decided move in all."""
        p = payer.lower() + ":"
        return sum(int(e["auth"]["value"]) for k, e in self.entries.items() if e.get("state") == "sent" and k.startswith(p))


def payer_of(body):
    """Who says they signed, for the log and the answer only."""
    try:
        who = body["paymentPayload"]["payload"]["authorization"]["from"]
    except (KeyError, TypeError):
        return None
    return who if isinstance(who, str) and ADDRESS.fullmatch(who) else None


class Facilitator:
    """The checks and the sending for one network.

    Locks: `state` guards the journal and the set of payments being worked on (`busy`: one thread for one payment
    at a time); `sending` guards the gas key's nonces, the balance check of a payer's payments in flight, the
    signature, the journal entry and the broadcast, one payment at a time; `checking` lets at most CHECKS_AT_ONCE new
    payments ask the chain at once. Nothing is held while a block is awaited or while a payment is looked up on the
    chain."""

    def __init__(self, network, rpc, key, pay_to, max_amount, confirm_timeout, state_dir, gas_floor, send_within=SEND_WITHIN,
                 expiry_margin=EXPIRY_MARGIN, lost_after=LOST_AFTER, log_chunk=LOG_CHUNK):
        from web3.middleware import ExtraDataToPOAMiddleware
        info = get_default_asset(network, "USDC")
        self.network, self.chain_id = network, int(network.split(":", 1)[1])
        self.pay_to, self.max_amount = Web3.to_checksum_address(pay_to), max_amount
        self.asset, self.name, self.version = Web3.to_checksum_address(info["asset"]), info["name"], info["version"]
        self.confirm_timeout, self.gas_floor = confirm_timeout, gas_floor
        self.account = Account.from_key(key)
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        got = self.w3.eth.chain_id
        if got != self.chain_id:
            sys.exit("the RPC serves chain %d, not %s" % (got, network))
        self.usdc = self.w3.eth.contract(address=self.asset, abi=ABI)
        self.journal = Journal(state_dir)
        # the SDK checks and simulates; it sends nothing here
        signer = FacilitatorWeb3Signer(private_key=key, rpc_url=rpc)
        config = ExactEvmSchemeConfig(eip6492_allowed_factories=[], simulate_in_settle=True)
        self.core = x402FacilitatorSync().register([network], ExactEvmFacilitatorScheme(signer, config))
        self.runtime(send_within, expiry_margin, lost_after, log_chunk)

    def runtime(self, send_within=SEND_WITHIN, expiry_margin=EXPIRY_MARGIN, lost_after=LOST_AFTER, log_chunk=LOG_CHUNK):
        """The locks and the times of work (also for a facilitator made without __init__, as in tests)."""
        self.state, self.sending = threading.Lock(), threading.Lock()
        self.checking = threading.BoundedSemaphore(CHECKS_AT_ONCE)
        self.stopped = threading.Event()
        self.busy = set()           # keys of payments being worked on this moment
        self.took = set()           # transfers the RPC took when they were sent (since this start)
        self.send_within, self.expiry_margin = send_within, expiry_margin
        self.lost_after, self.log_chunk = lost_after, log_chunk
        self.next_nonce = None      # the gas key's next nonce as this facilitator counts it (G4)
        self.ours = {}              # nonce -> the last transfer of ours handed over under it, not yet in a block (G4)
        self.gap = None             # the lowest nonce of ours found lost and not yet filled: {nonce, since, told}
        if getattr(self, "journal", None) is not None:
            self.recover()

    def recover(self):
        """At the start: the transfers of payments sent and not yet decided, from the journal (each entry keeps the
        signed transfer it named last), so that a nonce lost before a restart is seen and filled like any other
        (fourth reading, R3), and the next nonce is counted above them."""
        from eth_account.typed_transactions import TypedTransaction
        from hexbytes import HexBytes
        for k in self.journal.undecided():
            e = self.journal.get(k)
            if not e.get("raw") or e.get("tx") in (e.get("gone") or []):
                continue
            try:
                d = TypedTransaction.from_bytes(HexBytes(e["raw"])).as_dict()
            except Exception:
                continue
            n, at = int(d["nonce"]), e.get("last", e.get("at", 0))
            if n not in self.ours or self.ours[n]["at"] < at:
                fee, tip = int(d["maxFeePerGas"]), int(d["maxPriorityFeePerGas"])
                self.ours[n] = {"tx": e["tx"], "at": at, "fee": fee, "tip": tip, "first_fee": fee, "first_tip": tip}
        if self.ours:
            self.next_nonce = max(self.ours) + 1
            log.info("transfers of ours not yet decided: nonces %s", sorted(self.ours))

    def answer(self, ok, reason, payer, tx="", sent=None, known=None):
        """An answer of /settle. sent: False when this payment did not and will not move any money, True when a
        transfer of ours may be on the chain or the payment was decided, left out when that cannot be told yet."""
        a = {"success": ok, "transaction": tx or "", "network": self.network, "payer": payer}
        if sent is not None:
            a["sent"] = sent
        if not ok:
            a["errorReason"] = reason
        if known is not None:
            a["known"] = known
        return a

    def required(self, r):
        """(the requirements in one form, or None, why not)."""
        if not isinstance(r, dict) or set(r) != WANTED:
            return None, "invalid_requirements"
        if r["scheme"] != "exact":
            return None, "unsupported_scheme"
        if r["network"] != self.network:
            return None, "network_mismatch"
        if not isinstance(r["asset"], str) or r["asset"].lower() != self.asset.lower():
            return None, "asset_is_not_usdc"
        if not isinstance(r["payTo"], str) or r["payTo"].lower() != self.pay_to.lower():
            return None, "pay_to_is_not_ours"
        if not isinstance(r["amount"], str) or not DIGITS.fullmatch(r["amount"]) or not 0 < int(r["amount"]) <= self.max_amount:
            return None, "amount_out_of_range"
        t = r["maxTimeoutSeconds"]
        if not isinstance(t, int) or isinstance(t, bool) or not 0 < t <= LONGEST:
            return None, "invalid_requirements"
        if r["extra"] != {"name": self.name, "version": self.version}:
            return None, "invalid_requirements"
        return {"scheme": "exact", "network": self.network, "asset": self.asset, "amount": str(int(r["amount"])),
                "payTo": self.pay_to, "maxTimeoutSeconds": t, "extra": {"name": self.name, "version": self.version}}, None

    def checked(self, body):
        """(requirements, authorization, None) for a request this facilitator takes, else (None, None, why not).
        Nothing here asks the chain or looks at the time: a payment already in the journal is answered from it
        whatever the time (second reading, F1); window() is for payments seen for the first time."""
        if not isinstance(body, dict) or body.get("x402Version") != 2:
            return None, None, "unsupported_x402_version"
        p, r = body.get("paymentPayload"), body.get("paymentRequirements")
        if not isinstance(p, dict) or p.get("x402Version") != 2 or not set(p) <= {"x402Version", "accepted", "payload", "resource", "extensions"}:
            return None, None, "invalid_request"
        req, why = self.required(r)
        if why:
            return None, None, why
        if self.required(p.get("accepted"))[0] != req:
            return None, None, "accepted_does_not_match_requirements"
        c, why = canonical(p.get("payload"))
        if why:
            return None, None, why
        if c["to"] != self.pay_to:
            return None, None, "pay_to_is_not_ours"
        if int(c["value"]) != int(req["amount"]):
            return None, None, "value_does_not_match_amount"
        if signer_of(c, self.chain_id, self.asset, self.name, self.version) != c["from"]:
            return None, None, "signature_is_not_the_payers"
        return req, c, None

    @staticmethod
    def window(c):
        """Why a payment seen for the first time is refused for its time, or None."""
        now = int(time.time())
        if int(c["validAfter"]) > now:
            return "authorization_not_yet_valid"
        if int(c["validBefore"]) < now + EARLIEST:
            return "authorization_expires_too_soon"
        if int(c["validBefore"]) > now + LONGEST:
            return "authorization_valid_for_too_long"
        return None

    def payer_has_no_code(self, c):
        return len(self.w3.eth.get_code(c["from"])) == 0     # an EIP-7702 delegation is code too (0xef0100...)

    def sdk_says(self, req, c):
        """The SDK's own verdict on the authorization in its checked form: (True, None) or (False, its reason)."""
        payload = PaymentPayload.model_validate({"x402Version": 2, "accepted": req, "payload": {
            "signature": c["signature"], "authorization": {k: c[k] for k in AUTH}}})
        got = self.core.verify(payload, PaymentRequirements.model_validate(req))
        if got.is_valid:
            return True, None
        reason = got.invalid_reason if isinstance(got.invalid_reason, str) and re.fullmatch(r"[a-z0-9_]{1,80}", got.invalid_reason) else "invalid"
        return False, reason

    def verify(self, body):
        """/verify: what /settle would say of a payment seen for the first time, with nothing sent."""
        payer = payer_of(body)
        try:
            req, c, why = self.checked(body)
            if why:
                return {"isValid": False, "invalidReason": why, "payer": payer}
            payer = c["from"]
            with self.state:
                if self.journal.get(c["from"].lower() + ":" + c["nonce"]) is not None:
                    return {"isValid": False, "invalidReason": "payment_already_known", "payer": payer}
            why = self.window(c)
            if why:
                return {"isValid": False, "invalidReason": why, "payer": payer}
            if not self.checking.acquire(timeout=self.send_within):
                return {"isValid": False, "invalidReason": "facilitator_busy", "payer": payer}
            try:
                if not self.payer_has_no_code(c):
                    return {"isValid": False, "invalidReason": "payer_is_not_an_eoa", "payer": payer}
                ok, why = self.sdk_says(req, c)
            finally:
                self.checking.release()
            return {"isValid": True, "payer": payer} if ok else {"isValid": False, "invalidReason": why, "payer": payer}
        except Exception as ex:
            log.warning("verify error payer=%s %s", payer, type(ex).__name__)
            return {"isValid": False, "invalidReason": "unexpected_verify_error", "payer": payer}

    def settle(self, body, arrived=None):
        """/settle. `arrived`: when the request came (time.monotonic()); a payment seen for the first time that could
        not be sent within send_within seconds of it is refused, with nothing sent (second reading, F3)."""
        arrived = time.monotonic() if arrived is None else arrived
        payer, key = payer_of(body), None
        try:
            req, c, why = self.checked(body)
            if why:
                return self.answer(False, why, payer, sent=False)
            payer, key = c["from"], c["from"].lower() + ":" + c["nonce"]
            with self.state:
                e = self.journal.get(key)
                if key in self.busy:        # this payment is being worked on for another request this moment
                    return self.answer(False, "settling_now", payer, tx=(e or {}).get("tx", ""), sent=True if e else None)
                self.busy.add(key)
            try:
                if e is not None:
                    return self.again(key, e, c, req)
                return self.first(key, c, req, arrived)
            finally:
                with self.state:
                    self.busy.discard(key)
        except Exception as ex:   # what went wrong stays in our log; "sent" says whether the chain may have it
            log.warning("settle error payer=%s %s", payer, type(ex).__name__)
            e = None
            if key is not None:
                with self.state:
                    e = self.journal.get(key)
            # nothing is sent before it is in the journal: without an entry, nothing went to the chain
            sent = False if e is None else bool(e.get("state") != "failed" or e.get("sent", True))
            return self.answer(False, "unexpected_settle_error", payer, tx=(e or {}).get("tx", ""), sent=sent)

    def first(self, key, c, req, arrived):
        """A payment seen for the first time; its key is this thread's alone while this runs."""
        why = self.window(c)
        if why:
            return self.answer(False, why, c["from"], sent=False)
        if self.held():
            # a transfer sent now would wait behind a nonce of ours that cannot be filled: none goes out (fourth
            # reading, R1), and the payer is told to come back, with nothing sent
            return self.answer(False, "facilitator_busy", c["from"], sent=False)

        def left():
            return self.send_within - (time.monotonic() - arrived)
        if not self.checking.acquire(timeout=max(0.0, left())):
            return self.answer(False, "facilitator_busy", c["from"], sent=False)
        try:
            # one call first that a key holding no USDC fails: a payment anyone can sign for nothing costs one RPC
            # call, not the five of the checks after it (third reading, the RPC's budget)
            if self.usdc.functions.balanceOf(c["from"]).call(block_identifier="latest") < int(c["value"]):
                return self.answer(False, "insufficient_funds", c["from"], sent=False)
            if not self.payer_has_no_code(c):
                return self.answer(False, "payer_is_not_an_eoa", c["from"], sent=False)
            ok, why = self.sdk_says(req, c)
            if not ok:
                return self.answer(False, why, c["from"], sent=False)
            ready, why = self.prepare(c)
        finally:
            self.checking.release()
        if why:
            return self.answer(False, why, c["from"], sent=False)
        if left() <= 0 or not self.sending.acquire(timeout=max(0.0, left())):
            return self.answer(False, "facilitator_busy", c["from"], sent=False)
        try:
            if left() <= 0:
                return self.answer(False, "facilitator_busy", c["from"], sent=False)
            why = self.short_of(c)
            if why:
                return self.answer(False, why, c["from"], sent=False)
            txh, how = self.send(key, c, req, ready, until=arrived + self.send_within + 5)
        finally:
            self.sending.release()
        # the block is awaited without any lock: other payments are not held up behind this one (M1, F3); for a
        # transfer that no RPC took there is no block to wait for
        if how != "refused":
            self.await_block(txh, until=arrived + ANSWER_WITHIN)
        with self.state:
            e = self.journal.get(key)
        return self.again(key, e, c, req)

    def short_of(self, c):
        """With `sending` held: "insufficient_funds" when the payer's balance now does not cover this payment together
        with its other payments sent and not yet decided (third reading, G5); else None. Asked here every time, not
        only with others in flight: the simulation came before the lock, and another payment of the same payer may
        have been paid and decided since. One already in a block and not yet decided counts twice for a moment:
        stricter, never looser."""
        with self.state:
            owed = self.journal.owed(c["from"])
        if self.usdc.functions.balanceOf(c["from"]).call(block_identifier="latest") < owed + int(c["value"]):
            log.info("short payer=%s: %d in flight", c["from"], owed)
            return "insufficient_funds"
        return None

    def again(self, key, e, c, req):
        """A payer and nonce in the journal: the same payment gets the same answer, another one is refused."""
        if e.get("auth") != c or e.get("req") != req:
            log.warning("conflict payer=%s: another authorization under a nonce already seen", c["from"])
            return self.answer(False, "authorization_conflict", c["from"], sent=True)
        if e.get("state") == "sent":
            return self.reconcile(key, e)
        return self.told(e)

    def told(self, e):
        """The answer for a payment decided: paid until REPLAY_FOR after its authorization ran out, then used."""
        c = e["auth"]
        if e["state"] == "paid":
            if time.time() <= int(c["validBefore"]) + REPLAY_FOR:
                return self.answer(True, None, c["from"], tx=e["tx"], sent=True)
            return self.answer(False, "payment_already_used", c["from"], tx=e["tx"], sent=True)
        return self.answer(False, e.get("reason", "transaction_failed"), c["from"], tx=e.get("tx", ""), sent=e.get("sent", True))

    def prepare(self, c):
        """What a transfer of the authorization `c` needs, asked of the chain without any lock: ((the call, gas, fee,
        tip, latest block), None) or (None, why not); refused below the gas floor, or when the simulation fails."""
        sig = bytes.fromhex(c["signature"][2:])
        fn = self.usdc.functions.transferWithAuthorization(
            c["from"], c["to"], int(c["value"]), int(c["validAfter"]), int(c["validBefore"]), bytes.fromhex(c["nonce"][2:]),
            sig[64], sig[:32], sig[32:64])
        try:
            # at the latest block, as the SDK's own simulation, whatever a node takes as its default: transfers of ours
            # still in flight are counted by short_of(), not by a node's idea of a pending block
            gas = min(int(fn.estimate_gas({"from": self.account.address}, block_identifier="latest") * 13 // 10), GAS_MOST)
        except (ContractLogicError, ValueError):
            return None, "transaction_simulation_failed"
        tip = self.w3.eth.max_priority_fee
        block = self.w3.eth.get_block("latest")
        fee = 2 * block["baseFeePerGas"] + tip
        have = self.w3.eth.get_balance(self.account.address)
        if have < max(self.gas_floor, gas * fee):
            log.warning("ALARM the gas wallet %s holds %d wei, below what a settlement needs; nothing is sent until it is "
                        "filled", self.account.address, have)
            return None, "facilitator_out_of_gas"
        if self.w3.eth.chain_id != self.chain_id:
            return None, "rpc_serves_another_chain"
        return (fn, gas, fee, tip, int(block.get("number", 0) or 0)), None

    def signed(self, ready, above=None):
        """With `sending` held: (hash, raw bytes, nonce) of the transfer signed under the gas key's next nonce. Our own
        count comes first: a balanced RPC's pending count may lack transfers of ours still waiting in another node's
        pool, and a nonce used twice refuses or replaces one of them (third reading, G4); the RPC's count when it is
        higher (a restart, or a transfer made elsewhere). `above`: a nonce found used, which the next one must pass."""
        fn, gas, fee, tip, _ = ready
        nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")
        for n in (self.next_nonce, above + 1 if above is not None else None):
            if n is not None:
                nonce = max(nonce, n)
        tx = self.account.sign_transaction(fn.build_transaction({
            "from": self.account.address, "chainId": self.chain_id, "gas": gas, "maxFeePerGas": fee,
            "maxPriorityFeePerGas": tip, "nonce": nonce}))
        return Web3.to_hex(tx.hash), Web3.to_hex(tx.raw_transaction), nonce

    def send(self, key, c, req, ready, until):
        """With `sending` held: the transfer of `c` signed, written down and handed to the RPC: (its hash, "took",
        "unsure" or "refused", as put_out() says)."""
        txh, raw, nonce = self.signed(ready)
        now = int(time.time())
        # written down before the chain sees it, so that a restart can only ever ask about it, never send it anew
        with self.state:
            self.journal.put(key, {"auth": c, "req": req, "state": "sent", "txs": [txh], "tx": txh, "raw": raw, "at": now,
                                   "last": now, "block": ready[4]})
        log.info("sending payer=%s tx=%s", c["from"], txh)
        return self.handed(key, ready, txh, raw, nonce, until)

    def handed(self, key, ready, txh, raw, nonce, until):
        """With `sending` held, the entry of `key` in the journal naming `txh` last: `raw` handed to the RPC. Signed
        under a nonce the chain has used already (our count behind it, as after a restart on a node behind), it is
        signed once more above that nonce and the entry names both. (the hash handed last, how it went)"""
        gone = []                   # transfers of ours that can never be in a block: no RPC took them, or their nonce
        how = self.put_out(txh, raw, nonce, until, ready)       # was used before they were handed over
        if how == "stale":
            gone.append(txh)
            txh, raw, nonce = self.signed(ready, above=nonce)
            with self.state:
                e = self.journal.get(key)
                self.journal.put(key, dict(e, txs=(e.get("txs") or [e["tx"]]) + [txh], tx=txh, raw=raw, last=int(time.time())))
            log.info("sending payer=%s tx=%s (nonce found used)", e["auth"]["from"], txh)
            how = self.put_out(txh, raw, nonce, until, ready)
            how = "refused" if how == "stale" else how
        if how == "refused":
            gone.append(txh)
        with self.state:
            e = self.journal.get(key)
            # a refused transfer's nonce is the next one's: it and anything signed under it later exclude each other
            was = [t for t in (e.get("gone") or []) if t != txh or how == "refused"]
            if gone or was != (e.get("gone") or []) or bool(e.get("refused")) != (how == "refused"):
                self.journal.put(key, dict(e, refused=how == "refused", gone=sorted(set(was) | set(gone))))
        return txh, how

    def put_out(self, txh, raw, nonce, until, ready):
        """With `sending` held: the signed transfer `raw` handed to the RPC, tried again a second later while the RPC
        refuses it and time is left before `until` (time.monotonic()), at most BROADCAST_TRIES times: a payer told to
        come back would not, so a lost broadcast is tried within its own request, never later (third reading, G1).
        "took": the RPC holds it; "unsure": it may (an answer lost, or its nonce found used after such a try);
        "refused": no RPC took it, and its nonce is the next transfer's (G4); "stale": its nonce was used on the chain
        before it was ever handed over."""
        how = "refused"
        for i in range(BROADCAST_TRIES):
            if i:
                if time.monotonic() + 1 > until:
                    break
                time.sleep(1)
            try:
                self.w3.eth.send_raw_transaction(raw)
                how = "took"
            except Exception as ex:
                said = str(ex).lower()
                log.warning("broadcast %s", type(ex).__name__)
                if any(w in said for w in ("already known", "known transaction", "already imported")):
                    how = "took"
                elif "nonce too low" in said or "replacement transaction underpriced" in said:
                    if how != "unsure":
                        return "stale"
                    # after a try whose answer was lost: most likely that very try, now in a block
                elif any(w in said for w in ("timed out", "timeout", "reset", "aborted", "broken pipe")):
                    how = "unsure"          # it may have reached a node before the answer was lost
                    continue
                else:
                    continue                # refused: rate limited, no connection, or the node said no
            break
        if how == "refused":
            if self.next_nonce is None:
                self.next_nonce = nonce
        else:
            if how == "took":
                self.took.add(txh)
            self.next_nonce = max(self.next_nonce or 0, nonce + 1)
            self.ours[nonce] = {"tx": txh, "at": time.time(), "fee": ready[2], "tip": ready[3], "first_fee": ready[2],
                                "first_tip": ready[3]}
        return how

    def resend(self, key, e):
        """For a request that shows this payment again: the chain lost every transfer of ours for it while its
        authorization holds, or no RPC took the last one: one more, at today's fee. Not when the simulation fails now
        (taken meanwhile, or the coins moved)."""
        c = e["auth"]
        if self.held():             # it would wait behind a nonce of ours that stays open (fifth reading)
            return self.answer(False, "settlement_pending", c["from"], tx=e["tx"], sent=True, known=False)
        ready, why = self.prepare(c)
        if why or not self.sending.acquire(timeout=SEND_WAIT):
            return self.answer(False, "settlement_pending", c["from"], tx=e["tx"], sent=True, known=False)
        try:
            txh, raw, nonce = self.signed(ready)
            with self.state:
                e = dict(e, txs=(e.get("txs") or [e["tx"]]) + [txh], tx=txh, raw=raw, last=int(time.time()))
                self.journal.put(key, e)
            log.info("sending again payer=%s tx=%s", c["from"], txh)
            txh, how = self.handed(key, ready, txh, raw, nonce, time.monotonic() + 5)
            with self.state:
                e = self.journal.get(key)
        finally:
            self.sending.release()
        return self.answer(False, "settlement_pending", c["from"], tx=txh, sent=True, known=how != "refused" and self.known(txh, e))

    def await_block(self, txh, until=None):
        """Without any lock: waits up to --confirm-timeout for the block of `txh`, and no later than `until`
        (time.monotonic()), so that the answer comes within the gate's wait when the RPC answers in its own time
        (fourth reading, N2). Where things stand is asked after."""
        wait = self.confirm_timeout if until is None else min(self.confirm_timeout, until - time.monotonic())
        if wait <= 0:
            return
        try:
            self.w3.eth.wait_for_transaction_receipt(txh, timeout=wait, poll_latency=1)
        except Exception:         # not in a block within the time, or the RPC failed meanwhile
            pass

    def held(self):
        """True while a nonce of ours found lost has stayed open longer than GAP_HOLD (its filler could not be handed
        over, or is not in a block): new payments are then refused, nothing sent."""
        gap = self.gap
        return gap is not None and time.time() - gap["since"] > GAP_HOLD

    def receipt(self, txh):
        try:
            return self.w3.eth.get_transaction_receipt(txh)
        except TransactionNotFound:
            return None

    def known(self, txh, e):
        """Whether the chain holds the transfer `txh`, not yet in a block. When the RPC cannot say, or says it does not
        within half a minute of taking it (a node behind a balancer), its having taken it counts (second reading, N1)."""
        took = txh in self.took
        try:
            self.w3.eth.get_transaction(txh)
            return True
        except TransactionNotFound:
            return took and time.time() - e.get("last", e.get("at", 0)) < 30
        except Exception:
            return took

    def of_token(self, lg):
        return str(lg.get("address", "")).lower() == self.asset.lower()

    def paid_in(self, rc, c):
        """True for a receipt in which the token took the authorization `c` and with it moved its whole value from the
        payer to us: AuthorizationUsed(payer, nonce) and, right after it, Transfer(payer, payTo, value), both of the
        token, as FiatToken logs them. A use of the nonce may move any value to anyone (EIP-3009 nonces are the
        payer's own, whatever `to` and `value`), and a Transfer to us elsewhere in the same transaction may belong to
        another authorization of the same payer (third reading, G0)."""
        if rc is None or rc.get("status") != 1:
            return False
        logs = sorted(rc.get("logs") or [], key=lambda lg: int(lg.get("logIndex") or 0))
        used = [AUTH_USED, topic(c["from"]), c["nonce"].lower()]
        moved = [TRANSFER, topic(c["from"]), topic(c["to"])]
        for i, lg in enumerate(logs):
            if self.of_token(lg) and topics(lg) == used:
                after = logs[i + 1] if i + 1 < len(logs) else None
                return after is not None and self.of_token(after) and topics(after) == moved and amount(after) == int(c["value"])
        return False

    def logs_of(self, wanted, start, end):
        """The token's logs with the topics `wanted` from block `start` to `end`, asked log_chunk blocks at a time."""
        while start <= end:
            upto = min(end, start + self.log_chunk - 1)
            found = self.w3.eth.get_logs({"address": self.asset, "fromBlock": start, "toBlock": upto, "topics": wanted})
            if found:
                return list(found)
            start = upto + 1
        return []

    def fate(self, c, e, at):
        """What became of the authorization `c` by block `at`, for a payment no transfer of ours paid: ("free", None)
        while the token has not taken it; ("paid", tx) when another transaction used it and paid us with it;
        ("elsewhere", tx) when one used its nonce otherwise, which the payer can do with an authorization of its own
        under the same nonce, to anyone (third reading, G0); ("canceled", tx); ("unknown", tx or None) when the RPC
        cannot tell which. All of it as of `at`, so that a node behind cannot answer for later blocks (G2)."""
        try:
            if not self.usdc.functions.authorizationState(c["from"], bytes.fromhex(c["nonce"][2:])).call(block_identifier=at):
                return "free", None
            start = int(e["block"]) - 10 if e.get("block") else at - int((time.time() - e.get("at", 0)) / 2) - 100
            start = max(0, start)
            end = min(at, start + LOG_SPAN)
            used = self.logs_of([AUTH_USED, topic(c["from"]), c["nonce"].lower()], start, end)
            if used:
                by = hexed(used[0]["transactionHash"])
                rc = self.receipt(by)
                if rc is None:
                    return "unknown", by
                return ("paid" if self.paid_in(rc, c) else "elsewhere"), by
            canceled = self.logs_of([AUTH_CANCELED, topic(c["from"]), c["nonce"].lower()], start, end)
            if canceled:
                return "canceled", hexed(canceled[0]["transactionHash"])
            return "unknown", None
        except Exception as ex:
            log.warning("fate payer=%s %s", c["from"], type(ex).__name__)
            return "unknown", None

    def block_time(self, n):
        """The time of block `n`, or 0 when the RPC cannot say."""
        try:
            return int(self.w3.eth.get_block(n).get("timestamp") or 0)
        except Exception:
            return 0

    def head_past(self, t):
        """The number of the latest block this RPC has, when that block's time is at least `t`; else None: a node
        behind, or blocks later than the clock, from which no outcome is decided (third reading, G2)."""
        try:
            b = self.w3.eth.get_block("latest")
        except Exception as ex:
            log.warning("head %s", type(ex).__name__)
            return None
        return int(b["number"]) if int(b.get("timestamp") or 0) >= t else None

    def decide(self, key, e, state, tx, reason=None, sent=True, by=None):
        """Writes the outcome of a payment down and answers as for any payment decided. `by`: another transaction that
        took the authorization, for the record."""
        e = dict(e, state=state, tx=tx, decided=int(time.time()))
        if state == "failed":
            e.update(reason=reason, sent=sent)
        if by:
            e["by"] = by
        with self.state:
            self.journal.put(key, e)
        if state == "paid":
            log.info("settled payer=%s tx=%s", e["auth"]["from"], tx)
        else:
            log.warning("%s payer=%s tx=%s%s", reason, e["auth"]["from"], tx, " by=%s" % by if by else "")
        return self.told(e)

    def otherwise(self, key, e, how, by, ours, free):
        """The outcome of a payment none of whose transfers paid, from what became of its authorization (fate(), not
        "unknown"). `free`: the reason when the token never took it."""
        if how == "paid":
            return self.decide(key, e, "paid", by)          # another transaction paid us with this very authorization
        if how in ("elsewhere", "canceled"):
            # its nonce is gone and nothing came to us with it: nothing moved for this payment, nor will
            return self.decide(key, e, "failed", ours, reason="authorization_used_elsewhere" if how == "elsewhere"
                               else "authorization_canceled", sent=False, by=by)
        # never taken: after its end nothing can move any more (sent: false); before it, a transfer of ours failed in
        # its block, and the authorization stays usable by anyone until its end (sent: true; third reading, N4)
        return self.decide(key, e, "failed", ours, reason=free, sent=free == "transaction_failed")

    def reconcile(self, key, e, may_send=True):
        """Where a payment that was sent stands now; its key is this thread's alone while this runs.
        Paid: a transfer of ours paid us, or another transaction paid us with the same authorization (paid_in).
        Failed: every transfer of ours is in a block and none paid, and the token took the authorization otherwise
        (used elsewhere, canceled) or not at all, as read at the block of our last one. Waiting, and then whether the
        chain holds the transfer ("known"): a transfer the chain lost, or that no RPC took, is sent again only for a
        request that shows this payment (`may_send`), never by the background look, whose payer may have been told to
        go away (third reading, G1). Not paid ("sent": false, nothing moved and nothing can): once the chain's head is
        expiry_margin past validBefore, as read at that head (G2); a payment whose outcome still cannot be found
        UNKNOWN_FOR after its end is given up for a person to look at."""
        c, now, txs = e["auth"], int(time.time()), e.get("txs") or [e["tx"]]
        receipts = {t: self.receipt(t) for t in txs}
        for t, rc in receipts.items():
            if self.paid_in(rc, c):
                return self.decide(key, e, "paid", t)
        live = [t for t in txs if t not in (e.get("gone") or [])]   # those that can still be in a block
        if live and all(receipts[t] is not None for t in live):     # every one of them is in a block, none paid
            at = max(int(receipts[t].get("blockNumber") or 0) for t in live)
            how, by = self.fate(c, e, at)
            if how != "unknown":
                free = "transaction_failed"
                if how == "free" and self.block_time(at) >= int(c["validBefore"]):
                    free = "authorization_expired"  # ours came after its end: nothing can move any more (fourth reading)
                return self.otherwise(key, e, how, by, live[-1], free=free)
            if now > int(c["validBefore"]) + UNKNOWN_FOR:
                return self.give_up(key, e)
            return self.answer(False, "settlement_pending", c["from"], tx=live[-1], sent=True, known=False)
        waiting = [t for t in live if receipts[t] is None and self.known(t, e)]
        if now < int(c["validBefore"]):
            if waiting:
                return self.answer(False, "settlement_pending", c["from"], tx=waiting[-1], sent=True, known=True)
            if may_send and (e.get("refused") or now - e.get("last", e["at"]) >= 60):
                return self.resend(key, e)
            return self.answer(False, "settlement_pending", c["from"], tx=e["tx"], sent=True, known=False)
        end = int(c["validBefore"]) + self.expiry_margin
        head = self.head_past(end) if now >= end else None
        how, by = self.fate(c, e, head) if head is not None else ("unknown", None)
        if how != "unknown":
            return self.otherwise(key, e, how, by, e["tx"], free="authorization_expired")
        if now > int(c["validBefore"]) + UNKNOWN_FOR:
            return self.give_up(key, e)
        # past validBefore a transfer still waiting can no longer pay: "known" is no reason to let a call pass
        return self.answer(False, "settlement_pending", c["from"], tx=e["tx"], sent=True, known=False)

    def give_up(self, key, e):
        """A payment whose outcome the RPC still cannot tell UNKNOWN_FOR after its end: failed for the gate, which
        lets no call pass for it, and an ALARM for a person to look at the chain."""
        log.warning("ALARM payer=%s tx=%s: what became of this payment cannot be found; given up, look at it",
                    e["auth"]["from"], e["tx"])
        return self.decide(key, e, "failed", e["tx"], reason="settlement_unknown", sent=True)

    def mend(self):
        """Once a look: the lowest nonce of ours in no block whose transfer the chain lost (unknown to the RPC lost_after
        seconds after it was handed over) holds back every transfer of ours after it, while the chain holds those as
        known (third reading, G4). It alone is filled, with a transfer of nothing to ourselves, which charges nobody;
        the nonces after it are judged once it is in a block: a transfer queued behind a lost one is as old, and as
        unknown to a node that does not hold our pool, yet alive (fourth reading, R2). A nonce this facilitator holds
        no transfer for (lost before a restart and not in the journal) counts as lost once transfers of ours above it
        have waited lost_after (R3). The filler bids 1.2 times the last transfer handed over under that nonce, at most
        FILL_MOST times today's fee or that of the transfer it fills, and is kept only once handed over (R1). While
        the nonce stays open, new payments are refused after GAP_HOLD (held()) and an ALARM is written every
        ALARM_EVERY; the payments it was for are decided as any other, never sent again from here."""
        if not self.sending.acquire(timeout=1):
            return
        try:
            if self.next_nonce is None:
                return
            n = self.w3.eth.get_transaction_count(self.account.address, "latest")
            for k in [k for k in self.ours if k < n]:
                del self.ours[k]
            if self.gap is not None and self.gap["nonce"] < n:
                log.info("nonce %d is in a block", self.gap["nonce"])
                self.gap = None
            if n >= self.next_nonce:
                self.gap = None
                return
            o = self.ours.get(n)
            if o is None:
                later = [v["at"] for k, v in self.ours.items() if k > n]
                if not later or time.time() - min(later) < self.lost_after:
                    return
                o = {"tx": None, "fee": 0, "tip": 0, "first_fee": 0, "first_tip": 0}
            elif time.time() - o["at"] < self.lost_after:
                return
            else:
                try:
                    self.w3.eth.get_transaction(o["tx"])
                    return                              # the chain holds it: late, not lost
                except TransactionNotFound:
                    pass
            if self.gap is None or self.gap["nonce"] != n:
                self.gap = {"nonce": n, "since": time.time(), "first": time.time(), "told": 0.0, "fillers": 0}
            now_tip = self.w3.eth.max_priority_fee
            now_fee = 2 * self.w3.eth.get_block("latest")["baseFeePerGas"] + now_tip
            if o["tx"] is None:                         # what it was signed at is not known: well above today's fees
                tip, fee = 2 * now_tip, 2 * now_fee
            else:
                tip = max(now_tip, o["tip"] * 12 // 10 + 1)
                fee = max(now_fee, o["fee"] * 12 // 10 + 1)
            tip = min(tip, FILL_MOST * max(now_tip, o.get("first_tip") or 0))
            fee = max(min(fee, FILL_MOST * max(now_fee, o.get("first_fee") or 0)), tip)
            tx = self.account.sign_transaction({"to": self.account.address, "value": 0, "gas": 21_000, "nonce": n,
                                                "chainId": self.chain_id, "maxFeePerGas": fee, "maxPriorityFeePerGas": tip})
            txh = Web3.to_hex(tx.hash)
            try:
                self.w3.eth.send_raw_transaction(tx.raw_transaction)
            except Exception as ex:
                said = str(ex).lower()
                if "nonce too low" in said:
                    return                              # it is in a block meanwhile
                if not any(w in said for w in ("already known", "known transaction", "already imported")):
                    self.alarm("the filler of nonce %d could not be handed over (%s); transfers of ours wait behind it"
                               % (n, type(ex).__name__))
                    return
            self.ours[n] = {"tx": txh, "at": time.time(), "fee": fee, "tip": tip,
                            "first_fee": o.get("first_fee") or 0, "first_tip": o.get("first_tip") or 0}
            # the first filler handed over: its block comes in seconds, payments may go on meanwhile; one more for the
            # same nonce means the RPC lost the last: held from when the nonce was found lost (fifth reading)
            self.gap["fillers"] = self.gap.get("fillers", 0) + 1
            self.gap["since"] = time.time() if self.gap["fillers"] == 1 else self.gap["first"]
            log.warning("nonce %d lost with tx=%s: filled with tx=%s, a transfer of nothing", n,
                        o["tx"] or "(signed before this start)", txh)
        except Exception as ex:
            log.warning("mend %s", type(ex).__name__)
        finally:
            if self.held():
                self.alarm("nonce %d of the gas key has stayed open for %d s; new payments are refused until it is in a "
                           "block" % (self.gap["nonce"], time.time() - self.gap["first"]))
            self.sending.release()

    def alarm(self, text):
        """An ALARM line about the open nonce: at once the first time, then at most once every ALARM_EVERY."""
        gap = self.gap
        if gap is not None and time.time() - gap["told"] >= ALARM_EVERY:
            gap["told"] = time.time()
            log.warning("ALARM %s", text)

    def look_after(self):
        """Every RECONCILE_EVERY seconds, each payment sent and not yet decided is looked at, so that the journal
        comes to its outcome without anyone showing the payment (second reading, F1): decided, never sent again
        (third reading, G1). Then a nonce of ours the chain lost is mended."""
        while not self.stopped.wait(RECONCILE_EVERY):     # real seconds, whatever clock the module is given
            self.look_once()

    def look_once(self):
        with self.state:
            keys = self.journal.undecided()
        for k in keys:
            with self.state:
                if k in self.busy:
                    continue
                e = self.journal.get(k)
                if e is None or e.get("state") != "sent":
                    continue
                self.busy.add(k)
            try:
                self.reconcile(k, e, may_send=False)
            except Exception as ex:
                log.warning("look error payer=%s %s", e.get("auth", {}).get("from"), type(ex).__name__)
            finally:
                with self.state:
                    self.busy.discard(k)
        self.mend()

    def supported(self):
        return {"kinds": [{"x402Version": 2, "scheme": "exact", "network": self.network}], "extensions": [],
                "signers": {"eip155:*": [self.account.address]}}

    def health(self):
        """Who this facilitator is, always (200), and how it is: "ok" false with the error when its RPC does not answer.
        The gate checks at its start what this facilitator settles, whatever its RPC does (second reading, F4)."""
        h = {"ok": True, "protocol": PROTOCOL, "network": self.network, "asset": self.asset, "payTo": self.pay_to,
             "signer": self.account.address, "gasFloorWei": str(self.gas_floor), "maxAmount": str(self.max_amount),
             "confirmTimeout": self.confirm_timeout, "sendWithin": self.send_within}
        with self.state:
            h["waiting"] = self.journal.waiting()
        gap = self.gap
        if gap is not None:
            h["openNonce"], h["openFor"] = gap["nonce"], int(time.time() - gap["first"])
        try:
            h["gasWei"] = str(self.w3.eth.get_balance(self.account.address))
        except Exception as e:
            h.update(ok=False, error=type(e).__name__)
        return 200, h


def make_handler(fac, port):
    hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version, timeout = "usdc-facilitator", "", 10

        def log_message(self, fmt, *args):
            pass  # the facilitator logs what matters itself

        def answer(self, code, obj):
            data = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def here(self):
            if self.headers.get("Host") in hosts:      # a page in a browser of this machine is not let in (rebinding)
                return True
            self.answer(421, {"error": "wrong_host"})
            return False

        def do_GET(self):
            if not self.here():
                return
            if self.path == "/supported":
                self.answer(200, fac.supported())
            elif self.path == "/health":
                self.answer(*fac.health())
            else:
                self.answer(404, {"error": "not_found"})

        def do_POST(self):
            arrived = time.monotonic()
            if not self.here():
                return
            if self.path not in ("/verify", "/settle"):
                return self.answer(404, {"error": "not_found"})

            def refused(why):
                # nothing was done: said as /settle and /verify say it, so that the gate drops what it bound for this
                # payment instead of keeping it for a day (second reading, F2)
                if self.path == "/settle":
                    return self.answer(200, fac.answer(False, why, None, sent=False))
                return self.answer(200, {"isValid": False, "invalidReason": why, "payer": None})
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.close_connection = True
                return refused("length_required")
            if not 0 < n <= MAX_BODY:
                self.close_connection = True
                return refused("request_too_large")
            try:
                body = json.loads(self.rfile.read(n))
            except (ValueError, RecursionError):
                return refused("invalid_json")
            self.answer(200, fac.verify(body) if self.path == "/verify" else fac.settle(body, arrived))

    return Handler


def rpc_url(text):
    """An RPC is reached over https, or on this machine."""
    p = urllib.parse.urlsplit(text)
    if p.scheme == "https" or (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1")):
        return text
    sys.exit("the RPC is reached over https, or on this machine")


def main(argv=None):
    ap = argparse.ArgumentParser(description="USDC facilitator for x402 (exact, EIP-3009), on this machine only")
    ap.add_argument("--network", required=True, choices=NETWORKS)
    rpc = ap.add_mutually_exclusive_group(required=True)
    rpc.add_argument("--rpc", help="RPC of the network (one without a key in it; else --rpc-file)")
    rpc.add_argument("--rpc-file", help="a file only you may read with the RPC's address, which may hold a key")
    ap.add_argument("--key-file", required=True, help="the gas key, in a file only you may read")
    ap.add_argument("--pay-to", required=True, help="our address; payments to anyone else are refused")
    ap.add_argument("--max-amount", type=int, default=1_000_000, help="largest payment in base units (1 USDC = 1000000)")
    ap.add_argument("--port", type=int, default=8402, help="port on 127.0.0.1")
    ap.add_argument("--confirm-timeout", type=float, default=15.0, help="seconds to wait for a block before answering")
    ap.add_argument("--state-dir", required=True, help="folder of the journal of payments, kept over restarts")
    ap.add_argument("--gas-floor-wei", type=int, default=20_000_000_000_000, help="no settlement below this much gas")
    ap.add_argument("--send-within", type=float, default=SEND_WITHIN,
                    help="seconds from a request within which a new payment is sent, or refused with nothing sent")
    ap.add_argument("--expiry-margin", type=int, default=EXPIRY_MARGIN,
                    help="seconds after validBefore before an authorization the token never took is given up (tests)")
    ap.add_argument("--lost-after", type=int, default=LOST_AFTER,
                    help="seconds after which a transfer of ours in no block and unknown to the RPC is lost (tests)")
    ap.add_argument("--log-chunk", type=int, default=LOG_CHUNK,
                    help="blocks asked for at once in eth_getLogs (lower it for an RPC that caps the range)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # the SDK writes a warning for every payment that fails its simulation: payments of keys with no USDC cost their
    # sender nothing, and would fill the log; the reason goes back in the answer
    logging.getLogger("x402").setLevel(logging.ERROR)
    if not ADDRESS.fullmatch(a.pay_to) or not Web3.is_checksum_address(a.pay_to):
        sys.exit("--pay-to must be an address with its checksum (EIP-55)")
    if not 1 <= a.confirm_timeout <= 60:
        sys.exit("--confirm-timeout is 1 to 60 seconds")
    if not 5 <= a.send_within <= 60:
        sys.exit("--send-within is 5 to 60 seconds")
    if not 1 <= a.expiry_margin <= 3600:
        sys.exit("--expiry-margin is 1 to 3600 seconds")
    if not 5 <= a.lost_after <= 3600:
        sys.exit("--lost-after is 5 to 3600 seconds")
    if not 10 <= a.log_chunk <= LOG_SPAN:
        sys.exit("--log-chunk is 10 to %d blocks" % LOG_SPAN)
    url = rpc_url(a.rpc if a.rpc else read_secret(a.rpc_file))
    fac = Facilitator(a.network, url, read_secret(a.key_file), a.pay_to, a.max_amount, a.confirm_timeout, a.state_dir,
                      a.gas_floor_wei, a.send_within, a.expiry_margin, a.lost_after, a.log_chunk)
    threading.Thread(target=fac.look_after, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(fac, a.port))
    srv.daemon_threads = True
    log.info("listening on 127.0.0.1:%d network=%s signer=%s payTo=%s waiting=%d", a.port, a.network, fac.account.address,
             a.pay_to, fac.journal.waiting())
    srv.serve_forever()


if __name__ == "__main__":
    main()
