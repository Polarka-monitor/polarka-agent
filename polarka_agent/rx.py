"""The proof of work of TrueNorth for this program: RandomX with the chain's salt, as it stands in the
TrueNorth sources, behind a small C library (native/rxnorth.cpp).

The packages for Linux (x86-64, arm64) and macOS (arm64) carry the library in polarka_agent/lib, so there
`pip install polarka-agent` is all that mining needs. Elsewhere, or to use one's own build, `polarka-agent
build-rx --randomx DIR` builds it from the folder src/randomx of the TrueNorth sources and puts it into
~/.polarka-agent/lib. Without it mining and the pool do not start, and say why. The wallet never needs it.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading

EPOCH, LAG = 2048, 64       # the key changes every 2048 blocks, 64 blocks after the block it is taken from
NAME = "librxnorth.dylib" if sys.platform == "darwin" else "librxnorth.so"
HERE = os.path.dirname(os.path.abspath(__file__))
HOME_LIB = os.path.join(os.path.expanduser("~"), ".polarka-agent", "lib", NAME)
MOST = (1 << 256) - 1


class NoLibrary(RuntimeError):
    """The library is not there or does not load."""


def seed_height(next_height):
    """The height of the block whose hash is the key for the block at next_height."""
    return 0 if next_height < EPOCH + LAG else ((next_height - LAG) // EPOCH) * EPOCH


def target_of(bits):
    """The target a header's nBits stand for, as a number."""
    exponent, mantissa = bits >> 24, bits & 0x007FFFFF
    return mantissa >> (8 * (3 - exponent)) if exponent <= 3 else mantissa << (8 * (exponent - 3))


def find_library():
    for path in (os.environ.get("POLARKA_RX_LIB"), os.path.join(HERE, "lib", NAME), HOME_LIB, os.path.join(HERE, "native", "build", NAME)):
        if path and os.path.isfile(path):
            return path
    return None


class Rx:
    """The library's calls. One key is prepared at a time, for the whole process."""

    def __init__(self, path=None):
        path = path or find_library()
        if not path:
            raise NoLibrary("the proof-of-work library is not built: run `polarka-agent build-rx --randomx DIR`, "
                            "where DIR is src/randomx of the TrueNorth sources")
        try:
            self.lib = ctypes.CDLL(path)
        except OSError as e:
            raise NoLibrary("the proof-of-work library at %s does not load: %s" % (path, e))
        self.path = path
        self.lib.rxn_prepare.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        self.lib.rxn_vm_new.restype = ctypes.c_void_p
        self.lib.rxn_vm_free.argtypes = [ctypes.c_void_p]
        self.lib.rxn_hash.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        self.lib.rxn_scan.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p]

    def prepare(self, key, fast=False, threads=1):
        """key: the seed block's hash in the byte order of a header, or 32 zero bytes before the first epoch.
        fast builds the 2 GiB table that mining needs; without it a hash takes some 20 ms, enough to check one."""
        if len(key) != 32:
            raise ValueError("a key is 32 bytes")
        code = self.lib.rxn_prepare(key, 1 if fast else 0, max(1, int(threads)))
        if code:
            raise MemoryError("the proof of work could not be prepared (%d); the fast way needs 2 GiB of memory" % code)

    def vm(self):
        vm = self.lib.rxn_vm_new()
        if not vm:
            raise RuntimeError("nothing is prepared")
        return vm

    def free(self, vm):
        self.lib.rxn_vm_free(vm)

    def hash(self, vm, header80):
        if len(header80) != 80:
            raise ValueError("a header is 80 bytes")
        out = ctypes.create_string_buffer(32)
        self.lib.rxn_hash(vm, header80, out)
        return out.raw

    def scan(self, vm, header76, target, start, step, count):
        """(nonce, hash) of the first of `count` nonces start, start + step, ... whose hash is at or below the
        target, or None."""
        if len(header76) != 76:
            raise ValueError("a header without its nonce is 76 bytes")
        nonce, out = ctypes.c_uint32(0), ctypes.create_string_buffer(32)
        found = self.lib.rxn_scan(vm, header76, min(target, MOST).to_bytes(32, "little"), start & 0xFFFFFFFF, step, count,
                                  ctypes.byref(nonce), out)
        return (nonce.value, out.raw) if found else None


class Engine:
    """The one prepared key of this process. Whoever holds a machine from an earlier key must stop using it
    before ensure() prepares another: the caller sees that from `generation`."""

    def __init__(self, path=None):
        self.rx = Rx(path)
        self.lock = threading.Lock()
        self.key, self.fast, self.generation = None, None, 0

    def ensure(self, key, fast=False, threads=1):
        """Prepares the key unless it is the one prepared. A fast table also serves who asks for the slow way."""
        with self.lock:
            if key == self.key and (self.fast or not fast):
                return self.generation
            self.rx.prepare(key, fast, threads)
            self.key, self.fast = key, fast
            self.generation += 1
            return self.generation


_engine = None
_engine_lock = threading.Lock()


def engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        return _engine


def build(randomx_dir, cmake=None, out=HOME_LIB, say=lambda line: None):
    """Builds the library from native/ and RandomX in randomx_dir, and copies it to `out`. Returns the path."""
    randomx_dir = os.path.abspath(os.path.expanduser(randomx_dir))
    if not os.path.isfile(os.path.join(randomx_dir, "src", "randomx.h")):
        raise NoLibrary("%s is not src/randomx of the TrueNorth sources (no src/randomx.h in it)" % randomx_dir)
    cmake = os.path.expanduser(cmake) if cmake else shutil.which("cmake")
    if not cmake or not os.access(cmake, os.X_OK):
        raise NoLibrary("cmake is needed to build the library; name it with --cmake if it is not on the path")
    work = tempfile.mkdtemp(prefix="polarka-rx-build-")
    try:
        configure = [cmake, "-S", os.path.join(HERE, "native"), "-B", work, "-DRANDOMX_DIR=" + randomx_dir]
        ninja = os.path.join(os.path.dirname(cmake), "ninja")
        ninja = ninja if os.access(ninja, os.X_OK) else shutil.which("ninja")
        if ninja:
            configure += ["-G", "Ninja", "-DCMAKE_MAKE_PROGRAM=" + ninja]
        for step in (configure, [cmake, "--build", work, "--target", "rxnorth"]):
            say(" ".join(step))
            done = subprocess.run(step, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            if done.returncode:
                raise NoLibrary("the build failed:\n" + done.stdout[-2000:])
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copyfile(os.path.join(work, NAME), out + ".new")
        os.replace(out + ".new", out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    Rx(out)         # it must load, or the build is not done
    return out
