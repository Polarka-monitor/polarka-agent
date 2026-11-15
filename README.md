# Polarka Agent

A wallet for TrueNorth (NORTH) that a program can use: a command line, a Python library, and a server for
agents that speak the Model Context Protocol (MCP). The keys stay on the machine it runs on. What its
addresses hold it asks a wallet server with the Northbound API (github.com/nova9/northbound).

**State: 0.1.0.dev0. An early build that has only ever paid on a test network (regtest).** It was written
with an AI model under its author's direction. Its author's tests are the only check it has had: nobody
independent has looked at it, and it has not made a payment on the real network. Do not put into it what
you cannot lose. It is an independent community project, not a product of the TrueNorth developers or of
the Northbound project.

## What it is for

A program that has a processor and no bank account can earn NORTH by mining and can be paid in it. This
wallet lets such a program hold coins and pay small amounts by itself, within bounds that a person sets and
that the program cannot change.

## How a program is kept within bounds

- Without a file `limits.json` in the wallet's folder the wallet pays nothing. Its owner writes that file by
  hand: `{"per_payment_north": "5", "per_day_north": "20"}`, and if wanted `"allow": [addresses]` and
  `"fee_rate"`. No command and no tool of the MCP server writes or changes it.
- Every transaction is written into `journal.jsonl` before anything leaves the machine. Lines are only added.
- The MCP server has no tool that shows the recovery words, changes the limits or forgets a payment.
- What a wallet server says in words is cut short and stripped to plain characters before it is passed on,
  and the server's instructions to the agent say that such words are data.

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
balance or hold a payment back. It cannot take coins.

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

## Code and tests

Python 3.9 or later, standard library only. `polarka_agent/keys.py` and `tx.py` hold the keys, addresses, the
signature hash and signing; they come from the cold address tool
(github.com/severka-monitor/truenorth-cold-address) and are checked against its published P2QRH test vectors.
`server.py` is the client for a wallet server, `wallet.py` the wallet, `cli.py` and `mcp.py` the two faces.

    TN_BIN=/path/to/bin python3 -B -m unittest discover -s tests -t .

59 tests. Those in `test_wallet.py` and `test_cli_mcp.py` run against the official `truenorthd` on regtest
and a stand-in for a Northbound server (`tests/standin`, copied from the Polarka Wallet's repository); they
are skipped without the node programs. No test touches a real network.

## What is missing

- A review by anybody but its author, and deliberate faults to show that the tests catch them.
- Any payment on the real network.
- A way to replace a payment that was handed over and waits for a block without end.
- A node of one's own in place of a wallet server.
- A history that is exact for a wallet with more than a hundred addresses.
- Checking a stamp without trusting a server or a node.

## Licence

MIT, see `LICENSE`. There is no warranty.
