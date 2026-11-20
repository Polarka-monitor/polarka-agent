"""`polarka-agent start`: a wallet on main made once and kept, its first address used every time, a faucet that
says no does not stop the start, and without the proof-of-work library it says what to do. Nothing here reaches
the network: the faucet and the pool are stood in for."""
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from polarka_agent import cli, faucet, miner, rx
from polarka_agent.wallet import Refused, Wallet


class Start(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-agent-start-")
        self.folder = os.path.join(self.tmp, "w")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stderr", err):
            status = cli.run(["--dir", self.folder, "start"] + list(argv), out=out)
        return status, json.loads(out.getvalue()), [json.loads(line) for line in err.getvalue().splitlines()]

    def test_a_wallet_once_and_the_same_address_after(self):
        status, done, events = self.run_cli("--no-faucet", "--no-mining")
        self.assertEqual((status, done["wallet_made"]), (0, True))
        self.assertTrue(done["address"].startswith("north1"))
        self.assertEqual(Wallet(self.folder).chain, "main")
        self.assertEqual(events[0]["event"], "wallet")
        with open(os.path.join(self.folder, "wallet.json")) as f:
            words = json.load(f)["words"]
        self.assertNotIn(words.split()[0] + " " + words.split()[1], json.dumps(done) + json.dumps(events))   # they stay in wallet.json
        status, again, _ = self.run_cli("--no-faucet", "--no-mining")
        self.assertEqual((status, again["wallet_made"], again["address"]), (0, False, done["address"]))

    def test_the_faucet_and_the_pool_get_the_first_address(self):
        asked, mined = [], []
        with mock.patch.object(faucet, "ask", lambda url, address: asked.append((url, address)) or {"paid": "0.25", "txid": "ab" * 32}), \
                mock.patch.object(rx, "find_library", lambda: "/lib"), \
                mock.patch.object(miner, "mine", lambda url, address, threads, fast, shares, seconds, say: mined.append(
                    (url, address, threads, fast, seconds)) or {"shares": 3, "blocks": 0, "refused": 0, "hashes": 9, "credited": 71250000}):
            status, done, events = self.run_cli("--threads", "2", "--seconds", "5")
        self.assertEqual(status, 0)
        self.assertEqual(asked, [(cli.DEFAULT_FAUCET, done["address"])])
        self.assertEqual(mined, [(cli.DEFAULT_POOL, done["address"], 2, True, 5)])
        self.assertEqual(done["mining"]["credited"], "0.71250000")
        self.assertEqual([e["event"] for e in events], ["wallet", "faucet", "mining"])

    def test_a_faucet_that_says_no_does_not_stop_it_and_no_library_is_said(self):
        def no(url, address):
            raise Refused("FAUCET", "the faucet said no (ALREADY)")
        with mock.patch.object(faucet, "ask", no), mock.patch.object(rx, "find_library", lambda: None):
            status, done, events = self.run_cli()
        self.assertEqual((status, done["error"]), (2, "LIBRARY"))
        self.assertIn("build-rx", done["message"])
        self.assertEqual([e["event"] for e in events], ["wallet", "faucet"])
        self.assertEqual(events[1]["error"], "FAUCET")

    def test_threads_out_of_range(self):
        status, done, _ = self.run_cli("--threads", "0", "--no-faucet", "--no-mining")
        self.assertEqual((status, done["error"]), (2, "INPUT"))


if __name__ == "__main__":
    unittest.main()
