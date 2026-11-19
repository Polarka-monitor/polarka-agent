# Polarka Agent

A wallet for TrueNorth (NORTH) that a program can use: a command line, a Python library, and a server for
agents that speak the Model Context Protocol (MCP). The keys stay on the machine it runs on. What its
addresses hold it asks a wallet server with the Northbound API (github.com/nova9/northbound).

**State: 0.1.3, an early version.** It was written with an AI model under its author's direction. An
independent AI reviewer read it and found faults, the serious ones of which are mended (`REVIEW.md`); no
human security expert has read it. On 17 November 2026 it made its first payments on the real network: two
small payments went into blocks, and its owner's limits refused what they should (`NO_LIMITS`, `OVER_LIMIT`,
`OVER_DAY`). That is all the real use it has had. Do not put into it what you cannot lose. It is an independent community project, not a product of the TrueNorth developers or of
the Northbound project.

## What it is for

A program that has a processor and no bank account can earn NORTH by mining and can be paid in it. This
wallet lets such a program hold coins and pay small amounts by itself, within bounds that a person sets and
that the tools given to the program cannot change.

## How a program is kept within bounds

- Without a file `limits.json` in the wallet's folder the wallet pays nothing. Its owner writes that file by
  hand: `{"per_payment_north": "5", "per_day_north": "20"}`, and if wanted `"allow": [addresses]` and
  `"fee_rate"`. No command and no tool of the MCP server writes or changes it.
- Every transaction is written into `journal.jsonl` before anything leaves the machine. Lines are only added.
- The MCP server has no tool that shows the recovery words, changes the limits or forgets a payment.
- These bounds hold against a program that reaches the wallet through the MCP server only. A program that
  can run commands or write files as the same user can change `limits.json`, move the journal away or read
  the words; against that the wallet does nothing. Give an agent the MCP server, not a shell in that account.
- A payment counts into its day whatever became of it; only the owner's `forget` takes one out. A payment
  that another one replaced therefore counts twice for a day.
- What a wallet server says in words is not passed on to the program: a refusal comes as one of a few fixed
  words and an error as its HTTP status.

The recovery words are in `wallet.json`, readable by its owner only (mode 600; the program refuses to work
with a file that others can read). Whoever can read that file can spend. This is a wallet for small amounts
on a machine its owner controls.

## Paying, and a server that says no

The rules are those of the Polarka Wallet for Android, which an independent reviewer looked at.

Whoever was shown a signed transaction can keep it and pass it on later, whatever they answered. So a
payment that the server refused (`kept`) or that got lost (`unknown`) is not forgotten. The next payment is
built on one of its coins, and only one of the two can ever be in a block. The same payment asked for again
(same coins, same outputs) is the same transaction and is simply shown to the server once more. While the
server shows none of the coins of such a payment, the wallet pays nothing at all, until its owner has looked
at the wallet through another server or a node and forgets the payment by hand (`forget`).

A wallet server sees the wallet's addresses together and the machine's internet address. It can show a false
balance or hold a payment back. It can stop the wallet until its owner acts (by refusing a payment and then
hiding its coins), and it can keep one command waiting for a long time by answering slowly. It cannot take
coins. A program is not shown the server's own words: why a node refused a payment comes as one of a few
fixed words (`FEE_TOO_LOW`, `INPUTS_GONE`, `OTHER`, ...), an error as its HTTP status, and a line of the history is
numbers, an id and a flag. The words themselves appear at the command line alone,
under `server_said_untrusted`, for the owner.

## Installing

    pip install polarka-agent

That gives the command `polarka-agent`, which is the same as `python3 -m polarka_agent`. It needs Python 3.9
or later on Linux or macOS and nothing besides; it does not run on Windows. The package on PyPI is built from
this repository at the tag of its version; 0.1.1 and 0.1.2 were built and uploaded by the workflow in
`.github/workflows/release.yml`. A version can be here before it is on PyPI. PyPI may still carry 0.1.2, whose
faucet has the faults that `docs/faucet-review-0.1.2.md` names: whoever means to run a faucet installs this
version from the project's own server,

    pip install https://polarka.org/download/src/polarka-agent-0.1.3.tar.gz

and compares the file with `https://polarka.org/download/SHA256SUMS` first.

## Commands

    python3 -m polarka_agent --dir FOLDER new --chain regtest --server http://127.0.0.1:PORT
    python3 -m polarka_agent --dir FOLDER address | balance | history | payments | limits
    python3 -m polarka_agent --dir FOLDER pay ADDRESS 1.5 [--memo TEXT] [--dry-run]
    python3 -m polarka_agent --dir FOLDER pay-many FILE.json
    python3 -m polarka_agent --dir FOLDER received ADDRESS | wait ADDRESS 1.5
    python3 -m polarka_agent --dir FOLDER stamp FILE | stamp --sha256 HEX
    python3 -m polarka_agent --dir FOLDER faucet URL | postage URL --most 0.05
    python3 -m polarka_agent --dir FOLDER forget ID
    python3 -m polarka_agent --dir FOLDER mcp

Every command prints one JSON object. Amounts are NORTH, written as text. `new --chain main` takes the public
server of the Northbound project (`https://api.northboundwallet.org`) unless another is named; that server is
not run by this project. `new` shows the 12 or 24 words once; `--words-file` brings a wallet back.

A stamp writes `POLARKA\x01` and a SHA-256 into an output that carries data. The chain is young and its hash
rate is small: a stamp shows an order of events, it is not proof for a court.

For an agent: start `python3 -m polarka_agent --dir FOLDER mcp` as an MCP server over standard input and
output. Its tools are `north_balance`, `north_address`, `north_received`, `north_wait`, `north_pay`,
`north_stamp`, `north_payments`, `north_history`, `north_limits`, `north_faucet` and `north_postage`.

## A first coin: the faucet

A program that has no coin can ask a faucet for one. It does a small piece of work first (hashing, a few
seconds of one processor). The work is friction, not a wall: a program written for speed does it many times
faster. What bounds a faucet is what it counts, below.

    python3 -m polarka_agent --dir FOLDER faucet https://ADDRESS-OF-A-FAUCET

asks for a little NORTH to a fresh address of the wallet. There is no public faucet yet; when there is one,
its address will be on polarka.org.

Anybody can run one: `faucet-serve --amount 2` hands out 2 NORTH to an address once a day from the wallet in
FOLDER. What it may give in a day is that wallet's `limits.json`, nothing else, so a faucet is this wallet
under its own bounds. Under that bound it gives one place (an IPv4 address or an IPv6 /48) two payments a day
(`--per-source`) and at most a twelfth of the day's sum in one hour. None of this stops somebody who has many
internet addresses; it bounds them, and the day's limit is the only promise. Set it to a sum you can give
away every day. The limit for one payment has to be a little above the amount given, because a payment counts
with its fee; otherwise the faucet does not start.

It keeps the addresses it paid (`faucet.jsonl`) and no internet addresses on disk; it asks for no name and no
e-mail. It is meant to stand behind a web server that provides HTTPS (`--trust-proxy`); what our own will run
on is in `deploy/faucet`.

The faucet of version 0.1.2 was read by an independent reviewer (an AI agent that had not seen it being
written), who found that a script from one place could take nearly the whole day's sum within a minute and
that a claim the wallet refused could be sent again without end. After the fixes a second independent
reviewer looked at them and found two more things that stood in the way: questions that cost nothing raised
the work for everybody, and one customer with many IPv6 addresses counted as many places. Both are mended
here. What was found both times and what was done about it is in `docs/faucet-review-0.1.2.md`. The last
fixes have not been read by anybody else, and the faucet has never run in public.

## Something to pay for: postage

Whoever runs a web service and wants to keep robots from wearing it out makes them burn processor time for
nothing today. With postage the caller pays a little NORTH instead, and the one who runs the service gets it.

    python3 -m polarka_agent --dir FOLDER postage-serve --upstream 127.0.0.1:9000 --price 0.01 --calls 100 --free 20

stands before the service on port 9000. A caller without postage gets `402` and what to do, as JSON: ask for
a quote (`POST /_postage/quote`), pay the price to the address it names, and send the token in the header
`X-Postage`. Every quote has an address for itself for half an hour, so a payment is told apart without
anybody proving anything; an address that was not paid serves the next asker. `--free` lets one source make
some calls a day without postage.

The price can also be given as mining work: `--price-hashes 600` in place of `--price`. The gate then asks
for so many hashes' worth of NORTH. It reads from its wallet server's block template what the network needs
for a block and what a block pays, every ten minutes; a quote keeps the price it was given. When more
machines mine, the same work is fewer NORTH, so the price in NORTH moves and the price in work stays. This
needs a wallet server with the mining routes of Northbound. What a NORTH takes to make is a cost, not a
market price; NORTH has none.

Postage is counted in calls. It is not a balance, it is not given back, and the gate never pays from its
wallet, so the gate holds nobody's coins. Whoever runs a service for payment has the duties of one who does
(saying who they are, among others); the gate is a tool and takes none of them away. It is new: nobody
independent has read it, and it has never run in public.

The asker's side is `postage URL --most 0.05` (for an agent the tool `north_postage`): it asks the gate for a
quote, pays it from the wallet and shows the token. The owner's limits hold as for any payment, and nothing
above `--most` is paid whatever the gate asks.

## Mining for a pool (in work, regtest only)

This branch holds a pool that pays for each share, and a miner for it. It is not released, nobody else has
read it, and it has run on regtest only; `pool-serve` refuses any other chain.

    python3 -m polarka_agent build-rx --randomx /path/to/truenorth/src/randomx
    python3 -m polarka_agent --dir POOL-WALLET pool-serve --chain regtest --rpc-url http://127.0.0.1:PORT \
        --rpc-cookie /path/to/.cookie --pay-every 3600
    python3 -m polarka_agent mine http://127.0.0.1:8690 --address ADDRESS --threads 4

`build-rx` builds a small C library around RandomX as it stands in the TrueNorth sources (`native/`); it needs
cmake and a C++ compiler, and nothing else in this package needs it. The wallet stays standard library only.

The pool takes a block template from its own node, gives every asker a header of its own, checks the proof
of work of each nonce sent back, and writes one line into `pool-shares.jsonl` for each share: that file is
what the pool owes. A share is a header whose hash is at or below 2048 times the network's target, and is
worth the block reward without fees divided by 2048, less the fee. A share that is also a block goes to the
node. When the node does not answer, the pool hands out no work.

The calls: `GET /v1/pool`, `GET /v1/work?address=`, `GET /v1/tip`, `POST /v1/share {"job", "nonce"}`,
`GET /v1/account?address=`, `GET /v1/blocks`.

What is owed is paid out in rounds from the wallet in `--dir`, within that wallet's `limits.json`: one
transaction to everybody who is owed the minimum or more, written into `pool-payouts.jsonl` before the wallet
is asked. One round is on the way at a time, and a round that fell in the middle is found again in the
wallet's own journal, so nobody is paid twice and nobody is forgotten. Block rewards can be spent after 100
blocks, so a pool needs coins of its own to pay from before its first rewards are ripe, and whenever it has
found fewer blocks than its shares were worth.

The pool hands out no work while the blocks its node took from it, counted together with those of the
addresses in `--own-mining`, are 32 % or more of the blocks of the last day, and starts again below 30 %. An
hour's blocks stop it at 40 %: an hour is too few blocks to hold a pool to a third. One place may
ask for work 120 times a minute and for 50 addresses in a block, and is not listened to for ten minutes
after 20 wrong shares.

The two books, `pool-payouts.jsonl` and the wallet's journal, are held against each other before every round;
if one of them was lost or put back from an older copy, nothing is paid until a person has looked. One
process works over a folder at a time. The pool hands out no work while it owes more than `--most-owed` or
more than its wallet holds, and none to an address whose shares should long have found a block and did not.

An independent reviewer (an AI agent that had not seen the code being written) read the pool at commit
b4eac9b and found that a lost ledger, a second process or a lost wallet journal paid people twice, that
nothing bounded what the pool owed, and that the miner searched the same nonces again. Those are mended
here; nobody else has read the fixes. Still open from that review: stale shares credited for up to five
seconds after a new block, a payout counted as paid after one block on a server's word, an HTTP server
without bounds of its own (run it behind a proxy with `--trust-proxy`), and a cap that a stranger can close.

Not built: a share target that follows the miner's speed and taking back credit for a block that left the chain. Until somebody
else has read it, it must not run on the real network.

## Code and tests

Python 3.9 or later, standard library only. `polarka_agent/keys.py` and `tx.py` hold the keys, addresses, the
signature hash and signing; they come from the cold address tool
(github.com/severka-monitor/truenorth-cold-address) and are checked against its published P2QRH test vectors.
`server.py` is the client for a wallet server, `wallet.py` the wallet, `cli.py` and `mcp.py` the two faces,
`faucet.py` the faucet and its asker, `postage.py` the postage gate, `rx.py` the proof of work through the
library in `native/`, `pool.py` the pool, `payout.py` its payouts and `miner.py` its miner.

    TN_BIN=/path/to/bin python3 -B -m unittest discover -s tests -t .

152 tests. Those in `test_wallet.py`, `test_cli_mcp.py`, `test_faucet_node.py` and `test_pool_node.py` run against the official
`truenorthd` on regtest
and a stand-in for a Northbound server (`tests/standin`, copied from the Polarka Wallet's repository); they
are skipped without the node programs. No test touches a real network.

`tests/faults.py` is a test of the tests: it puts one deliberate fault at a time into a copy of the code
(a limit that is not applied, a day that does not count the payment being made, and so on) and expects the
tests to fail. It first runs the copy as it stands, and counts a fault as caught only when tests ran and failed. Ten faults
in the owner's rules. It covers those rules only, not the keys, the signing or the client of the server.

## What is missing

- A review by a person. An independent AI reviewer read version 0.1.0.dev0 and found two serious faults (a
  server that lies about a block could lift the day's limit; after two hundred addresses the wallet sent
  change where it did not look). Both are mended here, with tests. Its other findings are listed in
  `REVIEW.md` with what was done about each.
- More than two hundred addresses of a kind: beyond that the wallet hands out no new address and reuses
  its last change key.
- More than a first try on the real network.
- A way to replace a payment that was handed over and waits for a block without end.
- A node of one's own in place of a wallet server.
- A history that is exact for a wallet with more than a hundred addresses.
- Checking a stamp without trusting a server or a node.

## Licence

MIT, see `LICENSE`. There is no warranty.
