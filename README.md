# Polarka Agent

A wallet for TrueNorth (NORTH) that a program can use: a command line, a Python library, and a server for
agents that speak the Model Context Protocol (MCP). The keys stay on the machine it runs on. What its
addresses hold it asks a wallet server with the Northbound API (github.com/nova9/northbound).

**State: 0.1.1, an early version.** It was written with an AI model under its author's direction. An
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
fixed words (`FEE_TOO_LOW`, `INPUTS_GONE`, `OTHER`, ...), an error as its HTTP status, and a receiver in the
history only if it has the shape of an address. The words themselves appear at the command line alone,
under `server_said_untrusted`, for the owner.

## Installing

    pip install polarka-agent

That gives the command `polarka-agent`, which is the same as `python3 -m polarka_agent`. It needs Python 3.9
or later on Linux or macOS and nothing besides; it does not run on Windows. The package on PyPI is built from
this repository at the tag of its version; after 0.1.1 the workflow in `.github/workflows/release.yml` builds
and uploads it, and prints the SHA-256 of the files it made.

## Commands

    python3 -m polarka_agent --dir FOLDER new --chain regtest --server http://127.0.0.1:PORT
    python3 -m polarka_agent --dir FOLDER address | balance | history | payments | limits
    python3 -m polarka_agent --dir FOLDER pay ADDRESS 1.5 [--memo TEXT] [--dry-run]
    python3 -m polarka_agent --dir FOLDER pay-many FILE.json
    python3 -m polarka_agent --dir FOLDER received ADDRESS | wait ADDRESS 1.5
    python3 -m polarka_agent --dir FOLDER stamp FILE | stamp --sha256 HEX
    python3 -m polarka_agent --dir FOLDER forget ID
    python3 -m polarka_agent --dir FOLDER mcp

Every command prints one JSON object. Amounts are NORTH, written as text. `new --chain main` takes the public
server of the Northbound project (`https://api.northboundwallet.org`) unless another is named; that server is
not run by this project. `new` shows the 12 or 24 words once; `--words-file` brings a wallet back.

A stamp writes `POLARKA\x01` and a SHA-256 into an output that carries data. The chain is young and its hash
rate is small: a stamp shows an order of events, it is not proof for a court.

For an agent: start `python3 -m polarka_agent --dir FOLDER mcp` as an MCP server over standard input and
output. Its tools are `north_balance`, `north_address`, `north_received`, `north_wait`, `north_pay`,
`north_stamp`, `north_payments`, `north_history` and `north_limits`.

## A first coin: the faucet

A program that has no coin can ask a faucet for one. It does a small piece of work first (hashing, a few
seconds of one processor), so that asking a thousand times costs a thousand times as much.

    python3 -m polarka_agent --dir FOLDER faucet https://ADDRESS-OF-A-FAUCET

asks for a little NORTH to a fresh address of the wallet. There is no public faucet yet; when there is one,
its address will be on polarka.org.

Anybody can run one: `faucet-serve --amount 2` hands out 2 NORTH to an address once a day from the wallet in
FOLDER. What it may give in a day is that wallet's `limits.json`, nothing else, so a faucet is this wallet
under its own bounds. It keeps the addresses it paid (`faucet.jsonl`) and no internet addresses on disk; it
asks for no name and no e-mail. It is meant to stand behind a web server that provides HTTPS
(`--trust-proxy`). The faucet is new: nobody independent has read it, and it has never run in public.

## Something to pay for: postage

Whoever runs a web service and wants to keep robots from wearing it out makes them burn processor time for
nothing today. With postage the caller pays a little NORTH instead, and the one who runs the service gets it.

    python3 -m polarka_agent --dir FOLDER postage-serve --upstream 127.0.0.1:9000 --price 0.01 --calls 100 --free 20

stands before the service on port 9000. A caller without postage gets `402` and what to do, as JSON: ask for
a quote (`POST /_postage/quote`), pay the price to the address it names, and send the token in the header
`X-Postage`. Every quote has an address for itself for half an hour, so a payment is told apart without
anybody proving anything; an address that was not paid serves the next asker. `--free` lets one source make
some calls a day without postage.

Postage is counted in calls. It is not a balance, it is not given back, and the gate never pays from its
wallet, so the gate holds nobody's coins. Whoever runs a service for payment has the duties of one who does
(saying who they are, among others); the gate is a tool and takes none of them away. It is new: nobody
independent has read it, and it has never run in public. There is no asker's command for it yet: a program
pays the quoted address with `pay` and sends the token itself.

## Code and tests

Python 3.9 or later, standard library only. `polarka_agent/keys.py` and `tx.py` hold the keys, addresses, the
signature hash and signing; they come from the cold address tool
(github.com/severka-monitor/truenorth-cold-address) and are checked against its published P2QRH test vectors.
`server.py` is the client for a wallet server, `wallet.py` the wallet, `cli.py` and `mcp.py` the two faces,
`faucet.py` the faucet and its asker, `postage.py` the postage gate.

    TN_BIN=/path/to/bin python3 -B -m unittest discover -s tests -t .

79 tests. Those in `test_wallet.py` and `test_cli_mcp.py` run against the official `truenorthd` on regtest
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
