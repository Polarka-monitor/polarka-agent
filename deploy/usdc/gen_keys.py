#!/usr/bin/env python3
"""Make test-only keys for the USDC facilitator self-test: "gas" pays for settlement, "payer" signs payments.

Each key goes to its own file that only you may read. Only the addresses are printed. A key that is already
there is kept, so running this again changes nothing. These keys are for tests only: Base Sepolia or a local
fork of Base (run_fork_tests.sh), never the main network.
"""
import json
import os
import sys

from eth_account import Account


def main(folder):
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    out = {}
    for name in ("gas", "payer"):
        path = os.path.join(folder, name + ".key")
        if not os.path.exists(path):
            key = Account.create().key.hex()
            key = key if key.startswith("0x") else "0x" + key
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key + "\n")
        with open(path) as f:
            out[name] = Account.from_key(f.read().strip()).address
    with open(os.path.join(folder, "addresses.json"), "w") as f:
        json.dump(out, f, indent=1)
    with open(os.path.join(folder, "README.txt"), "w") as f:
        f.write("Test keys of the USDC facilitator's tests (Base Sepolia, a local fork of Base). Not for the main network.\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
