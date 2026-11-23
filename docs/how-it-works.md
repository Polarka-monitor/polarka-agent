# How Polarka works

What Polarka runs, who holds what and who sees what. The code is public, the proofs are in the chain and the
counters show what was used, so all of it can be checked.

The code was written with AI models under its author's direction. Independent AI reviewers that had not seen it
being written read it many times; what they found and what was done about it is in `REVIEW.md` and
`docs/review-guide.md`. On 22 November 2026 a security expert read the code and the server as a private person;
what he found is mended in 0.2.3, apart from what `docs/review-guide.md` names as open.

This page says what each part does, how a payment moves, who holds what and who sees what. All of it can be
checked against the code in this repository and the deployment notes in `deploy/`.

## The parts

| Part | Where it runs | What it does | Holds coins |
|---|---|---|---|
| Polarka Agent | your machine | a wallet as a command line, a Python library and an MCP server; pays services that speak x402, asks a faucet, mines for a pool | yours, within the `limits.json` you write |
| Wallet server | `api.northboundwallet.org` (the Northbound project's, not ours) unless another is named; `node.polarka.org` is ours, a process apart from the one our pool pays through | tells the wallet what its addresses hold and passes its transactions to the network | no |
| Faucet | `faucet.polarka.org` | 1 NORTH to an address once a day, after a few seconds of hashing | its own, at most 100 NORTH a day |
| Gate | `gate.polarka.org` | stands before paid routes and sells lots of 100 calls, for x402 or for a quote of its own | receives payments; holds no buyer's coins |
| x402 facilitator | the server, on loopback | checks an x402 payment, hands it to the node, waits for a block | no keys, no coins |
| x402 facilitator for other sellers | from 0.2.3: the server, on loopback, behind `gate.polarka.org/facilitator` | the same for sellers from outside, as a process apart under a user of the system and an RPC user of its own, with bounds on requests and on payments kept, and no catalog | no keys, no coins |
| Stamps | the server, on loopback, behind the gate | writes SHA-256s into the chain every ten minutes and gives proofs | a small wallet for fees |
| Pool | `pool.polarka.org` | pays for each share; open to everybody since 21 November 2026 | what it owes miners, from a reserve |
| Node | the server, on loopback | TrueNorth Core; every service that talks to it (the facilitator, the stamps service, the pool and both wallet servers) has an RPC user allowed only the calls it makes | no wallet |
| Usage counter | `polarka.org/data/usage.json` | counts every ten minutes, from the services' own records, what was used and what of it by others | none |

## An agent, from no coin to a paid call

1. Its owner starts the MCP server: `uvx polarka-agent@0.2.8 --dir FOLDER mcp`. With no wallet in FOLDER it makes
   one on the main chain. The recovery words are in `FOLDER/wallet.json`, readable by the owner only. Nothing is
   paid yet.
2. The owner writes `FOLDER/limits.json` by hand, for example `{"per_payment_north": "0.1", "per_day_north": "1"}`.
   No command and no tool writes or changes that file.
3. The agent gets a first coin with the tool `north_faucet` and the address `https://faucet.polarka.org`. The
   faucet asks for a little proof of work and pays 1 NORTH to a new address of the wallet.
4. The agent calls a service with `north_fetch` (at the command line `polarka-agent fetch URL --most 0.05`). The
   service answers `402 Payment Required` with what it takes: the price, the network
   `bip122:470c8b8ae824c06575cfae0851282c49` (TrueNorth's main chain), the scheme `exact` and the address to pay.
5. The wallet builds and signs a transaction and writes it into its journal before anybody sees it. It sends it
   in the header `PAYMENT-SIGNATURE`. It pays nothing above `--most` or above the owner's limits, and a second
   fetch of the same URL shows the same payment again instead of paying anew. A seller that refuses a payment
   after it has passed it to the network can still make the next fetch pay again; the owner's limits bound
   that. Our own gate did so after a restart when its price had moved meanwhile (review 8, L1); since 0.2.3
   it keeps the prices it asked.
6. The seller's facilitator checks the transaction and hands it to its node. While it waits for a block the
   seller answers `202` with `awaiting_confirmation`, and the agent shows the same payment again. Once it is in
   a block the service answers, and the header `PAYMENT-RESPONSE` names the transaction.
7. At `gate.polarka.org` one payment buys a lot of 100 calls, worth the NORTH that about 600 hashes make;
   `X-Postage-Calls-Left` says how many are left.
8. The first paid service is stamps: `GET https://gate.polarka.org/v1/stamp/<sha256>` answers `waiting`,
   `written` or `stamped` with a proof, and `polarka-agent stamp-verify` checks the proof, with a node also the
   block.

`examples/agent_that_pays_for_itself.py` does all of this in one program, and stops before paying when there
is no `limits.json`.

## Being found: the bazaar

x402 has an extension, the bazaar, through which a facilitator keeps a catalog of what was paid for through it.
The gate names each paid route in full and describes it (what it takes and what it gives, with a JSON schema).
When a payment for such a route is settled, the facilitator adds the route to its catalog. A buyer cannot put
words of its own into the catalog: the gate replaces the description a buyer echoes with its own before the
facilitator sees the payment.

Our catalog was first public for twelve minutes on 21 November 2026, until review 8 showed that a GET with a
body could carry a second request through the web server to the facilitator's private `/settle` (H1). In those
twelve minutes the facilitator settled only our own test payment. Since 0.2.2 every handler ends its connection
after a request with a body (`httpguard.py`) and the web server opens a new connection for each request, so the
read-only routes are public again:

    https://gate.polarka.org/x402/supported
    https://gate.polarka.org/x402/discovery/resources
    https://gate.polarka.org/x402/discovery/search?query=stamp

## A facilitator for other sellers (from 0.2.3)

A seller that takes NORTH over x402 and runs no node of its own may name `https://gate.polarka.org/facilitator`
as its facilitator (from 0.2.3). It is a process apart from our gate's own facilitator, under a user of its
own, so nothing done there reaches the gate. It takes at most 60 requests to check or settle a payment a
minute from one address, 120 from one IPv6 /48 and 600 from all, lists nothing in a catalog, and takes no new
payment once it keeps 20,000, of which one place brings at most 1,000 a day. The payment goes from the buyer to the seller; the facilitator only checks it and
hands it to the network, and asks no fee. `deploy/x402/README.md` says how it is run.

## Mining for the pool

    polarka-agent mine https://pool.polarka.org --address YOUR_ADDRESS --threads 4

The wheels for Linux (x86_64, arm64) and macOS on Apple silicon carry the RandomX library, so nothing else needs
building. The pool gives every miner a block header of its own. A share is a hash at or below 2048 times the
network's target and is worth the block reward without fees divided by 2048, less a fee of 5 %. A share that is
also a block goes to the network.

- Every share is one line in the pool's ledger; that ledger is what the pool owes.
- Every ten minutes the pool pays everybody it owes 1 NORTH or more, in one transaction, and counts a payout as
  paid when its own node sees it six blocks deep.
- Block rewards can be spent after 100 blocks, so the pool pays from a reserve of its own. It hands out no work
  while it owes more than 5000 NORTH or more than its wallet holds, and its wallet pays at most 10,000 NORTH a
  day.
- It stops handing out work while its blocks are 32 % or more of the network's blocks over a day (40 % over an
  hour), its operator's own solo miners counted in, and starts again below 30 %.
- It holds what it owes a miner until it pays. A miner who keeps back the blocks he finds can take the reserve
  before the pool notices; that risk is the pool's, and it is why the reserve is small.

`https://pool.polarka.org/v1/pool` shows the pool's state, its last payout and what it owes in all;
`/v1/account?address=` shows one miner's shares and payouts.

A pool can also take miners over stratum, the way IronMiner (truenorth-ironminer 1.0.8 or later) mines:
`-o stratum+tcp://HOST:PORT -u YOUR_ADDRESS -p x`. Those shares go into the same ledger, are paid the same way
and fall under the same bounds; only the share target follows the miner's speed there (a share about every
fifteen seconds, never easier than 1/2048 of a block). A connection gets an extranonce of its own, every job a
number of its own in the coinbase, and the coinbase of a stratum job is marked, so that no header is paid twice,
nor at a difficulty other than its own job's.

## Letters between agents

An agent has an inbox: a key of its own, apart from the wallet's keys, whose id (64 hexadecimal digits) it gives
to whoever should write to it. `north_mail_send` (or `polarka-agent mail-send`) seals a letter to that key, signs
it with the writer's own inbox key and posts it to `https://gate.polarka.org/v1/mail/<inbox>`, behind the gate:
one call of a lot of postage a letter, the lot bought over x402 as above, with the first letter itself; a letter
the post office refuses for one of its limits costs no call. `north_mail_read` fetches the letters at
`/v1/inbox/<inbox>`, for nothing but a signature of the inbox's key over the time and the post office's address,
opens them, says who wrote each one when its signature holds, shows no letter it has shown before, and tells the
post office to let them go. The post office keeps sealed letters it cannot read, at most seven days (two for an
inbox that does not ask for its letters); it knows which inbox got how much and when, not who wrote or what.

## Who holds what, and who sees what

- **Your keys** stay in your wallet's folder. Nobody at Polarka sees them. Whoever can read `wallet.json` can
  spend. With a signing service (from 0.2.3) that folder belongs to a user of the system of its own and
  `limits.json` to root: the agent's user, which must not be one that can administer the machine, can ask the
  service to pay within the limits, not read the words.
- **A wallet server** sees your wallet's addresses together and your internet address. It can show a false
  balance or hold a payment back; it cannot take coins. The default one is run by the Northbound project, not by
  Polarka. Ours, `node.polarka.org`, keeps no record of requests, only of its own faults.
- **A seller**, our gate included, sees the payment, the resource and your internet address at the moment of
  the call. Our gate keeps the lots it sold, the facilitator the payments it settled, and the stamps service
  the SHA-256s it was given, never the files.
- **The facilitator for other sellers** (from 0.2.3) sees the internet address of the seller's server, not the
  buyer's. It holds it in memory until a minute after the last request, to count requests, and writes none
  down. For a caller that brought new payments it holds, also in memory, until the end of that day (UTC), its
  address or its IPv6 /48 and how many it brought, so that one sender cannot fill what is kept for all. It keeps
  the payments it settled, as the gate's own does.
- **The faucet** keeps the addresses it paid, and no internet addresses on disk.
- **The pool** keeps every share with the address it is for.
- **The web server** in front of every name at polarka.org keeps no access log. Until 21 November 2026 its own
  lines about failed requests, and about requests for names it does not serve, held the visitor's internet address
  and headers (review 9); since then they hold neither, and on 22 November the system log was emptied. The
  services write short lines about what they did (a call passed, a payout, a batch of stamps) to the server's
  system log, without internet addresses. With an address the server keeps only what concerns SSH: attempts to
  log in and the bans fail2ban makes of them, for two weeks at most, and the operator's own logins.
- **The usage counter** counts from the services' own records. What is known to be ours (our miners, our tests)
  is counted apart, from a list kept by hand.

## What can change

- Any part may change, stop or be reset without notice: the gate's price, the faucet's amount, the pool's terms,
  the addresses of the services.
- When something looks wrong, the operator first stops what could lose coins (the pool's payouts, a service) and
  explains afterwards, at polarka.org and where the service was announced.
- NORTH has no price and is traded nowhere. What the pool, the faucet or a payment is worth in money is not known
  and is not promised.

## Reporting a fault

Open an issue at https://github.com/Polarka-monitor/polarka-agent/issues, or write to
[@Polarka_monitor](https://x.com/Polarka_monitor) on X. If a fault puts coins at risk, say there only that you
have found something; the details are then taken privately.

## What is missing

- The points of the security review of 22 November 2026 that `docs/review-guide.md` names as open.
- Use by anybody but its author: the pool, the gate and the stamps service have served nobody else yet
  (`polarka.org/data/usage.json` shows it as it changes).
- Somebody other than us who runs a faucet, takes postage or settles x402 in NORTH.
- A node of one's own in place of a wallet server, for the wallet.
- The rest is listed at the end of the README.
