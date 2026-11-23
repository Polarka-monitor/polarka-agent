"""0.2.7: each tool of the MCP server answers within an agent's call of it. Most agents end a call after 60 seconds
(the default of the MCP SDKs), and some clients have no setting for it (OpenClaw 2026.4.15 among them): there, a
north_fetch that waited up to 100 seconds for a block ended in an error after 60, though the payment had been made
(found in use, 23 November)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polarka_agent import faucet, mcp
from polarka_agent.wallet import Refused


class CallTime(unittest.TestCase):
    def test_what_waits_is_cut_to_well_under_a_minute(self):
        # a seller slow at each answer: the first, the paid one, and one more after the wait (x402.fetch), with a few
        # seconds for the payment itself
        self.assertLessEqual(mcp.SELLER_WAIT + 2 * mcp.SELLER_TIMEOUT, 45)
        self.assertLessEqual(mcp.WAIT_MOST, 45)
        # the faucet's work, then its claim, which waits up to PAY_WAIT for the faucet's payment
        self.assertLessEqual(mcp.WORK_MOST + faucet.PAY_WAIT, 45)

    def test_fetch_and_a_letter_wait_for_the_seller_within_one_call(self):
        seen = []

        def fetch(url, wallet, most, **k):
            seen.append(k)
            return {"status": 200, "paid": None, "body": b"the answer"}

        def send(wallet, to, text, most, office=None, **k):
            seen.append(k)
            return {"sent": True}

        with mock.patch("polarka_agent.x402.fetch", fetch), mock.patch("polarka_agent.mail.send", send):
            got = mcp.call(None, "north_fetch", {"url": "http://127.0.0.1:9/x", "most": "1"}, allow_local=True)
            self.assertEqual(got["body"], "the answer")
            self.assertEqual(mcp.call(None, "north_mail_send", {"to": "a" * 64, "text": "hello", "most": "1"}), {"sent": True})
        self.assertEqual(seen, [{"timeout": mcp.SELLER_TIMEOUT, "wait": mcp.SELLER_WAIT}] * 2)

    def test_a_wait_for_an_address_ends_within_one_call(self):
        seen = []

        def wait_for(wallet, address, amount, deep, timeout, every):
            seen.append(timeout)
            return {"paid": False}

        with mock.patch.object(mcp, "wait_for", wait_for):
            for given in ({}, {"timeout_seconds": 5}, {"timeout_seconds": 300}):     # 300 was taken by 0.2.6: still is
                mcp.call(None, "north_wait", dict(address="x", amount="1", **given))
            with self.assertRaises(Refused) as e:
                mcp.call(None, "north_wait", {"address": "x", "amount": "1", "timeout_seconds": 301})
        self.assertEqual(e.exception.kind, "INPUT")
        self.assertEqual(seen, [mcp.WAIT_MOST, 5, mcp.WAIT_MOST])

    def test_the_tools_say_how_long_they_wait(self):
        tools = {t["name"]: t for t in mcp.TOOLS}
        self.assertIn("up to %d seconds" % mcp.SELLER_WAIT, tools["north_fetch"]["description"])
        self.assertIn("up to %d seconds" % mcp.SELLER_WAIT, tools["north_mail_send"]["description"])
        self.assertIn("%d seconds at the most" % mcp.WAIT_MOST, tools["north_wait"]["description"])
        self.assertEqual(tools["north_wait"]["inputSchema"]["properties"]["timeout_seconds"]["maximum"], mcp.WAIT_MOST)
        self.assertIn("%d seconds at the most" % mcp.WORK_MOST, tools["north_faucet"]["description"])
        for tool in tools.values():
            self.assertNotIn("100 seconds", tool["description"])
            self.assertNotIn("a few minutes at the most", tool["description"])


if __name__ == "__main__":
    unittest.main()
