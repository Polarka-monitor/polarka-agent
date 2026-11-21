"""An agent given the MCP server from a registry starts with no wallet: `mcp` makes one on main, which pays
nothing until its owner writes limits.json, and answers. Needs no node and no network."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class McpStartsWithoutAWallet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-mcp-start-")
        self.folder = os.path.join(self.tmp, "w")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def talk(self, *messages):
        p = subprocess.run([sys.executable, "-m", "polarka_agent", "--dir", self.folder, "mcp"],
                           input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True, text=True,
                           timeout=60, cwd=HERE)
        return [json.loads(line) for line in p.stdout.splitlines()], p.stderr

    def test_a_wallet_on_main_is_made_and_pays_nothing(self):
        answers, said = self.talk(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "north_pay", "arguments": {"address": "x", "amount": "1"}}})
        with open(os.path.join(self.folder, "wallet.json")) as f:
            self.assertEqual(json.load(f).get("chain"), "main")
        self.assertEqual(oct(os.stat(self.folder).st_mode & 0o777), "0o700")
        self.assertIn('"made": true', said)
        self.assertIn("north_fetch", [t["name"] for t in answers[1]["result"]["tools"]])
        self.assertTrue(answers[2]["result"].get("isError"))            # no limits.json: nothing is paid
        self.assertFalse(os.path.exists(os.path.join(self.folder, "limits.json")))
        again, said = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertNotIn('"made": true', said)                          # the second start keeps the wallet


if __name__ == "__main__":
    unittest.main()
