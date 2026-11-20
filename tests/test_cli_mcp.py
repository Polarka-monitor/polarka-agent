"""The command line and the server for agents (MCP), against a regtest node and the stand-in server."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polarka_agent import cli, mcp
from polarka_agent.wallet import Wallet
from tests import bench

SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(bench.find_bin(), "truenorthd and truenorth-cli are needed (TN_BIN names their folder)")
class CliMcpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(bench.find_bin())
        cls.standin = bench.StandIn(cls.node)
        cls.tmp = tempfile.mkdtemp(prefix="polarka-agent-cli-")
        cls.dir = os.path.join(cls.tmp, "wallet")
        status, made = cls.cli("new", "--chain", "regtest", "--server", cls.standin.url)
        assert status == 0, made
        cls.words = made["words"]
        cls.first = made["first_address"]
        cls.node.mine(3, cls.first)
        cls.node.mine(100, SINK)
        cls.reward = int(cls.node.cli("getblock", cls.node.cli("getblockhash", 1), 2)["tx"][0]["vout"][0]["value"])

    @classmethod
    def tearDownClass(cls):
        cls.standin.stop()
        cls.node.stop()

    @classmethod
    def cli(cls, *args, folder=None):
        out = io.StringIO()
        status = cli.run(["--dir", folder or cls.dir] + list(args), out)
        return status, json.loads(out.getvalue())

    def limits(self, **limits):
        with open(os.path.join(self.dir, "limits.json"), "w") as f:
            json.dump(limits or {"per_payment_north": "50", "per_day_north": "200"}, f)

    def other(self):
        folder = tempfile.mkdtemp(prefix="other-", dir=self.tmp)
        os.rmdir(folder)
        status, made = self.cli("new", "--chain", "regtest", "--server", self.standin.url, "--twelve", folder=folder)
        self.assertEqual((status, len(made["words"].split())), (0, 12))
        return folder, made["first_address"]

    def test_1_a_wallet_at_the_command_line(self):
        self.assertEqual(len(self.words.split()), 24)
        self.assertEqual(self.cli("new", "--chain", "regtest", "--server", self.standin.url), (2, {"error": "EXISTS", "message": "there is a wallet in this folder already; it is not replaced"}))
        status, b = self.cli("balance")
        self.assertEqual((status, b["spendable"], b["unripe"], b["coins"]), (0, "%d.00000000" % (3 * self.reward), "0.00000000", 3))
        self.assertEqual(self.cli("limits")[1]["pays"], False)
        folder, to = self.other()
        self.assertEqual(self.cli("pay", to, "1.5")[1]["error"], "NO_LIMITS")
        self.limits()
        self.assertEqual(self.cli("limits")[1], {"pays": True, "per_payment": "50.00000000", "per_day": "200.00000000", "fee_rate": 2, "only_to": None})
        status, dry = self.cli("pay", to, "1.5", "--dry-run")
        self.assertEqual((status, dry["state"], dry["fee"]), (0, "not made (a dry run)", "0.00000326"))
        status, paid = self.cli("pay", to, "1.5", "--memo", "a first payment")
        self.assertEqual((status, paid["state"], paid["sent"]), (0, "handed", "1.50000000"))
        self.node.mine(1, SINK)
        status, shown = self.cli("payments")
        self.assertEqual([(p["id"], p["state"], p["memo"]) for p in shown["payments"]], [(paid["id"], "in_block", "a first payment")])
        self.assertEqual(self.cli("received", to, folder=folder)[1], {"height": 104, "amount": "1.50000000", "coins": 1, "confirmations": 1})
        self.assertEqual(self.cli("wait", to, "1.5", folder=folder)[1]["paid"], True)
        self.assertEqual(self.cli("wait", to, "1.6", "--timeout", "1", folder=folder)[1]["paid"], False)
        status, h = self.cli("history")
        self.assertEqual((h["lines"][0]["txid"], h["lines"][0]["net"], h["lines"][0]["fee"]), (paid["id"], "-1.50000326", "0.00000326"))
        for args, kind in ((("pay", to, "1e3"), "AMOUNT"), (("pay", to, "-1"), "AMOUNT"), (("pay", "nonsense", "1"), "ADDRESS"), (("pay", to, "60"), "OVER_LIMIT"),
                           (("forget", "00" * 32), "NOT_OPEN"), (("received", SINK), "NOT_OURS"), (("stamp",), "DIGEST"), (("stamp", "--sha256", "zz"), "DIGEST"),
                           (("pay-many", os.path.join(self.dir, "limits.json")), "FILE"), (("pay-many", "/nonexistent/file"), "FILE")):
            status, said = self.cli(*args)
            self.assertEqual((status, said["error"]), (2, kind), args)

    def test_2_several_payments_and_a_stamp(self):
        self.limits()
        (f1, a1), (f2, a2) = self.other(), self.other()
        many = os.path.join(self.tmp, "many.json")
        with open(many, "w") as f:
            json.dump([{"address": a1, "amount": "2"}, {"address": a2, "amount": "0.5"}], f)
        status, paid = self.cli("pay-many", many)
        self.assertEqual((status, paid["state"], paid["sent"], paid["to"]), (0, "handed", "2.50000000", [a1, a2]))
        doc = os.path.join(self.tmp, "doc.txt")
        with open(doc, "wb") as f:
            f.write(b"the document")
        status, stamped = self.cli("stamp", doc)
        self.assertEqual((status, stamped["state"], stamped["sha256"]), (0, "handed", hashlib.sha256(b"the document").hexdigest()))
        self.node.mine(1, SINK)
        self.assertEqual(self.cli("balance", folder=f1)[1]["spendable"], "2.00000000")
        self.assertEqual(self.cli("balance", folder=f2)[1]["spendable"], "0.50000000")
        seen = self.node.cli("getrawtransaction", stamped["id"], 1, self.node.cli("getbestblockhash"))
        self.assertIn("6a28" + (b"POLARKA\x01" + hashlib.sha256(b"the document").digest()).hex(), [o["scriptPubKey"]["hex"] for o in seen["vout"]])

    def test_3_a_wallet_brought_back_from_its_words(self):
        words = os.path.join(self.tmp, "words.txt")
        with open(words, "w") as f:
            f.write(self.words + "\n")
        folder = os.path.join(self.tmp, "back")
        status, back = self.cli("new", "--chain", "regtest", "--server", self.standin.url, "--words-file", words, folder=folder)
        self.assertEqual((status, back), (0, {"folder": folder, "first_address": self.first, "brought_back": True}))
        self.assertEqual(self.cli("balance", folder=folder)[1]["spendable"], self.cli("balance")[1]["spendable"])
        with open(words, "w") as f:
            f.write("abandon " * 12)
        status, said = self.cli("new", "--chain", "regtest", "--server", self.standin.url, "--words-file", words, folder=os.path.join(self.tmp, "bad"))
        self.assertEqual((status, said["error"]), (2, "WORDS"))
        self.assertEqual(self.cli("balance", folder=os.path.join(self.tmp, "nowhere"))[1]["error"], "NO_WALLET")
        self.assertEqual(self.cli("new", "--chain", "regtest", folder=os.path.join(self.tmp, "x"))[1]["error"], "SERVER")
        self.assertEqual(self.cli("new", "--chain", "main", "--server", "http://example.org", folder=os.path.join(self.tmp, "y"))[1]["error"], "SERVER")

    def test_4_a_server_that_is_not_there(self):
        folder = os.path.join(self.tmp, "gone")
        self.cli("new", "--chain", "regtest", "--server", "http://127.0.0.1:9", folder=folder)
        status, said = self.cli("balance", folder=folder)
        self.assertEqual((status, said["error"], said["status"], said["kind"]), (3, "SERVER", 0, "unreachable"))

    # ---------------------------------------------------------------- the server for agents

    def ask(self, wallet, *messages):
        source = io.StringIO("".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages))
        sink = io.StringIO()
        mcp.serve(wallet, source, sink)
        return [json.loads(l) for l in sink.getvalue().splitlines()]

    def tool(self, wallet, name, **arguments):
        reply, = self.ask(wallet, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        self.assertEqual(reply["id"], 7)
        content = reply["result"]["content"]
        self.assertEqual((len(content), content[0]["type"]), (1, "text"))
        return reply["result"]["isError"], json.loads(content[0]["text"])

    def test_5_an_agent_at_the_wallet(self):
        self.limits()
        w = Wallet(self.dir)
        replies = self.ask(w, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
                           {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                           {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, "this is not json", {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
                           {"id": 5, "method": "ping"}, "", [1, 2])
        self.assertEqual([r.get("id") for r in replies], [1, 2, 3, None, 4, 5, None])
        self.assertEqual((replies[0]["result"]["protocolVersion"], replies[0]["result"]["capabilities"], replies[0]["result"]["serverInfo"]["name"]),
                         ("2025-03-26", {"tools": {}}, "polarka-agent"))
        self.assertEqual(replies[1]["result"], {})
        names = [t["name"] for t in replies[2]["result"]["tools"]]
        self.assertEqual(names, ["north_balance", "north_address", "north_received", "north_wait", "north_pay", "north_stamp", "north_payments",
                                 "north_history", "north_limits", "north_faucet", "north_fetch", "north_postage"])
        self.assertEqual([replies[3]["error"]["code"], replies[4]["error"]["code"], replies[5]["error"]["code"], replies[6]["error"]["code"]],
                         [-32700, -32601, -32600, -32600])
        other = self.ask(w, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(other[0]["result"]["protocolVersion"], mcp.VERSIONS[0])

        failed, b = self.tool(w, "north_balance")
        self.assertFalse(failed)
        self.assertRegex(b["spendable"], r"^\d+\.\d{8}$")
        failed, a = self.tool(w, "north_address")
        self.assertTrue(not failed and a["address"].startswith("rnorth1z"))
        folder, to = self.other()
        failed, dry = self.tool(w, "north_pay", address=to, amount="0.75", dry_run=True)
        self.assertEqual((failed, dry["state"]), (False, "not made (a dry run)"))
        failed, paid = self.tool(w, "north_pay", address=to, amount="0.75", memo="from an agent")
        self.assertEqual((failed, paid["state"], paid["sent"]), (False, "handed", "0.75000000"))
        failed, stamped = self.tool(w, "north_stamp", sha256="ab" * 32, dry_run=True)
        self.assertEqual((failed, stamped["sha256"], stamped["state"]), (False, "ab" * 32, "not made (a dry run)"))
        self.node.mine(1, SINK)
        failed, waited = self.tool(Wallet(folder), "north_wait", address=to, amount="0.75", timeout_seconds=5)
        self.assertEqual((failed, waited["paid"], waited["amount"]), (False, True, "0.75000000"))
        failed, shown = self.tool(w, "north_payments")
        self.assertEqual(shown["payments"][-1]["state"], "in_block")
        self.assertEqual(self.tool(w, "north_limits")[1]["per_payment"], "50.00000000")
        self.assertFalse(self.tool(w, "north_history")[0])
        self.assertEqual(self.tool(Wallet(folder), "north_received", address=to)[1]["amount"], "0.75000000")
        for name, arguments, kind in (
            ("north_pay", {"address": to, "amount": 0.75}, "AMOUNT"), ("north_pay", {"address": to, "amount": 1}, "AMOUNT"), ("north_pay", {"address": to}, "INPUT"),
            ("north_pay", {"address": "x", "amount": "1"}, "ADDRESS"), ("north_pay", {"address": to, "amount": "51"}, "OVER_LIMIT"),
            ("north_forget", {"id": paid["id"]}, "NO_TOOL"), ("north_words", {}, "NO_TOOL"), ("north_stamp", {"sha256": "12"}, "DIGEST"),
            ("north_wait", {"address": to, "amount": "1", "timeout_seconds": 9999}, "INPUT"), ("north_received", {"address": SINK}, "NOT_OURS"),
        ):
            failed, said = self.tool(w, name, **arguments)
            self.assertEqual((failed, said["error"]), (True, kind), (name, arguments))
        reply, = self.ask(w, {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "north_pay", "arguments": "all of it"}})
        self.assertTrue(reply["result"]["isError"])

    def test_6_nothing_an_agent_is_told_holds_the_words(self):
        w = Wallet(self.dir)
        said = json.dumps(self.ask(w, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        for name in ("north_balance", "north_address", "north_payments", "north_history", "north_limits"):
            said += json.dumps(self.tool(w, name))
        words = self.words.split()
        for i in range(len(words) - 2):
            self.assertNotIn(" ".join(words[i:i + 3]), said)
        self.assertNotIn("raw", json.loads(json.dumps(self.tool(w, "north_payments")[1]))["payments"][0])

    def test_7_the_program_itself_over_its_pipes(self):
        p = subprocess.run([sys.executable, "-B", "-m", "polarka_agent", "--dir", self.dir, "mcp"], cwd=ROOT, universal_newlines=True, timeout=60,
                           input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
                           + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "north_limits", "arguments": {}}}) + "\n",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        lines = p.stdout.splitlines()
        self.assertEqual((p.returncode, len(lines)), (0, 2), p.stderr)
        self.assertEqual([json.loads(l)["id"] for l in lines], [1, 2])
        q = subprocess.run([sys.executable, "-B", "-m", "polarka_agent", "--dir", self.dir, "balance"], cwd=ROOT, universal_newlines=True, timeout=60,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertIn("spendable", json.loads(q.stdout))


if __name__ == "__main__":
    unittest.main()
