# Polarka Agent

<!-- mcp-name: org.polarka/polarka-agent -->

A wallet for TrueNorth (NORTH) that a program can use: a command line, a Python library, and a server for
agents that speak the Model Context Protocol (MCP). The keys stay on the machine it runs on. What its
addresses hold it asks a wallet server with the Northbound API (github.com/nova9/northbound).

**State: 0.2.3.** Any part of this package and of the services at polarka.org may change, and nothing is
promised.
`docs/how-it-works.md` (also at https://polarka.org/how/) says what each part does, who holds what and who sees
what. Faults: an issue at github.com/Polarka-monitor/polarka-agent, or @Polarka_monitor on X; if one puts coins
at risk, say only that you found something.

It was written with AI models under its author's direction, and its author read it. Independent AI reviewers
that had not seen it being written read each part and found faults. Every High finding is mended, with tests. Of
the Medium ones, review 5's M4 is mended in part, and one was open until 0.2.3, found twice (the first review's F4
and review 8's M4: the owner's limits bound a program that has only the MCP tools, not one that can also run
commands or write files as the same user). For it 0.2.3 brings a signing service, which keeps the words and the
limits under a user of their own; review 10 read it, and its reviewer read the fixes again (`REVIEW.md`,
`docs/review-guide.md`). On 22 November 2026 a security expert read the code and the server's configuration as a
private person; what he found is mended in 0.2.3, apart from what `docs/review-guide.md` names as open.
On the real network it pays since 17 November 2026, and since 20 November it runs a pool, a gate of postage,
an x402 facilitator and a stamps service at polarka.org. It is an
independent community project, not a product of the TrueNorth developers or of the Northbound project.

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
- With the wallet in a folder of the agent's own user, these bounds hold against a program that reaches it
  through the MCP server only. A program that can run commands or write files as the same user, as most agents
  can, can change `limits.json`, move the journal away or read the words. Against that, run the wallet as a
  signing service under a user of its own (from 0.2.3; next section): the agent's user then only asks.
- A payment counts into its day whatever became of it; only the owner's `forget` takes one out. A payment
  that another one replaced therefore counts twice for a day. One whose outcome stayed open for a day or more (a
  payment signed for x402 and held back, say) and then goes out counts for the day it goes out (from 0.2.3;
  review 10, H1).
- What a wallet server says in words is not passed on to the program: a refusal comes as one of a few fixed
  words and an error as its HTTP status.

The recovery words are in `wallet.json`, readable by its owner only (mode 600; the program refuses to work
with a file that others can read). Whoever can read that file can spend. This is a wallet for small amounts
on a machine its owner controls.

## The words apart: a signing service (from 0.2.3)

From 0.2.3; review 10 read it, its reviewer read the fixes again, and what both found is mended
(`docs/review-guide.md`). The wallet runs
as a service under a user of the system of its own: its folder, with the words and the journal, is that user's alone,
and `limits.json` is root's. The agent runs as a user of its own too, one that cannot administer the machine, in a
group that may use the service's socket, and asks: what the wallet holds, an address, to pay, to stamp. It cannot
read the words, change the limits or forget a payment. The rules of paying are the wallet's, unchanged, and when the
service does not run, the agent pays nothing. Set it up from an account the agent does not use:

    polarka-agent signer-setup --agent-user AGENT --per-payment 0.1 --per-day 1 > setup.sh   # a script: read it
    sudo sh setup.sh                          # the users, the group, the program, a new wallet, the service

The script ends by saying how to look at the result as the agent's user, with the service's own Python (on Linux
`sudo -u AGENT /opt/polarka-signer/venv/bin/python3 -I -m polarka_agent signer-setup --check`), and the agent is then
given `polarka-agent --signer SOCKET mcp`, or `POLARKA_SIGNER=SOCKET`.

On Linux the service is a systemd unit run as `polarka-signer`, with the wallet in `/var/lib/polarka-signer` and its
socket at `/run/polarka-signer/signer.sock`. On macOS it is a LaunchDaemon run as a hidden user `_polarkasigner`,
with the wallet in `/Library/Application Support/PolarkaSigner/wallet` and the socket in `.../PolarkaSigner/run`.
The program the service runs is installed where only root can change it (on macOS with Apple's own Python), from PyPI
at the version the script names, or from a wheel named with `--wheel`, used only if its SHA-256 is still the one it
had when the script was printed. The script takes its commands from the system's own folders and runs Python and
pip in an empty environment, so that nothing of the account that ran sudo reaches them; behind a proxy, pip finds
it only in root's own settings (`/etc/pip.conf`, on macOS `/Library/Application Support/pip/pip.conf`). It makes a new wallet in the service and shows its words
once. `--move-from FOLDER` moves an existing one instead, reading only plain files and as the agent's user; but an
agent that could read that wallet may know its words, so the whole of the gain is a new wallet and the coins paid
over to it. `--check` looks, as the agent's user, at the socket, the service, who may open its folder, who may
change `limits.json`, the program and every place it takes modules from, and whether that user can administer the
machine (a sudo rule of its own that wants a password is seen only by the script, as root). Run it with the service's
own Python, as above: a copy that the agent can change could say anything.

It keeps nothing from an agent whose user can administer the machine (sudo with a password or without, admin,
docker and the like: such an agent can wait for its owner's password), nor on a machine that someone else took over;
then what the wallet holds is at stake, so keep little in it. `signer-setup` refuses such an agent's user, by its groups
and by what sudo lets it do, unless told `--admin-agent`, and the service then keeps only the simple ways in shut: reading the words, writing the limits.
`forget` and making a wallet stay with the owner, run as the service's user on its folder. Without `--signer`
everything works as before, and the MCP server says in its log that the words can be read by its user.

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

    pip install polarka-agent==0.2.3

That gives the command `polarka-agent`, which is the same as `python3 -m polarka_agent`. The version is named so
that a later upload to PyPI changes nothing on your machine until you name it. It needs Python 3.9 or later on Linux
or macOS and nothing besides; it does not run on Windows. Versions are built from this repository at the tag of
their version and uploaded by hand; 0.1.1 and 0.1.2 were built by a workflow that is no longer used. The same files
are on the project's own server,

    pip install https://polarka.org/download/polarka-agent-0.2.3/polarka_agent-0.2.3.tar.gz

and can be compared with `https://polarka.org/download/SHA256SUMS` first. Versions before 0.1.3 carry a
faucet with the faults that `docs/faucet-review-0.1.2.md` names.

## Commands

    python3 -m polarka_agent --dir FOLDER new --chain regtest --server http://127.0.0.1:PORT
    python3 -m polarka_agent --dir FOLDER address | balance | history | payments | limits
    python3 -m polarka_agent --dir FOLDER pay ADDRESS 1.5 [--memo TEXT] [--dry-run]
    python3 -m polarka_agent --dir FOLDER pay-many FILE.json
    python3 -m polarka_agent --dir FOLDER received ADDRESS | wait ADDRESS 1.5
    python3 -m polarka_agent --dir FOLDER stamp FILE | stamp --sha256 HEX
    python3 -m polarka_agent --dir FOLDER faucet URL | postage URL --most 0.05
    python3 -m polarka_agent --dir FOLDER fetch URL --most 0.05
    python3 -m polarka_agent stamp-verify PROOF.json
    python3 -m polarka_agent --dir FOLDER forget ID
    python3 -m polarka_agent --dir FOLDER mcp
    python3 -m polarka_agent --signer SOCKET balance | pay ... | fetch ... | mcp      (through a signing service)
    python3 -m polarka_agent --dir FOLDER signer-serve --socket SOCKET --group GROUP
    python3 -m polarka_agent signer-setup --agent-user USER [--per-payment N --per-day N] [--move-from FOLDER]
                                          [--package polarka-agent==X | --wheel FILE] [--admin-agent]
    python3 -m polarka_agent signer-setup --check            (as the agent's user, with the service's Python)

Every command prints one JSON object, but `signer-setup` without `--check`, which prints a script. Amounts are
NORTH, written as text. `new --chain main` takes the public server of the Northbound project
(`https://api.northboundwallet.org`) unless another is named; that server is not run by this project. `new` shows
the 12 or 24 words once; `--words-file` brings a wallet back.

A stamp writes `POLARKA\x01` and a SHA-256 into an output that carries data. The chain is young and its hash
rate is small: a stamp shows an order of events, it is not proof for a court.

For an agent: start `python3 -m polarka_agent --dir FOLDER mcp` (or `uvx polarka-agent@0.2.3 mcp`) as an MCP
server over standard input and output. Without a wallet in the folder it makes one on main, which pays
nothing until its owner writes `limits.json`; the recovery words are in `wallet.json` there. Its tools are
`north_balance`, `north_address`, `north_received`, `north_wait`, `north_pay`, `north_stamp`, `north_payments`,
`north_history`, `north_limits`, `north_faucet`, `north_postage` and `north_fetch`. The tools that ask a web
address reach public https addresses only, not this machine or its network, so that a page the agent reads
cannot make it ask the owner's own services; `mcp --allow-local` lets them, for a test service of one's own
(from 0.2.3). With a signing service, `polarka-agent --signer SOCKET mcp` makes no wallet: the folder
then keeps only this program's own record of x402 payments under way.

## A first coin: the faucet

A program that has no coin can ask a faucet for one. It does a small piece of work first (hashing, a few
seconds of one processor). The work is friction, not a wall: a program written for speed does it many times
faster. What bounds a faucet is what it counts, below.

    python3 -m polarka_agent --dir FOLDER faucet https://ADDRESS-OF-A-FAUCET

asks for a little NORTH to a fresh address of the wallet. The project runs one at https://faucet.polarka.org
(an agent gives that address to the tool `north_faucet`; `start` asks it by itself).

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
fixes have not been read by anybody else. Ours runs in public at faucet.polarka.org since 19 November 2026.

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
(saying who they are, among others); the gate is a tool and takes none of them away. Independent AI reviewers
have read it (`docs/review-guide.md`), no person yet. Ours runs at gate.polarka.org since 20 November 2026.

The asker's side is `postage URL --most 0.05` (for an agent the tool `north_postage`): it asks the gate for a
quote, pays it from the wallet and shows the token. The owner's limits hold as for any payment, and nothing
above `--most` is paid whatever the gate asks.

## Paying any service: x402

x402 is an open standard for paying over HTTP: a service answers `402 Payment Required` with what it takes,
the caller pays and asks again with the payment in a header. This package speaks x402 version 2 in NORTH,
with the scheme `exact` on the network `bip122:470c8b8ae824c06575cfae0851282c49` (TrueNorth's main chain). The
scheme is written down in `docs/x402-scheme-exact-bip122.md` and proposed to the x402 Foundation.

    python3 -m polarka_agent --dir FOLDER fetch https://gate.polarka.org/v1/stamp/SHA256 --most 0.05

`fetch` (for an agent the tool `north_fetch`) asks the URL; when it answers 402 in NORTH it pays no more than
`--most` and the owner's limits allow, and asks again. The payment is kept in the wallet's folder before
anybody sees it, so a later `fetch` of the same URL shows the same payment again and pays nothing more. It
follows no redirect, and talks x402 only over https or on the same machine.

The seller gives the payment to the network itself and takes it as paid when it is in a block
(`--confirmations`, one by default): a payment that only waits in the mempool can still be replaced, and
TrueNorth has full replace-by-fee. While the block is not there the seller answers `202` with
`awaiting_confirmation`, and the buyer shows the same payment again. One payment buys one thing once, a copy
of a payment seen in the mempool buys nothing, and a payment counts only for the resource it was made for.

    python3 -m polarka_agent x402-facilitator --chain main --rpc-url http://127.0.0.1:9554 \
        --rpc-cookie /path/to/.cookie --listen 127.0.0.1:9570

is the facilitator, the part of a seller that checks payments and hands them to its node (`/supported`,
`/verify`, `/settle`, as in the standard). It holds no keys and no coins. `postage-serve --x402-facilitator
http://127.0.0.1:9570` makes the gate of postage take x402 as well as its own quotes: one x402 payment buys
a lot of calls, and the gate says how many are left in `X-Postage-Calls-Left`. With `--public` (from 0.2.3)
a facilitator serves sellers from outside: it counts requests by address, keeps
no catalog and keeps at most so many payments, as a process apart from the gate's own (`deploy/x402/README.md`).
Ours is at `https://gate.polarka.org/facilitator`.

Being found: the bazaar extension of x402. With `--public-url https://HOST --offers FILE` the gate names its
resources in full in the 402 and adds `extensions.bazaar` (what a route takes and gives, with a JSON schema) for
the routes in FILE, a JSON list of objects such as

    [{"prefix": "/v1/stamp", "route": "/v1/stamp/:sha256", "description": "Stamps a SHA-256 into the TrueNorth chain",
      "pathParams": {"sha256": "..."}, "output": {"state": "stamped"}, "serviceName": "Polarka stamps", "tags": ["stamps"]}]

and a facilitator started with `--bazaar-origin https://HOST` keeps a catalog of what was paid for there, at
`GET /discovery/resources` and `GET /discovery/search?query=`, as the bazaar specification describes. A buyer
echoes the extension (`fetch` does); the gate puts back its own resource and extension before the facilitator
sees the payment, so a buyer cannot write something else into the catalog. Ours is at
https://gate.polarka.org/x402/discovery/resources (`docs/how-it-works.md`).

An agent that pays for itself, from no coin to a stamped file: `examples/agent_that_pays_for_itself.py`.

## A service to pay for: stamps

A stamp shows that a file existed by a given block. The stamps service takes SHA-256s, puts those that came
in during ten minutes into one tree of hashes, and writes the tree's root into the chain with one
transaction. Each SHA-256 then gets a proof: its path to the root, the transaction, the block, and the node's
proof that the transaction is in that block. Only the SHA-256 is sent, never the file.

    GET https://gate.polarka.org/v1/stamp/<sha256>    (behind the gate: pay with x402 or postage)
    python3 -m polarka_agent stamp-verify PROOF.json [--rpc-url URL --rpc-cookie FILE]

The answer is `waiting` (with the time of the next batch), `written` (the transaction waits for a block) or
`stamped` with the proof. `stamp-verify` checks the path, that the transaction carries the root, and that
the proof of the block holds the transaction; with a node it also checks that the block is in the chain.
Without a node it says that the block was not checked: everything else in a proof can be made up together
with a block that was never mined.

The tree: leaf = SHA-256(0x00 || digest), node = SHA-256(0x01 || left || right), the leaves sorted, an odd one
at the end of a level goes up as it is. The transaction carries `OP_RETURN` `POLARKA` 0x01 || root.

    python3 -m polarka_agent --dir STAMPS-WALLET stamps-serve --rpc-url http://127.0.0.1:9554 \
        --rpc-cookie /path/to/credentials --listen 127.0.0.1:8681
    python3 -m polarka_agent --dir GATE-WALLET postage-serve ... --route /v1/stamp=127.0.0.1:8681

runs one; it belongs behind the gate, which takes the payment. Its wallet pays only the network fee of one
transaction a batch. A batch whose transaction the node has lost for an hour goes back to waiting. The
digests of a batch are written once into a file of their own, and a SHA-256 that waits is one line appended
to `waiting.log`. The chain is young and its hash rate is small: a stamp shows an order of events, it is not
proof for a court.

## Letters between agents

An agent can write to another agent's inbox and read its own. A letter costs its writer one call of postage
at the post office's gate (bought with x402 like any lot), and only the inbox it is for can read it.

    python3 -m polarka_agent mail-inbox                       # this wallet's inbox: 64 hexadecimal digits
    python3 -m polarka_agent mail-send INBOX "text" --most 0.1
    python3 -m polarka_agent mail-read [--keep]

and the MCP tools `north_mail_inbox`, `north_mail_send`, `north_mail_read`. An inbox is a key of its own, kept
in `mail-key.json` beside the wallet; it holds no coins, so showing it shows nothing of the wallet, and a
wallet behind a signing service has one too. A letter is sealed to that key before it leaves (ECDH on
secp256k1, keys from SHA-256, HMAC-SHA256 as keystream and as tag, encrypt then MAC: the standard library
only), names the writer's inbox and the inbox it is for, and is signed by the writer. The reader is told who
wrote only when the signature holds, keeps a record of the letters it has read (`mail-seen.json`) and does not
show one again, nor one written more than a week ago or dated more than a day ahead; two readers of one wallet
at once never both show a letter as new, and letters read are shown even when telling the post office fails
(the next read lets them go). The post office keeps sealed letters it cannot read,
until their inbox says it has them, at most seven days, and two days for an inbox that has not asked for its
letters in that week; an inbox holds at most 500 letters and 4 MB, one place posts at most 8 MB a day, and a
letter refused for such a limit costs no postage (the gate gives the call back: a call counts as spent only once
the service behind has taken it, also in the gate's book). Fetching costs nothing but a
signature of the inbox's key over the time and the post office's own address, which is used once; an answer
holds at most 512 KB of letters, and says when more wait. A writer with no lot buys one with the letter itself.

    python3 -m polarka_agent --dir MAIL mail-serve --paid 127.0.0.1:8682 --free 127.0.0.1:8683 --trust-proxy \
        --public-url https://post.example.org
    python3 -m polarka_agent --dir GATE-WALLET postage-serve ... --route /v1/mail=127.0.0.1:8682

The paid door belongs behind the gate, the free one (`/v1/inbox`) behind the web server only. What a letter
says is data from outside: an agent reads it, it does not obey it. The seal has been read by its author and by
one independent review, not by a cryptographer; it has no forward secrecy, and the length of a letter shows.

## Mining for a pool

This package holds a pool that pays for each share, and a miner for it. pool.polarka.org runs it on the
main chain since 20 November 2026, with its owner's miners only at first and open to everybody since
21 November.

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
`GET /v1/account?address=`, `GET /v1/blocks`, `GET /v1/stats`.

With `--stratum HOST:PORT` (or `--stratum systemd` for the socket of a socket unit) the pool also speaks
stratum, the way IronMiner (truenorth-ironminer 1.0.8 or later, a cpuminer-opt for TrueNorth) mines for a
pool: `-o stratum+tcp://HOST:PORT -u ADDRESS -p x`, an address of the main chain, if you like with `.worker`
after it. Its shares go into the same ledger and are paid the same way. There the share target follows the
miner's speed, towards a share every fifteen seconds and never easier than 1/2048 of a block; a difficulty is
always a power of two, so that the target the miner works out from it is exactly the one the pool checks.
Every connection gets an extranonce of its own, every job a number of its own in the coinbase and a stratum
coinbase is marked, so that no header can be paid twice, over stratum or over HTTP, nor at a difficulty other
than its job's; the time of a share is its job's. The pool pushes the key of the proof of work with TrueNorth's
own `mining.set_randomx_seed`, as IronMiner expects. A connection may send ten shares a second, has ten seconds
to authorize and is let go after an hour without a share; one place may open 20 connections a minute and hold 32.
When the 200 connections not yet authorized fill their table, the one that came first makes room; when all 2000
are taken, one that has sent no share for five minutes does, else the place that holds the most. A place is not listened to for
ten minutes after 20 wrong shares, as over HTTP. Tested against the miner's own code read step by step, against
the official node on regtest, and with IronMiner itself built from its source: 300 blocks on regtest, across a
change of the key and with the difficulty moving both ways.

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
here, and so are these from the same review: a share for a block that is no longer the tip is refused within
a second, a payout counts as paid when the pool's own node sees it six blocks deep, the server turns askers
away above 200 at once (it still belongs behind a web server, with `--trust-proxy`), and a stranger can no
longer close the cap. A second independent reviewer then read those fixes and paid one round twice: a wallet server that forgot a
payment for a moment made the pool build a second one. A round now has one transaction and never another;
it is only shown to the server again. From the same review: the test for kept-back blocks also runs over all
the pool's shares together, the smallest debts are paid first, a round is looked for as far back as its own
time, and the pool no longer calls itself short of coins while a round is on the way. Nobody else has read
these fixes either.

A third independent reviewer could not make it pay twice by any path of its own (also not with a wallet
server that lies and a process that falls at every seam), but showed that the way out this page used to
name, `forget`, did: a forgotten payment can still be passed on by whoever kept it. With a node of the
pool's own, a round is now paid when that node sees its payment six blocks deep, whatever a server says,
and taken back only when that node shows a coin of the payment spent by something else, as deep.

For whoever runs it: a payment that sticks stops all payouts. Cancel it with a small payment from the
pool's wallet to an address of its own (the wallet builds it on a coin of the stuck one); do not `forget`
it. Use the wallet for nothing else. Keep the float in some ten coins, not one. Never put the pool's folder
back from an older copy: it pays again what the copy does not know. The wallet server must be the owner's
own. A miner who keeps back the blocks he finds can take the float before the pool notices, and no more
than the float and the wallet's limit for a day. The share ledger grows without end.

Not built: taking back credit for a block that left the chain. Independent AI reviewers have read the pool
over HTTP several times, and a person once (22 November 2026); the stratum side and the letters have been read by
one independent AI reviewer twice (review 11 and a second look at its mending, 22 November 2026), and what both
found is mended; the mending of the second look has been read by its author only.

## Code and tests

Python 3.9 or later, standard library only. `polarka_agent/keys.py` and `tx.py` hold the keys, addresses, the
signature hash and signing; they come from the cold address tool
(github.com/Polarka-monitor/truenorth-cold-address) and are checked against its published P2QRH test vectors.
`server.py` is the client for a wallet server, `wallet.py` the wallet, `cli.py` and `mcp.py` the two faces,
`faucet.py` the faucet and its asker, `postage.py` the postage gate, `rx.py` the proof of work through the
library in `native/`, `pool.py` the pool, `stratum.py` its stratum, `payout.py` its payouts, `miner.py` its
miner, `x402.py` the x402 buyer, paywall and facilitator, `bazaar.py` the catalog of the bazaar extension,
`stamps.py` the stamps service and the check of a proof, and `mail.py` the letters and the post office.

    TN_BIN=/path/to/bin python3 -B -m unittest discover -s tests -t .

384 tests. Those in `test_wallet.py`, `test_cli_mcp.py`, `test_faucet_node.py`, `test_pool_node.py`,
`test_stratum_node.py`, `test_payout_node.py`, `test_walletd.py`, `test_x402.py` and `test_stamps.py` run against the official
`truenorthd` on regtest and a stand-in for a Northbound server (`tests/standin`, copied from the Polarka Wallet's repository); they
are skipped without the node programs. No test touches a real network.

`tests/faults.py` is a test of the tests: it puts one deliberate fault at a time into a copy of the code
(a limit that is not applied, a day that does not count the payment being made, and so on) and expects the
tests to fail. It first runs the copy as it stands, and counts a fault as caught only when tests ran and failed. Ten faults
in the owner's rules. It covers those rules only, not the keys, the signing or the client of the server.

## What is missing

- The points of the security review of 22 November 2026 that `docs/review-guide.md` names as open.
- Use by anybody but its author: the pool, the gate and the stamps service have served nobody else yet.
- More than two hundred addresses of a kind: beyond that the wallet hands out no new address, and change uses
  the keys of the window again in turn (from 0.2.3).
- A way to replace a payment that was handed over and waits for a block without end.
- A node of one's own in place of a wallet server, for the wallet.
- A history that is exact for a wallet with more than a hundred addresses.
- Checking the block of a stamp without a node.

## Licence

MIT, see `LICENSE`. There is no warranty.
