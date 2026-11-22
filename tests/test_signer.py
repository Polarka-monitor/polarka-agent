"""The signing service (second review, M4): an agent that asks over a socket and never holds the words.

In this process: what the service answers and refuses, its socket and its folder, the client's failures, the command
line and the MCP server through it, the scripts that set it up, and what --check sees. With a regtest node (TN_BIN):
payments, a stamp and x402 through the service, with the x402 book in the agent's own folder. On Linux as root with
POLARKA_TEST_USERS=1: the service as a user of its own, an agent in its group that asks and cannot open the words,
and a user outside the group that cannot ask."""
import grp
import io
import json
import os
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from polarka_agent import cli, mcp, signer, walletd, x402
from polarka_agent.server import ServerError
from polarka_agent.signer import Service, Signer
from polarka_agent.wallet import Refused, Wallet, units

from . import bench

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"
NOWHERE = "http://127.0.0.1:9"          # a wallet server that is not there: only what needs none is asked
LIMITS = {"per_payment_north": "3", "per_day_north": "20"}


def wallet_folder(parent, name="service", server=NOWHERE, limits=None):
    folder = os.path.join(parent, name)
    Wallet.create(folder, "regtest", server)
    os.chmod(folder, 0o700)
    if limits:
        with open(os.path.join(folder, "limits.json"), "w") as f:
            json.dump(limits, f)
        os.chmod(os.path.join(folder, "limits.json"), 0o640)
    return folder


def serving(test, folder, group=None, **k):
    lines = []
    listening = signer.start(folder, os.path.join(tempfile.mkdtemp(prefix="ps-"), "s.sock"), group, log=lines.append, **k)
    threading.Thread(target=listening.serve_forever, daemon=True).start()
    test.addCleanup(listening.close)
    return listening.path, lines


def run_cli(*args, **env):
    out = io.StringIO()
    with mock.patch.dict(os.environ, env):
        status = cli.run(list(args), out)
    return status, json.loads(out.getvalue())


def raw(path, data, wait=5):
    """What the service at path answers to these bytes, as they are."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(wait)
    s.connect(path)
    try:
        s.sendall(data)
        got = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return got
            got += chunk
    finally:
        s.close()


def stand_in(answer):
    """A socket that answers each request with answer(line) (bytes, or None for no answer at all)."""
    path = os.path.join(tempfile.mkdtemp(prefix="ps-"), "f.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(4)

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            line = conn.makefile("rb").readline()
            said = answer(line)
            if said is None:
                time.sleep(3)
            else:
                conn.sendall(said)
            conn.close()
    threading.Thread(target=serve, daemon=True).start()
    return path, listener


class ServiceInProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="polarka-signer-")
        cls.folder = wallet_folder(cls.tmp)

    def test_what_the_service_answers_and_refuses(self):
        s = Service(Wallet(self.folder), log=lambda line: None)

        def ask(request):
            return s.answer(request if isinstance(request, bytes) else json.dumps(request).encode())
        facts = ask({"v": 1, "op": "info"})["ok"]
        self.assertEqual((facts["chain"], facts["protocol"], facts["folder_private"], facts["limits_file"]), ("regtest", 1, True, None))
        self.assertEqual(ask({"v": 1, "op": "limits"})["ok"]["pays"], False)
        self.assertEqual(ask({"v": 1, "op": "first_address"})["ok"], Wallet(self.folder).account.address(0, 0))
        for request, kind in ((b"not json", "INPUT"), (b"\xff\xfe", "INPUT"), (b"[" * 100000, "INPUT"), ({"v": 2, "op": "info"}, "PROTOCOL"),
                              ({"op": "info"}, "PROTOCOL"), ({"v": 1, "op": "info", "args": []}, "PROTOCOL"),
                              ({"v": 1, "op": "info", "args": {"x": 1}}, "INPUT"),
                              ({"v": 1, "op": "pay", "args": {"outputs": [[SINK, 1.5]]}}, "INPUT"),
                              ({"v": 1, "op": "pay", "args": {"outputs": [[SINK, True]]}}, "INPUT"),
                              ({"v": 1, "op": "pay", "args": {"outputs": []}}, "INPUT"),
                              ({"v": 1, "op": "pay", "args": {"outputs": [[SINK, 100000]] * 101}}, "INPUT"),
                              ({"v": 1, "op": "pay", "args": {"outputs": [[SINK, 100000]], "hand": "no"}}, "INPUT"),
                              ({"v": 1, "op": "pay", "args": {"outputs": [[SINK, 100000]], "memo": 5}}, "MEMO"),
                              ({"v": 1, "op": "pay", "args": {"outputs": [[SINK, 100000]]}}, "NO_LIMITS"),
                              ({"v": 1, "op": "pay", "args": {"outputs": [["nonsense", 100000]]}}, "ADDRESS"),
                              ({"v": 1, "op": "stamp", "args": {"digest": "ab"}}, "DIGEST"),
                              ({"v": 1, "op": "stamp", "args": {"digest": "AB" * 32}}, "DIGEST"),
                              ({"v": 1, "op": "hand_again", "args": {"id": "00" * 32}}, "NOT_OPEN"),
                              ({"v": 1, "op": "history", "args": {"most": 201}}, "INPUT"),
                              ({"v": 1, "op": "received", "args": {"address": 7}}, "INPUT")):
            self.assertEqual(ask(request)["refused"][0], kind, request if not isinstance(request, bytes) else request[:20])
        # what belongs to the owner is not there to be asked for
        for op in ("forget", "words", "account", "create", "limits_file", "_send", None, ["pay"], {"op": "pay"}):
            self.assertEqual(ask({"v": 1, "op": op})["refused"][0], "NO_SUCH", op)
        # the wallet server is not there: said as the wallet says it, status 0
        self.assertEqual(ask({"v": 1, "op": "balance"})["server"][:2], [0, "unreachable"])
        said = json.dumps([ask({"v": 1, "op": op}) for op in ("info", "limits", "first_address", "payments")])
        with open(os.path.join(self.folder, "wallet.json")) as f:
            words = json.load(f)["words"].split()
        for i in range(len(words) - 2):
            self.assertNotIn(" ".join(words[i:i + 3]), said)

    def test_the_socket_the_client_and_the_log(self):
        path, lines = serving(self, self.folder)
        st = os.stat(path)
        self.assertEqual((stat.S_ISSOCK(st.st_mode), stat.S_IMODE(st.st_mode)), (True, 0o600))
        w = Signer(path, os.path.join(self.tmp, "agent"))
        self.assertEqual((w.chain, w.limits_shown()["pays"], w.first_address()), ("regtest", False, Wallet(self.folder).account.address(0, 0)))
        for call, kind in ((lambda: w.pay([(SINK, units("1"))]), "NO_LIMITS"), (lambda: w.stamp(b"short"), "DIGEST"),
                           (lambda: w.forget("00" * 32), "OWNER"), (lambda: w.hand_again("zz"), "INPUT")):
            with self.assertRaises(Refused) as e:
                call()
            self.assertEqual(e.exception.kind, kind)
        with self.assertRaises(ServerError) as e:
            w.balance()
        self.assertEqual((e.exception.status, e.exception.kind), (0, "unreachable"))
        if hasattr(socket, "SO_PEERCRED") or sys.platform == "darwin":
            self.assertIn(" uid %d " % os.getuid(), lines[-1])
        self.assertRegex(lines[-1], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d balance server 0 unreachable")
        # a request longer than the service reads, and one that never comes
        told = json.loads(raw(path, b"x" * (signer.MAX_REQUEST + 1)))
        self.assertEqual(told["refused"][0], "INPUT")
        with mock.patch.object(signer, "READ_TIME", 0.3):
            t0 = time.monotonic()
            self.assertEqual(raw(path, b""), b"")
            self.assertLess(time.monotonic() - t0, 3)
        # a request without its newline, when the caller ends its side: still one request
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        s.sendall(json.dumps({"v": 1, "op": "limits"}).encode())
        s.shutdown(socket.SHUT_WR)
        self.assertEqual(json.loads(s.makefile("rb").readline())["ok"]["pays"], False)
        s.close()

    def test_a_socket_for_a_group_and_a_busy_service(self):
        group = grp.getgrgid(os.getegid()).gr_name
        with mock.patch.object(signer, "READ_TIME", 2):
            path, _ = serving(self, self.folder, group, most=1)
            st = os.stat(path)
            self.assertEqual((stat.S_IMODE(st.st_mode), st.st_gid), (0o660, os.getegid()))
            holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            holder.connect(path)                    # says nothing, and holds the one place
            time.sleep(0.3)
            with self.assertRaises(Refused) as e:
                Signer(path, self.tmp).limits_shown()
            self.assertEqual(e.exception.kind, "SIGNER_BUSY")
            holder.close()
            time.sleep(0.5)
            self.assertEqual(Signer(path, self.tmp).limits_shown()["pays"], False)

    def test_where_the_service_does_not_begin(self):
        run = tempfile.mkdtemp(prefix="ps-")
        sock = os.path.join(run, "s.sock")
        folder = wallet_folder(self.tmp, "open")
        os.chmod(folder, 0o750)
        for call, kind in ((lambda: signer.start(folder, sock), "OPEN_FOLDER"),
                           (lambda: signer.start(os.path.join(self.tmp, "none"), sock), "NO_WALLET"),
                           (lambda: signer.start(self.folder, sock, "no-such-group-here"), "GROUP"),
                           (lambda: signer.start(self.folder, os.path.join(run, "x" * 120)), "SOCKET"),
                           (lambda: signer.start(self.folder, os.path.join(run, "no", "s.sock")), "SOCKET")):
            with self.assertRaises(Refused) as e:
                call()
            self.assertEqual(e.exception.kind, kind)
        os.chmod(folder, 0o700)
        with open(os.path.join(folder, "limits.json"), "w") as f:
            json.dump(LIMITS, f)
        os.chmod(os.path.join(folder, "limits.json"), 0o666)
        with self.assertRaises(Refused) as e:
            signer.start(folder, sock)
        self.assertEqual(e.exception.kind, "OPEN_LIMITS")
        with mock.patch.object(signer.os, "geteuid", return_value=0), self.assertRaises(Refused) as e:
            signer.start(self.folder, sock)
        self.assertEqual(e.exception.kind, "ROOT")
        # a folder others may write: another program could put its own socket there
        everyone = tempfile.mkdtemp(prefix="ps-")
        os.chmod(everyone, 0o777)
        with self.assertRaises(Refused) as e:
            signer.start(self.folder, os.path.join(everyone, "s.sock"))
        self.assertEqual(e.exception.kind, "SOCKET")
        # a file in the socket's place is left as it is; a socket left behind is taken over; a live one is not
        with open(sock, "w") as f:
            f.write("mine")
        with self.assertRaises(Refused) as e:
            signer.start(self.folder, sock)
        self.assertEqual(e.exception.kind, "SOCKET")
        with open(sock) as f:
            self.assertEqual(f.read(), "mine")
        os.unlink(sock)
        left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        left.bind(sock)
        left.close()                            # a socket left behind by a service that ended
        listening = signer.start(self.folder, sock, log=lambda line: None)
        listening.close()
        self.assertFalse(os.path.exists(sock))
        path, _ = serving(self, self.folder)
        os.rename(path, sock)                    # a live service's socket, at the place that is asked for
        with self.assertRaises(Refused) as e:
            signer.start(self.folder, sock)
        self.assertIn("another service", str(e.exception))

    def test_a_client_whose_service_fails_it(self):
        for path, kind in ((os.path.join(self.tmp, "nothing.sock"), "SIGNER"),):
            with self.assertRaises(Refused) as e:
                Signer(path, self.tmp).balance()
            self.assertEqual(e.exception.kind, kind)
            self.assertIn("nothing is paid", str(e.exception))
        for answer, kind in ((b"hello\n", "SIGNER"), (b'{"v": 2, "ok": 1}\n', "SIGNER"), (b'{"v": 1, "refused": [1, 2]}\n', "SIGNER"),
                             (b'{"v": 1, "failed": "KeyError"}\n', "SIGNER_FAILED"), (b'{"v": 1, "refused": ["OVER_DAY", "too much"]}\n', "OVER_DAY"),
                             (b"", "SIGNER")):
            path, listener = stand_in(lambda line, answer=answer: answer)
            with self.assertRaises(Refused) as e:
                Signer(path, self.tmp).payments()
            self.assertEqual(e.exception.kind, kind, answer)
            listener.close()
        path, listener = stand_in(lambda line: b'{"v": 1, "server": [502, "late", "the server is behind"]}\n')
        with self.assertRaises(ServerError) as e:
            Signer(path, self.tmp).balance()
        self.assertEqual((e.exception.status, e.exception.kind, str(e.exception)), (502, "late", "the server is behind"))
        listener.close()
        path, listener = stand_in(lambda line: None)            # answers nothing in time
        with self.assertRaises(Refused) as e:
            Signer(path, self.tmp, timeout=1).pay([(SINK, units("1"))])
        self.assertIn("look at the payments", str(e.exception))
        listener.close()
        if os.geteuid() != 0:                                   # root is let in whatever the socket's mode
            served, _ = serving(self, self.folder)
            os.chmod(served, 0)
            with self.assertRaises(Refused) as e:
                Signer(served, self.tmp).limits_shown()
            self.assertIn("may not ask", str(e.exception))

    def run_cli(self, *args, **env):
        return run_cli(*args, **env)

    def test_the_command_line_and_mcp_through_the_service(self):
        path, _ = serving(self, self.folder)
        agent = os.path.join(self.tmp, "agent-cli")
        self.assertEqual(self.run_cli("--dir", agent, "--signer", path, "limits"), (0, Wallet(self.folder).limits_shown()))
        self.assertEqual(self.run_cli("--dir", agent, "limits", POLARKA_SIGNER=path)[1], Wallet(self.folder).limits_shown())
        for args in (("forget", "00" * 32), ("new", "--chain", "regtest", "--server", NOWHERE), ("faucet-serve", "--amount", "1"),
                     ("stamps-serve", "--rpc-url", "http://127.0.0.1:9", "--rpc-cookie", "x"), ("postage-serve", "--upstream", "127.0.0.1:9", "--price", "1")):
            status, said = self.run_cli("--dir", agent, "--signer", path, *args)
            self.assertEqual((status, said["error"]), (2, "SIGNER"), args)
        status, said = self.run_cli("--dir", agent, "--signer", path, "start", "--no-faucet", "--no-mining")
        self.assertEqual((status, said["error"]), (2, "CHAIN"))                 # a wallet of regtest: no start on main
        status, said = self.run_cli("--dir", self.folder, "--signer", path, "signer-serve", "--socket", path + "2")
        self.assertEqual((status, said["error"]), (2, "INPUT"))
        self.assertEqual(self.run_cli("--dir", agent, "--signer", os.path.join(self.tmp, "none.sock"), "balance")[1]["error"], "SIGNER")
        self.assertFalse(os.path.exists(os.path.join(agent, "wallet.json")))
        # the MCP server, in this process and as the program itself
        w = Signer(path, agent)

        def tool(name, **arguments):
            reply = mcp.answer(w, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            return reply["result"]["isError"], json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(tool("north_limits"), (False, Wallet(self.folder).limits_shown()))
        self.assertEqual(tool("north_pay", address=SINK, amount="1")[1]["error"], "NO_LIMITS")
        self.assertEqual(tool("north_forget", id="00" * 32)[1]["error"], "NO_TOOL")
        self.assertEqual(tool("north_balance")[1]["error"], "SERVER")
        lines = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "north_limits", "arguments": {}}}) + "\n"
        for args, words_open in ((["--dir", agent, "mcp", "--signer", path], False), (["--dir", agent, "--signer", path, "mcp"], False),
                                 (["--dir", self.folder, "mcp"], True)):
            p = subprocess.run([sys.executable, "-B", "-m", "polarka_agent"] + args, cwd=ROOT, input=lines, universal_newlines=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            replies = [json.loads(l) for l in p.stdout.splitlines()]
            self.assertEqual((p.returncode, [r["id"] for r in replies]), (0, [1, 2]), p.stderr)
            self.assertEqual(json.loads(replies[1]["result"]["content"][0]["text"]), Wallet(self.folder).limits_shown())
            self.assertEqual('"words_open"' in p.stderr, words_open, p.stderr)
        self.assertTrue(os.path.isdir(agent))                   # made for x402's book, with nothing of the wallet in it
        self.assertEqual(os.listdir(agent), [])

    def test_the_scripts_that_set_it_up(self):
        moved = wallet_folder(self.tmp, "a wallet to move")
        for system in ("linux", "macos"):
            for k in ({}, {"move_from": moved, "per_payment": "1", "per_day": "5"}):
                text = signer.setup_script(system, "agent_1", package="polarka-agent==0.2.3", **k)
                p = subprocess.run(["/bin/sh", "-n"], input=text, universal_newlines=True, stderr=subprocess.PIPE)
                self.assertEqual(p.returncode, 0, p.stderr)
                for placeholder in ("@WALLET@", "@LIMITS@", "@USER@", "@UNIT@", "@PLIST@", "@AGENT@", "@PACKAGE@", "@VERSION@", "@FROM@", "@AS_SERVICE@", "@LIMITS_JSON@"):
                    self.assertNotIn(placeholder, text)
                self.assertIn("AGENT_USER=agent_1\n", text)
                self.assertIn("PACKAGE=polarka-agent==0.2.3\n", text)
                if k:
                    self.assertIn("FROM='%s'" % moved, text)
                    self.assertIn("""printf '%s\\n' '{"per_payment_north": "1", "per_day_north": "5"}' > "$DIR/limits.json\"""", text)
                else:
                    self.assertIn("new --chain main", text)
                    self.assertIn("the service pays nothing", text)
            self.assertIn("signer-serve --socket /run/polarka-signer/signer.sock --group polarka-pay" if system == "linux"
                          else "<string>signer-serve</string>", text)
        for k in ({"agent_user": "root"}, {"agent_user": "a b"}, {"agent_user": "_polarkasigner", "system": "macos"},
                  {"agent_user": "x", "per_payment": "1"}, {"agent_user": "x", "per_payment": "1e3", "per_day": "5"},
                  {"agent_user": "x", "package": "polarka-agent; rm -rf /"}, {"agent_user": "x", "move_from": self.tmp + "/nothing"}):
            with self.assertRaises(Refused):
                signer.setup_script(k.pop("system", "linux"), **k)
        status = cli.run(["signer-setup", "--system", "linux", "--agent-user", "agent_1"], io.StringIO())
        self.assertEqual(status, 0)

    def test_what_a_check_sees(self):
        path, _ = serving(self, self.folder)
        venv = os.path.join(self.tmp, "venv")
        os.makedirs(os.path.join(venv, "bin"), exist_ok=True)
        if not os.path.lexists(os.path.join(venv, "bin", "python3")):
            os.symlink(sys.executable, os.path.join(venv, "bin", "python3"))
        os.chmod(venv, 0o775)                   # its group may write it: changeable by others than root, whoever runs this
        done = signer.check(path, self.folder, venv)
        seen = {c["check"]: c["ok"] for c in done["checks"]}
        self.assertFalse(done["ok"])
        self.assertEqual((seen["socket"], seen["service"], seen["folder"], seen["limits"], seen["program"]), (False, True, True, None, False))
        if os.geteuid() != 0:
            self.assertEqual(seen["words"], False)               # this user can open the folder: it is its own
        self.assertEqual(signer.check(os.path.join(self.tmp, "no.sock"), self.folder, venv)["checks"][0]["ok"], False)
        self.assertIsNone(signer._program(os.path.join(self.tmp, "no-venv")))
        self.assertIn(venv, signer._changeable(venv))
        self.assertEqual(signer._hops(os.path.join(venv, "bin", "python3"))[1], sys.executable)


@unittest.skipUnless(bench.find_bin(), "truenorthd and truenorth-cli are needed (TN_BIN names their folder)")
class ServiceOnRegtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(bench.find_bin())
        cls.standin = bench.StandIn(cls.node)
        cls.tmp = tempfile.mkdtemp(prefix="polarka-signer-rt-")
        cls.folder = wallet_folder(cls.tmp, server=cls.standin.url, limits=LIMITS)
        cls.node.mine(3, Wallet(cls.folder).account.address(0, 0))
        cls.node.mine(100, SINK)
        cls.seller = Wallet(wallet_folder(cls.tmp, "seller", server=cls.standin.url))
        rpc = walletd.Node("http://127.0.0.1:%d" % cls.node.port, cls.node.cookie)
        cls.network = x402.network_of(rpc)
        cls.facilitator = x402.Facilitator(rpc, "regtest", os.path.join(cls.tmp, "used.jsonl"), confirmations=0)
        cls.paywall = x402.paywall(cls.facilitator, units("0.5"), cls.seller.account.address(0, 0), b"the answer is 42")
        threading.Thread(target=cls.paywall.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/answer" % cls.paywall.server_address[1]
        cls.listening = signer.start(cls.folder, os.path.join(tempfile.mkdtemp(prefix="ps-"), "s.sock"), log=lambda line: None)
        threading.Thread(target=cls.listening.serve_forever, daemon=True).start()
        cls.agent = os.path.join(cls.tmp, "agent")
        os.makedirs(cls.agent, mode=0o700)

    @classmethod
    def tearDownClass(cls):
        cls.listening.close()
        cls.paywall.shutdown()
        cls.standin.stop()
        cls.node.stop()

    def test_1_payments_and_a_stamp(self):
        w = Signer(self.listening.path, self.agent)
        self.assertEqual(w.balance()["coins"], 3)
        to = self.seller.account.address(0, 7)
        self.assertEqual(w.pay([(to, units("1"))], dry_run=True)["state"], "not made (a dry run)")
        paid = w.pay([(to, units("1"))], "through the service")
        self.assertEqual((paid["state"], paid["sent"]), ("handed", "1.00000000"))
        with self.assertRaises(Refused) as e:
            w.pay([(to, units("4"))])
        self.assertEqual(e.exception.kind, "OVER_LIMIT")
        status, said = run_cli("--dir", self.agent, "--signer", self.listening.path, "pay", to, "0.25", "--memo", "from the command line")
        self.assertEqual((status, said["state"]), (0, "handed"))
        self.assertEqual(w.stamp(b"\x01" * 32, dry_run=True)["state"], "not made (a dry run)")
        self.node.mine(1, SINK)
        self.assertEqual([(p["id"], p["state"], p["memo"]) for p in w.payments()],
                         [(paid["id"], "in_block", "through the service"), (said["id"], "in_block", "from the command line")])
        self.assertEqual(self.seller.received(to)["amount"], units("1.25"))
        fresh = w.fresh_address()
        self.assertEqual(w.received(fresh)["amount"], 0)
        self.assertTrue(w.history()["complete"])
        self.assertEqual(os.listdir(self.agent), [])            # nothing of the wallet with the agent

    def test_2_x402_through_the_service(self):
        w = Signer(self.listening.path, self.agent)
        got = x402.fetch(self.url, w, units("1"), network=self.network, wait=30)
        self.assertEqual((got["status"], got["body"], got["paid"]), (200, b"the answer is 42", "0.50000000"))
        with open(os.path.join(self.folder, "journal.jsonl")) as f:
            self.assertIn(got["txid"], [json.loads(line)["txid"] for line in f])
        self.assertIn("x402-pending.json", os.listdir(self.agent))   # the book of payments under way is the agent's own
        self.assertNotIn("x402-pending.json", os.listdir(self.folder))
        with mock.patch.dict(x402.NETWORKS, {"regtest": self.network}):
            reply = mcp.answer(w, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "north_fetch", "arguments": {"url": self.url, "most": "1"}}}, allow_local=True)
        shown = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual((reply["result"]["isError"], shown["status"], shown["body"]), (False, 200, "the answer is 42"))
        self.assertNotIn("wallet.json", os.listdir(self.agent))


USERS = os.environ.get("POLARKA_TEST_USERS") == "1" and sys.platform.startswith("linux") and os.geteuid() == 0


@unittest.skipUnless(USERS, "as root on Linux with POLARKA_TEST_USERS=1 (it makes the users pst-signer, pst-agent, pst-other)")
class ApartOnLinux(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def sh(*a):
            subprocess.run(a, check=True, stdout=subprocess.DEVNULL)
        sh("groupadd", "--system", "pst-pay")
        sh("useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", "-G", "pst-pay", "pst-signer")
        sh("useradd", "--system", "--user-group", "--no-create-home", "--shell", "/bin/sh", "-G", "pst-pay", "pst-agent")
        sh("useradd", "--system", "--user-group", "--no-create-home", "--shell", "/bin/sh", "pst-other")
        cls.base = tempfile.mkdtemp(prefix="pst-")
        os.chmod(cls.base, 0o755)
        cls.code = os.path.join(cls.base, "code")
        shutil.copytree(os.path.join(ROOT, "polarka_agent"), os.path.join(cls.code, "polarka_agent"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        sh("chmod", "-R", "a+rX", cls.code)
        cls.folder = wallet_folder(cls.base, "wallet")
        sh("chown", "-R", "pst-signer:pst-signer", cls.folder)
        cls.limits = os.path.join(cls.folder, "limits.json")
        with open(cls.limits, "w") as f:
            json.dump(LIMITS, f)
        os.chown(cls.limits, 0, grp.getgrnam("pst-signer").gr_gid)
        os.chmod(cls.limits, 0o640)
        run = os.path.join(cls.base, "run")
        os.mkdir(run)
        os.chmod(run, 0o755)
        os.chown(run, pwd.getpwnam("pst-signer").pw_uid, grp.getgrnam("pst-signer").gr_gid)
        cls.sock = os.path.join(run, "signer.sock")
        cls.agent = os.path.join(cls.base, "agent")
        os.mkdir(cls.agent, 0o700)
        os.chown(cls.agent, pwd.getpwnam("pst-agent").pw_uid, -1)
        cls.log = open(os.path.join(cls.base, "service.log"), "w")
        cls.service = subprocess.Popen(["runuser", "-u", "pst-signer", "--", sys.executable, "-B", "-m", "polarka_agent", "--dir", cls.folder,
                                        "signer-serve", "--socket", cls.sock, "--group", "pst-pay"], cwd=cls.code,
                                       stdout=subprocess.DEVNULL, stderr=cls.log)
        until = time.monotonic() + 20
        while not os.path.exists(cls.sock) and time.monotonic() < until and cls.service.poll() is None:
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.service.terminate()
        cls.service.wait(10)
        cls.log.close()
        for user in ("pst-signer", "pst-agent", "pst-other"):
            subprocess.run(["userdel", user], stderr=subprocess.DEVNULL)
        subprocess.run(["groupdel", "pst-pay"], stderr=subprocess.DEVNULL)
        shutil.rmtree(cls.base, ignore_errors=True)

    def as_user(self, user, *args, program=None):
        return subprocess.run(["runuser", "-u", user, "--"] + (program or [sys.executable, "-B", "-m", "polarka_agent"]) + list(args),
                              cwd=self.code, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def test_1_the_socket(self):
        with open(os.path.join(self.base, "service.log")) as f:
            self.assertTrue(os.path.exists(self.sock), f.read())
        st = os.stat(self.sock)
        self.assertEqual((stat.S_IMODE(st.st_mode), pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name),
                         (0o660, "pst-signer", "pst-pay"))

    def test_2_the_agent_asks_and_cannot_open_the_words(self):
        r = self.as_user("pst-agent", "--dir", self.agent, "--signer", self.sock, "limits")
        self.assertEqual((r.returncode, json.loads(r.stdout).get("per_day")), (0, "20.00000000"), r.stdout + r.stderr)
        r = self.as_user("pst-agent", os.path.join(self.folder, "wallet.json"), program=["cat"])
        self.assertEqual((r.returncode != 0, r.stdout), (True, ""))
        r = self.as_user("pst-agent", "-c", "open(%r, 'a')" % self.limits, program=[sys.executable])
        self.assertNotEqual(r.returncode, 0)
        r = self.as_user("pst-agent", "--dir", self.agent, "signer-setup", "--check", "--socket", self.sock,
                         "--service-dir", self.folder, "--venv", os.path.join(self.base, "no-venv"))
        done = json.loads(r.stdout)
        self.assertEqual({c["check"]: c["ok"] for c in done["checks"]},
                         {"socket": True, "service": True, "folder": True, "limits": True, "words": True, "program": None, "sudo": True})
        self.assertTrue(done["ok"])

    def test_3_a_user_outside_the_group_cannot_ask(self):
        r = self.as_user("pst-other", "--dir", self.base, "--signer", self.sock, "limits")
        said = json.loads(r.stdout)
        self.assertEqual((r.returncode, said["error"]), (2, "SIGNER"))
        self.assertIn("may not ask", said["message"])

    def test_4_not_as_root(self):
        r = subprocess.run([sys.executable, "-B", "-m", "polarka_agent", "--dir", self.folder, "signer-serve", "--socket",
                            os.path.join(self.base, "root.sock")], cwd=self.code, stdout=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]), (2, "ROOT"))


if __name__ == "__main__":
    unittest.main()
