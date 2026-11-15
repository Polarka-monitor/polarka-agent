"""What the tests of the wallet stand on: a TrueNorth Core node on regtest and the stand-in for a Northbound
wallet server in front of it (tests/standin, copied from the Polarka Wallet's repository). No real network is
touched. TN_BIN names the folder with truenorthd and truenorth-cli; without it ./bin and the cold address
tool's own folder are tried."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def find_bin():
    for d in (os.environ.get("TN_BIN"), os.path.join(os.path.dirname(HERE), "bin"), os.path.expanduser("~/severka-cold/bin")):
        if d and os.access(os.path.join(d, "truenorthd"), os.X_OK) and os.access(os.path.join(d, "truenorth-cli"), os.X_OK):
            return d
    return None


class Node:
    def __init__(self, bindir):
        self.bin, self.d = bindir, tempfile.mkdtemp(prefix="polarka-agent-node-")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.p = subprocess.Popen([bindir + "/truenorthd", "-datadir=" + self.d, "-chain=regtest", "-listen=0", "-connect=0", "-dnsseed=0",
                                   "-fixedseeds=0", "-server=1", "-rpcport=%d" % self.port, "-printtoconsole=0"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):
            try:
                self.cli("getblockcount")
                return
            except Exception:
                time.sleep(0.5)
        self.stop()
        raise RuntimeError("the test node did not start")

    @property
    def cookie(self):
        return os.path.join(self.d, "regtest", ".cookie")

    def cli(self, *a):
        r = subprocess.run([self.bin + "/truenorth-cli", "-datadir=" + self.d, "-chain=regtest", "-rpcport=%d" % self.port] + [str(x) for x in a],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=600)
        if r.returncode:
            raise RuntimeError(r.stderr.strip()[:700])
        try:
            return json.loads(r.stdout)
        except ValueError:
            return r.stdout.strip()

    def mine(self, blocks, address):
        return self.cli("generatetoaddress", blocks, address)

    def stop(self):
        try:
            self.cli("stop")
        except Exception:
            pass
        try:
            self.p.wait(timeout=40)
        except Exception:
            self.p.kill()
        shutil.rmtree(self.d, ignore_errors=True)


class StandIn:
    def __init__(self, node):
        script = os.path.join(HERE, "standin", "northbound_standin.py")
        self.p = subprocess.Popen([sys.executable, "-B", script, "--rpc-url", "http://127.0.0.1:%d" % node.port, "--cookie", node.cookie,
                                   "--port", "0", "--exit-with-stdin"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True)
        line = self.p.stdout.readline()
        if not line.startswith("listening "):
            self.stop()
            raise RuntimeError("the stand-in server did not start: %r" % line)
        self.url = "http://127.0.0.1:%d" % int(line.split()[1])

    def set(self, **switch):
        """The stand-in's switches: stale, busy, hold, refuse, hide (see its own first lines)."""
        req = urllib.request.Request(self.url + "/__standin/set", data=json.dumps(switch).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def stop(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()
        self.p.stdout.close()
