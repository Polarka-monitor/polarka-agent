"""Review 10 (22 November 2026): what it found, as tests, each of which failed on 665d8e5. Several are the reviewer's
own, taken over. They run as any user and need no node."""
import collections
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from polarka_agent import keys, mcp, signer, x402
from polarka_agent.httpguard import Limiter
from polarka_agent.signer import Listening, Service, Signer
from polarka_agent.wallet import Refused, Wallet

from .test_wallet_rules import Model

N = keys.UNIT
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class H1AHeldPaymentCountsForTheDayItGoesOut(unittest.TestCase):
    """A payment signed with hand=False, kept by the agent for a day and then handed to the network by the agent itself,
    counts for the day on which it goes out, as F6 has it for a payment the wallet shows again."""

    def setUp(self):
        self.m, self.now = Model(), [1_800_000_000]
        self.addCleanup(self.m.fake.stop)
        folder = os.path.join(tempfile.mkdtemp(), "w")
        Wallet.create(folder, "regtest", self.m.fake.url, "abandon " * 11 + "about")
        with open(os.path.join(folder, "limits.json"), "w") as f:
            json.dump({"per_payment_north": "5", "per_day_north": "5"}, f)
        self.w = Wallet(folder, clock=lambda: self.now[0])
        self.m.coin(self.w.account.address(0, 0), 10 * N, "aa" * 32)
        self.m.coin(self.w.account.address(0, 1), 10 * N, "bb" * 32)
        self.s = Service(self.w, log=lambda line: None)
        self.to = keys.Account.from_words(keys.new_words(12), "regtest").address(0, 0)
        self.other = keys.Account.from_words(keys.new_words(12), "regtest").address(0, 0)

    def ask(self, op, **args):
        return self.s.answer(json.dumps({"v": 1, "op": op, "args": args}).encode())

    def test_the_day_limit_holds_against_a_payment_the_agent_held(self):
        a = self.ask("pay", outputs=[[self.to, 49 * N // 10]], hand=False)["ok"]
        self.assertEqual(self.ask("pay", outputs=[[self.other, 49 * N // 10]])["refused"][0], "OVER_DAY")
        self.now[0] += 86400 + 1
        self.m.broadcast({"transaction_hex": a["raw"]})          # the agent hands it over itself, a day later
        b = self.ask("pay", outputs=[[self.other, 49 * N // 10]])
        self.assertEqual(b.get("refused", [None])[0], "OVER_DAY", "9.8 NORTH left within seconds against a limit of 5 a day")
        shown = {p["id"]: p for p in self.ask("payments")["ok"]}
        self.assertEqual((shown[a["id"]]["state"], shown[a["id"]]["made"]), ("handed", self.now[0]))   # counted for today
        self.now[0] += 86400 + 1                                  # and on the next day the limit is whole again
        self.assertEqual(self.ask("pay", outputs=[[self.other, 49 * N // 10]])["ok"]["state"], "handed")

    def test_a_payment_the_wallet_handed_itself_counts_once(self):
        a = self.ask("pay", outputs=[[self.to, 2 * N]])["ok"]
        self.assertEqual(a["state"], "handed")
        self.now[0] += 86400 + 1
        self.assertEqual(self.ask("pay", outputs=[[self.to, 2 * N]])["ok"]["state"], "handed")   # it went out on day 0


class H2ASettleAsksTheNodeABoundedNumberOfTimes(unittest.TestCase):
    """The reviewer's test: a payment whose transaction its payer replaced, shown again and again, costs a few calls of
    the node each time, not two scans of 288 blocks."""

    def test_a_replaced_payment_shown_again(self):
        calls, now = [0], time.time()
        pay_to = keys.Account.from_words(keys.new_words(12), "main").address(0, 0)

        class Node:
            replaced = False

            def call(self, method, *p):
                calls[0] += 1
                if method == "getblockhash":
                    return ("470c8b8ae824c06575cfae0851282c49" + "00" * 16) if p[0] == 0 else "%064x" % p[0]
                if method == "decoderawtransaction":
                    return {"txid": "cd" * 32, "vout": [{"n": 0, "value": "0.01", "scriptPubKey": {"address": pay_to}}]}
                if method == "testmempoolaccept":
                    return [{"allowed": True}]
                if method == "sendrawtransaction":
                    if self.replaced:
                        raise RuntimeError("insufficient fee, rejecting replacement")
                    return "cd" * 32
                if method == "gettxout":
                    return None
                if method == "getmempoolentry":
                    raise RuntimeError("Transaction not in mempool")
                if method == "getblockcount":
                    return 500_000
                if method == "getblock":
                    return {"tx": [], "time": int(now - (500_000 - int(p[0], 16)) * 300)}
        node, clock = Node(), [now - 30 * 3600]
        f = x402.Facilitator(node, "main", os.path.join(tempfile.mkdtemp(), "used.jsonl"), confirmations=1, clock=lambda: clock[0])
        wanted = x402.requirements(f.network, 1_000_000, pay_to, confirmations=1)
        payment = {"x402Version": 2, "accepted": wanted, "payload": {"transaction": "00" * 100, "claim": "ab" * 16}}
        f.settle(payment, wanted)
        node.replaced, clock[0], calls[0] = True, now, 0
        for _ in range(3):
            self.assertEqual(f.settle(payment, wanted)["errorReason"], "payment_not_found")
        self.assertLess(calls[0], 50, "three /settle asked the node %d times" % calls[0])


class ChainOfBlocks:
    """A node with a chain of blocks that a test may grow, cut back and replace."""

    def __init__(self, n):
        self.blocks = [("%064x" % (h + 1), []) for h in range(n)]       # height -> (hash, txids)
        self.calls = collections.Counter()

    def call(self, method, *p):
        self.calls[method] += 1
        if method == "getblockcount":
            return len(self.blocks) - 1
        if method == "getblockhash":
            return self.blocks[p[0]][0]
        if method == "getblock":
            return {"tx": next(t for h, t in self.blocks if h == p[0])}
        raise RuntimeError(method)


class H2BlocksAreReadOnce(unittest.TestCase):
    def test_a_payment_in_a_block_is_found_with_each_block_read_once(self):
        node = ChainOfBlocks(400)
        blocks = x402.Blocks(node, most=288)
        self.assertIsNone(blocks.depth("", lambda: set()))
        self.assertEqual(node.calls["getblock"], 288)               # the window, read once
        node.blocks.append(("%064x" % 1000, ["cd" * 32]))           # the payment goes into a block
        node.blocks.append(("%064x" % 1001, []))
        self.assertEqual(blocks.depth("cd" * 32, lambda: {"cd" * 32}), 2)
        self.assertEqual(node.calls["getblock"], 290)               # two new blocks, nothing read again
        for _ in range(5):
            self.assertIsNone(blocks.depth("ef" * 32, lambda: {"cd" * 32, "ef" * 32}))
        self.assertEqual(node.calls["getblock"], 290)
        # another branch: the block with the payment is gone, and a new one is read in its place
        node.blocks[-2:] = [("%064x" % 2000, []), ("%064x" % 2001, []), ("%064x" % 2002, [])]
        self.assertIsNone(blocks.depth("cd" * 32, lambda: {"cd" * 32}))
        node.blocks.append(("%064x" % 2003, ["cd" * 32]))
        self.assertEqual(blocks.depth("cd" * 32, lambda: {"cd" * 32}), 1)


class M2OneSenderDoesNotHoldEverybody(unittest.TestCase):
    def test_a_newcomer_is_let_in_while_one_48_fills_the_table(self):
        t = [0.0]
        lim = Limiter(60, clock=lambda: t[0])
        for i in range(10000):
            address = "2001:db8:1:%x::1" % i
            lim.take(x402.caller(address), x402.wide(address))
            t[0] += 0.005
        self.assertLessEqual(len(lim.buckets), 10000)
        self.assertTrue(lim.take(x402.caller("198.51.100.1"), x402.wide("198.51.100.1")), "a seller from elsewhere is refused")

    def test_a_place_brings_so_many_new_payments_a_day(self):
        pay_to = keys.Account.from_words(keys.new_words(12), "main").address(0, 0)
        txids = iter("%064x" % i for i in range(1, 100))

        class Node:
            def call(self, method, *p):
                if method == "getblockhash":
                    return "470c8b8ae824c06575cfae0851282c49" + "00" * 16
                if method == "decoderawtransaction":
                    return {"txid": next(txids), "vout": [{"n": 0, "value": "0.01", "scriptPubKey": {"address": pay_to}}]}
                if method == "testmempoolaccept":
                    return [{"allowed": True}]
                if method == "sendrawtransaction":
                    return "ok"
                if method == "gettxout":
                    return {"confirmations": 1}
                raise RuntimeError(method)
        now = [1_800_000_000.0]
        f = x402.Facilitator(Node(), "main", os.path.join(tempfile.mkdtemp(), "used.jsonl"), confirmations=1,
                             clock=lambda: now[0], new_per_place=2)
        wanted = x402.requirements(f.network, 1_000_000, pay_to, confirmations=1)

        def pay(who):
            payment = {"x402Version": 2, "accepted": wanted, "payload": {"transaction": "00" * 100, "claim": "ab" * 16}}
            return f.settle(payment, wanted, who)
        self.assertEqual([pay("2001:db8:1::/48")["success"] for _ in range(3)], [True, True, False])
        self.assertTrue(pay("2001:db8:2::/48")["success"])          # another place
        self.assertTrue(pay(None)["success"])                       # the gate's own: no place, no bound
        now[0] += 86400
        self.assertTrue(pay("2001:db8:1::/48")["success"])          # the next day


class M3WhatTheServiceRunsIsRootsAlone(unittest.TestCase):
    def test_pip_options_and_paths_are_not_a_package(self):
        for package in ("-e/srv/agent/src", "-r/srv/agent/req.txt", "--index-url=https://example.org/simple", "/srv/agent/x.whl",
                        "polarka-agent>=0.2.3", "polarka-agent==0.2.3 other", "other==1.0"):
            with self.assertRaises(Refused, msg=package):
                signer.setup_script("linux", "agent_1", package=package)
        self.assertIn("PACKAGE=polarka-agent==0.2.3\n", signer.setup_script("linux", "agent_1", package="polarka-agent==0.2.3"))

    def test_a_wheel_is_installed_only_with_its_sha256(self):
        wheel = os.path.join(tempfile.mkdtemp(), "polarka_agent-0.2.3-py3-none-any.whl")
        with open(wheel, "wb") as f:
            f.write(b"a wheel")
        import hashlib
        for system, tool in (("linux", "sha256sum"), ("macos", "shasum -a 256")):
            text = signer.setup_script(system, "agent_1", wheel=wheel)
            self.assertIn("WHEEL_SHA256=%s\n" % hashlib.sha256(b"a wheel").hexdigest(), text)
            self.assertIn("WHEEL=%s\n" % wheel, text)
            self.assertIn('[ "$(%s "$COPY" | cut -d \' \' -f 1)" != "$WHEEL_SHA256" ]' % tool, text)
            self.assertIn('pip_in install --quiet --no-deps --force-reinstall "$COPY"', text)
        with self.assertRaises(Refused):
            signer.setup_script("linux", "agent_1", wheel=wheel, package="polarka-agent==0.2.3")

    def test_a_check_looks_where_the_program_takes_its_modules_from(self):
        venv = os.path.join(tempfile.mkdtemp(), "venv")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], check=True)
        elsewhere = tempfile.mkdtemp(prefix="elsewhere-")
        os.chmod(elsewhere, 0o777)                                  # anybody may change it, whoever runs this
        os.symlink(os.path.join(ROOT, "polarka_agent"), os.path.join(elsewhere, "polarka_agent"))
        site = subprocess.run([os.path.join(venv, "bin", "python3"), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                              stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.strip()
        with open(os.path.join(site, "editable.pth"), "w") as f:
            f.write(elsewhere + "\n")                               # what pip install -e leaves behind
        self.assertIn(elsewhere, signer._program(venv))


class M1AMoveFollowsNoLink(unittest.TestCase):
    def script(self, old, new, base):
        part = signer.MOVED_WALLET
        for name, value in (("@FROM@", old), ("@USER@", "nobody"), ("@AS_AGENT@", "")):
            part = part.replace(name, value)
        # chown needs root, and clean only empties the environment: stood in for
        return 'chown() { :; }\nclean() { "$@"; }\nAGENT_USER=me\nBASE=%s\nDIR=%s\n%s' % (base, new, part)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old, self.new, self.base = os.path.join(self.tmp, "agent"), os.path.join(self.tmp, "service"), os.path.join(self.tmp, "base")
        Wallet.create(self.old, "regtest", "http://127.0.0.1:9")
        os.mkdir(self.new)
        os.makedirs(os.path.join(self.base, "venv", "bin"))
        os.symlink(sys.executable, os.path.join(self.base, "venv", "bin", "python3"))

    def test_a_link_left_by_the_agent_is_not_followed(self):
        canary = os.path.join(self.tmp, "canary")
        with open(canary, "w") as f:
            f.write("x")
        os.chmod(canary, 0o644)
        os.symlink(canary, os.path.join(self.old, "journal.jsonl"))
        p = subprocess.run(["/bin/sh", "-e"], input=self.script(self.old, self.new, self.base), universal_newlines=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not a plain file", p.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(canary).st_mode), 0o644, "the script changed the mode of the file the link names")
        self.assertEqual(os.listdir(self.new), [])
        self.assertTrue(os.path.exists(os.path.join(self.old, "wallet.json")))

    def test_plain_files_are_moved(self):
        with open(os.path.join(self.old, "wallet.json")) as f:
            before = f.read()
        p = subprocess.run(["/bin/sh", "-e"], input=self.script(self.old, self.new, self.base), universal_newlines=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Its wallet server, as written in it: "http://127.0.0.1:9"', p.stdout)
        with open(os.path.join(self.new, "wallet.json")) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.new, "wallet.json")).st_mode), 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.old, "wallet.json")))


class M4H3TheAgentAndTheEnvironment(unittest.TestCase):
    def test_an_agent_that_can_administer_is_refused_unless_said(self):
        with mock.patch.object(signer, "groups_of", return_value={"staff", "sudo"}):
            with self.assertRaises(Refused) as e:
                signer.setup_script("linux", "agent_1")
            self.assertEqual(e.exception.kind, "ADMIN_AGENT")
            text = signer.setup_script("linux", "agent_1", admin_agent=True)
        self.assertIn("# With --admin-agent: agent_1 may administer this machine.", text)
        for said, code in (("no", 1), ("yes", 0)):
            snippet = signer.ADMIN_CHECK.replace("@ADMIN_GROUPS@", " ".join(signer.ADMIN_GROUPS)).replace("@ADMIN_AGENT@", said)
            fake = 'id() { if [ "$1" = -Gn ]; then echo "staff sudo"; fi; return 0; }\nAGENT_USER=agent_1\n'
            p = subprocess.run(["/bin/sh", "-e"], input=fake + snippet, universal_newlines=True, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, code, p.stderr)

    def test_the_scripts_start_clean_and_python_runs_isolated(self):
        for system in ("linux", "macos"):
            text = signer.setup_script(system, "agent_1", package="polarka-agent==0.2.3")
            self.assertNotRegex(text, r"@[A-Z0-9_]+@")              # every piece filled in
            for line in text.splitlines():
                if not line.lstrip().startswith(("#", "echo")):
                    for option in re.findall(r'python3"? (-\S*)', line):
                        self.assertEqual(option, "-I", "%s: python without -I: %s" % (system, line))
            self.assertIn("pip --isolated", text)
            self.assertIn("clean() { /usr/bin/env -i HOME=%s " % ("/root" if system == "linux" else "/var/root"), text)
            # the commands from the system's own folders before the first of them, the id of the one who runs it
            self.assertLess(text.index("\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\n"), text.index('"$(id -u)"'))
            for line in text.splitlines():                  # every Python and pip of the script runs through clean
                if "python3" in line and not line.lstrip().startswith(("#", "echo", "ExecStart=", "<string>")):
                    self.assertIn("clean ", line, "%s: %s" % (system, line))
        script = os.path.join(tempfile.mkdtemp(), "clean.sh")
        with open(script, "w") as f:
            f.write("set -eu\n" + signer.CLEAN.replace("@ROOT_HOME@", "/nowhere") + 'echo "PATH=$PATH"\n'
                    "clean /bin/sh -c 'echo \"HOME=$HOME PYTHONPATH=${PYTHONPATH:-} PIP=${PIP_INDEX_URL:-}\"'\n")
        env = dict(os.environ, PYTHONPATH="/agent", PIP_INDEX_URL="https://agent.example/simple", HOME="/home/agent",
                   PATH="/home/agent/bin:" + os.environ.get("PATH", ""))
        p = subprocess.run(["/bin/sh", script], env=env, stdout=subprocess.PIPE, universal_newlines=True, check=True)
        self.assertEqual(p.stdout.split(), ["PATH=/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/nowhere", "PYTHONPATH=", "PIP="])


class LowsOfReview10(unittest.TestCase):
    def test_placeholders_in_a_folder_name_stay_text(self):
        tmp = tempfile.mkdtemp()
        for name in ("@UNIT@", "@LIMITS@", "it's @PACKAGE@"):
            moved = os.path.join(tmp, "a b", name)
            Wallet.create(moved, "regtest", "http://127.0.0.1:9")
            text = signer.setup_script("linux", "agent_1", move_from=moved, package="polarka-agent==0.2.3")
            p = subprocess.run(["/bin/sh", "-n"], input=text, universal_newlines=True, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(text.count("[Install]"), 1)

    def test_a_check_that_could_not_look_is_not_ok(self):
        # a service that answers and is another user's, and a check told folders that are not there (--venv and
        # --service-dir given wrong): before review 10 the points it could not look at counted as in order (L1)
        path = os.path.join(tempfile.mkdtemp(prefix="ps-"), "s.sock")
        held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        held.bind(path)
        self.addCleanup(held.close)
        os.chown(path, -1, os.getegid())                # on macOS a new file takes its folder's group
        os.chmod(path, 0o660)
        facts = {"version": "0", "chain": "regtest", "folder_private": True, "limits_file": None}
        with mock.patch.object(signer.os, "geteuid", return_value=os.geteuid() + 1), \
                mock.patch.object(signer.Signer, "info", return_value=facts), \
                mock.patch.object(signer, "groups_of", return_value=set(), create=True), \
                mock.patch.object(signer.subprocess, "run",
                                  return_value=mock.Mock(returncode=1, stdout="Sorry, user a may not run sudo on h.\n")):
            done = signer.check(path, "/nonexistent/dir", "/nonexistent/venv")
        self.assertEqual([c["ok"] for c in done["checks"] if c["check"] in ("words", "program")], [None, None])
        self.assertFalse(done["ok"])

    def test_mcp_tools_do_not_reach_this_machine_by_another_spelling(self):
        for url in ("https://127.1/", "https://0x7f000001/", "https://2130706433/"):
            with self.assertRaises(Refused, msg=url):
                mcp.outside(url)
        private = lambda host, port, **k: [(2, 1, 6, "", ("10.1.2.3", port))]       # a name of the home network
        with self.assertRaises(Refused):
            mcp.outside("https://intranet.example/", private)

    def test_one_user_does_not_hold_every_place_at_the_service(self):
        tmp = tempfile.mkdtemp()
        folder = os.path.join(tmp, "w")
        Wallet.create(folder, "regtest", "http://127.0.0.1:9")
        path = os.path.join(tempfile.mkdtemp(prefix="ps-"), "s.sock")
        listening = Listening(Service(Wallet(folder), log=lambda line: None), signer.bind(path), path, most=8, most_of_one=1)
        threading.Thread(target=listening.serve_forever, daemon=True).start()
        self.addCleanup(listening.close)
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        holder.connect(path)                                        # says nothing, and holds the one place of its user
        time.sleep(0.3)
        with self.assertRaises(Refused) as e:
            Signer(path, tmp).limits_shown()
        self.assertEqual(e.exception.kind, "SIGNER_BUSY")
        holder.close()
        time.sleep(0.5)
        self.assertFalse(Signer(path, tmp).limits_shown()["pays"])

    def test_the_book_is_the_owners_alone_from_its_first_byte(self):
        class W:
            folder = tempfile.mkdtemp()
        modes, dump = [], json.dump

        def watched(book, f, **k):          # the file's mode at the moment the claims and payments go into it (L5)
            modes.append(stat.S_IMODE(os.fstat(f.fileno()).st_mode))
            return dump(book, f, **k)
        old = os.umask(0)
        try:
            with mock.patch.object(x402.json, "dump", watched):
                x402._keep_book(W, {"pending": {}, "lots": {}})
        finally:
            os.umask(old)
        self.assertEqual(modes, [0o600])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(W.folder, x402.PENDING)).st_mode), 0o600)

    def test_the_default_agent_user_is_who_runs_it(self):
        with mock.patch.dict(os.environ, {"USER": "somebody", "LOGNAME": "somebody"}):
            import pwd
            self.assertEqual(signer.this_user(), pwd.getpwuid(os.getuid()).pw_name)



HOST = "macos" if sys.platform == "darwin" else "linux"


class SecondLookOfReview10(unittest.TestCase):
    """What the reviewer of review 10 found in the fixes (N1 to N8)."""

    def test_a_piped_script_runs_no_file_named_sh(self):
        # N1: `... | sudo sh` gives the script the name "sh"; it read itself again from there, a file in the folder
        tmp = tempfile.mkdtemp()
        marker = os.path.join(tmp, "ran")
        with open(os.path.join(tmp, "sh"), "w") as f:
            f.write("echo ran > %s\n" % marker)
        for system in ("linux", "macos"):
            text = signer.setup_script(system, "agent_1", package="polarka-agent==0.2.3")
            head = text.split("\nAGENT_USER=", 1)[0] + "\nexit 0\n"
            subprocess.run(["sh"], executable="/bin/sh", input="id() { echo 0; }\n" + head, cwd=tmp, universal_newlines=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            self.assertFalse(os.path.exists(marker), system)

    def test_a_wheel_is_read_as_a_plain_file_into_a_copy_only_root_can_read(self):
        # N2: the copy followed a link and kept the mode of what it read
        tmp = tempfile.mkdtemp()
        wheel = os.path.join(tmp, "polarka_agent-0.2.3-py3-none-any.whl")
        with open(wheel, "wb") as f:
            f.write(b"not much of a wheel")
        text = signer.setup_script(HOST, "agent_1", wheel=wheel)
        self.assertIn("(umask 077; head -c 67108865", text)
        block = text.split("# the wheel named", 1)[1].split("pip_in install --quiet --upgrade pip", 1)[0]
        base = os.path.join(tmp, "base")

        def run():
            return subprocess.run(["/bin/sh", "-c", 'set -eu\ninstall() { for d; do :; done; mkdir -p "$d"; }\nBASE=%s\n#%s'
                                   % (base, block)], universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p = run()
        self.assertEqual(p.returncode, 0, p.stderr)
        copy = os.path.join(base, "dist", os.path.basename(wheel))
        self.assertEqual(stat.S_IMODE(os.stat(copy).st_mode), 0o644)
        secret = os.path.join(tmp, "secret")
        with open(secret, "w") as f:
            f.write("only root's")
        os.unlink(wheel)
        os.symlink(secret, wheel)
        p = run()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("is not a plain file", p.stderr)
        self.assertFalse(os.path.exists(copy))

    def test_a_move_takes_at_most_64_mib_of_a_file_and_names_the_server_plainly(self):
        # N3: a sparse file of the agent's became real blocks written by root; N4: the server named in the wallet
        # reached the owner's terminal as it was, escape sequences and all
        tmp = tempfile.mkdtemp()
        old, base = os.path.join(tmp, "agent"), os.path.join(tmp, "base")
        Wallet.create(old, "regtest", "http://127.0.0.1:9")
        with open(os.path.join(old, "wallet.json")) as f:
            wallet = json.load(f)
        wallet["server"] = "\x1b[2K\x1b]0;owned\x07 all is well"
        with open(os.path.join(old, "wallet.json"), "w") as f:
            json.dump(wallet, f)
        with open(os.path.join(old, "journal.jsonl"), "w") as f:
            f.truncate((64 << 20) + 1)
        text = signer.setup_script(HOST, "agent_1", move_from=old, package="polarka-agent==0.2.3")
        block = text.split("# 4. the wallet moved here", 1)[1].split("\n# 5. ", 1)[0]
        os.makedirs(os.path.join(base, "venv", "bin"))
        os.symlink(sys.executable, os.path.join(base, "venv", "bin", "python3"))
        folder = os.path.join(base, "wallet")
        os.makedirs(folder)
        stubs = 'runuser() { shift 3; "$@"; }\nsudo() { shift 2; "$@"; }\nchown() { :; }\nclean() { "$@"; }\n'

        def run():
            return subprocess.run(["/bin/sh", "-c", "set -eu\n%sAGENT_USER=agent_1\nBASE=%s\nDIR=%s\n#%s" % (stubs, base, folder, block)],
                                  universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p = run()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("larger than 64 MiB; nothing is moved", p.stderr)
        self.assertEqual(os.listdir(folder), [])
        self.assertTrue(os.path.exists(os.path.join(old, "wallet.json")))
        with open(os.path.join(old, "journal.jsonl"), "w") as f:
            f.truncate(10)
        p = run()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(os.listdir(folder)), ["journal.jsonl", "wallet.json"])
        self.assertNotIn("\x1b", p.stdout)
        self.assertIn("\\u001b[2K", p.stdout)

    def test_an_agent_that_sudo_lets_run_commands_is_refused_unless_said(self):
        # N5: a sudoers line of the agent's own was not seen
        self.assertTrue({"incus-admin", "shadow"} <= set(signer.ADMIN_GROUPS))
        block = signer.ADMIN_CHECK.replace("@ADMIN_GROUPS@", " ".join(signer.ADMIN_GROUPS))

        def run(said, admin_agent="no"):
            stubs = 'id() { [ "$1" = -Gn ] && echo agent_1; return 0; }\nsudo() { echo "%s"; }\n' % said
            return subprocess.run(["/bin/sh", "-c", "set -eu\n" + stubs + "AGENT_USER=agent_1\n"
                                   + block.replace("@ADMIN_AGENT@", admin_agent) + "echo passed\n"],
                                  universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(run("User agent_1 is not allowed to run sudo on here.").stdout, "passed\n")
        p = run("User agent_1 may run the following commands on here:")
        self.assertEqual(p.stdout, "")
        self.assertIn("sudo lets it run commands", p.stderr)
        self.assertEqual(run("User agent_1 may run the following commands on here:", "yes").stdout, "passed\n")
        said = lambda code, out: mock.patch.object(signer.subprocess, "run", return_value=mock.Mock(returncode=code, stdout=out))
        with said(0, "User agent_1 may run the following commands on here:\n    (ALL) NOPASSWD: ALL\n"):
            self.assertTrue(signer._may_sudo())
        with said(1, "sudo: a password is required\n"):        # rules or none: sudo says no more without the password
            self.assertFalse(signer._may_sudo())
        with said(1, "Sorry, user agent_1 may not run sudo on here.\n"):
            self.assertFalse(signer._may_sudo())
        with mock.patch.object(signer.subprocess, "run", side_effect=OSError("no sudo")):
            self.assertFalse(signer._may_sudo())

    def test_after_a_quiet_spell_only_the_last_blocks_are_read(self):
        # N6: the blocks between were read once more, only to be dropped
        node = ChainOfBlocks(400)
        blocks = x402.Blocks(node, most=288)
        blocks.depth("", lambda: set())
        node.blocks.extend(("%064x" % (10000 + h), []) for h in range(5000))
        node.calls.clear()
        self.assertIsNone(blocks.depth("", lambda: set()))
        self.assertLessEqual(node.calls["getblock"], 288)

    def test_a_name_behind_nat64_that_leads_here_is_refused(self):
        # N8: Python counts 64:ff9b::/96 as global, whatever IPv4 address is in it
        for address in ("64:ff9b::7f00:1", "64:ff9b::a00:1", "2002:7f00:1::1"):
            resolve = lambda host, port, address=address, **k: [(10, 1, 6, "", (address, port, 0, 0))]
            with self.assertRaises(Refused, msg=address):
                mcp.outside("https://nat64.example/", resolve)
        mcp.outside("https://public.example/", lambda host, port, **k: [(10, 1, 6, "", ("64:ff9b::5db8:d70e", port, 0, 0))])


if __name__ == "__main__":
    unittest.main()
