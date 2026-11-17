#!/usr/bin/env python3
"""A test of the tests: one deliberate fault at a time is put into a copy of the code, and the tests must fail.

    TN_BIN=/path/to/bin python3 -B tests/faults.py [number ...]

Each fault removes or weakens one rule the wallet's owner relies on. A fault the tests do not notice is a hole
in the tests and is printed as MISSED. Nothing in the real folder is touched; every run is in a temporary copy.
"""
import os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (file, text as it stands, the fault, what the fault means)
FAULTS = [
    ("polarka_agent/wallet.py", 'if out > limits["per_payment"]:', 'if False and out > limits["per_payment"]:',
     "the limit for one payment is not applied"),
    ("polarka_agent/wallet.py", 'if spent + out > limits["per_day"]:', 'if spent > limits["per_day"]:',
     "the day's limit does not count the payment being made"),
    ("polarka_agent/wallet.py", 'if spent + out > limits["per_day"]:', 'if False:',
     "the day's limit is not applied"),
    ("polarka_agent/wallet.py", 'if limits["allow"] is not None and any(', 'if False and any(',
     "the list of allowed addresses is not applied"),
    ("polarka_agent/wallet.py", 'p["state"] not in ("overtaken", "forgotten")', 'p["state"] in ("confirmed",)',
     "payments still waiting are not counted into the day"),
    ("polarka_agent/wallet.py", 'not tx.MIN_FEE_RATE <= limits["fee_rate"] <= tx.MAX_FEE_RATE', 'not tx.MIN_FEE_RATE <= limits["fee_rate"]',
     "a fee rate without an upper bound is taken from limits.json"),
    ("polarka_agent/wallet.py", 'len(memo) > 200', 'len(memo) > 200000',
     "a memo of any length is taken"),
]


def run(number, fault):
    path, old, new, meaning = fault
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "w")
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        target = os.path.join(work, path)
        text = open(target).read()
        if text.count(old) != 1:
            return "STALE (the text is in the code %d times, the fault list needs care)" % text.count(old)
        open(target, "w").write(text.replace(old, new))
        r = subprocess.run([sys.executable, "-B", "-W", "ignore", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                           cwd=work, capture_output=True, text=True, timeout=600)
        tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""
        if "skipped" in tail and "FAILED" not in tail:
            return "MISSED or not run (%s): tests that need a node were skipped, set TN_BIN" % tail
        return "caught (%s)" % tail if r.returncode != 0 else "MISSED (%s)" % tail


def main():
    chosen = [int(a) for a in sys.argv[1:]] or list(range(1, len(FAULTS) + 1))
    missed = 0
    for n in chosen:
        result = run(n, FAULTS[n - 1])
        missed += not result.startswith("caught")
        print("%d. %s: %s" % (n, FAULTS[n - 1][3], result), flush=True)
    print("faults: %d, not caught: %d" % (len(chosen), missed))
    sys.exit(1 if missed else 0)


if __name__ == "__main__":
    main()
