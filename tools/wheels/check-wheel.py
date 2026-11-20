"""Checks an installed polarka-agent wheel: the proof-of-work library is the one inside the package, and it gives
the right hashes for three headers of the real chain (the same as tests/test_rx.py). Run it with the python of a
fresh environment the wheel was installed into, from a folder outside the repository."""
import os
import sys

from polarka_agent import __version__, rx

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


lib = rx.find_library()
inside = os.path.join(os.path.dirname(rx.__file__), "lib")
assert lib and os.path.dirname(lib) == inside, "the library found is %r, not the one in %s" % (lib, inside)
engine = rx.engine()
for height, header, seed, shown in REAL:
    header = bytes.fromhex(header)
    engine.ensure(bytes(32) if seed is None else bytes.fromhex(seed)[::-1])
    vm = engine.rx.vm()
    try:
        proof = engine.rx.hash(vm, header)
    finally:
        engine.rx.free(vm)
    assert proof[::-1].hex() == shown, "block %d: %s" % (height, proof[::-1].hex())
print("ok polarka-agent %s, library %s, %d real headers" % (__version__, lib, len(REAL)))
