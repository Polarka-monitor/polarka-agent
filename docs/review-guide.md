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

Five reviews by AI models that did not write the code (not by people):

- review 5 (Claude Opus): 2 High, 4 Medium, 7 Low; High and Medium fixed in 1ea787c (M4 in part);
- review 6 (Claude Fable): 1 High, 4 Medium, 9 Low; High and Medium fixed in 6ddc827;
- review 7 (Claude Sonnet): 0 High, 1 Medium, 8 Low; the Medium (the resource a payment buys was only the
  buyer's word) and five Low fixed in the commit after 6ddc827, L7 (stamps.json rewritten whole on each new
  stamp) and L8 (the gate had no read timeout) in e9512fc. Open: L3 (a huge proof is slow to check), L6.

- review 8 (a fresh AI agent, 21 November 2026, of b7b2d68..ea557ab and of the deployment): 1 High, 5 Medium, 8 Low,
  6 Info. H1: a GET with a body reached a handler as two requests, so the proposed public route to the facilitator's
  discovery would have opened its `/settle`; the route was live for twelve minutes, then off until 0.2.2 ran. M1:
  the same on pool.polarka.org and node.polarka.org. Fixed: every handler now ends a connection after a request with
  a body (`httpguard.py`, `tests/test_http_guard.py`), and the web server opens one upstream connection per request
  (`keepalive off`, tried against Caddy 2.11.9). Also fixed: M3 (a search costs little whatever it asks), L2 (an
  offer needs a route), L3 (the filters), M5 and parts of L8 (texts), and in the deployment M2 (node.polarka.org has
  a walletd of its own, with an RPC user allowed only its calls). All of it is in 0.2.2. Open: M4 (the bounds hold
  against MCP-only programs, and most MCP hosts also give file tools; the texts and the registry entry say so), L1
  (a payment shown again after a gate restart can be refused; mended on main on 22 November, not yet released), L4
  (stamps storage grows), I2 to I6. Fixed later on 21 November: L5 (the pool and its wallet server have RPC users of
  their own; the facts service runs as a user of its own; polarka-usage keeps only the right to read and opens no
  record through a link, as does polarka-status; under the web root only the pages are the refresh job's), L6 (the
  watcher passes on only known units, states by pattern and numbers, and reports its own faults), L7 (the example
  agent pays for at most two lots and asks for an hour at most).

- review 9 (a fresh AI agent, 21 November 2026, of ea557ab..18cabd8, of the deployment, the vectors, the watcher
  and the public texts): 0 High, 0 Medium, 3 Low, 8 Info. It tried H1 and M1 again through Caddy 2.11.9: the code
  before the fix, with Caddy's keep-alive, did settle a smuggled payment; `keepalive off` and the guard each stop
  it alone. It found the RPC users exactly as narrow as the code, 235 tests passing, the vectors reproducible byte
  for byte and the sdist on PyPI equal to the tag. Fixed on 21 and 22 November: L1 (the refresh job writes only in
  a folder of its own, root copies its nine pages into the web root, and no folder there is writable by anybody
  else), L2 (the web server's own lines hold no internet address, headers or query, none are written for names it
  does not serve, and the system log was emptied on 22 November and keeps two weeks), L3 (texts; in the registry
  0.2.0 and 0.2.1 are marked deprecated), I1, I2 (the facts service keeps the guard), I3 (the watcher bounds
  numbers and output), I4 (the example agent waits no longer than its hour) and part of I7; I6 was checked (root
  logs in by key only). While I2 was fixed it came out that the faucet still ran 0.1.3, without the guard, so that
  Caddy's `keepalive off` was its one layer; it runs 0.2.2 since 22 November. Open: I5 (services on the loopback
  answer every local process), I8 (the public wallet server has no rate limit), the rest of I7. On cloning: the git
  that ships with macOS fails on plain-HTTP clones over HTTP/2 (a fault of its libcurl), so polarka.org speaks
  HTTP/1.1 only since 22 November.

Review 9 read the fixes of review 8 and found them sound. Nobody but its author has read the fixes of review 9.

Their reports can be had on request. Low findings that are still open are listed in them. Earlier parts of the
project (wallet, pool) went through four further AI reviews. No person outside has read the code.

## How to run it yourself

    git clone https://github.com/Polarka-monitor/polarka-agent && cd polarka-agent   # or the copy you were given
    # a TrueNorth Core build (truenorthd, truenorth-cli) in a folder; tests start their own regtest node
    TN_BIN=/path/to/bin python3 -m unittest discover -s tests -t .      # about 5 minutes, 240 tests

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
