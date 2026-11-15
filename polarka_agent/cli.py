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
from .server import ServerError
from .wallet import Refused, Wallet, north, units

DEFAULT_SERVER = "https://api.northboundwallet.org"     # the public server of the Northbound project; not run by this project


def _amounts(result):
    """Base units to NORTH, for the fields that are amounts."""
    for name in ("spendable", "set_aside", "unripe", "amount"):
        if isinstance(result.get(name), int) and not isinstance(result.get(name), bool):
            result[name] = north(result[name])
    for line in result.get("lines", []):
        for name in ("received", "sent", "fee"):
            if isinstance(line.get(name), int):
                line[name] = north(line[name])
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
        wallet = Wallet(folder)
        if a.cmd == "mcp":
            from .mcp import serve
            return serve(wallet)
        if a.cmd == "address":
            return show({"address": wallet.fresh_address()})
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
    except ServerError as e:
        return show({"error": "SERVER", "status": e.status, "kind": e.kind, "message": str(e)}, 3)
    except OSError as e:
        return show({"error": "FILE", "message": "%s: %s" % (type(e).__name__, e.strerror or e)}, 2)
    return 1


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
