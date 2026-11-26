#!/usr/bin/env python3
"""Only on a local anvil fork: what happens when the receipt of a settlement comes late.

Phase "send" stops mining, asks the facilitator to settle a fresh payment and expects settlement_pending with the
transaction; it keeps the request in --save. The facilitator may be restarted between the phases. Phase "finish"
mines a block, sends the same payment again and expects it to be matched with that transaction (paid once, not
twice), and a third time to be refused.
"""
import argparse
import json
import sys
import time

from eth_account import Account
from web3 import Web3
from x402 import x402ClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.default_assets import get_default_asset
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.schemas import PaymentRequired, PaymentRequirements

from usdc_facilitator import read_key
from usdc_selftest import BALANCE_ABI, call, check, results


def main():
    ap = argparse.ArgumentParser(description="late-receipt test of the USDC facilitator, on an anvil fork only")
    ap.add_argument("phase", choices=("send", "finish"))
    ap.add_argument("--url", default="http://127.0.0.1:8402")
    ap.add_argument("--network", default="eip155:8453")
    ap.add_argument("--rpc", default="http://127.0.0.1:8545")
    ap.add_argument("--payer-key", required=True)
    ap.add_argument("--pay-to", required=True)
    ap.add_argument("--amount", type=int, default=10000)
    ap.add_argument("--save", required=True, help="file that carries the request from one phase to the other")
    ap.add_argument("--leave", action="store_true", help="send: do not wait for the answer (the facilitator is to be killed)")
    a = ap.parse_args()
    w3 = Web3(Web3.HTTPProvider(a.rpc))
    if "anvil" not in w3.client_version.lower():
        sys.exit("this test stops and starts mining, so it runs on a local anvil fork only")
    info = get_default_asset(a.network, "USDC")
    usdc = w3.eth.contract(address=Web3.to_checksum_address(info["asset"]), abi=BALANCE_ABI)
    payer = Account.from_key(read_key(a.payer_key))
    if a.phase == "send":
        client = register_exact_evm_client(x402ClientSync(), EthAccountSigner(payer), networks=a.network)
        req = PaymentRequirements(scheme="exact", network=a.network, asset=info["asset"], amount=str(a.amount),
                                  pay_to=a.pay_to, max_timeout_seconds=600,
                                  extra={"name": info["name"], "version": info["version"]})
        pay = client.create_payment_payload(PaymentRequired(accepts=[req])).model_dump(
            mode="json", by_alias=True, exclude_none=True)
        body = {"x402Version": 2, "paymentPayload": pay,
                "paymentRequirements": req.model_dump(mode="json", by_alias=True, exclude_none=True)}
        before = usdc.functions.balanceOf(payer.address).call()
        w3.provider.make_request("evm_setAutomine", [False])  # stays off until "finish"
        if a.leave:
            # the facilitator is stopped while it waits for the block (L5a): the answer never comes; the transfer it
            # sent is found in the mempool and kept with the request
            import threading
            threading.Thread(target=lambda: call(a.url, "/settle", body), daemon=True).start()
            for _ in range(20):
                time.sleep(0.5)
                pending = w3.provider.make_request("txpool_content", [])["result"]["pending"]
                if pending:
                    break
            txs = [t["hash"] for by in pending.values() for t in by.values()]
            check("1 the facilitator sent the transfer and has not answered yet", len(txs) == 1, json.dumps(txs))
            with open(a.save, "w") as f:
                json.dump({"body": body, "tx": txs[0] if txs else None, "before": before}, f)
            print("%d of %d checks passed" % (sum(results), len(results)))
            return 0 if all(results) else 1
        code, s = call(a.url, "/settle", body)
        check("1 a late receipt answers settlement_pending with the transaction", s.get("success") is False
              and s.get("errorReason") == "settlement_pending" and bool(s.get("transaction")) and s.get("known") is True,
              json.dumps(s))
        with open(a.save, "w") as f:
            json.dump({"body": body, "tx": s.get("transaction"), "before": before}, f)
    else:
        with open(a.save) as f:
            saved = json.load(f)
        w3.provider.make_request("evm_setAutomine", [True])
        w3.provider.make_request("evm_mine", [])
        code, s = call(a.url, "/settle", saved["body"])
        check("2 the same payment again is matched with that transaction", s.get("success") is True
              and s.get("transaction") == saved["tx"], json.dumps(s))
        after = usdc.functions.balanceOf(payer.address).call()
        check("3 the payer paid once", saved["before"] - after == a.amount, "%d -> %d" % (saved["before"], after))
        code, s = call(a.url, "/settle", saved["body"])
        check("4 a third time it gets the same answer, and nothing more moves", s.get("success") is True
              and s.get("transaction") == saved["tx"] and usdc.functions.balanceOf(payer.address).call() == after, json.dumps(s))
    print("%d of %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
