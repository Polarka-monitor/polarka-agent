"""polarka-agent: a TrueNorth (NORTH) wallet that a program can use. The command line.

Every command prints one JSON object. A command that is refused prints {"error": KIND, "message": ...} and
ends with status 2; a server that cannot be believed or reached ends with status 3.
"""
import argparse
import hashlib
import json
import os
import sys
import time

from . import __version__
from .keys import BadInput
from .miner import PoolSaid
from .rx import NoLibrary
from .server import ServerError
from .wallet import Refused, Wallet, north, units

DEFAULT_SERVER = "https://api.northboundwallet.org"     # the public server of the Northbound project; not run by this project
DEFAULT_POOL = "https://pool.polarka.org"
DEFAULT_FAUCET = "https://faucet.polarka.org"


def _amounts(result):
    """Base units to NORTH, for the fields that are amounts."""
    for name in ("spendable", "set_aside", "unripe", "amount"):
        if isinstance(result.get(name), int) and not isinstance(result.get(name), bool):
            result[name] = north(result[name])
    for line in result.get("lines", []):
        if isinstance(line.get("net"), int):
            line["net"] = ("-" if line["net"] < 0 else "") + north(abs(line["net"]))
        if isinstance(line.get("fee"), int):
            line["fee"] = north(line["fee"])
    return result


def wait_for(wallet, address, amount, confirmations=1, timeout=600, every=15, sleep=time.sleep, clock=time.monotonic):
    """Looks at one of the wallet's addresses until it holds the amount that deep, or the time is over."""
    until = clock() + timeout
    while True:
        seen = wallet.received(address)
        if seen["amount"] >= amount and seen["confirmations"] >= confirmations:
            return dict(_amounts(seen), paid=True)
        if clock() + every > until:
            return dict(_amounts(seen), paid=False)
        sleep(every)


def start(folder, a, say=lambda event: sys.stderr.write(json.dumps(event) + "\n"), wallet=None):
    """The `start` command: what an agent needs to begin, each step said as it is done. Mining goes on until
    --seconds or for ever; the summary comes when it ends. With wallet (a signing service) no wallet is made here."""
    if not 1 <= a.threads <= 256:
        raise Refused("INPUT", "--threads is 1 to 256")
    if wallet is None:
        made = not os.path.exists(os.path.join(folder, "wallet.json"))
        if made:
            Wallet.create(folder, "main", DEFAULT_SERVER)
        wallet = Wallet(folder)
        note = ("The recovery words are in wallet.json in that folder; whoever reads that file can spend. Keep a copy "
                "somewhere else. The wallet pays nothing until its owner writes limits.json.")
    else:
        made, note = False, "The wallet is the signing service's: its words and its limits are kept by the service, not here."
    if wallet.chain != "main":
        raise Refused("CHAIN", "the wallet is not of the main chain")
    # one address the agent keeps, rather than a new one each time
    address = wallet.first_address() if hasattr(wallet, "first_address") else wallet.account.address(0, 0)
    done = {"folder": folder, "address": address, "wallet_made": made}
    say({"event": "wallet", "made": made, "folder": folder, "address": address, "note": note})
    if not a.no_faucet:
        from .faucet import ask
        try:
            done["faucet"] = ask(a.faucet, address)
        except Refused as e:                     # a faucet that says no (once a day, empty) does not stop the start
            done["faucet"] = {"error": e.kind, "message": str(e)}
        say(dict(done["faucet"], event="faucet"))
    if not a.no_mining:
        from .miner import mine
        from .rx import find_library
        if not find_library():
            raise NoLibrary("this package has no proof-of-work library for this platform; build it with `polarka-agent build-rx` "
                            "(see `build-rx --help`), or start with --no-mining")
        say({"event": "mining", "pool": a.pool, "address": address, "threads": a.threads,
             "note": "The pool pays for each share. Mine only on a machine whose owner allows it."})
        done["mining"] = mine(a.pool, address, a.threads, True, None, a.seconds, say=say)
        for name in ("credited",):
            if isinstance(done["mining"].get(name), int):
                done["mining"][name] = north(done["mining"][name])
    return done


def run(argv=None, out=sys.stdout):
    p = argparse.ArgumentParser(prog="polarka-agent", description="A TrueNorth (NORTH) wallet that a program can use.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--dir", default=os.environ.get("POLARKA_AGENT_DIR", "~/.polarka-agent/default"), help="the wallet's folder")
    p.add_argument("--signer", default=os.environ.get("POLARKA_SIGNER") or None, metavar="SOCKET",
                   help="ask the signing service at this socket (signer-serve) in place of a wallet in --dir: the words and "
                        "the limits stay with the service, and --dir keeps only this program's own records (also POLARKA_SIGNER)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("new", help="make a wallet, or bring one back from its words")
    c.add_argument("--chain", required=True, choices=("main", "regtest"))
    c.add_argument("--server", help="the wallet server (default on main: %s)" % DEFAULT_SERVER)
    c.add_argument("--words-file", help="a file with the 12 or 24 words of a wallet to bring back")
    c.add_argument("--twelve", action="store_true", help="12 words in place of 24")
    c = sub.add_parser("start", help="begin in one go: a wallet on main (made when --dir has none), a first coin from the faucet, "
                                     "then mining for the pool to the wallet's first address")
    c.add_argument("--pool", default=DEFAULT_POOL)
    c.add_argument("--faucet", default=DEFAULT_FAUCET)
    c.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 2) // 2), help="default: half of the processors")
    c.add_argument("--seconds", type=int, help="stop mining after this many seconds (default: never)")
    c.add_argument("--no-faucet", action="store_true")
    c.add_argument("--no-mining", action="store_true")
    sub.add_parser("address", help="a fresh address to be paid to")
    sub.add_parser("balance")
    sub.add_parser("history")
    sub.add_parser("payments", help="what this wallet paid and what became of it")
    sub.add_parser("limits", help="what the owner lets the wallet pay by itself")
    c = sub.add_parser("received", help="what sits on one of the wallet's addresses")
    c.add_argument("address")
    c = sub.add_parser("wait", help="wait until an address of the wallet holds an amount")
    c.add_argument("address")
    c.add_argument("amount")
    c.add_argument("--confirmations", type=int, default=1)
    c.add_argument("--timeout", type=int, default=600)
    c = sub.add_parser("pay")
    c.add_argument("address")
    c.add_argument("amount", help="NORTH, for example 1.5")
    c.add_argument("--memo")
    c.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("pay-many", help="several payments in one transaction")
    c.add_argument("file", help='a JSON list: [{"address": "...", "amount": "1.5"}, ...]')
    c.add_argument("--memo")
    c.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("stamp", help="write the SHA-256 of a file into the chain")
    c.add_argument("file", nargs="?")
    c.add_argument("--sha256", help="the SHA-256 itself, 64 hex characters, in place of a file")
    c.add_argument("--memo")
    c.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("forget", help="the owner's word that a payment which did not go through stands against nothing any more")
    c.add_argument("id")
    c = sub.add_parser("mcp", help="serve the wallet to an agent over the Model Context Protocol (standard input and output)")
    c.add_argument("--allow-local", action="store_true", help="let the agent's tools reach plain http on this machine and "
                   "addresses of its network (for a test service of your own); without it they reach public https only")
    c.add_argument("--signer", dest="mcp_signer", metavar="SOCKET", help="the same as --signer before the command")
    c = sub.add_parser("signer-serve", help="be the signing service: hold the wallet in --dir, a folder of this user's alone, "
                                            "and pay for the agents that ask over --socket, within its limits.json")
    c.add_argument("--socket", required=True, help="the socket, in a folder that only this user (or root) may write")
    c.add_argument("--group", help="the group whose members may ask (the socket is then 660 for it); without it this user only")
    c = sub.add_parser("signer-setup", help="print a script that sets up a signing service on this system (Linux with systemd, "
                                            "or macOS), for its owner to read and run as root; with --check, look at one "
                                            "that is set up, as the agent's user. Without --check this prints a script, not JSON")
    c.add_argument("--agent-user", help="the user the agent runs as, who may ask the service (default: this user); it must "
                   "not be able to administer the machine")
    c.add_argument("--admin-agent", action="store_true", help="let --agent-user be a user who can administer the machine "
                   "(sudo, admin, docker ...), knowing that the service then keeps from the agent only the simple ways in")
    c.add_argument("--move-from", metavar="FOLDER", help="move the wallet of this folder to the service; without it a new "
                   "wallet is made there")
    c.add_argument("--per-payment", help="NORTH the service may pay by itself at once (with --per-day; without them it pays "
                   "nothing until limits.json is written)")
    c.add_argument("--per-day", help="NORTH the service may pay by itself in a day")
    c.add_argument("--package", help="the version from PyPI that the service runs, polarka-agent==VERSION (default: "
                   "polarka-agent==%s)" % __version__)
    c.add_argument("--wheel", help="in place of --package: a wheel file, installed only if its SHA-256 is still the one "
                   "it has when the script is printed")
    c.add_argument("--system", choices=("linux", "macos"), help="default: this system")
    c.add_argument("--check", action="store_true", help="look at the service as this user: its socket, that this user "
                   "cannot open the words, who may change the program it runs, whether this user can administer the "
                   "machine; best run with the service's own Python (the script says how)")
    c.add_argument("--socket", help="with --check: the socket (default: --signer, else where the script puts it)")
    c.add_argument("--service-dir", help="with --check: the service's folder")
    c.add_argument("--venv", help="with --check: the folder of the program the service runs")
    c = sub.add_parser("faucet", help="ask a faucet for a first coin, to a fresh address of this wallet")
    c.add_argument("url")
    c.add_argument("--max-bits", type=int, default=24, help="the most work done for it, in zero bits (24 is about ten seconds of one core)")
    c = sub.add_parser("postage", help="buy postage at a gate: ask it for a quote and pay it, within the limits")
    c.add_argument("url")
    c.add_argument("--most", required=True, help="the most NORTH to pay for one lot of calls")
    c = sub.add_parser("fetch", help="get a resource that may answer 402 (x402): pay it in NORTH within the limits and never more "
                                     "than --most, then get it")
    c.add_argument("url")
    c.add_argument("--most", required=True, help="the most NORTH to pay for it")
    c.add_argument("--out", help="write what came back to this file (else up to 4000 characters of it are shown)")
    c.add_argument("--wait", type=int, default=600, help="seconds to wait while the seller waits for a block (default 600); "
                   "fetching the same url again later pays nothing more")
    c = sub.add_parser("x402-facilitator", help="settle x402 payments in NORTH for sellers, with this machine's node; --dir keeps "
                                                "what was taken")
    c.add_argument("--chain", required=True, choices=("main", "regtest"))
    c.add_argument("--rpc-url", required=True, help="the node, for example http://127.0.0.1:9554")
    c.add_argument("--rpc-cookie", required=True, help="the node's .cookie file")
    c.add_argument("--listen", default="127.0.0.1:9570", help="a loopback address; put a web server with a certificate before it")
    c.add_argument("--confirmations", type=int, default=1, help="blocks a payment must be in before it is settled (default 1)")
    c.add_argument("--instant-below", default="0", help="NORTH under which a payment is settled when the node takes it, before a "
                   "block (default 0: never)")
    c.add_argument("--redeliver", type=int, default=600, help="seconds after it was settled that the same payment and claim "
                   "are settled again (an answer lost on the way)")
    c.add_argument("--bazaar-origin", action="append", default=[], metavar="https://HOST",
                   help="keep a bazaar catalog (GET /discovery/resources, /discovery/search) of the resources of this origin "
                   "that were paid for (may be given more than once; none: no catalog)")
    c.add_argument("--public", action="store_true",
                   help="serve sellers from outside, behind a web server that sets X-Forwarded-For: a payment to /verify or "
                   "/settle at most --per-minute times a minute from one address, no catalog, at most --most-payments kept; "
                   "run it apart from the facilitator of your own gate, with its own --dir")
    c.add_argument("--per-minute", type=int, default=60, help="with --public: requests a minute from one address, 1 to 600 "
                   "(default 60; ten times that from all together)")
    c.add_argument("--most-payments", type=int, help="take no new payment once so many are kept (default: 20000 with --public, "
                   "else no bound)")
    c = sub.add_parser("postage-serve", help="be a gate before a web service that lets through who paid postage to this wallet")
    c.add_argument("--upstream", required=True, help="the service behind the gate, host:port")
    c.add_argument("--price", help="NORTH for one lot of calls")
    c.add_argument("--price-hashes", type=int, help="in place of --price: mining work for one lot of calls, in hashes; "
                   "turned into NORTH by what the network needs for a block now")
    c.add_argument("--calls", type=int, default=100, help="calls in one lot")
    c.add_argument("--free", type=int, default=0, help="calls a day that one source may make without postage")
    c.add_argument("--listen", default="127.0.0.1:8670")
    c.add_argument("--trust-proxy", action="store_true", help="take the caller's address from X-Forwarded-For")
    c.add_argument("--x402-facilitator", help="also sell the lot over x402, settled by this facilitator (http://127.0.0.1:9570)")
    c.add_argument("--route", action="append", default=[], metavar="PREFIX=HOST:PORT",
                   help="send paths that start with PREFIX to another service behind the gate (may be given more than once)")
    c.add_argument("--public-url", help="the https:// address the gate is seen at; the 402 then names its resources in full")
    c.add_argument("--offers", help="a JSON file of what the gate lets be found in an x402 bazaar (with --public-url)")
    c = sub.add_parser("stamps-serve", help="stamps as a service: many SHA-256s in one transaction of this wallet, a proof for each")
    c.add_argument("--rpc-url", required=True, help="the node the blocks are read from, for example http://127.0.0.1:9554")
    c.add_argument("--rpc-cookie", required=True, help="the node's .cookie file")
    c.add_argument("--listen", default="127.0.0.1:8681")
    c.add_argument("--every", type=int, default=600, help="seconds between two batches, at least (default 600)")
    c = sub.add_parser("stamp-verify", help="check a proof of a stamp; with a node, also that its block is in the chain")
    c.add_argument("file")
    c.add_argument("--rpc-url")
    c.add_argument("--rpc-cookie")
    c = sub.add_parser("faucet-serve", help="be a faucet: hand out a little from this wallet, within its limits.json")
    c.add_argument("--amount", required=True, help="NORTH given to one address")
    c.add_argument("--listen", default="127.0.0.1:8660")
    c.add_argument("--bits", type=int, default=20, help="zero bits of work asked for when the faucet is quiet")
    c.add_argument("--trust-proxy", action="store_true", help="take the asker's address from X-Forwarded-For")
    c.add_argument("--per-source", type=int, default=2, help="payments one place (an IPv4 address or an IPv6 /48) may get in a day")
    c = sub.add_parser("build-rx", help="build the proof-of-work library that mining and the pool need")
    c.add_argument("--randomx", required=True, help="the folder src/randomx of the TrueNorth sources")
    c.add_argument("--cmake", help="the cmake program, if it is not on the path")
    c = sub.add_parser("mine", help="mine for a pool to an address; the wallet is not touched")
    c.add_argument("url", help="the pool")
    c.add_argument("--address", required=True, help="the address the pool writes the shares down for")
    c.add_argument("--threads", type=int, default=1)
    c.add_argument("--light", action="store_true", help="without the 2 GiB table: tens of hashes a second, for trying it out")
    c.add_argument("--shares", type=int, help="stop after this many shares were taken")
    c.add_argument("--seconds", type=int, help="stop after this many seconds")
    c = sub.add_parser("pool-serve", help="be a pool that pays for shares; --dir is its folder and its wallet")
    c.add_argument("--chain", required=True, choices=("main", "regtest"))
    c.add_argument("--rpc-url", required=True, help="the pool's own node, for example http://127.0.0.1:9554")
    c.add_argument("--rpc-cookie", required=True, help="the node's .cookie file")
    c.add_argument("--pay-to", help="the address the blocks pay (default: the first address of the wallet in --dir)")
    c.add_argument("--pay-every", type=int, default=0, help="seconds between payout rounds from the wallet in --dir; 0: no payouts")
    c.add_argument("--minimum", default="1", help="NORTH owed from which an address is paid")
    c.add_argument("--miners", help="a file with the only addresses that get work, one a line (for a pool not open to others yet)")
    c.add_argument("--own-mining", help="a file with the addresses the pool's owner mines to elsewhere, one a line; they count for the cap")
    c.add_argument("--cap", type=int, default=32, help="percent of a day's blocks at which no work is handed out (and 8 more of an hour's); 0: no cap")
    c.add_argument("--trust-proxy", action="store_true", help="take the asker's address from X-Forwarded-For")
    c.add_argument("--listen", default="127.0.0.1:8690")
    c.add_argument("--ratio", type=int, default=2048, help="shares in a block on average")
    c.add_argument("--fee", type=int, default=5, help="the pool's fee in percent")
    c.add_argument("--most-owed", default="5000", help="NORTH the pool may owe at once; above it no work is handed out")
    c.add_argument("--deep", type=int, default=6, help="blocks on top of a payout, by the pool's own node, before it counts as paid")
    c.add_argument("--surplus-to", help="the owner's address: what the wallet holds beyond what the pool owes and --keep goes there")
    c.add_argument("--keep", default="5000", help="NORTH the wallet keeps as its reserve beyond what it owes (with --surplus-to)")
    c.add_argument("--surplus-most", default="1000", help="NORTH sent to --surplus-to at a time, at most")
    c.add_argument("--surplus-every", type=int, default=21600, help="seconds between two payments to --surplus-to, at least")
    c.add_argument("--stratum", help="also take miners over stratum (IronMiner) on HOST:PORT (:3333 for every address), or "
                                     "'systemd' for the socket of a socket unit; the port faces the internet itself, not the web server")
    c.add_argument("--public-url", default=DEFAULT_POOL, help="where miners look up their account, said to them over stratum")
    a = p.parse_args(argv)
    folder = os.path.expanduser(a.dir)
    signer = getattr(a, "mcp_signer", None) or a.signer

    def show(result, status=0):
        out.write(json.dumps(result, indent=1) + "\n")
        return status

    try:
        if a.cmd == "signer-serve":
            from .signer import start as signer_start
            if signer:
                raise Refused("INPUT", "the signing service holds its own wallet in --dir; it is not run through another (--signer)")
            listening = signer_start(folder, a.socket, a.group)
            sys.stderr.write("signing service for the wallet in %s, at %s, for %s\n"
                             % (folder, listening.path, "the group %s" % a.group if a.group else "this user only"))
            return listening.run()
        if a.cmd == "signer-setup":
            from .signer import check, setup_script, this_user
            if a.check:
                done = check(a.socket or signer, a.service_dir and os.path.expanduser(a.service_dir),
                             a.venv and os.path.expanduser(a.venv), a.system)
                return show(done, 0 if done["ok"] else 2)
            out.write(setup_script(a.system, a.agent_user or this_user(), a.move_from, a.per_payment, a.per_day, a.package,
                                   a.wheel, a.admin_agent))
            return 0
        if signer and a.cmd in ("new", "forget", "postage-serve", "stamps-serve", "faucet-serve", "pool-serve"):
            raise Refused("SIGNER", "%s works on a wallet's own folder, not through a signing service: run it as the service's "
                                    "user on the service's folder (polarka-agent signer-setup shows where it is)" % a.cmd)
        if a.cmd == "new":
            server = a.server or (DEFAULT_SERVER if a.chain == "main" else None)
            if server is None:
                raise Refused("SERVER", "name the wallet server with --server")
            words = None
            if a.words_file:
                with open(os.path.expanduser(a.words_file)) as f:
                    words = f.read()
            made = Wallet.create(folder, a.chain, server, words, 12 if a.twelve else 24)
            first = Wallet(folder).account.address(0, 0)
            if words is not None:
                return show({"folder": folder, "first_address": first, "brought_back": True})
            return show({"folder": folder, "first_address": first, "words": made,
                         "note": "Write the words on paper now. They are the wallet. They are also in wallet.json, which only you may read; "
                                 "whoever reads that file can spend. The wallet pays nothing until you write limits.json."})
        if a.cmd == "start":
            if signer:
                from .signer import Signer
                return show(start(folder, a, wallet=Signer(signer, folder)))
            return show(start(folder, a))
        if a.cmd == "build-rx":
            from .rx import build
            return show({"built": build(a.randomx, a.cmake)})
        if a.cmd == "mine":
            from .miner import mine
            if not 1 <= a.threads <= 256:
                raise Refused("INPUT", "--threads is 1 to 256")
            return show(mine(a.url, a.address, a.threads, not a.light, a.shares, a.seconds,
                             say=lambda event: sys.stderr.write(json.dumps(event) + "\n")))
        if a.cmd == "pool-serve":
            from .keys import script_for_address
            from .payout import Payouts
            from .pool import Cap, Guard, Node, Pool, depth_by, serve as pool_serve
            if not 1 <= a.ratio <= 1 << 20 or not 0 <= a.fee <= 50 or not 0 <= a.cap <= 50 or a.pay_every < 0:
                raise Refused("INPUT", "--ratio is 1 to 1048576, --fee 0 to 50, --cap 0 to 50, --pay-every 0 or more")
            wallet = Wallet(folder) if a.pay_every or not a.pay_to else None
            if wallet is not None and wallet.chain != a.chain:
                raise Refused("CHAIN", "the wallet in --dir is of another chain")
            pay_to = a.pay_to or wallet.account.address(0, 0)
            node = Node(a.rpc_url, os.path.expanduser(a.rpc_cookie))
            own = []
            if a.own_mining:
                with open(os.path.expanduser(a.own_mining)) as f:
                    own = [script_for_address(a.chain, line.strip()) for line in f if line.strip()]
            cap = Cap(node, own, ((86400, a.cap, max(a.cap - 2, 0)), (3600, a.cap + 8, a.cap + 6))) if a.cap else None
            miners = None
            if a.miners:
                with open(os.path.expanduser(a.miners)) as f:
                    miners = [line.strip() for line in f if line.strip()]
                for m in miners:
                    script_for_address(a.chain, m)
            surplus = None
            if a.surplus_to:
                if not a.pay_every:
                    raise Refused("INPUT", "--surplus-to needs --pay-every: the surplus is sent from the payout rounds")
                if a.surplus_every < 0:
                    raise Refused("INPUT", "--surplus-every is 0 or more")
                script_for_address(a.chain, a.surplus_to)
                from .payout import Surplus
                if units(a.surplus_most) <= 0:
                    raise Refused("INPUT", "--surplus-most is more than 0")
                surplus = Surplus(a.surplus_to, units(a.keep), units(a.surplus_most), a.surplus_every)
            payouts = Payouts(wallet, folder, units(a.minimum), depth=depth_by(node), deep=a.deep, surplus=surplus) if a.pay_every else None
            welcome = None
            if a.stratum:
                welcome = "Polarka pool: every share is paid (PPS), fee %d %%%s. Your account: %s/v1/account?address={address}" % (
                    a.fee, ", paid out from %s NORTH" % a.minimum if a.pay_every else "", a.public_url.rstrip("/"))
            return pool_serve(Pool(node, a.chain, pay_to, folder, a.ratio, a.fee, cap=cap, guard=Guard(), payouts=payouts, miners=miners,
                                   most_owed=units(a.most_owed) if a.pay_every else None),
                              a.listen, a.trust_proxy, a.pay_every, a.stratum, welcome)
        if a.cmd == "stamp-verify":
            from .stamps import verify
            with open(os.path.expanduser(a.file)) as f:
                proof = json.load(f)
            rpc = None
            if a.rpc_url:
                from .walletd import Node as RpcNode
                rpc = RpcNode(a.rpc_url, os.path.expanduser(a.rpc_cookie or "~/.truenorth/.cookie"))
            try:
                done = verify(proof, rpc)
                if rpc is None:      # consistent, but the chain was not asked: not "valid"
                    return show(dict(done, valid=None, digest=proof.get("digest"), block_height=proof.get("block_height")), 3)
                return show(dict(done, valid=True, digest=proof.get("digest"), block_height=proof.get("block_height")))
            except ValueError as e:
                return show({"valid": False, "error": str(e)}, 2)
        if a.cmd == "x402-facilitator":
            from .walletd import Node as RpcNode, loopback
            from .x402 import NETWORKS, Facilitator, facilitator_server
            try:
                host, port = loopback(a.listen)
            except ValueError:
                raise Refused("INPUT", "--listen is a loopback address with a port, for example 127.0.0.1:9570")
            if not 0 <= a.confirmations <= 100 or a.redeliver < 0:
                raise Refused("INPUT", "--confirmations is 0 to 100, --redeliver 0 or more")
            if a.public and a.bazaar_origin:
                raise Refused("INPUT", "a facilitator for sellers from outside keeps no catalog: --public or --bazaar-origin, not both")
            if not 1 <= a.per_minute <= 600:
                raise Refused("INPUT", "--per-minute is 1 to 600")
            most_payments = a.most_payments if a.most_payments is not None else (20000 if a.public else None)
            if most_payments is not None and most_payments < 1:
                raise Refused("INPUT", "--most-payments is 1 or more")
            os.makedirs(folder, mode=0o700, exist_ok=True)
            catalog = None
            if a.bazaar_origin:
                from .bazaar import Catalog, origin
                if not all(o.startswith("https://") and origin(o) for o in a.bazaar_origin):
                    raise Refused("INPUT", "--bazaar-origin is an https:// origin, for example https://gate.polarka.org")
                catalog = Catalog(os.path.join(folder, "x402-bazaar.json"), a.bazaar_origin)
            facilitator = Facilitator(RpcNode(a.rpc_url, os.path.expanduser(a.rpc_cookie), timeout=30), a.chain, os.path.join(folder, "x402-used.jsonl"),
                                      a.confirmations, units(a.instant_below), a.redeliver, catalog=catalog, most_payments=most_payments,
                                      # one place brings at most a twentieth of what is kept, in a day (review 10, M2)
                                      new_per_place=max(1, most_payments // 20) if a.public and most_payments else None)
            if a.chain in NETWORKS and facilitator.network != NETWORKS[a.chain]:
                raise Refused("CHAIN", "the node runs another chain than --chain")
            server = facilitator_server(facilitator, (host, port), most=32 if a.public else 64, public=a.public, per_minute=a.per_minute)
            sys.stderr.write("x402 facilitator on %s for %s: %d confirmations, at once below %s NORTH%s%s%s\n"
                             % (a.listen, facilitator.network, a.confirmations, a.instant_below,
                                ", a bazaar catalog of %s" % " ".join(a.bazaar_origin) if catalog is not None else "",
                                "; for sellers from outside, %d requests a minute from one address" % a.per_minute if a.public else "",
                                ", at most %d payments kept" % most_payments if most_payments is not None else ""))
            server.serve_forever()
            return 0
        if signer:
            from .signer import Signer
            if a.cmd in ("mcp", "fetch"):
                os.makedirs(folder, mode=0o700, exist_ok=True)      # this program's own records: x402 payments under way
            wallet = Signer(signer, folder)
        else:
            if a.cmd == "mcp" and not os.path.exists(os.path.join(folder, "wallet.json")):
                # an agent that is given this server from a registry starts with no wallet: it gets one on main, which
                # pays nothing until its owner writes limits.json, as `start` does
                Wallet.create(folder, "main", DEFAULT_SERVER)
                sys.stderr.write(json.dumps({"event": "wallet", "made": True, "folder": folder,
                                             "note": "The recovery words are in wallet.json in that folder; whoever reads that "
                                                     "file can spend. The wallet pays nothing until its owner writes limits.json."}) + "\n")
            wallet = Wallet(folder)
            if a.cmd == "mcp":
                # said to the owner, in the agent host's log: the limits hold for the agent's tools, not for its shell
                sys.stderr.write(json.dumps({"event": "words_open", "folder": folder,
                                             "note": "The words of this wallet are in a file that this user can read. An agent that "
                                                     "can run commands as this user can read them and pay past every limit. A "
                                                     "signing service keeps them apart: see polarka-agent signer-setup --help."}) + "\n")
        if a.cmd == "mcp":
            from .mcp import serve
            return serve(wallet, allow_local=a.allow_local)
        if a.cmd == "address":
            return show({"address": wallet.fresh_address()})
        if a.cmd == "faucet":
            from .faucet import ask
            if not 1 <= a.max_bits <= 30:
                raise Refused("INPUT", "--max-bits is 1 to 30")
            return show(ask(a.url, wallet.fresh_address(), most_bits=a.max_bits))
        if a.cmd == "postage":
            from .postage import buy
            return show(buy(wallet, a.url, units(a.most)))
        if a.cmd == "fetch":
            from .x402 import fetch
            if a.wait < 0:
                raise Refused("INPUT", "--wait is 0 or more")
            got = fetch(a.url, wallet, units(a.most), wait=a.wait)
            body = got.pop("body")
            if a.out:
                with open(os.path.expanduser(a.out), "wb") as f:
                    f.write(body)
                got["saved"] = {"file": a.out, "bytes": len(body)}
            else:
                got["body"] = body[:4000].decode("utf-8", "replace")
            return show(got, 0 if got["status"] == 200 else 3 if got.get("pending") else 2)
        if a.cmd == "postage-serve":
            from .postage import serve as postage_serve
            if (a.price is None) == (a.price_hashes is None):
                raise Refused("INPUT", "give --price or --price-hashes, one of the two")
            facilitator = None
            if a.x402_facilitator:
                from .x402 import RemoteFacilitator
                facilitator = RemoteFacilitator(a.x402_facilitator)
            routes = []
            for r in a.route:
                prefix, eq, where = r.partition("=")
                if not eq or not prefix.startswith("/") or ":" not in where:
                    raise Refused("INPUT", "--route is PREFIX=HOST:PORT, for example /v1/stamp=127.0.0.1:8681")
                routes.append((prefix, where))
            offers = ()
            if a.offers:
                from .postage import load_offers
                offers = load_offers(a.offers)
            return postage_serve(wallet, a.upstream, units(a.price) if a.price is not None else 0, a.calls, a.listen, a.free,
                                 a.trust_proxy, a.price_hashes or 0, facilitator, routes, a.public_url, offers)
        if a.cmd == "stamps-serve":
            from .stamps import serve as stamps_serve
            from .walletd import Node as RpcNode
            if not 30 <= a.every <= 86400:
                raise Refused("INPUT", "--every is 30 to 86400 seconds")
            return stamps_serve(wallet, RpcNode(a.rpc_url, os.path.expanduser(a.rpc_cookie)), folder, a.listen, a.every)
        if a.cmd == "faucet-serve":
            from .faucet import serve as faucet_serve
            if not 10 <= a.bits <= 30:
                raise Refused("INPUT", "--bits is 10 to 30")
            if not 1 <= a.per_source <= 100:
                raise Refused("INPUT", "--per-source is 1 to 100")
            return faucet_serve(wallet, units(a.amount), a.listen, a.trust_proxy, a.bits, a.per_source)
        if a.cmd == "balance":
            return show(_amounts(wallet.balance()))
        if a.cmd == "history":
            return show(_amounts(wallet.history()))
        if a.cmd == "payments":
            return show({"payments": wallet.payments()})
        if a.cmd == "limits":
            return show(wallet.limits_shown())
        if a.cmd == "received":
            return show(_amounts(wallet.received(a.address)))
        if a.cmd == "wait":
            return show(wait_for(wallet, a.address, units(a.amount), a.confirmations, a.timeout))
        if a.cmd == "pay":
            return show(wallet.pay([(a.address, units(a.amount))], a.memo, a.dry_run))
        if a.cmd == "pay-many":
            with open(os.path.expanduser(a.file)) as f:
                wanted = json.load(f)
            if type(wanted) is not list or not all(type(w) is dict and set(w) == {"address", "amount"} for w in wanted):
                raise Refused("FILE", 'the file is a JSON list of {"address": ..., "amount": "1.5"}')
            return show(wallet.pay([(w["address"], units(w["amount"])) for w in wanted], a.memo, a.dry_run))
        if a.cmd == "stamp":
            if bool(a.file) == bool(a.sha256):
                raise Refused("DIGEST", "name a file or give --sha256, one of the two")
            if a.file:
                h = hashlib.sha256()
                with open(os.path.expanduser(a.file), "rb") as f:
                    for piece in iter(lambda: f.read(1 << 20), b""):
                        h.update(piece)
                digest = h.digest()
            else:
                try:
                    digest = bytes.fromhex(a.sha256)
                except ValueError:
                    raise Refused("DIGEST", "a SHA-256 is 64 hex characters")
            return show(dict(wallet.stamp(digest, a.memo, a.dry_run), sha256=digest.hex()))
        if a.cmd == "forget":
            wallet.forget(a.id)
            return show({"forgotten": a.id})
    except Refused as e:
        return show({"error": e.kind, "message": str(e)}, 2)
    except BadInput as e:
        return show({"error": "INPUT", "message": str(e)}, 2)
    except ValueError as e:     # a pool that does not start says why: a second process, a ledger that cannot be read
        return show({"error": "REFUSED", "message": str(e)}, 2)
    except (NoLibrary, PoolSaid) as e:
        return show({"error": "POOL" if isinstance(e, PoolSaid) else "LIBRARY", "message": str(e)}, 2)
    except ServerError as e:
        shown = {"error": "SERVER", "status": e.status, "kind": e.kind, "message": str(e)}
        if e.said:
            shown["server_said_untrusted"] = e.said      # for the owner at the command line; the MCP server never shows it
        return show(shown, 3)
    except OSError as e:
        return show({"error": "FILE", "message": "%s: %s" % (type(e).__name__, e.strerror or e)}, 2)
    except Exception as e:      # whatever else: one JSON object, never a traceback with what it may hold
        return show({"error": "INTERNAL", "message": type(e).__name__}, 4)
    return 1


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
