"""Checks of the provider's RPC before USDC is offered (review, question 5). Run as user usdc. Prints no secret: not
the RPC's address, not the key. Sends at most two transfers of nothing from the gas key to itself (a fraction of a cent)."""
import sys
import time

from eth_account import Account
from web3 import Web3

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
AUTH_USED = Web3.to_hex(Web3.keccak(text="AuthorizationUsed(address,bytes32)"))
URL = open("/var/lib/polarka-usdc/rpc-url").read().strip()
SECRET = URL.rstrip("/").rsplit("/", 1)[-1]          # the provider's key is the last part of its address
w3 = Web3(Web3.HTTPProvider(URL, request_kwargs={"timeout": 15}))
acct = Account.from_key(open("/var/lib/polarka-usdc/gas.key").read().strip())


def said(ex):
    """An error as text, with the RPC's address and key taken out: a client error may name the url it asked."""
    text = str(ex).replace(URL, "<rpc>").replace(SECRET, "<key>") if len(SECRET) >= 8 else "(text withheld)"
    return "%s: %s" % (type(ex).__name__, text[:200])


def main():
    print("chain", w3.eth.chain_id, "head", w3.eth.block_number, "gas wei", w3.eth.get_balance(acct.address))
    print("estimate_gas at latest:", w3.eth.estimate_gas({"from": acct.address, "to": acct.address, "value": 0}, block_identifier="latest"))
    head = w3.eth.block_number
    for span in (500, 3000):
        try:
            got = w3.eth.get_logs({"address": USDC, "fromBlock": head - span + 1, "toBlock": head,
                                   "topics": [AUTH_USED, "0x" + acct.address[2:].lower().rjust(64, "0")]})
            print("eth_getLogs over %d blocks: OK (%d logs)" % (span, len(got)))
        except Exception as ex:
            print("eth_getLogs over %d blocks: refused, %s" % (span, said(ex)))
    if "--send" not in sys.argv:
        return
    n = w3.eth.get_transaction_count(acct.address, "pending")
    tip = w3.eth.max_priority_fee
    fee = 2 * w3.eth.get_block("latest")["baseFeePerGas"] + tip
    one = acct.sign_transaction({"to": acct.address, "value": 0, "gas": 21000, "nonce": n, "chainId": 8453,
                                 "maxFeePerGas": fee, "maxPriorityFeePerGas": tip})
    other = acct.sign_transaction({"to": acct.address, "value": 0, "gas": 21100, "nonce": n, "chainId": 8453,
                                   "maxFeePerGas": fee, "maxPriorityFeePerGas": tip, "data": b"\x01"})
    h = w3.eth.send_raw_transaction(one.raw_transaction)
    try:
        w3.eth.get_transaction(h)
        print("a transfer of ours waiting in the pool, found by its hash: yes")
    except Exception as ex:
        print("a transfer of ours waiting in the pool, found by its hash: no,", said(ex))
    for name, raw in (("the same transfer again", one.raw_transaction), ("another under the same nonce, same fee", other.raw_transaction)):
        try:
            w3.eth.send_raw_transaction(raw)
            print(name + ": taken")
        except Exception as ex:
            print(name + ":", said(ex))
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=60, poll_latency=1)
    print("in a block:", rc["blockNumber"], "status", rc["status"])
    time.sleep(2)
    try:
        w3.eth.send_raw_transaction(one.raw_transaction)
        print("the same transfer once in a block: taken")
    except Exception as ex:
        print("the same transfer once in a block:", said(ex))
    print("gas wei", w3.eth.get_balance(acct.address))


try:
    main()
except Exception as ex:          # never a traceback: its text may name the RPC's address
    print("stopped:", said(ex))
    sys.exit(1)
