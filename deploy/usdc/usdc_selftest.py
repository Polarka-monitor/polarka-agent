#!/usr/bin/env python3
"""Self-test of the USDC facilitator with the official x402 client.

A test payer signs EIP-3009 authorizations as any x402 buyer would, and the facilitator is asked to check them:
a good one, a forged one, ones it must refuse (someone else's address, too much, another network, expired).
With --settle it also settles the good one, checks the balances moved, and tries to use the same one again.
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

from eth_account import Account
from web3 import Web3
from x402 import x402ClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.default_assets import get_default_asset
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.schemas import PaymentRequired, PaymentRequirements

from usdc_facilitator import read_key

BALANCE_ABI = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
                "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]}]
results = []


def call(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, ok, detail):
    results.append(ok)
    print("%s  %s -> %s" % ("OK  " if ok else "FAIL", name, detail), flush=True)


def attacks(a, info, payer, good, req, body, copy, signed, wanted, balance):
    """On a local anvil fork only: what must be refused before anything is sent (and says so: sent false), and
    payments at once whose blocks are late."""
    import os
    from usdc_facilitator import CURVE_N, typed
    w3 = Web3(Web3.HTTPProvider(a.rpc))
    chain, asset = int(a.network.split(":")[1]), Web3.to_checksum_address(info["asset"])

    def sign(auth, key):
        c = {k: auth[k] for k in ("from", "to", "value", "validAfter", "validBefore", "nonce")}
        return Web3.to_hex(Account.sign_message(typed(c, chain, asset, info["name"], info["version"]), key).signature)

    other = copy(good)
    other["payload"]["authorization"]["validBefore"] = str(int(other["payload"]["authorization"]["validBefore"]) + 60)
    other["payload"]["signature"] = sign(other["payload"]["authorization"], payer.key)
    code, s = call(a.url, "/settle", body(other, req))
    check("14 another authorization of the payer under a nonce already settled is refused",
          s.get("errorReason") == "authorization_conflict" and s.get("success") is False, json.dumps(s))

    fresh = signed(req)
    sig = bytes.fromhex(fresh["payload"]["signature"][2:])
    high = copy(fresh)          # the same signature with the other s (n - s) and v: valid for ecrecover, not canonical
    high["payload"]["signature"] = "0x" + (sig[:32] + (CURVE_N - int.from_bytes(sig[32:64], "big")).to_bytes(32, "big")
                                           + bytes([55 - sig[64]])).hex()
    code, s = call(a.url, "/settle", body(high, req))
    check("15 a signature not in canonical form is refused before anything is sent",
          s.get("errorReason") == "invalid_signature_form" and s.get("sent") is False, json.dumps(s))

    ghost = Account.create()    # an account with code (a contract wallet, or an EIP-7702 delegation)
    w3.provider.make_request("anvil_setCode", [ghost.address, "0x6001600055"])
    # it holds the price: the facilitator reads a payer's balance first (one call for a key holding nothing), and
    # it is the check of its code that must refuse it here
    erc20 = w3.eth.contract(address=asset, abi=[{"name": "transfer", "type": "function", "stateMutability": "nonpayable",
                                                 "inputs": [{"name": "to", "type": "address"}, {"name": "v", "type": "uint256"}],
                                                 "outputs": [{"name": "", "type": "bool"}]}])
    w3.provider.make_request("anvil_setBalance", [payer.address, hex(10 ** 18)])
    w3.eth.send_transaction({"from": payer.address, "to": asset, "data": erc20.encode_abi("transfer", [ghost.address, a.amount])})
    now = int(time.time())
    auth = {"from": ghost.address, "to": Web3.to_checksum_address(a.pay_to), "value": str(a.amount), "validAfter": str(now - 60),
            "validBefore": str(now + 300), "nonce": "0x" + os.urandom(32).hex()}
    coded = copy(fresh)
    coded["payload"] = {"signature": sign(auth, ghost.key), "authorization": auth}
    code, s = call(a.url, "/settle", body(coded, req))
    check("16 a payer with code is refused before anything is sent",
          s.get("errorReason") == "payer_is_not_an_eoa" and s.get("sent") is False, json.dumps(s))

    gas = call(a.url, "/health")[1]["signer"]
    held = w3.eth.get_balance(gas)
    w3.provider.make_request("anvil_setBalance", [gas, hex(10 ** 9)])
    try:
        code, s = call(a.url, "/settle", body(signed(req), req))
    finally:
        w3.provider.make_request("anvil_setBalance", [gas, hex(held)])
    check("17 below the gas floor nothing is sent", s.get("errorReason") == "facilitator_out_of_gas" and s.get("sent") is False,
          json.dumps(s))

    # M1: the block is awaited without the lock, so payments at once are not answered one wait after another
    wait = float(call(a.url, "/health")[1].get("confirmTimeout", 5))
    start = (balance(payer.address), balance(a.pay_to))
    many, out = [signed(req) for _ in range(5)], [None] * 5

    def one(i):
        out[i] = call(a.url, "/settle", body(many[i], req))[1]
    w3.provider.make_request("evm_setAutomine", [False])
    try:
        t0 = time.time()
        threads = [threading.Thread(target=one, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        took = time.time() - t0
    finally:
        w3.provider.make_request("evm_setAutomine", [True])
    check("18 five payments at once, their blocks late, are answered within about one wait, not five",
          took < 2 * wait + 5 and all(s.get("errorReason") == "settlement_pending" and s.get("known") is True for s in out),
          "%.1f s for five, one wait is %.0f s: %s" % (took, wait, [s.get("errorReason") for s in out]))
    w3.provider.make_request("evm_mine", [])
    again = [call(a.url, "/settle", body(p, req))[1] for p in many]
    end = (balance(payer.address), balance(a.pay_to))
    check("19 then each of the five is paid once, by a transfer of its own",
          all(s.get("success") is True for s in again) and len({s.get("transaction") for s in again}) == 5
          and (start[0] - end[0], end[1] - start[1]) == (5 * a.amount, 5 * a.amount), "%s -> %s" % (start, end))
    later(a, info, payer, body, signed, wanted, balance, w3, asset, sign)


def later(a, info, payer, body, signed, wanted, balance, w3, asset, sign):
    """The second reading's findings on the fork: F3 (payments of keys with no USDC do not hold up a real one), F2 (an
    r off the curve and a request too large are answered, with nothing sent), F1 (a paid payment is answered from the
    journal after its validBefore; one the chain lost and the token never took is decided as not paid, by the
    facilitator's own look, without anyone showing it). The facilitator runs with --expiry-margin 5."""
    import os
    erc20 = w3.eth.contract(address=asset, abi=[{"name": "transfer", "type": "function", "stateMutability": "nonpayable",
                                                 "inputs": [{"name": "to", "type": "address"}, {"name": "v", "type": "uint256"}],
                                                 "outputs": [{"name": "", "type": "bool"}]}])
    nobody = [Account.create() for _ in range(20)]
    now = int(time.time())

    def bogus(who):
        auth = {"from": who.address, "to": Web3.to_checksum_address(a.pay_to), "value": str(a.amount), "validAfter": "0",
                "validBefore": str(now + 300), "nonce": "0x" + os.urandom(32).hex()}
        p = signed(wanted())
        p["payload"] = {"signature": sign(auth, who.key), "authorization": auth}
        return p
    out, real = [None] * len(nobody), {}

    def one(i):
        out[i] = call(a.url, "/settle", body(bogus(nobody[i]), wanted()))[1]

    def good():
        t0 = time.time()
        real["s"], real["took"] = call(a.url, "/settle", body(signed(wanted()), wanted()))[1], None
        real["took"] = time.time() - t0
    threads = [threading.Thread(target=one, args=(i,)) for i in range(len(nobody))] + [threading.Thread(target=good)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("20 twenty payments of keys with no USDC at once do not hold up a real one, and none sends anything",
          real["s"].get("success") is True and real["took"] < 10 and all(s.get("success") is False and s.get("sent") is False
                                                                          for s in out),
          "real: %s in %.1f s; refused: %s" % (real["s"].get("success"), real["took"], sorted({s.get("errorReason") for s in out})))

    r = 5       # no point of the curve has this x
    bent = signed(wanted())
    bent["payload"]["signature"] = "0x" + r.to_bytes(32, "big").hex() + (12345).to_bytes(32, "big").hex() + "1b"
    code, s = call(a.url, "/settle", body(bent, wanted()))
    check("21 a signature whose r is no point of the curve is answered, with nothing sent", code == 200
          and s.get("errorReason") == "signature_is_not_the_payers" and s.get("sent") is False, json.dumps(s))
    big = json.dumps(body(dict(signed(wanted()), padding="x" * 70000), wanted())).encode()
    req = urllib.request.Request(a.url + "/settle", big, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        s = json.loads(resp.read())
        code = resp.status
    check("22 a request too large is answered as a payment refused, with nothing sent", code == 200
          and s.get("errorReason") == "request_too_large" and s.get("sent") is False, json.dumps(s))

    short = wanted(timeout=61)
    p1, p2 = signed(short), signed(short)
    s1 = call(a.url, "/settle", body(p1, short))[1]
    # third reading, G1 and G4: a payer whose coins stay where they are, its transfer lost as well
    gas = call(a.url, "/health")[1]["signer"]
    third = Account.create()
    w3.provider.make_request("anvil_setBalance", [payer.address, hex(10 ** 18)])
    w3.eth.send_transaction({"from": payer.address, "to": asset, "data": erc20.encode_abi("transfer", [third.address, a.amount])})
    count = w3.eth.get_transaction_count(gas, "latest")
    w3.provider.make_request("evm_setAutomine", [False])
    try:
        s2 = call(a.url, "/settle", body(p2, short))[1]
        lost = s2.get("transaction")
        w3.provider.make_request("anvil_dropTransaction", [lost])
        longer = wanted(timeout=70)     # made now, so that it is valid for more than a minute when shown
        p3 = signed(longer)
        a3 = {"from": third.address, "to": Web3.to_checksum_address(a.pay_to), "value": str(a.amount), "validAfter": "0",
              "validBefore": str(int(time.time()) + 70), "nonce": "0x" + os.urandom(32).hex()}
        p3["payload"] = {"signature": sign(a3, third.key), "authorization": a3}
        s3 = call(a.url, "/settle", body(p3, longer))[1]
        w3.provider.make_request("anvil_dropTransaction", [s3.get("transaction")])
    finally:
        w3.provider.make_request("evm_setAutomine", [True])
    # the payer's coins go elsewhere for a while, so that the facilitator could not send the lost one again
    aside = Account.create().address
    held = balance(payer.address)
    for who in (payer.address, aside):
        w3.provider.make_request("anvil_setBalance", [who, hex(10 ** 18)])
    w3.eth.send_transaction({"from": payer.address, "to": asset, "data": erc20.encode_abi("transfer", [aside, held])})
    until = max(int(p2["payload"]["authorization"]["validBefore"]), int(a3["validBefore"])) + 5 + 35
    print("     (waiting %d s for the authorizations to run out and the facilitator to look)" % max(0, until - time.time()), flush=True)
    while time.time() < until:     # a block every 2 s at the clock's time, as on Base: an end is decided only from a
        # head past it (G2), and this fork of anvil gives a new block its parent's time unless told otherwise
        w3.provider.make_request("evm_setNextBlockTimestamp", [int(time.time())])
        w3.provider.make_request("evm_mine", [])
        time.sleep(2)
    waiting = call(a.url, "/health")[1].get("waiting")
    s1b = call(a.url, "/settle", body(p1, short))[1]
    s2b = call(a.url, "/settle", body(p2, short))[1]
    s3b = call(a.url, "/settle", body(p3, longer))[1]
    w3.eth.send_transaction({"from": aside, "to": asset, "data": erc20.encode_abi("transfer", [payer.address, held])})
    check("23 a payment paid is answered as paid after its authorization ran out", s1.get("success") is True
          and s1b.get("success") is True and s1b.get("transaction") == s1.get("transaction"), json.dumps(s1b))
    check("24 one the chain lost is decided as not paid by the facilitator itself, with nothing sent", s2.get("errorReason") ==
          "settlement_pending" and waiting == 0 and s2b.get("errorReason") == "authorization_expired" and s2b.get("sent") is False
          and balance(payer.address) == held, "waiting %s; %s" % (waiting, json.dumps(s2b)))
    check("25 a lost transfer whose payer could pay is not sent again by the facilitator's look (G1)",
          s3.get("errorReason") == "settlement_pending" and s3b.get("errorReason") == "authorization_expired"
          and s3b.get("sent") is False and balance(third.address) == a.amount,
          "payer holds %d; %s" % (balance(third.address), json.dumps(s3b)))
    mined, pending = w3.eth.get_transaction_count(gas, "latest"), w3.eth.get_transaction_count(gas, "pending")
    check("26 the two nonces those transfers left empty are filled by the look, so nothing of ours waits behind them (G4)",
          mined == pending == count + 2, "gas key: %d mined, %d pending, %d before the two" % (mined, pending, count))
    fourth(a, payer, body, signed, wanted, balance, w3, asset, sign)


def fourth(a, payer, body, signed, wanted, balance, w3, asset, sign):
    """The third reading's G0 and G5 on the fork, with USDC's own code: another use of a payment's nonce mined before
    our transfer, by the payer to itself (not paid) or of the very same authorization by someone else (paid, once);
    two payments of one payer whose balance covers one."""
    import os
    from usdc_facilitator import ABI
    token = w3.eth.contract(address=asset, abi=ABI)
    erc20 = w3.eth.contract(address=asset, abi=[{"name": "transfer", "type": "function", "stateMutability": "nonpayable",
                                                 "inputs": [{"name": "to", "type": "address"}, {"name": "v", "type": "uint256"}],
                                                 "outputs": [{"name": "", "type": "bool"}]}])
    gas = call(a.url, "/health")[1]["signer"]
    relay = Account.create().address           # anyone may hand an authorization to the token
    w3.provider.make_request("anvil_setBalance", [relay, hex(10 ** 18)])

    def front(auth, sig):
        """`auth` handed to the token by `relay` with a high tip: mined before our transfer in the same block."""
        s = bytes.fromhex(sig[2:])
        data = token.encode_abi("transferWithAuthorization", [auth["from"], auth["to"], int(auth["value"]), int(auth["validAfter"]),
                                                              int(auth["validBefore"]), bytes.fromhex(auth["nonce"][2:]),
                                                              s[64], s[:32], s[32:64]])
        return Web3.to_hex(w3.eth.send_transaction({"from": relay, "to": asset, "data": data, "gas": 200_000,
                                                    "maxPriorityFeePerGas": 10 ** 10, "maxFeePerGas": 10 ** 11}))

    def ours_waits(n0):
        for _ in range(150):
            if w3.eth.get_transaction_count(gas, "pending") > n0:
                return True
            time.sleep(0.1)
        return False

    def settle_while(payment, req, during):
        """/settle of `payment` with automine off: once our transfer waits in the pool, `during()`, then a block."""
        out, n0 = {}, w3.eth.get_transaction_count(gas, "pending")

        def go():
            out["s"] = call(a.url, "/settle", body(payment, req))[1]
        w3.provider.make_request("evm_setAutomine", [False])
        try:
            t = threading.Thread(target=go)
            t.start()
            ours_waits(n0)
            out["during"] = during()
            w3.provider.make_request("evm_mine", [])
            t.join()
        finally:
            w3.provider.make_request("evm_setAutomine", [True])
        return out

    req = wanted()
    p = signed(req)
    c = p["payload"]["authorization"]
    own = Account.create().address
    other = {"from": c["from"], "to": own, "value": "1", "validAfter": "0", "validBefore": c["validBefore"], "nonce": c["nonce"]}
    start = (balance(payer.address), balance(a.pay_to))
    out = settle_while(p, req, lambda: front(other, sign(other, payer.key)))
    end = (balance(payer.address), balance(a.pay_to))
    s = out["s"]
    check("27 the payer's own authorization under the same nonce, mined first, is not taken as payment (G0)",
          s.get("success") is False and s.get("errorReason") == "authorization_used_elsewhere" and s.get("sent") is False
          and end == (start[0] - 1, start[1]) and balance(own) == 1, "%s; %s -> %s" % (json.dumps(s), start, end))

    p = signed(req)
    c = p["payload"]["authorization"]
    start = (balance(payer.address), balance(a.pay_to))
    out = settle_while(p, req, lambda: front(c, p["payload"]["signature"]))
    again = call(a.url, "/settle", body(p, req))[1]
    end = (balance(payer.address), balance(a.pay_to))
    s = out["s"]
    check("28 the same authorization handed to the token by someone else first is paid, once, by that transaction (G0)",
          s.get("success") is True and s.get("transaction") == out["during"] and again.get("transaction") == out["during"]
          and end == (start[0] - a.amount, start[1] + a.amount), "%s; %s -> %s" % (json.dumps(s), start, end))

    fourth_payer = Account.create()
    w3.eth.send_transaction({"from": payer.address, "to": asset, "data": erc20.encode_abi("transfer", [fourth_payer.address, a.amount])})
    now = int(time.time())

    def of_fourth():
        q = signed(req)
        auth = {"from": fourth_payer.address, "to": Web3.to_checksum_address(a.pay_to), "value": str(a.amount), "validAfter": "0",
                "validBefore": str(now + 300), "nonce": "0x" + os.urandom(32).hex()}
        q["payload"] = {"signature": sign(auth, fourth_payer.key), "authorization": auth}
        return q
    q1, q2 = of_fourth(), of_fourth()
    n0 = w3.eth.get_transaction_count(gas, "pending")
    out = settle_while(q1, req, lambda: call(a.url, "/settle", body(q2, req))[1])    # q2 while q1's transfer waits
    again = call(a.url, "/settle", body(q1, req))[1]
    second, sent = out["during"], w3.eth.get_transaction_count(gas, "pending") - n0
    check("29 two payments of one payer whose balance covers one: the second is refused while the first is in flight, "
          "with nothing sent (G5)", again.get("success") is True and second.get("errorReason") == "insufficient_funds"
          and second.get("sent") is False and sent == 1 and balance(fourth_payer.address) == 0,
          "first %s; second %s; transfers of ours %d" % (again.get("errorReason") or "paid", json.dumps(second), sent))


def main():
    ap = argparse.ArgumentParser(description="self-test of the USDC facilitator")
    ap.add_argument("--url", default="http://127.0.0.1:8402")
    ap.add_argument("--network", default="eip155:84532")
    ap.add_argument("--rpc", default="https://sepolia.base.org")
    ap.add_argument("--payer-key", required=True)
    ap.add_argument("--pay-to", required=True)
    ap.add_argument("--amount", type=int, default=10000, help="base units; 10000 is 0.01 USDC")
    ap.add_argument("--settle", action="store_true", help="also settle one payment (needs funded keys)")
    ap.add_argument("--fork", action="store_true", help="on a local anvil fork: also the attacks that change the chain")
    a = ap.parse_args()
    info = get_default_asset(a.network, "USDC")
    payer = Account.from_key(read_key(a.payer_key))
    client = register_exact_evm_client(x402ClientSync(), EthAccountSigner(payer), networks=a.network)
    usdc = Web3(Web3.HTTPProvider(a.rpc)).eth.contract(address=Web3.to_checksum_address(info["asset"]), abi=BALANCE_ABI)

    def balance(who):
        return usdc.functions.balanceOf(Web3.to_checksum_address(who)).call()

    def wanted(amount=a.amount, pay_to=a.pay_to, timeout=300):
        return PaymentRequirements(scheme="exact", network=a.network, asset=info["asset"], amount=str(amount),
                                   pay_to=pay_to, max_timeout_seconds=timeout,
                                   extra={"name": info["name"], "version": info["version"]})

    def signed(req):
        return client.create_payment_payload(PaymentRequired(accepts=[req])).model_dump(
            mode="json", by_alias=True, exclude_none=True)

    def body(payment, req):
        return {"x402Version": 2, "paymentPayload": payment,
                "paymentRequirements": req.model_dump(mode="json", by_alias=True, exclude_none=True)}

    def copy(obj):
        return json.loads(json.dumps(obj))

    code, sup = call(a.url, "/supported")
    kinds = sup.get("kinds", []) if isinstance(sup, dict) else []
    check("1 /supported names exact on %s" % a.network, code == 200 and any(
        k.get("x402Version") == 2 and k.get("scheme") == "exact" and k.get("network") == a.network for k in kinds),
        json.dumps(sup))
    code, h = call(a.url, "/health")
    check("2 /health", code == 200 and h.get("ok") is True, json.dumps(h))
    before = (balance(payer.address), balance(a.pay_to))
    funded = before[0] >= a.amount
    print("payer %s holds %d; payTo holds %d (base units); funded=%s" % (payer.address, before[0], before[1], funded))

    req = wanted()
    good = signed(req)
    print("payload holds %s, authorization holds %s" % (sorted(good["payload"]), sorted(good["payload"].get("authorization", {}))))
    code, v = call(a.url, "/verify", body(good, req))
    if funded:
        check("3 a good payment verifies", code == 200 and v.get("isValid") is True, json.dumps(v))
    else:
        check("3 a good payment passes the signature checks and stops at the balance", code == 200
              and v.get("isValid") is False and v.get("invalidReason") in (
                  "invalid_exact_evm_insufficient_balance", "invalid_exact_evm_transaction_simulation_failed"), json.dumps(v))

    forged, req2 = copy(good), wanted(amount=a.amount * 2)
    forged["payload"]["authorization"]["value"] = forged["accepted"]["amount"] = str(a.amount * 2)
    code, v = call(a.url, "/verify", body(forged, req2))
    check("4 a forged amount is caught by the signature", v.get("isValid") is False
          and "signature" in str(v.get("invalidReason")), json.dumps(v))

    req3 = wanted(pay_to=Account.create().address)
    code, v = call(a.url, "/verify", body(signed(req3), req3))
    check("5 a payment to someone else is refused", v.get("invalidReason") == "pay_to_is_not_ours", json.dumps(v))

    # The SDK's own buyer will not even sign more than $1 (its spend controls), so the request is made by hand.
    big = str(int(h.get("maxAmount", "1000000")) + 1)
    over = copy(good)
    over["payload"]["authorization"]["value"] = over["accepted"]["amount"] = big
    code, v = call(a.url, "/verify", body(over, wanted(amount=int(big))))
    check("6 more than the cap is refused", v.get("invalidReason") == "amount_out_of_range", json.dumps(v))

    elsewhere = "eip155:84532" if a.network == "eip155:8453" else "eip155:8453"
    moved = copy(good)
    moved["accepted"]["network"] = elsewhere
    code, v = call(a.url, "/verify", body(moved, req.model_copy(update={"network": elsewhere})))
    check("7 another network is refused", v.get("invalidReason") == "network_mismatch", json.dumps(v))

    req6 = wanted(timeout=5)
    stale = signed(req6)
    time.sleep(2)
    code, v = call(a.url, "/verify", body(stale, req6))
    check("8 an authorization about to expire is refused", v.get("invalidReason") == "authorization_expires_too_soon",
          json.dumps(v))

    bare = copy(good)
    bare["payload"] = {"signature": good["payload"].get("signature"), "permit2Authorization": {}}
    code, v = call(a.url, "/verify", body(bare, req))
    check("9 anything but EIP-3009 is refused", v.get("invalidReason") == "only_eip3009_authorizations", json.dumps(v))

    if not a.settle:
        print("settling skipped (run with --settle once the keys are funded)")
    elif not funded:
        check("10 settling needs a funded payer", False, "the payer holds %d, the test needs %d" % (before[0], a.amount))
    else:
        code, s = call(a.url, "/settle", body(good, req))
        check("10 a good payment settles", code == 200 and s.get("success") is True and bool(s.get("transaction")),
              json.dumps(s))
        want = (before[0] - a.amount, before[1] + a.amount)
        for _ in range(15):
            after = (balance(payer.address), balance(a.pay_to))
            if after == want:
                break
            time.sleep(2)
        check("11 the coins moved by exactly the price", after == want,
              "payer %d -> %d, payTo %d -> %d" % (before[0], after[0], before[1], after[1]))
        code, s2 = call(a.url, "/settle", body(good, req))
        check("12 the same payment asked again gets the same transfer, and nothing more moves", s2.get("success") is True
              and s2.get("transaction") == s.get("transaction") and (balance(payer.address), balance(a.pay_to)) == want,
              json.dumps(s2))
        code, v = call(a.url, "/verify", body(good, req))
        check("13 the same payment no longer verifies", v.get("isValid") is False, json.dumps(v))
        if a.fork:
            attacks(a, info, payer, good, req, body, copy, signed, wanted, balance)
    print("%d of %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
