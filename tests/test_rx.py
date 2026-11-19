"""The proof of work against three headers of the real chain: one before the first key change, the first one
after it, and one of 19 November 2026. Skipped where the library is not built (build-rx)."""
import unittest

from polarka_agent import rx

# (height, header as the node gives it, hash of the seed block or None, the proof-of-work hash as a number shown)
REAL = [
    (2111, "000000207d7d2b1a7b7cc221c0bdffccb8ebabab83b8c8c0e87ab4b91ca137a4677b2cc349532b9d796e11d37d18e80cd22c0738a88cabdd"
           "6da7405250a4b7640f888b171743cf6a5ba8081ddc9a6b00", None,
     "00000006145b73bb45efd62d3080ed5cac64c83a512f0c32c03fb4b5fe5fbc61"),
    (2112, "00000020914de345561ebef912f900d112f0d56cb14b7f18a649f1d0ee45551f79a6f2f161fb377fa648d085383e94bd4c40c3b170ca4e88d4"
           "f2a20d702847300873adf87943cf6ae3c6081d288d1300", "5a9484ea8db8bf513d95ff73cd36149fba52b6e79813b1f1c96a48be7d94f1a2",
     "0000000192bd3858ed39a3279edce6b95f6d15d70b448ea143dccb8b3bd7dc10"),
    (27468, "00000020974e6f35580b5819fb1857dafe395c635aa390e354db85e83a127b8fd8183269610ac0e559ffcfacaf3d188a6e715488aada12bc"
            "0f57a7f0f434538b48dc6ba5c732fe6acac57d1d0c029434", "39777a9202b6edeb7d143eb2251eccfccb97fe8198e19e894c68e59a7faa8ddd",
     "00000016eb3c8c5268e47f1a6cc99285001b8d2326f97f9ef847329d2195ef5a"),
]


class Rules(unittest.TestCase):
    def test_seed_height(self):
        self.assertEqual([rx.seed_height(h) for h in (1, 2111, 2112, 4159, 4160, 27468)], [0, 0, 2048, 2048, 4096, 26624])

    def test_target(self):
        self.assertEqual(rx.target_of(0x1d00ffff), 0xffff << 208)
        self.assertEqual(rx.target_of(0x207fffff), 0x7fffff << 232)
        self.assertEqual(rx.target_of(0x03123456), 0x123456)
        self.assertEqual(rx.target_of(0x01120000), 0x12)


@unittest.skipUnless(rx.find_library(), "the proof-of-work library is not built")
class RealHeaders(unittest.TestCase):
    def test_headers_of_the_chain(self):
        engine = rx.engine()
        for height, header, seed, shown in REAL:
            header = bytes.fromhex(header)
            key = bytes(32) if seed is None else bytes.fromhex(seed)[::-1]
            self.assertEqual(seed is None, rx.seed_height(height) == 0)
            engine.ensure(key)
            vm = engine.rx.vm()
            try:
                proof = engine.rx.hash(vm, header)
                self.assertEqual(proof[::-1].hex(), shown, "block %d" % height)
                self.assertLessEqual(int.from_bytes(proof, "little"), rx.target_of(int.from_bytes(header[72:76], "little")))
                # the same header through the search: its own nonce is found, the one before it is not
                nonce = int.from_bytes(header[76:], "little")
                target = int.from_bytes(proof, "little")
                self.assertEqual(engine.rx.scan(vm, header[:76], target, nonce, 1, 1), (nonce, proof))
                self.assertIsNone(engine.rx.scan(vm, header[:76], target - 1, nonce, 1, 1))
            finally:
                engine.rx.free(vm)

    def test_what_is_refused(self):
        engine = rx.engine()
        with self.assertRaises(ValueError):
            engine.rx.prepare(b"short")
        engine.ensure(bytes(32))
        vm = engine.rx.vm()
        try:
            with self.assertRaises(ValueError):
                engine.rx.hash(vm, bytes(79))
            with self.assertRaises(ValueError):
                engine.rx.scan(vm, bytes(80), 1, 0, 1, 1)
        finally:
            engine.rx.free(vm)


if __name__ == "__main__":
    unittest.main()
