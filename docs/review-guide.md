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
| Stratum for the pool (from 0.2.4) | `stratum.py` | server, public on port 3333 (a systemd socket) | no: it pays through the pool |
| Letters between agents (from 0.2.4) | `mail.py`; the gate's `--route /v1/mail` | post office: server, behind the gate and Caddy; writers and readers: the agents' machines | no |
| Wallet server: one for the pool and stamps, one for the public | `walletd.py` | server, loopback; the second behind node.polarka.org | no |
| Faucet | `faucet.py` | server, public | yes, small |
| Remote connector (from 0.2.5) | `connector.py`; in `cli.py` `connector-serve` | server, public behind Caddy at /mcp | no |

Specification draft for x402: `docs/x402-scheme-exact-bip122.md` (also open as x402-foundation/x402 PR #4363).

## Where to look first (by risk)

1. `x402.py`, `Facilitator.verify` and `Facilitator.settle`: one payment must buy one resource once; a copy of
   a waiting transaction must buy nothing (the `claim`); a payment shown again must cover today's price.
2. New in 0.2.3: `x402.py`, `facilitator_server(public=True)`, `Limiter`, `caller`,
   `Facilitator._full` and `_may_add`, and `Blocks`, with `deploy/x402/polarka-x402-public.service` and the
   `/facilitator` routes in `deploy/postage/gate.caddy` (the facilitator for sellers from outside). Nothing an
   outside caller sends may reach the gate's own facilitator, its payments or its catalog; an X-Forwarded-For the
   caller writes must count for nothing; the bounds must hold for a machine with a whole IPv6 /64, and one with a
   /48 must not use them all up; a full facilitator must take no new payment and still settle the ones it has; a
   payment that never settles must not cost the node a reading of the chain on each request. Review 10 read it
   (H2 and M2 below), and its reviewer read the fixes again.
3. New in 0.2.3: `signer.py`, the signing service (the first review's F4, review 8's M4). The agent's
   user must reach neither the words nor `limits.json`, and through the socket only the wallet's own operations,
   under the wallet's rules, the day limit included; a caller that holds a connection or sends too much holds
   nothing long. Then the scripts `signer-setup` prints (the users and groups, the program installed where only
   root can change it from a pinned version or a wheel of known SHA-256, in an empty environment; the systemd unit
   and the LaunchDaemon), what `--check` concludes, and the refusal of an agent's user that can administer the
   machine. Tests: `tests/test_signer.py` and `tests/test_review10.py`, part of them as root on Linux with real
   users (`POLARKA_TEST_USERS=1`). Review 10 read it (H1, H3, M1, M3 and M4 below), its reviewer read the
   fixes again, and what that second look found (N1 to N5) is mended as well.
4. New in 0.2.4: `stratum.py` and `mail.py`. Stratum: one header must belong to one job id and one difficulty
   (the issue number in the coinbase), a share must be paid once whether it comes over stratum or HTTP, and no
   number of places may keep a newcomer out (`_room`). Letters: only the inbox's key reads a letter and only its
   writer's key signs it (`seal`, `letter`, `read`), a signature is used once and names the post office, a reader
   shows a letter once (`fetch_letters`, `_take_new`), and a call of postage counts as spent only once the post
   office has taken the letter (`postage.py`, `reserve`, `done`, `give_back`). Review 11 read both, its reviewer
   read the fixes again, and what both found is mended (below).
5. New in 0.2.5: `connector.py`, the remote MCP server at https://polarka.org/mcp. It must hold and show nothing of
   anyone's, write down no caller's address, give no more free stamps than its bounds a day (all together and one
   place), give a free stamp back when the stamp service did not take it, answer only what the Streamable HTTP
   transport asks of a server without sessions (one JSON object, 202, 405, 403 for an Origin that is none, 400 for a
   protocol version it does not speak), and read the stamp service, the pool and the node of its own machine only.
   `tests/test_connector.py`.
6. `x402.py`, `fetch`: an agent must never pay twice for one resource, nor more than `most`, nor send its
   payment anywhere but the URL asked.
7. `postage.py`, `Gate.x402_pay` and the handler: a lot of calls must not be had without a confirmed payment.
8. `payout.py` and `pool.py`: the pool pays for shares; caps, surplus sweep, what happens on restart.
9. `stamps.py`, `parse_tx`, `parse_txoutproof`, `check`, `verify`: a forged proof must not verify; the
   block scan must survive reorganizations.
10. `wallet.py`, `pay` and `_send`: limits, the journal written before anything leaves, an open payment
   standing against the next one.

## What has been checked, and by whom

One review by a person, and nine by AI models that did not write the code (five before it, four after it, one of
these with a second look):

- the first review by a person (a security expert, reading as a private person and not for an employer or a
  client, 22 November 2026, of the package of that morning: all of
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
  Caddy lines with addresses at the DEBUG level. Mended on 22 November, in 0.2.3, with tests
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
  signing service for it came with 0.2.3: item 3 above), L1
  (a payment shown again after a gate restart can be refused; mended in 0.2.3), L4
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
  Mended on 22 November, in 0.2.3, with tests (`tests/test_review10.py`): all of these, L1 to L6,
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

- review 11 (a fresh AI agent, 22 November 2026, of the stratum branch: `stratum.py`, `mail.py` and what they
  touch in `pool.py`, `postage.py` and `x402.py`, and their deployment): 5 High, 4 Medium, 7 Low, 7 Info. H1: one
  header could be paid at more than one difficulty, as job ids shared a coinbase; H2: a few places could keep
  every miner out of stratum; H3: an answer of the post office too large to read whole could be taken for no
  letters; H4: the free door's signature checks could be spent by anybody and its readers shut out; H5: the post
  office could be filled for little. Mended the same day, with tests (aa5c283, 3149728). Its reviewer read the
  fixes again: none High, four Medium (N2: letters the post office refused emptied the paid door's bucket for
  nothing; N3: each call given back rewrote the gate's book; N4: a letter read while telling the post office
  failed was lost; N7: some 200 places could still keep newcomers out of stratum), five Low and two Info. All
  mended in fbc8702, with tests that fail on the code before (`tests/test_review11b.py`); the deployment and the
  watcher were changed to match. Open by choice: stratum over plain TCP (I1), one call of postage a letter
  whatever its size (H5: what bounds a fill is the place, 8 MB a day), and a faster signature check (H4: the
  bucket and the unit's CPU quota bound it meanwhile).

- review 11c (a fresh AI agent, 23 November 2026, the third look: the fixes of the second look as released in
  0.2.4): none High, 1 Medium, 3 Low, 3 Info. T1: letters dated ahead could lift the reader's floor over honest
  letters, which were then let go unread; T2: the pool's own block left the HTTP jobs of the tip before it; T3:
  miners coming back together after a restart pushed each other out of stratum; T4: twenty unpaid quotes left the
  gate no address for forty minutes; T5: a record of letters read written by 0.2.4.dev3 read as empty. Mended in
  0.2.5 with tests (`tests/test_review11c.py`). Open: what is left of N1 (a tip question answered out of order can
  put back a template on the old tip for about a second) and of N4 (a reader stopped while it tells the post office
  shows those letters next time without their text), both as expected failures in the same file; T6 (the stratum
  socket starts a stopped pool: stop both, `systemctl stop polarka-stratum.socket polarka-pool`); I1 (a TLS port
  for stratum is the next step); H5 and H4 as above.

- review 12 (a fresh AI agent, 23 November 2026: the remote connector of 0.2.5 and the mending of review 11c, at
  6981971, before the connector was reachable from outside): 1 High, 2 Medium, 6 Low, 8 Info. H1: a SHA-256 with a
  newline after it passed the connector's check (`$` matches before a last newline), urllib stripped the newline,
  and the stamp service took the hash while the connector counted nothing, so one place could fill every batch and
  shut the paying users out; M1: the bucket of all requests was ten places' shares, so ten places within their own
  bounds shut the connector for everybody; M2: two saves at once shared one temporary file, so a stamp that was
  taken was answered as failed and state.json could be left torn. Low: L1 a node that is starting called a good
  proof invalid; L2 north_network showed the height of the block being mined and its reward as issued; L3 two
  claims in the texts (a check here "without trusting Polarka"; who read the code); L4 north_mail_read and the local
  north_stamp were not marked as destructive; L5 thirteen places could still hold all the gate's quotes; L6 a
  chunked body was refused. All mended in 0.2.5 with the reviewer's tests (`tests/test_review12.py`), three of them
  changed where the mending took another of the ways the review offered: 2025-03-26 is no longer spoken (rather than
  batches taken), a read is warned once the record of letters let go of records younger than a day (rather than
  each letter flagged), and a letter is shown up to a day ahead with a word that a clock is wrong, its record kept
  that much longer (I4). Every `^...$` pattern of the package that reads input from outside now ends in `\Z`.
  Open: T6 and N1, N4 of review 11c as above; L5 in part (twenty places can still hold the gate's twenty addresses
  for forty minutes; x402 is not touched); walletd's own limits (the wallet's review, N5).

- the human review of 23 November 2026 (people outside this project, whose names are not given here; a security
  review of the Polarka Wallet 0.7.1 together with `walletd.py` and `faucet.py` as they run on the server): of its
  findings two low ones, L-4 and L-5, are in this package. Mended in 0.2.6 with tests (`tests/test_walletd.py`,
  `tests/test_faucet.py`): walletd gives a whole request a time to arrive (`--request-time`, 20 seconds), starts its
  revision at a number drawn at random, names an address back as it was asked and reads ids in a history cursor in
  either case; with `--trust-proxy` the faucet listens on loopback only, and its asker keeps to its own bounds. The
  wallet's own findings are mended in the wallet (its `review/changes-0.7.md`).

- 0.2.7, found in use with an agent on OpenClaw: most agents end a call of a tool after 60 seconds (the default of
  the MCP SDKs), and some clients have no setting for it (OpenClaw 2026.4.15 among them); north_fetch waited up to
  100 seconds for a block, so the call ended in an error, though the payment had been made and a later call showed
  it. Now every tool answers well within a minute (`mcp.py`, SELLER_WAIT and
  the three after it): north_fetch and north_mail_send wait 25 seconds for a seller and 10 for each of its answers,
  north_wait 40 seconds at the most (up to 300 is still taken, as before), and north_faucet works 20 seconds at the
  most and then says done false, having claimed nothing. What is not done by then is said so, and the same call
  later goes on; nothing is paid twice. No payment, limit or check was changed. Tests: `tests/test_call_time.py`
  and one more in `tests/test_faucet.py`.

- 0.2.8, found in use with an agent on OpenClaw, and a second case by reading the code: while the payment for a lot
  of postage waited for its block, a call to another path of the same gate paid for a second lot (two stamps of two
  files, 66 seconds apart, bought two lots), and a letter to a second inbox showed there the payment that waited for
  the first; the facilitator refused it, as it bound a payment to the path it was first shown on, and the wallet
  dropped that payment from its book, claim and all. Now a gate names in its offer what a payment buys there
  (`extra.resource`, the gate's lot, the same on every path; `postage.py`, `x402_wanted`), and north_fetch shows a
  payment that waits for its block on any url of the same origin that names the same thing for the same payee, with
  that url's resource in its payload and within that call's `most`, and pays nothing more. The facilitator binds a
  payment when it is first seen, to the seller's name or else to the payload's resource; one bound to its payload is
  bound to a name once, where a seller first names the thing while that binding holds, and from then on to that name
  only (`x402.py`, `_binding`); a record from before 0.2.8 passes as it did. The book keeps a payment refused for
  good at a url other than its own, for its own url, and takes from its place only the payment that was shown.
  An independent AI reviewer read the change before its release: no high finding, five medium and five low. The
  medium ones (records from before 0.2.8 refused, a buyer's own first showing without a name, letters still losing
  a payment where a seller binds to the path, a late answer taking a newer payment from the book, the order of the
  deploy) and three of the low ones (`most`, the resource of the url, the tests) are mended. Still so: two calls at
  the same moment buy two lots, as before (nothing is lost); where a facilitator ignores `extra.resource` and a
  payment is first seen at another url, its own url may refuse it. A gate of 0.2.8 needs its facilitator at 0.2.8
  first, and taking the gate back refuses what waits under it. Tests: ten in `tests/test_x402.py`.

Review 9 read the fixes of review 8 and found them sound; review 10 found most of the fixes of the human review
correct, and its reviewer read the fixes of its own findings again; review 11c and review 12 read the fixes of the
looks before them. Nobody but their author has read the fixes of review 12, of the human review of 23 November, the waits of 0.2.7 and the fixes of the review of 0.2.8. The facilitator for sellers from outside (item 2 above) opened with 0.2.3.

Their reports can be had on request. Low findings that are still open are listed in them. Earlier parts of the
project (wallet, pool) went through four further AI reviews. No person outside has read the code since the
package that the first review by a person read.

## How to run it yourself

    git clone https://github.com/Polarka-monitor/polarka-agent && cd polarka-agent   # or the copy you were given
    # a TrueNorth Core build (truenorthd, truenorth-cli) in a folder; tests start their own regtest node
    TN_BIN=/path/to/bin python3 -m unittest discover -s tests -t .      # about 6 minutes, 384 tests
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
payments kept, and no catalog. From 0.2.4 the pool also takes miners over stratum on port 3333, straight from
the internet through a socket of systemd, and a post office keeps sealed letters between agents: they come in
through the gate, one call of postage each, and go out at `/v1/inbox` to whoever signs for the inbox; the post
office cannot read them. Buyers are programs with a spending limit set by their owner in a file the program
cannot change while it has the MCP tools only, or, with the signing service (from 0.2.3), whatever it can run as a
user of its own that cannot administer the machine.
Miners are anybody: the pool is open and pays any address since 21 November 2026. NORTH has no market price
today; the money at risk is the pool's reserve (5000 NORTH, at most 10,000 paid out a day), the faucet's and the
stamps' small wallets, and every buyer's limit. The chain is a Bitcoin Core 30 fork: waiting transactions are
replaced by fee. How the parts work together, and who sees what: `docs/how-it-works.md`.
