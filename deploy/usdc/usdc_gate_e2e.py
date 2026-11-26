#!/usr/bin/env python3
"""End to end on a local fork of Base: a standard x402 buyer (the official SDK's client for requests) asks the
polarka-agent gate for a stamp, finds USDC among the offers of the 402, pays 0.01 USDC through our facilitator and
is let through. The same payment shown again for the same call passes without being paid again, for another call it
is refused, and the payer has paid once. Needs anvil (fork_start.sh) and usdc_facilitator.py on 127.0.0.1:8402."""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from eth_account import Account
from web3 import Web3
from x402 import x402ClientSync
from x402.http.clients import wrapRequestsWithPayment
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import register_exact_evm_client

sys.path.insert(0, os.path.expanduser(os.environ.get("POLARKA_AGENT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))))
from polarka_agent import postage, x402 as px  # noqa: E402

from usdc_facilitator import read_key  # noqa: E402
from usdc_selftest import BALANCE_ABI, check, results  # noqa: E402

TO = "0x522475540CA835D14E287a645d22c4B367703dac"
STAMP = "/v1/stamp/" + "ab" * 32


class Behind(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"served": self.path, "payment_seen": "PAYMENT-SIGNATURE" in self.headers}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Folder:
    """All the gate needs of a wallet when it takes only USDC: a folder for its book."""
    def __init__(self):
        self.folder = tempfile.mkdtemp(prefix="gate-e2e-")


def main():
    payer = Account.from_key(read_key(os.path.join(os.path.expanduser(os.environ.get("KEYS", "~/.config/usdc-fork-test")), "payer.key")))
    usdc = Web3(Web3.HTTPProvider("http://127.0.0.1:8545")).eth.contract(address=px.USDC["eip155:8453"][0], abi=BALANCE_ABI)

    def balances():
        return usdc.functions.balanceOf(payer.address).call(), usdc.functions.balanceOf(TO).call()
    behind = ThreadingHTTPServer(("127.0.0.1", 0), Behind)
    fac = px.EvmFacilitator("http://127.0.0.1:8402", "eip155:8453")
    gate = postage.Gate(Folder(), 10 ** 6, 100, usdc=postage.Usdc(fac, TO, 10000, ["/v1/stamp"]))
    front = ThreadingHTTPServer(("127.0.0.1", 0), postage.make_handler(gate, "127.0.0.1:%d" % behind.server_address[1], False))
    for s in (behind, front):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % front.server_address[1]

    r = requests.get(url + STAMP, timeout=30)
    offers = px.from_b64json(r.headers["PAYMENT-REQUIRED"])["accepts"] if "PAYMENT-REQUIRED" in r.headers else []
    check("1 without a payment: 402, and USDC on Base among the offers", r.status_code == 402 and any(
        o["network"] == "eip155:8453" and o["amount"] == "10000" and o["payTo"] == TO for o in offers), json.dumps(offers))

    before = balances()
    client = register_exact_evm_client(x402ClientSync(), EthAccountSigner(payer), networks="eip155:8453")
    r = wrapRequestsWithPayment(requests.Session(), client).get(url + STAMP, timeout=90)
    said = px.from_b64json(r.headers["PAYMENT-RESPONSE"]) if "PAYMENT-RESPONSE" in r.headers else {}
    check("2 a standard x402 buyer pays and is let through", r.status_code == 200 and said.get("success") is True,
          "%d %s %s" % (r.status_code, r.text[:160], json.dumps(said)))
    check("3 the service behind does not see the payment", r.status_code == 200 and r.json()["payment_seen"] is False, r.text[:160])
    after = balances()
    check("4 0.01 USDC went straight from the payer to the seller", (before[0] - after[0], after[1] - before[1]) == (10000, 10000),
          "%s -> %s" % (before, after))

    signed = r.request.headers.get("PAYMENT-SIGNATURE")
    again = requests.get(url + STAMP, headers={"PAYMENT-SIGNATURE": signed}, timeout=30)
    check("5 the same payment again for the same call passes, not paid again", again.status_code == 200 and balances() == after,
          "%d %s" % (again.status_code, balances()))
    other = requests.get(url + "/v1/stamp/" + "cd" * 32, headers={"PAYMENT-SIGNATURE": signed}, timeout=30)
    reason = px.from_b64json(other.headers["PAYMENT-RESPONSE"]).get("errorReason") if "PAYMENT-RESPONSE" in other.headers else None
    check("6 for another call it is refused", other.status_code == 402 and reason == "payment_is_for_another_call",
          "%d %s" % (other.status_code, reason))

    # M2: the block is late; a standard buyer does not come back with the same payment after a 202
    w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:8545"))
    before = balances()
    w3.provider.make_request("evm_setAutomine", [False])
    try:
        r = wrapRequestsWithPayment(requests.Session(), client).get(url + "/v1/stamp/" + "ef" * 32, timeout=120)
    finally:
        w3.provider.make_request("evm_setAutomine", [True])
    check("7 with the block late, a standard buyer is let through once the chain holds the transfer", r.status_code == 200,
          "%d %s" % (r.status_code, r.text[:120]))
    w3.provider.make_request("evm_mine", [])
    time.sleep(1)
    after = balances()
    check("8 and it paid once", (before[0] - after[0], after[1] - before[1]) == (10000, 10000), "%s -> %s" % (before, after))
    for s in (behind, front):
        s.shutdown()
    print("%d of %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
