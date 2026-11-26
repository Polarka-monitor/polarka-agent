"""Positive control for eth_getLogs at the provider (run as usdc; prints no secret).
1) the same settled blocks must give the same AuthorizationUsed logs at the provider and at the public RPC;
2) asked by payer in chunks of 500, as the facilitator asks, the provider must find exactly what it found unfiltered."""
import sys
from collections import Counter

from web3 import Web3

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
AUTH_USED = Web3.to_hex(Web3.keccak(text="AuthorizationUsed(address,bytes32)"))
URL = open("/var/lib/polarka-usdc/rpc-url").read().strip()
SECRET = URL.rstrip("/").rsplit("/", 1)[-1]
ours = Web3(Web3.HTTPProvider(URL, request_kwargs={"timeout": 20}))
pub = Web3(Web3.HTTPProvider(sys.argv[1] if len(sys.argv) > 1 else "https://mainnet.base.org", request_kwargs={"timeout": 20}))


def said(ex):
    text = str(ex).replace(URL, "<rpc>").replace(SECRET, "<key>") if len(SECRET) >= 8 else "(text withheld)"
    return "%s: %s" % (type(ex).__name__, text[:200])


def logs(w3, lo, hi, *topics):
    return w3.eth.get_logs({"address": USDC, "fromBlock": lo, "toBlock": hi, "topics": [AUTH_USED, *topics]})


def ids(got):
    return {(Web3.to_hex(x["transactionHash"]), x["logIndex"]) for x in got}


def main():
    top = ours.eth.block_number - 30          # settled blocks, the same at both
    for width in (100, 20):
        lo = top - width + 1
        a = logs(ours, lo, top)
        try:
            b = logs(pub, lo, top)
        except Exception as ex:
            print("public RPC, %d blocks: refused, %s %s" % (width, type(ex).__name__, getattr(getattr(ex, "response", None), "status_code", "")))
            continue
        print("%d blocks %d..%d: provider %d, public %d, same set: %s" % (width, lo, top, len(a), len(b), ids(a) == ids(b)))
        break
    every = []
    for s in range(top - 2999, top + 1, 500):
        every += logs(ours, s, min(s + 499, top))
    print("3000 blocks in chunks of 500: %d AuthorizationUsed" % len(every))
    if not every:
        print("SUSPICIOUS: no AuthorizationUsed at all in 3000 blocks")
        return
    payer = Counter(Web3.to_hex(x["topics"][1]) for x in every).most_common(1)[0][0]
    want = ids(x for x in every if Web3.to_hex(x["topics"][1]) == payer)
    got = []
    for s in range(top - 2999, top + 1, 500):
        got += logs(ours, s, min(s + 499, top), payer)
    print("busiest payer: %d unfiltered, %d asked by topic, same set: %s" % (len(want), len(got), ids(got) == want))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print("FAILED:", said(ex))
