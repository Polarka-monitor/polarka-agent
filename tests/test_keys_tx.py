"""Keys, addresses, the signature hash and signing, against the published P2QRH test vectors
(tests/p2qrh-test-vectors.json, made by the cold address tool and accepted by TrueNorth Core v30.5.4)."""
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polarka_agent import keys, tx

HERE = os.path.dirname(os.path.abspath(__file__))
VEC = json.load(open(os.path.join(HERE, "p2qrh-test-vectors.json")))


class KeysTest(unittest.TestCase):
    def test_the_keys_and_addresses_of_the_vectors(self):
        for chain in ("main", "regtest"):
            account = keys.Account.from_words(VEC["mnemonic"], chain)
            for want in VEC["chains"][chain]["keys"]:
                branch, index = (int(x) for x in want["path"].split("/")[-2:])
                private, xonly, script, address = account.key(branch, index)
                self.assertEqual(keys.b32(private).hex(), want["private_key"])
                self.assertEqual(xonly.hex(), want["xonly_pubkey"])
                self.assertEqual(keys.commitment(xonly).hex(), want["commitment"])
                self.assertEqual(script.hex(), want["scriptPubKey"])
                self.assertEqual(address, want["address"])
                self.assertEqual(keys.script_for_address(chain, address), script)
                self.assertEqual(keys.address_for_script(chain, script), address)

    def test_the_seed_of_the_vectors(self):
        self.assertEqual(keys.seed_from_words(VEC["mnemonic"]).hex(), VEC["seed"])

    def test_bip340_vector_0(self):
        sig = keys.schnorr_sign(bytes(32), 3, bytes(32))
        self.assertEqual(sig.hex().upper(), "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
                                            "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0")
        pub = bytes.fromhex("F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9")
        self.assertTrue(keys.schnorr_verify(bytes(32), pub, sig))
        self.assertFalse(keys.schnorr_verify(bytes(31) + b"\x01", pub, sig))
        self.assertFalse(keys.schnorr_verify(bytes(32), pub, sig[:63] + bytes([sig[63] ^ 1])))

    def test_a_signature_is_new_each_time_and_holds(self):
        msg = keys.sha(b"a message")
        a, b = keys.schnorr_sign(msg, 12345), keys.schnorr_sign(msg, 12345)
        self.assertNotEqual(a, b)
        pub = keys.b32(keys.mul(12345)[0])
        self.assertTrue(keys.schnorr_verify(msg, pub, a) and keys.schnorr_verify(msg, pub, b))

    def test_words(self):
        self.assertIsNone(keys.check_words(VEC["mnemonic"]))
        for count in (12, 24):
            words = keys.new_words(count)
            self.assertEqual(len(words.split()), count)
            self.assertIsNone(keys.check_words(words))
        self.assertEqual(keys.new_words(12, bytes(16)), VEC["mnemonic"])
        self.assertEqual(keys.check_words("abandon " * 11 + "abbout"), "word 12 is not on the list")
        self.assertIn("do not fit", keys.check_words("abandon " * 12))
        self.assertIn("12 or 24", keys.check_words("abandon " * 14 + "about"))
        with self.assertRaises(keys.BadInput):
            keys.seed_from_words("abandon " * 12)
        with self.assertRaises(keys.BadInput):
            keys.new_words(15)

    def test_addresses_that_are_refused(self):
        account = keys.Account.from_words(VEC["mnemonic"], "main")
        good = account.address(0, 0)
        self.assertEqual(keys.script_for_address("main", good.upper()), account.script(0, 0))
        refused = [
            good[:10] + ("q" if good[10] != "q" else "p") + good[11:],      # one character changed
            good[:20].upper() + good[20:],                                   # mixed case
            keys.Account.from_words(VEC["mnemonic"], "regtest").address(0, 0),   # another network
            good[:-1], good + "q", "", "north1", "1" + good, None, 5,
            keys.encode_address("north", 3, bytes(32)),                      # a witness version it does not know
            keys.encode_address("north", 2, bytes(20)),                      # the right version, the wrong length
            keys.encode_address("north", 0, bytes(25)),
        ]
        # version 0 with the checksum of later versions, and version 2 with the checksum of version 0
        data = [0] + [0] * 32
        for version, const in ((0, 0x2BC830A3), (2, 1)):
            body = [version] + [0] * 52
            pm = keys._polymod(keys._expand("north") + body + [0] * 6) ^ const
            refused.append("north1" + "".join(keys._B32[d] for d in body + [pm >> 5 * (5 - i) & 31 for i in range(6)]))
        for bad in refused:
            with self.assertRaises(keys.BadInput, msg=repr(bad)):
                keys.script_for_address("main", bad)
        # the other kinds TrueNorth Core accepts
        for version, size in ((0, 20), (0, 32), (1, 32)):
            address = keys.encode_address("north", version, bytes(range(size)))
            script = keys.script_for_address("main", address)
            self.assertEqual(script, bytes([0 if version == 0 else 0x50 + version, size]) + bytes(range(size)))
            self.assertEqual(keys.address_for_script("main", script), address)
        self.assertIsNone(keys.address_for_script("main", b"\x6a\x02ab"))


class TxTest(unittest.TestCase):
    def setUp(self):
        s = VEC["spend"]
        self.s = s
        self.vin = [(i["txid"], i["vout"], i["sequence"]) for i in s["inputs"]]
        self.prevouts = [(i["amount_sat"], bytes.fromhex(i["scriptPubKey"])) for i in s["inputs"]]
        self.vout = [(o["amount_sat"], bytes.fromhex(o["scriptPubKey"])) for o in s["outputs"]]
        self.account = keys.Account.from_words(VEC["mnemonic"], "regtest")

    def test_the_signature_hashes_and_signatures_of_the_vectors(self):
        s = self.s
        self.assertEqual((s["version"], s["locktime"], s["inputs"][0]["sequence"]), (tx.VERSION, tx.LOCKTIME, tx.SEQUENCE))
        for i in range(2):
            h = tx.sighash(s["version"], s["locktime"], self.vin, self.prevouts, self.vout, i)
            self.assertEqual(h.hex(), s["signature_hashes"][i])
            self.assertEqual(keys.schnorr_sign(h, self.account.key(0, i)[0], bytes(32)).hex(), s["signatures"][i])
        self.assertEqual(tx.ser_tx(s["version"], self.vin, self.vout, s["locktime"]).hex(), s["unsigned_tx"])
        self.assertEqual(tx.txid_of(s["version"], self.vin, self.vout, s["locktime"]), s["txid"])
        self.assertEqual(tx.vsize(2, [o[1] for o in self.vout]), s["vsize"])

    def test_signing_a_plan_gives_the_transaction_of_the_vectors(self):
        s = self.s
        coins = [tx.Coin(i["txid"], i["vout"], i["amount_sat"], bytes.fromhex(i["scriptPubKey"]), 1 + n, True, 0, n)
                 for n, i in enumerate(s["inputs"])]
        the_plan = tx.Plan(coins, [(sc, a) for a, sc in self.vout], s["fee_sat"], s["vsize"], self.vout[1][0])
        raw, txid = tx.sign(the_plan, lambda c: self.account.key(c.branch, c.index)[:2], bytes(32))
        self.assertEqual(raw, s["signed_tx"])
        self.assertEqual(txid, s["txid"])
        # a coin that does not sit on its key, a plan that does not add up, a coin named twice
        with self.assertRaises(keys.BadInput):
            tx.sign(the_plan, lambda c: self.account.key(1, 0)[:2], bytes(32))
        with self.assertRaises(keys.BadInput):
            tx.sign(the_plan._replace(fee=s["fee_sat"] + 1), lambda c: self.account.key(c.branch, c.index)[:2])
        with self.assertRaises(keys.BadInput):
            tx.sign(the_plan._replace(coins=[coins[0], coins[0]]), lambda c: self.account.key(c.branch, c.index)[:2])

    def test_sizes(self):
        script = self.account.script(0, 0)
        self.assertEqual(tx.weight(1, [script, script]), 651)        # the payment of 14 November 2026, block 24432
        self.assertEqual(tx.vsize(1, [script, script]), 163)
        self.assertEqual(tx.vsize(1, [script]), 120)
        data = tx.data_script(b"x" * 40)
        self.assertEqual(data[:2], b"\x6a\x28")
        self.assertEqual(tx.data_script(b"x" * 76)[:3], b"\x6a\x4c\x4c")
        for bad in (b"", b"x" * 81, "text"):
            with self.assertRaises(keys.BadInput):
                tx.data_script(bad)

    def test_the_rules_of_choosing_coins(self):
        rnd = random.Random(20261115)
        pay, change = self.account.script(0, 5), self.account.script(1, 5)
        for case in range(3000):
            coins = [tx.Coin("%064x" % rnd.getrandbits(256), rnd.randrange(3), rnd.choice((500, 999, 1000, 5000, 10 ** 5, 10 ** 8, 512 * 10 ** 8)) + rnd.randrange(1000),
                             self.account.script(0, 1), 10, False, 0, 1) for _ in range(rnd.randrange(1, 12))]
            must = rnd.sample(coins, rnd.randrange(0, min(3, len(coins)) + 1))
            outputs = [(pay, rnd.choice((1000, 1001, 4000, 10 ** 5, 10 ** 8, 600 * 10 ** 8)))]
            if rnd.random() < 0.3:
                outputs.append((tx.data_script(b"d" * 40), 0))
            rate = rnd.choice((1, 2, 10, 1000))
            total = sum(a for _, a in outputs)
            try:
                p = tx.plan(coins, outputs, rate, change, must)
            except tx.PlanError as e:
                self.assertEqual(e.reason, "NOT_ENOUGH", case)
                everything = sum(c.amount for c in coins)
                self.assertTrue(everything - total < tx.vsize(len(coins), [s for s, _ in outputs]) * rate, case)
                continue
            paid_in, paid_out = sum(c.amount for c in p.coins), sum(a for _, a in p.outputs)
            self.assertEqual(paid_in - paid_out, p.fee, case)
            self.assertEqual(p.vsize, tx.vsize(len(p.coins), [s for s, _ in p.outputs]), case)
            self.assertGreaterEqual(p.fee, p.vsize * rate, case)
            self.assertEqual(p.outputs[:len(outputs)], outputs, case)
            for c in must:
                self.assertIn(c, p.coins, case)
            self.assertEqual(len(set((c.txid, c.vout) for c in p.coins)), len(p.coins), case)
            if p.change:
                self.assertEqual(p.outputs[-1], (change, p.change), case)
                self.assertGreaterEqual(p.change, tx.MIN_OUTPUT, case)
                self.assertEqual(p.fee, p.vsize * rate, case)
            else:
                self.assertEqual(len(p.outputs), len(outputs), case)
                # what went to the fee beyond the rate would not have made an output worth having
                self.assertLess(p.fee - tx.vsize(len(p.coins), [s for s, _ in outputs] + [change]) * rate, tx.MIN_OUTPUT, case)
            # no coin was taken that the payment did not need: without the last one it would not be covered
            if len(p.coins) > max(1, len(set((c.txid, c.vout) for c in must))):
                less = paid_in - p.coins[-1].amount
                self.assertTrue(less - total < tx.vsize(len(p.coins) - 1, [s for s, _ in outputs]) * rate, case)

    def test_plans_that_are_refused(self):
        pay, change = self.account.script(0, 5), self.account.script(1, 5)
        coin = tx.Coin("11" * 32, 0, 10 ** 6, self.account.script(0, 1), 10, False, 0, 1)
        for outputs, rate, reason in (
            ([(pay, 999)], 2, "AMOUNT_TOO_SMALL"), ([(pay, 1000)], 0, "FEE_RATE"), ([(pay, 1000)], 1001, "FEE_RATE"),
            ([(pay, 1000)], 2.0, "FEE_RATE"), ([], 2, "OUTPUTS"), ([(pay, 1000.0)], 2, "AMOUNT"), ([(pay, True)], 2, "AMOUNT"),
            ([(tx.data_script(b"x"), 1)], 2, "AMOUNT"), ([(tx.data_script(b"x"), 0), (tx.data_script(b"y"), 0)], 2, "OUTPUTS"),
            ([(pay, 10 ** 6)], 2, "NOT_ENOUGH"), ([(pay, tx.MAX_MONEY + 1)], 2, "AMOUNT"),
        ):
            with self.assertRaises(tx.PlanError, msg=reason) as e:
                tx.plan([coin], outputs, rate, change)
            self.assertEqual(e.exception.reason, reason)
        with self.assertRaises(tx.PlanError) as e:
            tx.plan([], [(pay, 1000)], 2, change)
        self.assertEqual(e.exception.reason, "NOTHING_TO_SPEND")

    def test_ripe_and_unripe_coins(self):
        script = self.account.script(0, 0)
        reward = tx.Coin("22" * 32, 0, 512 * 10 ** 8, script, 50, True, 0, 0)
        self.assertFalse(tx.spendable(reward, 149))
        self.assertTrue(tx.spendable(reward, 150))
        self.assertTrue(tx.spendable(reward._replace(coinbase=False), 50))
        self.assertFalse(tx.spendable(reward._replace(coinbase=False, height=51), 50))
        self.assertFalse(tx.spendable(reward._replace(coinbase=False, height=0), 50))


if __name__ == "__main__":
    unittest.main()
