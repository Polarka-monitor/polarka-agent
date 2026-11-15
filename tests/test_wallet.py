"""The wallet against a TrueNorth Core node on regtest, through the stand-in for a Northbound wallet server.
What is checked on the node itself: which transactions it holds, which are in blocks, and that a payment
which did not go through can never arrive beside the one made after it."""
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polarka_agent import keys, tx
from polarka_agent.server import Server
from polarka_agent.wallet import Refused, Wallet, north, units
from tests import bench

N = keys.UNIT
SINK = "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"       # key 0/0 of the public test phrase


@unittest.skipUnless(bench.find_bin(), "truenorthd and truenorth-cli are needed (TN_BIN names their folder)")
class WalletTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = bench.Node(bench.find_bin())
        cls.standin = bench.StandIn(cls.node)
        cls.tmp = tempfile.mkdtemp(prefix="polarka-agent-wallets-")
        cls.count = 0
        # a bank for the tests: five block rewards, ripe
        cls.bank = cls.make({"per_payment_north": "100000", "per_day_north": "1000000"})
        cls.node.mine(5, cls.bank.fresh_address())
        cls.node.mine(100, SINK)

    @classmethod
    def tearDownClass(cls):
        cls.standin.stop()
        cls.node.stop()

    @classmethod
    def make(cls, limits=None, clock=None, words=None):
        cls.count += 1
        folder = os.path.join(cls.tmp, "w%d" % cls.count)
        Wallet.create(folder, "regtest", cls.standin.url, words)
        if limits is not None:
            with open(os.path.join(folder, "limits.json"), "w") as f:
                json.dump(limits, f)
        return Wallet(folder, **({"clock": clock} if clock else {}))

    def setUp(self):
        self.standin.set(stale=False, busy=0, hold=False, refuse="", hide=False)

    def block(self, n=1):
        self.node.mine(n, SINK)

    def funded(self, amounts=("10",), limits={"per_payment_north": "5", "per_day_north": "8"}, clock=None):
        """A wallet with one coin for each amount, in a block."""
        w = self.make(limits, clock)
        for a in amounts:
            done = self.bank.pay([(w.fresh_address(), units(a))])
            self.assertEqual(done["state"], "handed", done)
            self.block()
        return w

    def states(self, w):
        return {p["id"]: p["state"] for p in w.payments()}

    def in_mempool(self, txid):
        return txid in self.node.cli("getrawmempool")

    def confirmations(self, txid):
        try:
            return self.node.cli("gettransaction", txid)
        except RuntimeError:
            return None

    def raw_of(self, w, txid):
        with open(os.path.join(w.folder, "journal.jsonl")) as f:
            return next(json.loads(l)["raw"] for l in f if '"signed"' in l and txid in l)

    # ---------------------------------------------------------------- reading

    def test_a_new_wallet_and_what_it_holds(self):
        w = self.make()
        self.assertEqual(w.balance(), {"height": self.node.cli("getblockcount"), "spendable": 0, "set_aside": 0, "unripe": 0, "coins": 0,
                                       "beyond": False, "open_payments": []})
        first, second = w.fresh_address(), w.fresh_address()
        self.assertEqual((first, second), (w.account.address(0, 0), w.account.address(0, 1)))
        self.node.mine(1, first)                                    # a block reward, not ripe yet
        b = w.balance()
        self.assertEqual((b["spendable"], b["coins"]), (0, 1))
        self.assertGreater(b["unripe"], 0)
        reward = b["unripe"]
        self.block(99)
        self.assertEqual(w.balance()["unripe"], reward)
        self.block(1)
        b = w.balance()
        self.assertEqual((b["spendable"], b["unripe"]), (reward, 0))
        self.assertEqual(w.fresh_address(), w.account.address(0, 2))       # the first was paid to, the second was handed out

    def test_only_its_owner_may_read_the_wallet_file(self):
        w = self.make()
        path = os.path.join(w.folder, "wallet.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        os.chmod(path, 0o644)
        with self.assertRaises(Refused) as e:
            Wallet(w.folder)
        self.assertEqual(e.exception.kind, "OPEN_FILE")
        os.chmod(path, 0o600)
        with self.assertRaises(Refused) as e:
            Wallet.create(w.folder, "regtest", self.standin.url)
        self.assertEqual(e.exception.kind, "EXISTS")

    def test_twenty_addresses_handed_out_and_none_paid_to(self):
        w = self.make()
        handed = [w.fresh_address() for _ in range(20)]
        self.assertEqual(len(set(handed)), 20)
        with self.assertRaises(Refused) as e:
            w.fresh_address()
        self.assertEqual(e.exception.kind, "TOO_MANY_OPEN")
        self.bank.pay([(handed[19], 2 * N)])
        self.block()
        self.assertEqual(w.fresh_address(), w.account.address(0, 20))
        self.assertEqual(w.balance()["spendable"], 2 * N)
        # the same words in another folder find the coin again
        again = self.make(words=json.load(open(os.path.join(w.folder, "wallet.json")))["words"])
        self.assertEqual(again.balance()["spendable"], 2 * N)

    # ---------------------------------------------------------------- paying

    def test_a_payment_from_the_first_word_to_a_block(self):
        w, other = self.funded(), self.make()
        to = other.fresh_address()
        done = w.pay([(to, units("1.5"))], memo="for the test")
        self.assertEqual((done["state"], done["sent"], done["coins"], done["stands_against"]), ("handed", "1.50000000", 1, []))
        fee = units(done["fee"])
        self.assertEqual(fee, tx.vsize(1, [b"x" * 34, b"x" * 34]) * 2)
        self.assertTrue(self.in_mempool(done["id"]))
        b = w.balance()
        self.assertEqual((b["spendable"], b["open_payments"][0]["state"]), (0, "handed"))      # its one coin is in use
        self.block()
        self.assertEqual(self.states(w), {done["id"]: "in_block"})
        self.assertEqual(w.balance()["spendable"], 10 * N - units("1.5") - fee)
        self.assertEqual(other.balance()["spendable"], units("1.5"))
        self.assertEqual(self.node.cli("getrawtransaction", done["id"], 1, self.node.cli("getbestblockhash"))["confirmations"], 1)
        line = w.history()["lines"][0]
        self.assertEqual((line["txid"], line["sent"], line["fee"]), (done["id"], units("1.5"), fee))

    def test_without_limits_nothing_is_paid(self):
        w = self.funded(limits=None)
        to = self.make().fresh_address()
        with self.assertRaises(Refused) as e:
            w.pay([(to, N)])
        self.assertEqual(e.exception.kind, "NO_LIMITS")
        self.assertFalse(os.path.exists(os.path.join(w.folder, "journal.jsonl")))

    def test_the_owners_limits(self):
        w = self.funded(("10", "10"), {"per_payment_north": "5", "per_day_north": "8"})
        to = self.make().fresh_address()
        for amount, kind in (("5", "OVER_LIMIT"), ("0.00000999", "AMOUNT_TOO_SMALL"), ("25", "NOT_ENOUGH")):
            with self.assertRaises(Refused) as e:
                w.pay([(to, units(amount))])
            self.assertEqual(e.exception.kind, kind, amount)
        dry = w.pay([(to, 4 * N)], dry_run=True)
        self.assertEqual(dry["state"], "not made (a dry run)")
        self.assertFalse(os.path.exists(os.path.join(w.folder, "journal.jsonl")))
        self.assertEqual(w.pay([(to, 4 * N)])["state"], "handed")
        self.block()
        with self.assertRaises(Refused) as e:
            w.pay([(to, 4 * N)])                                    # 4 and its fee, and 4 again, is more than 8 in a day
        self.assertEqual(e.exception.kind, "OVER_DAY")
        self.assertEqual(w.pay([(to, units("3.9"))])["state"], "handed")
        for bad in ("rnorth1zqqqq", self.bank.account.address(0, 0).replace("rnorth", "north"), "", None):
            with self.assertRaises(Refused) as e:
                w.pay([(bad, N)])
            self.assertEqual(e.exception.kind, "ADDRESS", bad)

    def test_a_list_of_addresses_that_may_be_paid(self):
        friend, stranger = self.make().fresh_address(), self.make().fresh_address()
        w = self.funded(limits={"per_payment_north": "5", "per_day_north": "8", "allow": [friend], "fee_rate": 10})
        with self.assertRaises(Refused) as e:
            w.pay([(stranger, N)])
        self.assertEqual(e.exception.kind, "NOT_ALLOWED")
        with self.assertRaises(Refused) as e:
            w.pay([(friend, N), (stranger, N)])
        self.assertEqual(e.exception.kind, "NOT_ALLOWED")
        done = w.pay([(friend, N)])
        self.assertEqual((done["state"], units(done["fee"])), ("handed", 163 * 10))
        for broken in ({"per_payment_north": 5, "per_day_north": "8"}, {"per_payment": "5", "per_day": "8"}, {"per_day_north": "8"},
                       {"per_payment_north": "5", "per_day_north": 8.5}, {"per_payment_north": "500000000000000", "per_day_north": "8"}, {"per_payment_north": "5", "per_day_north": "8", "fee_rate": 0},
                       {"per_payment_north": "5", "per_day_north": "8", "allow": ["not an address"]}, "text"):
            with open(os.path.join(w.folder, "limits.json"), "w") as f:
                json.dump(broken, f)
            with self.assertRaises(Refused) as e:
                w.pay([(friend, N)])
            self.assertEqual(e.exception.kind, "LIMITS", broken)

    def test_several_payments_in_one_transaction(self):
        w = self.funded(("10",), {"per_payment_north": "9", "per_day_north": "9"})
        others = [self.make() for _ in range(3)]
        done = w.pay([(o.fresh_address(), (i + 1) * N) for i, o in enumerate(others)])
        self.assertEqual((done["state"], done["sent"], len(done["to"])), ("handed", "6.00000000", 3))
        self.block()
        self.assertEqual([o.balance()["spendable"] for o in others], [N, 2 * N, 3 * N])

    # ---------------------------------------------------------------- a server that says no, loses or hides

    def test_a_payment_the_server_refused_is_not_forgotten_and_cannot_arrive_beside_the_next(self):
        w, other = self.funded(("10", "10")), self.make()
        to = other.fresh_address()
        self.standin.set(refuse="we do not like it")
        first = w.pay([(to, 2 * N)])
        self.assertEqual(first["state"], "kept")
        self.assertIn("we do not like it", first["note"])
        self.assertFalse(self.in_mempool(first["id"]))
        self.assertEqual(self.standin.set()["kept"], [self.raw_of(w, first["id"])])    # the server has it, whatever it said
        self.standin.set(refuse="")
        kept = self.raw_of(w, first["id"])
        # the same thing is paid in another way: 3 in place of 2. It is built on a coin of the first.
        second = w.pay([(to, 3 * N)])
        self.assertEqual((second["state"], second["stands_against"], second["same_as"]), ("handed", [first["id"]], None))
        self.assertNotEqual(second["id"], first["id"])
        spent_by = lambda raw: {(i["txid"], i["vout"]) for i in self.node.cli("decoderawtransaction", raw)["vin"]}
        self.assertTrue(spent_by(kept) & spent_by(self.raw_of(w, second["id"])))
        # the node holds the second and takes the first no more: never both
        with self.assertRaises(RuntimeError):
            self.node.cli("sendrawtransaction", kept)
        self.assertEqual(self.node.cli("getrawmempool"), [second["id"]])
        self.block()
        self.assertEqual(self.states(w), {first["id"]: "overtaken", second["id"]: "in_block"})
        with self.assertRaises(RuntimeError):
            self.node.cli("sendrawtransaction", kept)
        self.assertEqual(other.balance()["spendable"], 3 * N)                  # the second arrived, the first never can
        third = w.pay([(to, N)])
        self.assertEqual((third["state"], third["stands_against"]), ("handed", []))

    def test_the_same_payment_asked_for_again_is_the_same_transaction(self):
        w, other = self.funded(("10", "10"), {"per_payment_north": "5", "per_day_north": "5"}), self.make()
        to = other.fresh_address()
        self.standin.set(refuse="not now")
        first = w.pay([(to, 4 * N)])
        self.assertEqual(first["state"], "kept")
        dry = w.pay([(to, 4 * N)], dry_run=True)
        self.assertEqual((dry["same_as"], dry["stands_against"]), (first["id"], []))
        self.standin.set(refuse="")
        again = w.pay([(to, 4 * N)])           # 4 and 4 would be over the limit for a day; it is one payment, counted once
        self.assertEqual((again["id"], again["same_as"], again["state"]), (first["id"], first["id"], "handed"))
        self.assertEqual(self.node.cli("getrawmempool"), [first["id"]])
        self.assertEqual(len(w.payments()), 1)
        with self.assertRaises(Refused) as e:
            w.pay([(to, 4 * N)])               # now it is handed over, and a second one would be a second payment
        self.assertIn(e.exception.kind, ("OVER_DAY", "NOT_ENOUGH"))
        self.block()
        self.assertEqual(self.states(w), {first["id"]: "in_block"})
        self.assertEqual(other.balance()["spendable"], 4 * N)

    def test_a_refused_payment_that_the_server_passes_on_after_all(self):
        w, other = self.funded(("10", "10")), self.make()
        to = other.fresh_address()
        self.standin.set(refuse="no")
        first = w.pay([(to, 2 * N)])
        self.assertEqual(first["state"], "kept")
        self.standin.set(refuse="")
        self.node.cli("sendrawtransaction", self.raw_of(w, first["id"]))        # what a server that kept it can do at any time
        self.assertEqual(self.states(w), {first["id"]: "handed"})              # the wallet sees it waiting and no longer counts it lost
        self.block()
        self.assertEqual(self.states(w), {first["id"]: "in_block"})
        self.assertEqual(other.balance()["spendable"], 2 * N)

    def test_a_hand_over_that_is_lost(self):
        w, other = self.funded(("10", "10")), self.make()
        to = other.fresh_address()
        self.standin.set(hold=True)
        first = w.pay([(to, 2 * N)])
        self.assertEqual(first["state"], "unknown")
        self.assertFalse(self.in_mempool(first["id"]))
        self.standin.set(hold=False)
        second = w.pay([(to, units("2.5"))])
        self.assertEqual((second["state"], second["stands_against"]), ("handed", [first["id"]]))
        self.assertNotEqual(second["id"], first["id"])
        self.block()
        self.assertEqual(self.states(w), {first["id"]: "overtaken", second["id"]: "in_block"})
        self.assertEqual(other.balance()["spendable"], units("2.5"))

    def test_a_server_that_hides_the_coins_of_a_payment_it_kept(self):
        w, other = self.funded(("10", "10")), self.make()
        to = other.fresh_address()
        self.standin.set(refuse="no")
        first = w.pay([(to, 2 * N)])
        self.standin.set(refuse="", hide=True)
        for _ in range(2):
            with self.assertRaises(Refused) as e:
                w.pay([(to, 2 * N)])
            self.assertEqual(e.exception.kind, "OPEN_PAYMENT")
        self.assertEqual(self.states(w), {first["id"]: "kept"})
        with self.assertRaises(Refused):
            w.forget("00" * 32)
        w.forget(first["id"])                                                  # the owner's word, by hand
        second = w.pay([(to, 2 * N)])
        self.assertEqual((second["state"], second["stands_against"]), ("handed", []))
        with self.assertRaises(Refused):
            w.forget(second["id"])                                             # only what is open can be forgotten

    def test_a_server_whose_index_is_behind_and_one_that_is_busy(self):
        w = self.funded()
        to = self.make().fresh_address()
        self.standin.set(stale=True)
        from polarka_agent.server import ServerError
        with self.assertRaises(ServerError) as e:
            w.pay([(to, N)])
        self.assertEqual(e.exception.status, 503)
        self.assertFalse(os.path.exists(os.path.join(w.folder, "journal.jsonl")))
        self.standin.set(stale=False, busy=2)
        w.server._pause = lambda seconds: None
        self.assertEqual(w.pay([(to, N)])["state"], "handed")

    # ---------------------------------------------------------------- stamps

    def test_a_stamp(self):
        w = self.funded()
        digest = hashlib.sha256(b"a document").digest()
        done = w.stamp(digest, memo="a document")
        self.assertEqual((done["state"], done["sent"], done["to"]), ("handed", "0.00000000", []))
        self.block()
        seen = self.node.cli("getrawtransaction", done["id"], 1, self.node.cli("getbestblockhash"))
        data = [o["scriptPubKey"]["hex"] for o in seen["vout"] if o["scriptPubKey"]["type"] == "nulldata"]
        self.assertEqual(data, ["6a28" + (b"POLARKA\x01" + digest).hex()])
        self.assertEqual(self.states(w), {done["id"]: "in_block"})
        self.assertEqual(w.balance()["spendable"], 10 * N - units(done["fee"]))
        for bad in (b"short", "text", None):
            with self.assertRaises(Refused):
                w.stamp(bad)


if __name__ == "__main__":
    unittest.main()
