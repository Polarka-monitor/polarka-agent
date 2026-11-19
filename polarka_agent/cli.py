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
    c = sub.add_parser("postage-serve", help="be a gate before a web service that lets through who paid postage to this wallet")
    c.add_argument("--upstream", required=True, help="the service behind the gate, host:port")
    c.add_argument("--price", help="NORTH for one lot of calls")
    c.add_argument("--price-hashes", type=int, help="in place of --price: mining work for one lot of calls, in hashes; "
                   "turned into NORTH by what the network needs for a block now")
    c.add_argument("--calls", type=int, default=100, help="calls in one lot")
    c.add_argument("--free", type=int, default=0, help="calls a day that one source may make without postage")
    c.add_argument("--listen", default="127.0.0.1:8670")
    c.add_argument("--trust-proxy", action="store_true", help="take the caller's address from X-Forwarded-For")
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
    c = sub.add_parser("pool-serve", help="be a pool that pays for shares (regtest only so far); --dir is its folder and its wallet")
    c.add_argument("--chain", required=True, choices=("main", "regtest"))
    c.add_argument("--rpc-url", required=True, help="the pool's own node, for example http://127.0.0.1:9554")
    c.add_argument("--rpc-cookie", required=True, help="the node's .cookie file")
    c.add_argument("--pay-to", help="the address the blocks pay (default: the first address of the wallet in --dir)")
    c.add_argument("--pay-every", type=int, default=0, help="seconds between payout rounds from the wallet in --dir; 0: no payouts")
    c.add_argument("--minimum", default="1", help="NORTH owed from which an address is paid")
    c.add_argument("--own-mining", help="a file with the addresses the pool's owner mines to elsewhere, one a line; they count for the cap")
    c.add_argument("--cap", type=int, default=32, help="percent of a day's blocks at which no work is handed out (and 8 more of an hour's); 0: no cap")
    c.add_argument("--trust-proxy", action="store_true", help="take the asker's address from X-Forwarded-For")
    c.add_argument("--listen", default="127.0.0.1:8690")
    c.add_argument("--ratio", type=int, default=2048, help="shares in a block on average")
    c.add_argument("--fee", type=int, default=5, help="the pool's fee in percent")
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
            from .pool import Cap, Guard, Node, Pool, serve as pool_serve
            if a.chain != "regtest":
                raise Refused("STAGE", "the pool has run on regtest only and nobody else has read it; it does not start on another chain")
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
            payouts = Payouts(wallet, folder, units(a.minimum)) if a.pay_every else None
            return pool_serve(Pool(node, a.chain, pay_to, folder, a.ratio, a.fee, cap=cap, guard=Guard(), payouts=payouts),
                              a.listen, a.trust_proxy, a.pay_every)
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
        if a.cmd == "postage-serve":
            from .postage import serve as postage_serve
            if (a.price is None) == (a.price_hashes is None):
                raise Refused("INPUT", "give --price or --price-hashes, one of the two")
            return postage_serve(wallet, a.upstream, units(a.price) if a.price is not None else 0, a.calls, a.listen, a.free,
                                 a.trust_proxy, a.price_hashes or 0)
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
