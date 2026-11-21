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


def start(folder, a, say=lambda event: sys.stderr.write(json.dumps(event) + "\n")):
    """The `start` command: what an agent needs to begin, each step said as it is done. Mining goes on until
    --seconds or for ever; the summary comes when it ends."""
    if not 1 <= a.threads <= 256:
        raise Refused("INPUT", "--threads is 1 to 256")
    made = not os.path.exists(os.path.join(folder, "wallet.json"))
    if made:
        Wallet.create(folder, "main", DEFAULT_SERVER)
    wallet = Wallet(folder)
    if wallet.chain != "main":
        raise Refused("CHAIN", "the wallet in --dir is not of the main chain")
    address = wallet.account.address(0, 0)      # one address the agent keeps, rather than a new one each time
    done = {"folder": folder, "address": address, "wallet_made": made}
    say({"event": "wallet", "made": made, "folder": folder, "address": address,
         "note": "The recovery words are in wallet.json in that folder; whoever reads that file can spend. Keep a copy "
                 "somewhere else. The wallet pays nothing until its owner writes limits.json."})
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
    sub.add_parser("mcp", help="serve the wallet to an agent over the Model Context Protocol (standard input and output)")
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
    a = p.parse_args(argv)
    folder = os.path.expanduser(a.dir)

    def show(result, status=0):
        out.write(json.dumps(result, indent=1) + "\n")
        return status

    try:
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
            return pool_serve(Pool(node, a.chain, pay_to, folder, a.ratio, a.fee, cap=cap, guard=Guard(), payouts=payouts, miners=miners,
                                   most_owed=units(a.most_owed) if a.pay_every else None),
                              a.listen, a.trust_proxy, a.pay_every)
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
            os.makedirs(folder, mode=0o700, exist_ok=True)
            facilitator = Facilitator(RpcNode(a.rpc_url, os.path.expanduser(a.rpc_cookie), timeout=30), a.chain, os.path.join(folder, "x402-used.jsonl"),
                                      a.confirmations, units(a.instant_below), a.redeliver)
            if a.chain in NETWORKS and facilitator.network != NETWORKS[a.chain]:
                raise Refused("CHAIN", "the node runs another chain than --chain")
            server = facilitator_server(facilitator, (host, port))
            sys.stderr.write("x402 facilitator on %s for %s: %d confirmations, at once below %s NORTH\n"
                             % (a.listen, facilitator.network, a.confirmations, a.instant_below))
            server.serve_forever()
            return 0
        if a.cmd == "mcp" and not os.path.exists(os.path.join(folder, "wallet.json")):
            # an agent that is given this server from a registry starts with no wallet: it gets one on main, which
            # pays nothing until its owner writes limits.json, as `start` does
            Wallet.create(folder, "main", DEFAULT_SERVER)
            sys.stderr.write(json.dumps({"event": "wallet", "made": True, "folder": folder,
                                         "note": "The recovery words are in wallet.json in that folder; whoever reads that "
                                                 "file can spend. The wallet pays nothing until its owner writes limits.json."}) + "\n")
        wallet = Wallet(folder)
        if a.cmd == "mcp":
            from .mcp import serve
            return serve(wallet)
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
            return postage_serve(wallet, a.upstream, units(a.price) if a.price is not None else 0, a.calls, a.listen, a.free,
                                 a.trust_proxy, a.price_hashes or 0, facilitator, routes)
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
