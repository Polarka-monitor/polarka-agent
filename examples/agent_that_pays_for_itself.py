"""An agent that pays for itself: from no coin at all to a file stamped into the TrueNorth chain, with no person
in between but the owner who writes the wallet's limits.

    pip install polarka-agent
    python agent_that_pays_for_itself.py FILE [--dir FOLDER] [--mine SECONDS] [--rpc-url URL --rpc-cookie FILE]

What it does, one JSON line a step:
  1. a wallet on the main chain, made when FOLDER has none (its recovery words are in FOLDER/wallet.json);
  2. a first coin: 0.25 NORTH from the faucet of polarka.org after a few seconds of hashing, or with --mine that
     long mining for the Polarka pool, which pays from 1 NORTH owed every ten minutes;
  3. waits until the coin is in a block;
  4. pays the stamps service over x402 (HTTP 402) for the SHA-256 of FILE: never more than 0.05 NORTH, and within
     FOLDER/limits.json, which only the owner writes;
  5. asks again after the next batch and a block, from the lot of calls it paid for, until the proof is there;
  6. checks the proof, and with a node (--rpc-url, --rpc-cookie) also that its block is in the chain.

The file itself never leaves the machine, only its SHA-256. It takes some fifteen minutes, most of them waiting for
blocks and for the next batch of stamps. NORTH has no price; a stamp shows an order of events, not a proof for a
court."""
import argparse
import hashlib
import json
import os
import sys
import time

from polarka_agent import stamps, x402
from polarka_agent.cli import DEFAULT_FAUCET, DEFAULT_POOL, DEFAULT_SERVER, wait_for
from polarka_agent.wallet import Refused, Wallet, north, units

GATE = "https://gate.polarka.org/v1/stamp/"
MOST = units("0.05")             # the most this agent pays for one lot of stamps
LIMITS = '{"per_payment_north": "0.05", "per_day_north": "0.1"}'


def say(step, **what):
    print(json.dumps(dict(step=step, **what)), flush=True)


def first_coin(wallet, address, mine_seconds):
    if mine_seconds:
        from polarka_agent.miner import mine
        done = mine(DEFAULT_POOL, address, threads=max(1, (os.cpu_count() or 2) // 2), seconds=mine_seconds)
        say("mined", address=address, **{k: v for k, v in done.items() if isinstance(v, (int, float, str))})
        return units("1")                       # the pool pays from 1 NORTH owed, in its next round
    from polarka_agent.faucet import ask
    try:
        got = ask(DEFAULT_FAUCET, address)
        say("faucet", **{k: v for k, v in got.items() if isinstance(v, (int, float, str))})
        return units("0.25")
    except Refused as e:
        say("faucet", refused=e.kind, message=str(e))
        raise SystemExit("no first coin today from the faucet; try --mine 300")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("file")
    p.add_argument("--dir", default=os.path.expanduser("~/.polarka-agent/self-funded"))
    p.add_argument("--mine", type=int, default=0, metavar="SECONDS", help="earn the first coin by mining instead of the faucet")
    p.add_argument("--rpc-url")
    p.add_argument("--rpc-cookie")
    a = p.parse_args()
    with open(a.file, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()

    if not os.path.exists(os.path.join(a.dir, "wallet.json")):
        Wallet.create(a.dir, "main", DEFAULT_SERVER)
        say("wallet", made=True, folder=a.dir, note="the recovery words are in wallet.json in that folder")
    wallet = Wallet(a.dir)
    address = wallet.account.address(0, 0)
    have = wallet.balance()["spendable"]
    say("wallet", address=address, spendable=north(have))

    if have < MOST:
        need = first_coin(wallet, address, a.mine)
        seen = wait_for(wallet, address, need, confirmations=1, timeout=1800)
        say("coin", **seen)
        if not seen["paid"]:
            raise SystemExit("the first coin did not come within half an hour")

    if not os.path.exists(os.path.join(a.dir, "limits.json")):
        say("limits", missing=True, write=LIMITS, to=os.path.join(a.dir, "limits.json"),
            note="the owner writes this file, no program does; then run this again")
        raise SystemExit(2)

    url = GATE + digest
    while True:
        got = x402.fetch(url, wallet, MOST, wait=600)
        say("paid" if got.get("paid") else "asked", status=got["status"], paid=got.get("paid"), calls_left=got.get("calls_left"))
        if got["status"] == 200:
            answer = json.loads(got["body"])
            if answer.get("state") == "stamped":
                break
            pause = max(60, int(answer.get("next_batch", time.time())) - int(time.time()) + 150) if answer.get("state") == "waiting" else 120
            say("waiting", state=answer.get("state"), next_ask_in_seconds=pause)
            time.sleep(pause)
        elif got["status"] in (202, 503):
            time.sleep(30)
        else:
            raise SystemExit("the gate answered %s: %s" % (got["status"], str(got.get("body"))[:300]))

    proof_path = a.file + ".stamp.json"
    with open(proof_path, "w") as f:
        json.dump(answer, f, indent=1)
    node = None
    if a.rpc_url and a.rpc_cookie:
        from polarka_agent.walletd import Node
        node = Node(a.rpc_url, os.path.expanduser(a.rpc_cookie))
    checked = stamps.verify(answer, node)
    say("stamped", block_height=answer["block_height"], txid=answer["txid"], proof=proof_path, checked=checked,
        spendable_now=north(wallet.balance()["spendable"]))


if __name__ == "__main__":
    sys.exit(main())
