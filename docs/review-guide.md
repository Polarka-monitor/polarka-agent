# A guide for a human reviewer

Thank you for reading this. What follows is where to look first, what has been checked already and by what,
and how to run things yourself. Nothing here asks you to trust it: every claim can be checked against the code.

## What this is

polarka-agent is a TrueNorth (NORTH) wallet for programs and AI agents, in Python with no outside libraries,
plus the services that run beside it on one public server:

| Part | File | Runs where | Holds coins |
|---|---|---|---|
| Wallet, CLI, MCP tools | `wallet.py`, `cli.py`, `mcp.py`, `keys.py`, `tx.py`, `server.py` | the agent's machine | yes, the agent's own, within `limits.json` |
| Signing service (from 0.2.3) | `signer.py`; in `cli.py` `--signer`, `signer-serve`, `signer-setup` | the agent's machine, as a user of its own | yes, the agent's own, within `limits.json` |
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
2. New on main for 0.2.3: `x402.py`, `facilitator_server(public=True)`, `Limiter`, `caller`,
   `Facilitator._full` and `_may_add`, and `Blocks`, with `deploy/x402/polarka-x402-public.service` and the
   `/facilitator` routes in `deploy/postage/gate.caddy` (the facilitator for sellers from outside). Nothing an
   outside caller sends may reach the gate's own facilitator, its payments or its catalog; an X-Forwarded-For the
   caller writes must count for nothing; the bounds must hold for a machine with a whole IPv6 /64, and one with a
   /48 must not use them all up; a full facilitator must take no new payment and still settle the ones it has; a
   payment that never settles must not cost the node a reading of the chain on each request. Review 10 read it
   (H2 and M2 below); nobody but their author has read its fixes yet.
3. New on main for 0.2.3: `signer.py`, the signing service (the first review's F4, review 8's M4). The agent's
   user must reach neither the words nor `limits.json`, and through the socket only the wallet's own operations,
   under the wallet's rules, the day limit included; a caller that holds a connection or sends too much holds
   nothing long. Then the scripts `signer-setup` prints (the users and groups, the program installed where only
   root can change it from a pinned version or a wheel of known SHA-256, in an empty environment; the systemd unit
   and the LaunchDaemon), what `--check` concludes, and the refusal of an agent's user that can administer the
   machine. Tests: `tests/test_signer.py` and `tests/test_review10.py`, part of them as root on Linux with real
   users (`POLARKA_TEST_USERS=1`). Review 10 read it (H1, H3, M1, M3 and M4 below); nobody but their author has
   read its fixes yet.
4. `x402.py`, `fetch`: an agent must never pay twice for one resource, nor more than `most`, nor send its
   payment anywhere but the URL asked.
5. `postage.py`, `Gate.x402_pay` and the handler: a lot of calls must not be had without a confirmed payment.
6. `payout.py` and `pool.py`: the pool pays for shares; caps, surplus sweep, what happens on restart.
7. `stamps.py`, `parse_tx`, `parse_txoutproof`, `check`, `verify`: a forged proof must not verify; the
   block scan must survive reorganizations.
8. `wallet.py`, `pay` and `_send`: limits, the journal written before anything leaves, an open payment
   standing against the next one.

## What has been checked, and by whom

One review by a person, and six by AI models that did not write the code (five before it, one after):

- the first review by a person (a programmer, 22 November 2026, of the package of that morning: all of
  polarka-agent, the server's configuration and scripts, the watcher and the logs; not the Android wallet and
  miner, the web site or truenorth-cold-address; by reading, without running the code). In the keys, the
  signing and the wallet it found nothing by which coins could be taken or an output forged. It found: a way to
  hold up the gate's facilitator with large PAYMENT-SIGNATURE headers (the facilitator asked its node under
  one lock, the gate had no bound on threads or attempts); records that grow without end (the pool's
  pool-shares.jsonl against a 64 MiB bound in the usage count, walletd holding the chain twice in memory,
  stamps.json written twice a minute); a wallet that piles every change on one key after 200 payments and reads
  up to 2000 lines of history for each open payment; postage.py behind the other services (X-Forwarded-For,
  IPv6 places, an unbounded server, a 64 MiB read, no fsync); two Content-Length headers passing the guard;
  Unicode digits; a payment whose coin the seller spent before the buyer came back called lost; a buyer that
  took a number for an amount, did not wait on 503 and read without a deadline; stamps taken for written when
  the node refused them, or for dead when mined a moment before; MCP tools that reached this machine; the first
  audit's F6, F9 and F11; and on the server uneven hardening, a usage count that could read every key, and
  Caddy lines with addresses at the DEBUG level. Mended on 22 November, on main with tests
  (`tests/test_human_review.py`) and on the server: all of that except the ones below. Open: pool-shares.jsonl
  still grows (the count now stops loudly at 512 MiB, about six weeks), the pool asks its node under its lock
  now and then, the web pages are still built on the server, and polarka-usage and polarka-status are still
  root's (usage no longer sees the keys). Its report can be had on request; nobody but its author has
  read the fixes.


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
  against MCP-only programs, and most MCP hosts also give file tools; the texts and the registry entry say so; a
  signing service for it is on main since 22 November, for 0.2.3: item 3 above), L1
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

- review 10 (a fresh AI agent, 22 November 2026, of v0.2.2..665d8e5: the signing service, the facilitator for
  sellers from outside and the fixes of the human review): 3 High, 4 Medium, 6 Low, 9 Info. H1: a payment signed
  without being handed over (as x402 signs them) counted only for the day it was signed, so an agent that held it
  back for a day and then sent it itself could at once pay a second day's limit; in 0.2.2 the same is open to an
  MCP-only agent through `north_fetch` to a seller of its own. H2: a payment replaced by fee stayed waiting for
  ever, and each `/settle` that showed it again had the node read up to 288 blocks twice, about 1,160 calls, as
  often as the bounds allowed. H3: on macOS the setup script ran Python and pip as root with the HOME that sudo
  keeps, so a `.pth` or a `pip.conf` of the agent's could decide what runs as root. M1: moving a wallet followed
  the agent's links as root. M2: one sender with an IPv6 /48 could use up the public facilitator's bounds, and
  payments that never settle could fill its book. M3: `--package` took pip options and paths, and `--check` did
  not look where the program is imported from. M4: the agent's user was by default the owner's admin account.
  Mended on 22 November, on main with tests (`tests/test_review10.py`), not yet released: all of these, L1 to L6,
  I3 (the public facilitator runs as a user of its own, with an RPC user of its own), I8 and I9. A payment that
  stayed open for a day or more and then goes out counts for the day it goes out; the facilitator keeps an index
  of the blocks it has read and reads each new block once; the script starts itself again in an empty
  environment, runs Python with `-I` and pip with `--isolated`; a move reads plain files only, as the agent's
  user; a /48 has a bucket of its own over its /64s, the table of callers lets the longest idle go, and one place
  may add only a twentieth of the book's room a day; the program is a pinned version or a wheel whose SHA-256 the
  script checks, and `--check` follows every place the program takes modules from; an agent's user that can
  administer the machine is refused unless the owner says `--admin-agent`. The same reviewer read these fixes
  again (858c22a, the same day): all mended, M2 and M4 in part. It found N1 (Medium: piped into `sudo sh`, the
  script read itself again from a file named `sh` in the current folder, so root ran whatever was there), four
  Low (N2 a wheel copied through a link and readable for a moment, N3 a moved file without a bound on its size,
  N4 the moved wallet's server name printed raw to root's terminal, N5 a sudo rule of the agent's own user not
  seen) and five Info. Mended on 22 November, in 0.2.3, with tests (`SecondLookOfReview10`): N1 to N6 and N8; the
  script is no longer read again at all, takes its commands from the system's folders before the first of them,
  and runs every Python and pip through `env -i`. Open: I1 until the release, I2, I4 to I7, N7 in part (sudo
  finds `sh` by the caller's PATH), N10 (the gate's own facilitator keeps a replaced payment waiting for ever:
  cheap now, but it grows), and what a sender with many /48s can still do against the public facilitator
  (`deploy/x402/README.md`).

Review 9 read the fixes of review 8 and found them sound, and review 10 found most of the fixes of the human
review correct. Nobody but its author has read the fixes of review 9 and review 10. The facilitator for sellers
from outside (item 2 above) opens only with 0.2.3.

Their reports can be had on request. Low findings that are still open are listed in them. Earlier parts of the
project (wallet, pool) went through four further AI reviews. No person outside has read the code since the
package that the first review by a person read.

## How to run it yourself

    git clone https://github.com/Polarka-monitor/polarka-agent && cd polarka-agent   # or the copy you were given
    # a TrueNorth Core build (truenorthd, truenorth-cli) in a folder; tests start their own regtest node
    TN_BIN=/path/to/bin python3 -m unittest discover -s tests -t .      # about 6 minutes, 315 tests
    # as root on Linux, the signing service with real users of the system (it makes and removes three):
    sudo POLARKA_TEST_USERS=1 python3 -m unittest tests.test_signer.ApartOnLinux

Without `TN_BIN` the tests that need a node are skipped. To check a stamp from the main chain:

    polarka-agent stamp-verify proof.json --rpc-url http://127.0.0.1:9554 --rpc-cookie ~/.truenorth/.cookie

## The threat model in one paragraph

The server is public. Its node, the facilitator, the stamps service and the wallet server listen on loopback;
Caddy is in front of the gate, the faucet, the pool, polarka.org and a wallet server for the public
(node.polarka.org, a walletd apart from the pool's), and passes to the facilitator only its read-only routes
(`GET /x402/supported` and `/x402/discovery/*` at gate.polarka.org), never `/verify` or `/settle`. From 0.2.3 a
second facilitator, for sellers from outside, gets `/facilitator/{supported,verify,settle}` there: a process
apart, with a user of the system and an RPC user of its own, a folder of its own, bounds on requests and on
payments kept, and no catalog. Buyers are programs with a spending limit set by their owner in a file the program
cannot change while it has the MCP tools only, or, with the signing service (from 0.2.3), whatever it can run as a
user of its own that cannot administer the machine.
Miners are anybody: the pool is open and pays any address since 21 November 2026. NORTH has no market price
today; the money at risk is the pool's reserve (5000 NORTH, at most 10,000 paid out a day), the faucet's and the
stamps' small wallets, and every buyer's limit. The chain is a Bitcoin Core 30 fork: waiting transactions are
replaced by fee. How the parts work together, and who sees what: `docs/how-it-works.md`.
