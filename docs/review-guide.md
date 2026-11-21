# A guide for a human reviewer

Thank you for reading this. What follows is where to look first, what has been checked already and by what,
and how to run things yourself. Nothing here asks you to trust it: every claim can be checked against the code.

## What this is

polarka-agent is a TrueNorth (NORTH) wallet for programs and AI agents, in Python with no outside libraries,
plus the services that run beside it on one public server:

| Part | File | Runs where | Holds coins |
|---|---|---|---|
| Wallet, CLI, MCP tools | `wallet.py`, `cli.py`, `mcp.py`, `keys.py`, `tx.py`, `server.py` | the agent's machine | yes, the agent's own, within `limits.json` |
| x402 payments in NORTH | `x402.py`, `bazaar.py` | buyer: agent; seller/facilitator: server | buyer pays; facilitator holds none |
| Postage gate | `postage.py` | server, public behind Caddy | its wallet only receives |
| Stamps service | `stamps.py` | server, loopback | a small fee wallet |
| Pool (pay per share) | `pool.py`, `payout.py`, `miner.py`, `rx.py` | server; miners anywhere | yes: the pool's reserve |
| Wallet server: one for the pool and stamps, one for the public | `walletd.py` | server, loopback; the second behind node.polarka.org | no |
| Faucet | `faucet.py` | server, public | yes, small |

Specification draft for x402: `docs/x402-scheme-exact-bip122.md` (also open as x402-foundation/x402 PR #4363).

## Where to look first (by risk)

1. `x402.py`, `Facilitator.verify` and `Facilitator.settle`: one payment must buy one resource once; a copy of
   a waiting transaction must buy nothing (the `claim`); a payment shown again must cover today's price.
2. `x402.py`, `fetch`: an agent must never pay twice for one resource, nor more than `most`, nor send its
   payment anywhere but the URL asked.
3. `postage.py`, `Gate.x402_pay` and the handler: a lot of calls must not be had without a confirmed payment.
4. `payout.py` and `pool.py`: the pool pays for shares; caps, surplus sweep, what happens on restart.
5. `stamps.py`, `parse_tx`, `parse_txoutproof`, `check`, `verify`: a forged proof must not verify; the
   block scan must survive reorganizations.
6. `wallet.py`, `pay` and `_send`: limits, the journal written before anything leaves, an open payment
   standing against the next one.

## What has been checked, and by whom

Four reviews by AI models that did not write the code (not by people):

- review 5 (Claude Opus): 2 High, 4 Medium, 7 Low; High and Medium fixed in 1ea787c (M4 in part);
- review 6 (Claude Fable): 1 High, 4 Medium, 9 Low; High and Medium fixed in 6ddc827;
- review 7 (Claude Sonnet): 0 High, 1 Medium, 8 Low; the Medium (the resource a payment buys was only the
  buyer's word) and five Low fixed in the commit after 6ddc827, L7 (stamps.json rewritten whole on each new
  stamp) and L8 (the gate had no read timeout) in e9512fc. Open: L3 (a huge proof is slow to check), L6.

- review 8 (a fresh AI agent, 21 November 2026, of b7b2d68..ea557ab and of the deployment): 1 High, 5 Medium,
  8 Low, 6 Info. H1: a GET with a body reached a handler as two requests, so the proposed public route to the
  facilitator's discovery would have opened its `/settle`; the route was live for twelve minutes, then off until
  0.2.2 ran. M1: the same on pool.polarka.org and node.polarka.org. Fixed: every handler now ends a connection
  after a request with a body (`httpguard.py`, `tests/test_http_guard.py`), and the web server opens one upstream
  connection per request (`keepalive off`, tried against Caddy 2.11.9). Also fixed: M3 (a search costs little
  whatever it asks), L2 (an offer needs a route), L3 (the filters), M5 and parts of L8 (texts), and in the
  deployment M2 (node.polarka.org has a walletd of its own, with an RPC user allowed only its calls). All of it is
  in 0.2.2. Open: M4 (the bounds hold against MCP-only programs, and most MCP hosts also give file tools; the
  texts and the registry entry say so), L1 (a payment shown again after a gate restart can be refused), L4 (stamps
  storage grows), L5 (least privilege; since 21 November the pool and its wallet server no longer hold the node's
  cookie, the rest is open), L6 (the watcher), L7, I2 to I6.

Nobody but its author has read the fixes of review 8.

Their reports can be had on request. Low findings that are still open are listed in them. Earlier parts of the
project (wallet, pool) went through four further AI reviews. No person outside has read the code.

## How to run it yourself

    git clone https://github.com/Polarka-monitor/polarka-agent && cd polarka-agent   # or the copy you were given
    # a TrueNorth Core build (truenorthd, truenorth-cli) in a folder; tests start their own regtest node
    TN_BIN=/path/to/bin python3 -m unittest discover -s tests -t .      # about 5 minutes, 231 tests

Without `TN_BIN` the tests that need a node are skipped. To check a stamp from the main chain:

    polarka-agent stamp-verify proof.json --rpc-url http://127.0.0.1:9554 --rpc-cookie ~/.truenorth/.cookie

## The threat model in one paragraph

The server is public. Its node, the facilitator, the stamps service and the wallet server listen on loopback;
Caddy is in front of the gate, the faucet, the pool, polarka.org and a wallet server for the public
(node.polarka.org, a walletd apart from the pool's), and passes to the facilitator only its read-only routes
(`GET /x402/supported` and `/x402/discovery/*` at gate.polarka.org), never `/verify` or `/settle`. Buyers are
programs with a spending limit set by their owner in a file the program cannot change while it has the MCP tools
only. Miners are anybody: the pool is open and pays any address since 21 November 2026. NORTH has no market price
today; the money at risk is the pool's reserve (5000 NORTH, at most 10,000 paid out a day), the faucet's and the
stamps' small wallets, and every buyer's limit. The chain is a Bitcoin Core 30 fork: waiting transactions are
replaced by fee. How the parts work together, and who sees what: `docs/how-it-works.md`.
