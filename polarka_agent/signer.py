"""The signing service: the wallet held by a user of the system of its own, which an agent asks over a local socket.

Without it the words are in wallet.json and the limits in limits.json, in a folder of the agent's own user: an agent
that can run commands as that user (most can) can read the words and pay past every limit, or write limits of its
own (second review, M4; first audit, F4). With the service the folder is the service user's, limits.json is root's,
and the agent's user can only ask:

  polarka-agent --dir FOLDER signer-serve --socket PATH --group GROUP     the service, run as its own user
  polarka-agent --signer PATH balance | pay | fetch | mcp | ...            the agent, as a user in GROUP
  polarka-agent signer-setup [--check]                                     the commands that set it up; a look at it

The service is the wallet of wallet.py and nothing more: the same journal, the same limits, the same payment shown
again rather than signed anew. It is asked what an agent's tools need: what the wallet holds, a fresh address, what
an address received, the payments and the history, the limits, to pay (for x402 also to sign a payment that the
seller hands over), to stamp, and to show a payment once more. The words, limits.json and forgetting a payment stay
with the owner, at the service's folder.

It keeps nothing from an agent whose user can become root or the service's user (sudo without a password, or root
itself), nor on a machine that someone else took over: then what the wallet holds is at stake. Keep little in it.
"""
import grp
import json
import os
import re
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

from . import __version__, keys
from .server import ServerError
from .wallet import Refused, Wallet, units

PROTOCOL = 1
MAX_REQUEST = 65536                 # bytes in one request
MAX_ANSWER = 32 << 20               # bytes in one answer: the payments of a wallet used for long
READ_TIME = 5                       # seconds a caller has to send its request in
ANSWER_TIME = 330                   # seconds a caller waits: a payment may wait three times for the wallet server
MOST_AT_ONCE = 8                    # callers served at once; one more is told that the service is busy
MOST_OF_ONE = 4                     # of them from one user of the system (review 10, L4)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
OPS = ("info", "balance", "address", "first_address", "received", "history", "payments", "limits", "pay", "stamp",
       "hand_again")


def _to_stderr(line):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


# ------------------------------------------------------------------------------------------------ the service

def _only(args, *names):
    extra = sorted(str(n)[:40] for n in set(args) - set(names))
    if extra:
        raise Refused("INPUT", "the service takes no %s here" % ", ".join(extra[:5]))


def _flag(args, name, default=False):
    value = args.get(name, default)
    if type(value) is not bool:
        raise Refused("INPUT", "%s is true or false" % name)
    return value


def _hex64(args, name, kind):
    value = args.get(name)
    if not isinstance(value, str) or not HEX64.match(value):
        raise Refused(kind, "%s is 64 hex characters" % name)
    return value


class Service:
    """What the service answers, apart from its socket. One request at a time: the wallet takes one program at a time
    (this lock within the process, and the wallet's own lock on its folder against other processes)."""

    def __init__(self, wallet, log=None):
        self.wallet, self._lock = wallet, threading.Lock()
        self.log = log if log is not None else _to_stderr

    def facts(self):
        """What the agent's side may check of the service: its chain, and that the owner's files are kept apart."""
        folder = self.wallet.folder
        try:
            st = os.stat(os.path.join(folder, "limits.json"))
            limits = {"owner_is_root": st.st_uid == 0, "others_may_write": bool(st.st_mode & 0o022)}
        except FileNotFoundError:
            limits = None
        return {"protocol": PROTOCOL, "version": __version__, "chain": self.wallet.chain,
                "folder_private": not os.stat(folder).st_mode & 0o077, "limits_file": limits,
                # what runs: --check looks at who may change these (review 10, M3)
                "program": {"python": sys.executable, "module": os.path.dirname(os.path.abspath(__file__)), "path": list(sys.path)}}

    def do(self, op, args):
        w = self.wallet
        plain = {"info": self.facts, "balance": w.balance, "address": w.fresh_address, "payments": w.payments,
                 "limits": w.limits_shown, "first_address": lambda: w.account.address(0, 0)}
        if op in plain:
            _only(args)
            return plain[op]()
        if op == "received":
            _only(args, "address")
            if not isinstance(args.get("address"), str) or len(args["address"]) > 100:
                raise Refused("INPUT", "address is a text")
            return w.received(args["address"])
        if op == "history":
            _only(args, "most")
            most = args.get("most", 200)
            if type(most) is not int or not 1 <= most <= 200:
                raise Refused("INPUT", "most is 1 to 200")
            return w.history(most)
        if op == "pay":
            _only(args, "outputs", "memo", "dry_run", "hand")
            outputs = args.get("outputs")
            if type(outputs) is not list or not 1 <= len(outputs) <= 100 or not all(
                    type(o) is list and len(o) == 2 and isinstance(o[0], str) and len(o[0]) <= 100 and type(o[1]) is int
                    and 0 < o[1] < 1 << 62 for o in outputs):
                raise Refused("INPUT", "outputs are up to a hundred pairs [address, amount in base units]")
            # hand false: signed and written down, not shown to the wallet server; the answer carries it for x402
            return w.pay([(a, v) for a, v in outputs], args.get("memo"), _flag(args, "dry_run"), _flag(args, "hand", True))
        if op == "stamp":
            _only(args, "digest", "memo", "dry_run")
            return w.stamp(bytes.fromhex(_hex64(args, "digest", "DIGEST")), args.get("memo"), _flag(args, "dry_run"))
        if op == "hand_again":
            _only(args, "id")
            return w.hand_again(_hex64(args, "id", "INPUT"))
        raise Refused("NO_SUCH", "the service does not do this")

    def answer(self, data, peer=None):
        """The answer to one request: the bytes of its line, as they came."""
        t0, op = time.monotonic(), "-"
        try:
            try:
                request = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise Refused("INPUT", "a request is one JSON object on one line")
            if type(request) is not dict or request.get("v") != PROTOCOL or type(request.get("args", {})) is not dict:
                raise Refused("PROTOCOL", "this service speaks protocol %d" % PROTOCOL)
            if request.get("op") not in OPS:
                raise Refused("NO_SUCH", "the service does not do this")
            op = request["op"]
            with self._lock:
                told, said = {"v": PROTOCOL, "ok": self.do(op, request.get("args", {}))}, "ok"
        except Refused as e:
            told, said = {"v": PROTOCOL, "refused": [e.kind, str(e)]}, "refused %s" % e.kind
        except keys.BadInput as e:
            told, said = {"v": PROTOCOL, "refused": ["INPUT", str(e)]}, "refused INPUT"
        except ServerError as e:
            told, said = {"v": PROTOCOL, "server": [e.status, e.kind, str(e)]}, "server %s %s" % (e.status, e.kind)
        except Exception as e:      # nothing of the wallet's inside goes out with an unexpected failure
            told, said = {"v": PROTOCOL, "failed": type(e).__name__}, "failed %s" % type(e).__name__
        self.log("%s %s %s%s %.0f ms" % (time.strftime("%Y-%m-%d %H:%M:%S"), op, said, "" if peer is None else " uid %d" % peer,
                                         (time.monotonic() - t0) * 1000))
        return told


def peer_uid(conn):
    """The user of the program at the other end of the socket, as the system tells it, for the log; None elsewhere."""
    try:
        if hasattr(socket, "SO_PEERCRED"):                  # Linux: pid, uid, gid
            return struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
        if sys.platform == "darwin":                        # macOS: LOCAL_PEERCRED gives a struct xucred
            return struct.unpack_from("=I", conn.getsockopt(0, 1, 76), 4)[0]
    except (OSError, struct.error):
        pass
    return None


class Listening:
    """The service at its socket: serve_forever() until close(), or run() until the process is told to end."""

    def __init__(self, service, listener, path, most=MOST_AT_ONCE, most_of_one=MOST_OF_ONE):
        self.service, self.listener, self.path = service, listener, path
        self._slots, self._closed = threading.BoundedSemaphore(most), threading.Event()
        self._most_of_one, self._of = most_of_one, {}       # user of the system -> callers of it being served
        self._of_lock = threading.Lock()
        self._inode = os.stat(path).st_ino
        listener.settimeout(0.5)            # so that close() is seen within half a second

    def serve_forever(self):
        while not self._closed.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._closed.is_set():
                    break
                time.sleep(0.2)             # too many files open, for one: a moment later it may be better
                continue
            uid = peer_uid(conn)
            with self._of_lock:
                if self._of.get(uid, 0) >= self._most_of_one:
                    uid = False             # one user holds as many places as one may: the others stay free
                else:
                    self._of[uid] = self._of.get(uid, 0) + 1
            if uid is False or not self._slots.acquire(blocking=False):
                if uid is not False:
                    self._done(uid)
                self._busy(conn)
                continue
            try:
                threading.Thread(target=self._one, args=(conn, uid), daemon=True).start()
            except RuntimeError:            # no thread to be had: the caller is told, and nothing is held
                self._slots.release()
                self._done(uid)
                self._busy(conn)

    def _done(self, uid):
        with self._of_lock:
            left = self._of.get(uid, 1) - 1
            if left > 0:
                self._of[uid] = left
            else:
                self._of.pop(uid, None)

    def _send(self, conn, told):
        try:
            text = json.dumps(told, separators=(",", ":"))
        except (TypeError, ValueError):
            text = json.dumps({"v": PROTOCOL, "failed": "TypeError"})
        conn.settimeout(30)
        conn.sendall(text.encode() + b"\n")

    def _busy(self, conn):
        try:
            conn.settimeout(1)
            self._send(conn, {"v": PROTOCOL, "failed": "BUSY"})
        except OSError:
            pass
        finally:
            conn.close()

    def _one(self, conn, uid=None):
        try:
            # one line within READ_TIME, whatever pace it comes at: a caller that holds the line holds nothing long
            until, data = time.monotonic() + READ_TIME, b""
            while b"\n" not in data and len(data) <= MAX_REQUEST:
                left = until - time.monotonic()
                if left <= 0:
                    return
                conn.settimeout(left)
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            line = data.split(b"\n", 1)[0]
            if len(line) > MAX_REQUEST:
                self._send(conn, {"v": PROTOCOL, "refused": ["INPUT", "a request is at most %d bytes" % MAX_REQUEST]})
            else:
                self._send(conn, self.service.answer(line, uid))
        except OSError:
            pass
        finally:
            conn.close()
            self._slots.release()
            self._done(uid)

    def close(self):
        self._closed.set()
        try:
            if os.lstat(self.path).st_ino == self._inode:
                os.unlink(self.path)        # the socket this service made, and no other
        except OSError:
            pass
        try:
            self.listener.close()
        except OSError:
            pass

    def run(self):
        """Serves until SIGTERM or SIGINT, then takes its socket away."""
        def end(*a):
            raise SystemExit(0)
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, end)
        try:
            self.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()
        return 0


def bind(path, group=None):
    """The socket at path, 600 from the first moment, then 660 for the group if one is named. Its folder may be
    written by the service's user or root alone: no other program can put a socket of its own there in its place."""
    path = os.path.abspath(path)
    if len(path.encode()) > 100:
        raise Refused("SOCKET", "the socket's path is longer than a socket's path may be (100 bytes)")
    where = os.path.dirname(path)
    try:
        st = os.stat(where)
    except FileNotFoundError:
        raise Refused("SOCKET", "there is no folder %s for the socket" % where)
    if st.st_mode & 0o022 or st.st_uid not in (0, os.geteuid()):
        raise Refused("SOCKET", "the socket's folder %s may be written by others than the service's user: another program "
                                "could stand in for the service there" % where)
    gid = None
    if group is not None:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            raise Refused("GROUP", "there is no group %s" % group)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(st.st_mode):
            raise Refused("SOCKET", "%s is there and is not a socket; it is left as it is" % path)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(2)
            probe.connect(path)
        except ConnectionRefusedError:
            os.unlink(path)                 # left behind by a service that ended
        except OSError as e:
            raise Refused("SOCKET", "%s is there and cannot be asked (%s); it is left as it is" % (path, type(e).__name__))
        else:
            raise Refused("SOCKET", "another service answers at %s" % path)
        finally:
            probe.close()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        listener.bind(path)
    except OSError:
        listener.close()
        raise
    finally:
        os.umask(old)
    if gid is not None:
        os.chown(path, -1, gid)
        os.chmod(path, 0o660)
    listener.listen(16)
    return listener


def start(folder, path, group=None, most=MOST_AT_ONCE, log=None):
    """The service for the wallet in folder, at its socket. It refuses to begin where it would keep nothing apart:
    as root, with a folder that others may open, or with a limits.json that others may write."""
    if os.geteuid() == 0:
        raise Refused("ROOT", "the service runs as a user of its own, not as root")
    folder = os.path.abspath(folder)
    try:
        st = os.stat(folder)
    except FileNotFoundError:
        raise Refused("NO_WALLET", "there is no folder %s" % folder)
    if st.st_mode & 0o077:
        raise Refused("OPEN_FOLDER", "the service's folder %s may be opened by others than its user; set its mode to 700" % folder)
    service = Service(Wallet(folder), log)
    limits = service.facts()["limits_file"]
    if limits is not None and limits["others_may_write"]:
        raise Refused("OPEN_LIMITS", "limits.json may be written by others than its owner; set its mode to 640")
    return Listening(service, bind(path, group), os.path.abspath(path), most)


# ------------------------------------------------------------------------------------------------ the agent's side

class Signer:
    """The wallet of a signing service, used as a Wallet is by the command line, the MCP server, x402 and postage:
    each method a request to the service. folder is this program's own folder, where x402 keeps its book of
    payments under way; the wallet's words are not in it. When the service does not answer, nothing is paid."""

    def __init__(self, path, folder, timeout=ANSWER_TIME):
        self.path, self.folder = os.path.abspath(os.path.expanduser(path)), os.path.abspath(folder)
        self.timeout, self._chain = timeout, None

    def _ask(self, op, **args):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(10)
            try:
                s.connect(self.path)
            except PermissionError:
                raise Refused("SIGNER", "this user may not ask the signing service at %s (is it in the service's group?); "
                                        "nothing is paid" % self.path)
            except (FileNotFoundError, ConnectionRefusedError):
                raise Refused("SIGNER", "the signing service does not answer at %s; nothing is paid" % self.path)
            except socket.timeout:
                raise Refused("SIGNER_BUSY", "the signing service takes no more calls just now; ask again in a moment")
            got, until = b"", time.monotonic() + self.timeout
            s.sendall(json.dumps({"v": PROTOCOL, "op": op, "args": args}, separators=(",", ":")).encode() + b"\n")
            while not got.endswith(b"\n"):
                left = until - time.monotonic()
                if left <= 0:
                    raise socket.timeout()
                s.settimeout(left)
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                got += chunk
                if len(got) > MAX_ANSWER:
                    raise Refused("SIGNER", "the signing service answered more than %d bytes" % MAX_ANSWER)
        except socket.timeout:
            raise Refused("SIGNER", "the signing service did not answer within %d seconds. A payment that was asked for may "
                                    "have been made: look at the payments before asking again" % self.timeout)
        except OSError as e:
            raise Refused("SIGNER", "the signing service at %s cannot be asked (%s)" % (self.path, type(e).__name__))
        finally:
            s.close()
        try:
            told = json.loads(got.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            told = None
        if type(told) is not dict or told.get("v") != PROTOCOL:
            raise Refused("SIGNER", "the signing service answered something this program does not understand")
        if "ok" in told:
            return told["ok"]
        refused, server, failed = told.get("refused"), told.get("server"), told.get("failed")
        if type(refused) is list and len(refused) == 2 and all(isinstance(x, str) for x in refused):
            raise Refused(refused[0][:40], refused[1][:2000])
        if type(server) is list and len(server) == 3 and type(server[0]) is int and isinstance(server[2], str) \
                and (server[1] is None or isinstance(server[1], str)):
            raise ServerError(server[0], server[2][:2000], server[1])
        if failed == "BUSY":
            raise Refused("SIGNER_BUSY", "the signing service is busy; ask again in a moment")
        if isinstance(failed, str):
            raise Refused("SIGNER_FAILED", "the signing service failed (%s)" % failed[:60])
        raise Refused("SIGNER", "the signing service answered something this program does not understand")

    @property
    def chain(self):
        if self._chain is None:
            facts = self.info()
            if type(facts) is not dict or facts.get("chain") not in keys.CHAINS:
                raise Refused("SIGNER", "the signing service names no chain this program knows")
            self._chain = facts["chain"]
        return self._chain

    def info(self):
        return self._ask("info")

    def balance(self):
        return self._ask("balance")

    def fresh_address(self):
        return self._ask("address")

    def first_address(self):
        """The wallet's first address, the one `start` mines to and asks the faucet for."""
        return self._ask("first_address")

    def received(self, address):
        return self._ask("received", address=address)

    def history(self, most=200):
        return self._ask("history", most=most)

    def payments(self):
        return self._ask("payments")

    def limits_shown(self):
        return self._ask("limits")

    def pay(self, outputs, memo=None, dry_run=False, hand=True):
        return self._ask("pay", outputs=[[a, v] for a, v in outputs], memo=memo, dry_run=dry_run, hand=hand)

    def stamp(self, digest, memo=None, dry_run=False):
        if not isinstance(digest, (bytes, bytearray)) or len(digest) != 32:
            raise Refused("DIGEST", "a stamp takes a SHA-256, 32 bytes")
        return self._ask("stamp", digest=bytes(digest).hex(), memo=memo, dry_run=dry_run)

    def hand_again(self, txid):
        return self._ask("hand_again", id=txid)

    def forget(self, txid):
        raise Refused("OWNER", "forgetting a payment is its owner's: run forget as the service's user, on the service's "
                               "folder (polarka-agent signer-setup shows where it is)")



# ------------------------------------------------------------------------------------------------ setting it up

LINUX = {"system": "linux", "venv": "/opt/polarka-signer/venv", "dir": "/var/lib/polarka-signer",
         "socket": "/run/polarka-signer/signer.sock", "user": "polarka-signer", "group": "polarka-pay"}
MACOS = {"system": "macos", "venv": "/Library/Application Support/PolarkaSigner/venv",
         "dir": "/Library/Application Support/PolarkaSigner/wallet",
         "socket": "/Library/Application Support/PolarkaSigner/run/signer.sock", "user": "_polarkasigner", "group": "polarka-pay"}
# groups whose members can make themselves root (with a password, or without one: docker, lxd, disk ...), or read
# what only root may (shadow); sudo rules of a user's own are asked of sudo (review 10, N5)
ADMIN_GROUPS = ("root", "wheel", "sudo", "admin", "docker", "lxd", "incus", "incus-admin", "disk", "libvirt", "shadow")
PACKAGE = re.compile(r"^polarka-agent==[0-9]+(\.[0-9]+){1,3}((\.dev|rc|a|b|\.post)[0-9]+)?$")


def places(system=None):
    """Where signer-setup puts the service, on this system or on "linux" or "macos"."""
    system = system or ("macos" if sys.platform == "darwin" else "linux")
    return dict(MACOS if system == "macos" else LINUX)


def groups_of(user):
    """The names of the groups a user of this machine is in, its own group with them; KeyError when there is no
    such user here."""
    import pwd
    entry = pwd.getpwnam(user)
    names = set()
    for gid in os.getgrouplist(user, entry.pw_gid):
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return names


def this_user():
    """The user this program runs as, by its id (not by what the environment says: review 10, L6)."""
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


UNIT = """[Unit]
Description=Polarka signing service: a TrueNorth wallet that agents ask to pay, within its owner's limits
After=network-online.target
Wants=network-online.target

[Service]
User=polarka-signer
Group=polarka-signer
SupplementaryGroups=polarka-pay
ExecStart=/opt/polarka-signer/venv/bin/python3 -I -B -m polarka_agent --dir /var/lib/polarka-signer signer-serve --socket /run/polarka-signer/signer.sock --group polarka-pay
RuntimeDirectory=polarka-signer
RuntimeDirectoryMode=0755
UMask=0077
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/polarka-signer
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
"""

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.polarka.signer</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Library/Application Support/PolarkaSigner/venv/bin/python3</string>
    <string>-I</string><string>-B</string><string>-m</string><string>polarka_agent</string>
    <string>--dir</string><string>/Library/Application Support/PolarkaSigner/wallet</string>
    <string>signer-serve</string>
    <string>--socket</string><string>/Library/Application Support/PolarkaSigner/run/signer.sock</string>
    <string>--group</string><string>polarka-pay</string>
  </array>
  <key>UserName</key><string>_polarkasigner</string>
  <key>GroupName</key><string>_polarkasigner</string>
  <key>InitGroups</key><true/>
  <key>Umask</key><integer>63</integer>
  <key>WorkingDirectory</key><string>/var/empty</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardErrorPath</key><string>/Library/Logs/PolarkaSigner/signer.log</string>
</dict>
</plist>
"""

# The pieces of the scripts. Pieces are put together first, then every @NAME@ is filled in one pass, so that nothing
# filled in (a folder's name, say) is read again as a piece (review 10, L2).

CLEAN = """# Nothing of the account that ran sudo reaches what runs as root (review 10, H3): the commands come from the
# system's own folders, and every Python and pip below runs through clean, in an empty environment, Python with -I
# and pip with --isolated. The script is read once, as it runs, and never again from a file, so that it may be piped
# too (review 10, N1). Behind a proxy, pip finds it in root's own settings only: /etc/pip.conf, on macOS
# /Library/Application Support/pip/pip.conf.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
clean() { /usr/bin/env -i HOME=@ROOT_HOME@ PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C "$@"; }
cd /
"""

ADMIN_CHECK = """# The agent's user must not be able to administer this machine: an agent that runs as such a user can wait for
# its owner's password, and then nothing is kept from it (review 10, M4). Its groups are looked at, and what sudo
# lets it do (review 10, N5).
id "$AGENT_USER" >/dev/null 2>&1 || { echo "there is no user $AGENT_USER on this machine" >&2; exit 1; }
admin=""
for g in $(id -Gn "$AGENT_USER"); do
  case " @ADMIN_GROUPS@ " in
    *" $g "*) admin="it is in the group $g" ;;
  esac
done
if [ -z "$admin" ] && command -v sudo >/dev/null 2>&1; then
  LC_ALL=C sudo -n -l -U "$AGENT_USER" 2>&1 | grep -q "is not allowed to run sudo" || admin="sudo lets it run commands"
fi
if [ -n "$admin" ] && [ @ADMIN_AGENT@ != yes ]; then
  echo "$AGENT_USER can administer this machine ($admin). Run the agent as a user of its own, or print this script with --admin-agent, knowing that it then keeps from the agent only the simple ways in." >&2
  exit 1
fi
"""

FROM_PYPI = """pip_in install --quiet --upgrade pip
pip_in install --quiet --upgrade "$PACKAGE"
"""

FROM_WHEEL = """# the wheel named when this script was printed, used only if its SHA-256 is still the one written here. It is read
# only as a plain file, at most 64 MiB of it, into a copy only root can read until it is checked (review 10, N2).
WHEEL=@WHEEL@
WHEEL_SHA256=@WHEEL_SHA256@
install -d -o root -g @ROOT_GROUP@ -m 755 "$BASE/dist"
COPY="$BASE/dist/$(basename "$WHEEL")"
rm -f "$COPY"
{ [ -f "$WHEEL" ] && [ ! -L "$WHEEL" ]; } || { echo "$WHEEL is not a plain file" >&2; exit 1; }
(umask 077; head -c 67108865 < "$WHEEL" > "$COPY")
if [ "$(@SHA256@ "$COPY" | cut -d ' ' -f 1)" != "$WHEEL_SHA256" ]; then
  rm -f "$COPY"; echo "$WHEEL is not the wheel this script was printed for" >&2; exit 1
fi
chmod 644 "$COPY"
pip_in install --quiet --upgrade pip
pip_in install --quiet --no-deps --force-reinstall "$COPY"
"""

LINUX_SCRIPT = """#!/bin/sh
# The Polarka signing service on Linux with systemd, as polarka-agent @VERSION@ signer-setup printed it.
# Read it, then run it as root, from an account the agent does not use: sudo sh polarka-signer-setup.sh
# It makes the user polarka-signer and the group polarka-pay, with @AGENT@ in that group; installs @PACKAGE@ where
# only root can change it; makes the wallet's folder, which only the service can open; and starts the service.
# Stop the agents that use the wallet before you run it.@ADMIN_NOTE@
set -eu
@CLEAN@
[ "$(id -u)" = 0 ] || { echo "run this as root: sudo sh FILE" >&2; exit 1; }
AGENT_USER=@AGENT@
PACKAGE=@PACKAGE@
BASE=/opt/polarka-signer
DIR=/var/lib/polarka-signer
pip_in() { clean "$BASE/venv/bin/python3" -I -m pip --isolated --disable-pip-version-check --no-input --no-cache-dir "$@"; }

@ADMIN_CHECK@
# 1. the service's user, and the group whose members may ask it to pay
getent group polarka-pay >/dev/null || groupadd --system polarka-pay
id -u polarka-signer >/dev/null 2>&1 || useradd --system --user-group --home-dir "$DIR" --no-create-home --shell /usr/sbin/nologin polarka-signer
usermod -a -G polarka-pay polarka-signer
usermod -a -G polarka-pay "$AGENT_USER"

# 2. the program, where only root can change it: the service runs it with the wallet's words
clean /usr/bin/python3 -I -c "import ensurepip" 2>/dev/null || { echo "python3-venv is needed (Debian, Ubuntu: apt install python3-venv)" >&2; exit 1; }
install -d -o root -g root -m 755 "$BASE"
[ -x "$BASE/venv/bin/python3" ] || clean /usr/bin/python3 -I -m venv "$BASE/venv"
@INSTALL@chown -R root:root "$BASE"
chmod -R go-w "$BASE"

# 3. the wallet's folder, which only the service's user can open
install -d -o polarka-signer -g polarka-signer -m 700 "$DIR"

@WALLET@
@LIMITS@
# 6. the service
cat > /etc/systemd/system/polarka-signer.service <<'UNIT'
@UNIT@UNIT
systemctl daemon-reload
systemctl enable polarka-signer.service
systemctl restart polarka-signer.service
sleep 3
[ -S /run/polarka-signer/signer.sock ] || { journalctl -u polarka-signer.service -n 20 --no-pager >&2; exit 1; }
echo "The service runs. $AGENT_USER may ask it once it logs in again (polarka-pay is new to it)."
echo "Look at it then as $AGENT_USER, with the service's own program, which only root can change:"
echo "  sudo -u $AGENT_USER /opt/polarka-signer/venv/bin/python3 -I -m polarka_agent signer-setup --check"
echo "and give the agent: polarka-agent --signer /run/polarka-signer/signer.sock mcp (or POLARKA_SIGNER=/run/polarka-signer/signer.sock)"
"""

MACOS_SCRIPT = """#!/bin/sh
# The Polarka signing service on macOS, as polarka-agent @VERSION@ signer-setup printed it.
# Read it, then run it with an administrator's password, from an account the agent does not use:
#   sudo sh polarka-signer-setup.sh
# It makes a hidden user _polarkasigner and the group polarka-pay, with @AGENT@ in that group; installs @PACKAGE@
# with Apple's own Python where only root can change it; makes the wallet's folder, which only the service can open;
# and starts the service with launchd. Stop the agents that use the wallet before you run it.@ADMIN_NOTE@
set -eu
@CLEAN@
[ "$(id -u)" = 0 ] || { echo "run this as root: sudo sh FILE" >&2; exit 1; }
AGENT_USER=@AGENT@
PACKAGE=@PACKAGE@
BASE="/Library/Application Support/PolarkaSigner"
DIR="$BASE/wallet"
RUN="$BASE/run"
pip_in() { clean "$BASE/venv/bin/python3" -I -m pip --isolated --disable-pip-version-check --no-input --no-cache-dir "$@"; }

@ADMIN_CHECK@
# 1. a hidden user for the service, and the group whose members may ask it to pay
free_id() {
  i=401
  while [ "$i" -lt 500 ]; do
    if [ -z "$(dscl . -search /Users UniqueID "$i")" ] && [ -z "$(dscl . -search /Groups PrimaryGroupID "$i")" ]; then
      echo "$i"; return 0
    fi
    i=$((i + 1))
  done
  echo "no free id between 401 and 499" >&2; return 1
}
if ! dscl . -read /Groups/_polarkasigner >/dev/null 2>&1; then
  ID=$(free_id)
  dscl . -create /Groups/_polarkasigner
  dscl . -create /Groups/_polarkasigner PrimaryGroupID "$ID"
  dscl . -create /Groups/_polarkasigner RealName "Polarka signing service"
  dscl . -create /Groups/_polarkasigner Password "*"
fi
GID=$(dscl . -read /Groups/_polarkasigner PrimaryGroupID | awk '{print $2}')
if ! dscl . -read /Users/_polarkasigner >/dev/null 2>&1; then
  if [ -z "$(dscl . -search /Users UniqueID "$GID")" ]; then NEW_UID=$GID; else NEW_UID=$(free_id); fi
  dscl . -create /Users/_polarkasigner
  dscl . -create /Users/_polarkasigner UniqueID "$NEW_UID"
  dscl . -create /Users/_polarkasigner PrimaryGroupID "$GID"
  dscl . -create /Users/_polarkasigner UserShell /usr/bin/false
  dscl . -create /Users/_polarkasigner NFSHomeDirectory /var/empty
  dscl . -create /Users/_polarkasigner RealName "Polarka signing service"
  dscl . -create /Users/_polarkasigner Password "*"
  dscl . -create /Users/_polarkasigner IsHidden 1
fi
if ! dscl . -read /Groups/polarka-pay >/dev/null 2>&1; then
  PAY=$(free_id)
  dscl . -create /Groups/polarka-pay
  dscl . -create /Groups/polarka-pay PrimaryGroupID "$PAY"
  dscl . -create /Groups/polarka-pay RealName "May ask the Polarka signing service to pay"
  dscl . -create /Groups/polarka-pay Password "*"
fi
for who in _polarkasigner "$AGENT_USER"; do
  dseditgroup -o checkmember -m "$who" polarka-pay >/dev/null 2>&1 || dseditgroup -o edit -a "$who" -t user polarka-pay
done

# 2. the program, where only root can change it: Apple's own Python, in a folder of root's
clean /usr/bin/python3 -I -c "import sys; assert sys.version_info >= (3, 9)" || { echo "Apple's Python 3 is needed: xcode-select --install" >&2; exit 1; }
install -d -o root -g wheel -m 755 "$BASE"
[ -x "$BASE/venv/bin/python3" ] || clean /usr/bin/python3 -I -m venv "$BASE/venv"
@INSTALL@chown -R root:wheel "$BASE/venv"
chmod -R go-w "$BASE/venv"
[ ! -d "$BASE/dist" ] || { chown -R root:wheel "$BASE/dist"; chmod -R go-w "$BASE/dist"; }

# 3. the wallet's folder, which only the service's user can open; the folder of its socket; its log
install -d -o _polarkasigner -g _polarkasigner -m 700 "$DIR"
install -d -o _polarkasigner -g polarka-pay -m 750 "$RUN"
install -d -o _polarkasigner -g _polarkasigner -m 755 /Library/Logs/PolarkaSigner

@WALLET@
@LIMITS@
# 6. the service
launchctl bootout system/org.polarka.signer 2>/dev/null || true
cat > /Library/LaunchDaemons/org.polarka.signer.plist <<'PLIST'
@PLIST@PLIST
chown root:wheel /Library/LaunchDaemons/org.polarka.signer.plist
chmod 644 /Library/LaunchDaemons/org.polarka.signer.plist
sleep 1
launchctl bootstrap system /Library/LaunchDaemons/org.polarka.signer.plist
sleep 3
[ -S "$RUN/signer.sock" ] || { tail -n 20 /Library/Logs/PolarkaSigner/signer.log >&2; exit 1; }
echo "The service runs. $AGENT_USER may ask it once it logs out and in again (polarka-pay is new to it)."
echo "Look at it then as $AGENT_USER, with the service's own program, which only root can change:"
echo "  sudo -u $AGENT_USER \\"$BASE/venv/bin/python3\\" -I -m polarka_agent signer-setup --check"
echo "and give the agent: polarka-agent --signer \\"$RUN/signer.sock\\" mcp (or POLARKA_SIGNER=\\"$RUN/signer.sock\\")"
"""

NEW_WALLET = """# 4. a new wallet in the service's folder. Its words are shown here, once: write them on paper.
if [ -e "$DIR/wallet.json" ]; then
  echo "$DIR holds a wallet already; it is kept"
else
  clean @AS_SERVICE@ "$BASE/venv/bin/python3" -I -B -m polarka_agent --dir "$DIR" new --chain main
fi
"""

MOVED_WALLET = """# 4. the wallet moved here from @FROM@.
#    An agent that could read it there may know its words. For the whole of the gain, make a new wallet here instead
#    (signer-setup without --move-from) and pay the coins over to it. Its files are read as $AGENT_USER, only plain
#    files and at most 64 MiB of each: no link left there leads root anywhere (review 10, M1, N3).
FROM=@FROM@
[ ! -e "$DIR/wallet.json" ] || { echo "$DIR holds a wallet already; nothing is moved" >&2; exit 1; }
for f in wallet.json journal.jsonl state.json; do
  if [ -L "$FROM/$f" ] || { [ -e "$FROM/$f" ] && [ ! -f "$FROM/$f" ]; }; then
    echo "$FROM/$f is not a plain file; nothing is moved" >&2; exit 1
  fi
done
@AS_AGENT@ test -f "$FROM/wallet.json" || { echo "$FROM has no wallet.json that $AGENT_USER can read" >&2; exit 1; }
for f in wallet.json journal.jsonl state.json; do
  rm -f "$DIR/$f.new"
  if @AS_AGENT@ test -f "$FROM/$f"; then
    @AS_AGENT@ head -c 67108865 "$FROM/$f" > "$DIR/$f.new"
    if [ "$(wc -c < "$DIR/$f.new" | tr -d ' ')" -gt 67108864 ]; then
      rm -f "$DIR/wallet.json.new" "$DIR/journal.jsonl.new" "$DIR/state.json.new"
      echo "$FROM/$f is larger than 64 MiB; nothing is moved" >&2; exit 1
    fi
    chown @USER@:@USER@ "$DIR/$f.new"
    chmod 600 "$DIR/$f.new"
  fi
done
for f in wallet.json journal.jsonl state.json; do
  [ ! -e "$DIR/$f.new" ] || mv -f "$DIR/$f.new" "$DIR/$f"
done
for f in wallet.json journal.jsonl state.json; do
  @AS_AGENT@ rm -f "$FROM/$f"
done
echo "Moved. Its wallet server, as written in it: $(clean "$BASE/venv/bin/python3" -I -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))["server"]))' "$DIR/wallet.json")"
echo "limits.json stays where it was: the agent could change it there. The service's limits are written below."
"""

LIMITS = """# 5. the owner's limits: what the service pays by itself, and no more. The file is root's: the service reads it
#    and nobody else can change it. To change them later: sudo nano "$DIR/limits.json"
printf '%s\\n' @LIMITS_JSON@ > "$DIR/limits.json"
chown root:@USER@ "$DIR/limits.json"
chmod 640 "$DIR/limits.json"
"""

NO_LIMITS = """# 5. the owner's limits: none were given, so none are written. Without limits.json the service pays nothing.
#    Print this script again with --per-payment and --per-day, or write the file as root (owner root, group @USER@, 640).
if [ -e "$DIR/limits.json" ]; then
  chown root:@USER@ "$DIR/limits.json"
  chmod 640 "$DIR/limits.json"
else
  echo "There is no limits.json: the service pays nothing until its owner writes one."
fi
"""


def setup_script(system, agent_user, move_from=None, per_payment=None, per_day=None, package=None, wheel=None,
                 admin_agent=False):
    """The commands that set up the service on this system, for its owner to read and to run as root. Nothing here is
    run: the program only prints them. The program the service runs is polarka-agent of a version from PyPI, or a
    wheel named by its path, installed only if its SHA-256 is still the one it had when this was printed."""
    import hashlib
    p = places(system)
    if not isinstance(agent_user, str) or not NAME.match(agent_user) or agent_user in ("root", p["user"]):
        raise Refused("INPUT", "--agent-user is the name of the user the agent runs as (not root, not the service's user)")
    try:
        admin = sorted(groups_of(agent_user) & set(ADMIN_GROUPS))
    except KeyError:
        admin = []                          # not a user of this machine: the script looks again where it runs
    if admin and not admin_agent:
        raise Refused("ADMIN_AGENT", "%s is in the group %s and can administer this machine: an agent that runs as it can "
                                     "wait for its owner's password, and then nothing is kept from it. Run the agent as a user "
                                     "of its own; or, knowing that, give --admin-agent" % (agent_user, admin[0]))
    if package is not None and wheel is not None:
        raise Refused("INPUT", "--package or --wheel, one of the two")
    if wheel is not None:
        wheel = os.path.abspath(os.path.expanduser(wheel))
        if not wheel.endswith(".whl") or not os.path.isfile(wheel) or any(c in wheel for c in "\n\r@"):
            raise Refused("INPUT", "--wheel is the path of a .whl file")
        with open(wheel, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        install, shown = FROM_WHEEL, wheel
    else:
        package = package or "polarka-agent==%s" % __version__
        if not isinstance(package, str) or not PACKAGE.match(package):
            # no pip options, no paths, no other names: what the service runs is root's alone (review 10, M3)
            raise Refused("INPUT", "--package is polarka-agent==VERSION, from PyPI; a wheel of one's own is named with --wheel")
        install, shown, digest = FROM_PYPI, package, ""
    if (per_payment is None) != (per_day is None):
        raise Refused("INPUT", "--per-payment and --per-day go together")
    if per_payment is not None:
        units(per_payment), units(per_day)
        limits_json = json.dumps({"per_payment_north": per_payment.strip(), "per_day_north": per_day.strip()})
        limits = LIMITS
    else:
        limits_json, limits = "", NO_LIMITS
    if move_from is not None:
        move_from = os.path.abspath(os.path.expanduser(move_from))
        if any(c in move_from for c in "\n\r") or not os.path.isfile(os.path.join(move_from, "wallet.json")):
            raise Refused("NO_WALLET", "there is no wallet in %s" % move_from)
        wallet = MOVED_WALLET
    else:
        wallet = NEW_WALLET
    linux = p["system"] == "linux"
    text = LINUX_SCRIPT if linux else MACOS_SCRIPT
    for name, piece in (("@CLEAN@", CLEAN), ("@ADMIN_CHECK@", ADMIN_CHECK), ("@INSTALL@", install), ("@WALLET@", wallet),
                        ("@LIMITS@", limits), ("@UNIT@", UNIT), ("@PLIST@", PLIST)):
        text = text.replace(name, piece)
    values = {
        "VERSION": __version__, "AGENT": shlex.quote(agent_user), "PACKAGE": shlex.quote(shown),
        "ROOT_HOME": "/root" if linux else "/var/root", "ROOT_GROUP": "root" if linux else "wheel",
        "SHA256": "sha256sum" if linux else "shasum -a 256", "ADMIN_GROUPS": " ".join(ADMIN_GROUPS),
        "ADMIN_AGENT": "yes" if admin_agent else "no",
        "ADMIN_NOTE": "" if not admin_agent else (
            "\n# With --admin-agent: %s may administer this machine. The service then keeps from an agent that runs as it\n"
            "# only the simple ways in (reading the words, writing the limits), not one that waits for its owner's password."
            % agent_user),
        "WHEEL": shlex.quote(wheel or ""), "WHEEL_SHA256": digest, "FROM": shlex.quote(move_from or ""),
        "USER": p["user"], "AS_SERVICE": "runuser -u polarka-signer --" if linux else "sudo -u _polarkasigner",
        "AS_AGENT": 'runuser -u "$AGENT_USER" --' if linux else 'sudo -u "$AGENT_USER"',
        "LIMITS_JSON": shlex.quote(limits_json)}
    return re.sub(r"@([A-Z0-9_]+)@", lambda m: values[m.group(1)], text)


# ------------------------------------------------------------------------------------------------ looking at it

def _changeable(top, most=60000):
    """What under top, top too, a user other than root could change: a file or folder that is not root's, or that its
    group or everyone may write. A link counts for who owns it; where it leads is looked at on its own. Past `most`
    entries it says that it did not look at all of them (review 10, L1)."""
    def bad(path):
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return st.st_uid != 0 or (not stat.S_ISLNK(st.st_mode) and bool(st.st_mode & 0o022))
    if os.path.islink(top) or not os.path.isdir(top):
        return [top] if bad(top) else []
    found, seen = [], 0
    for here, dirs, files in os.walk(top):
        links = [os.path.join(here, d) for d in dirs if os.path.islink(os.path.join(here, d))]
        for path in [here] + links + [os.path.join(here, f) for f in files]:
            seen += 1
            if bad(path):
                found.append(path)
        if len(found) >= 3:
            break
        if seen > most:
            found.append("(more than %d files under %s: not all were looked at)" % (most, top))
            break
    return found


def _hops(path, most=10):
    """path, and each place its links lead through, to what is at the end."""
    hops = [path]
    while os.path.islink(path) and len(hops) < most:
        path = os.path.normpath(os.path.join(os.path.dirname(path), os.readlink(path)))
        hops.append(path)
    return hops


def _holders(path):
    """The folders that hold path, up to /, that a user other than root could change."""
    found, here = [], os.path.dirname(os.path.abspath(path))
    while True:
        try:
            st = os.stat(here)
            if st.st_uid != 0 or st.st_mode & 0o022:
                found.append(here)
        except OSError:
            pass
        if here == os.path.dirname(here):
            return found
        here = os.path.dirname(here)


def _program(venv, ran=None):
    """What a user other than root could change of the program the service runs with the wallet's words: its folder,
    the Python it was made with and that Python's library, and every place its Python takes modules from, polarka_agent
    first (an editable install points elsewhere: review 10, M3). ran: what the service says it runs. None when there is
    no such program here."""
    python = os.path.join(venv, "bin", "python3")
    if not os.path.isfile(python):
        return None
    code = "import json, sys, sysconfig, polarka_agent; print(json.dumps([sysconfig.get_paths()['stdlib'], polarka_agent.__file__] + sys.path))"
    try:
        paths = json.loads(subprocess.run([python, "-I", "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, universal_newlines=True, timeout=30).stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ["(the Python in %s did not say what it runs)" % venv]
    if isinstance(ran, dict):
        paths += [ran.get("python"), ran.get("module")] + list(ran.get("path") or [])
    found = _changeable(venv) + _holders(venv)
    for hop in _hops(python)[1:]:               # the Python the venv was made with, through its links
        found += _changeable(hop) + _holders(hop)
    for path in paths:
        if isinstance(path, str) and path and os.path.exists(path):
            found += _changeable(path) + _holders(path)
    return sorted(set(found))


def _may_sudo():
    """Whether sudo has rules for this user (review 10, N5). It is asked without a password; only its plain "may not
    run sudo" counts as no. No sudo on the machine: no."""
    try:
        said = subprocess.run(["sudo", "-n", "-l"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=10, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "may not run sudo" not in str(said.stdout)


def check(path=None, folder=None, venv=None, system=None):
    """What the agent's user sees of a service that is set up. Each point is ok true or false, or null where there was
    nothing to look at; the whole is ok only when every point is true (limits.json may be missing: then the service
    pays nothing), so that a point not looked at is never taken for one in order (review 10, L1). Run it with the
    service's own Python, which only root can change: a copy of this program that the agent can change could say
    anything."""
    p = places(system)
    path, folder, venv = path or p["socket"], folder or p["dir"], venv or p["venv"]
    points = []

    def point(name, ok, said):
        points.append({"check": name, "ok": ok, "said": said})

    try:
        st = os.stat(path)
    except OSError as e:
        point("socket", False, "%s cannot be looked at (%s): the service does not run, or this user may not see it" % (path, type(e).__name__))
    else:
        if not stat.S_ISSOCK(st.st_mode):
            point("socket", False, "%s is not a socket" % path)
        elif st.st_uid == os.geteuid():
            point("socket", False, "the socket is this user's own: the service runs as the agent's user and keeps nothing apart")
        elif st.st_mode & 0o007:
            point("socket", False, "every user of this machine may ask the service; its socket should be 660, for its group")
        elif st.st_gid != os.getegid() and st.st_gid not in os.getgroups():
            point("socket", False, "this user is not in the socket's group yet; once it is put there, it logs out and in again")
        else:
            point("socket", True, "the socket is another user's, and only its group may ask")
    try:
        facts = Signer(path, ".").info()
        if type(facts) is not dict:
            raise Refused("SIGNER", "the signing service answered something this program does not understand")
    except (Refused, ServerError) as e:
        facts = None
        point("service", False, str(e))
    else:
        point("service", True, "the service answers: polarka-agent %s, chain %s" % (facts.get("version"), facts.get("chain")))
        point("folder", facts.get("folder_private") is True, "the service's folder is its user's alone" if facts.get("folder_private")
              else "others than the service's user may open its folder")
        limits = facts.get("limits_file")
        if limits is None:
            point("limits", None, "there is no limits.json: the service pays nothing until its owner writes one")
        elif type(limits) is dict and limits.get("owner_is_root") is True and limits.get("others_may_write") is False:
            point("limits", True, "limits.json is root's, and only root may change it")
        else:
            point("limits", False, "limits.json should be root's and 640; as it is, others than root may change it")
    if not os.path.lexists(folder):
        point("words", None, "there is no %s to look at (is it elsewhere? --service-dir)" % folder)
    elif os.access(folder, os.R_OK) or os.access(folder, os.X_OK):
        point("words", False, "this user may open %s, where the words are" % folder)
    else:
        point("words", True, "this user cannot open the service's folder, where the words are")
    found = _program(venv, (facts or {}).get("program"))
    if found is None:
        point("program", None, "there is no %s to look at (is it elsewhere? --venv)" % venv)
    elif found:
        point("program", False, "a user other than root could change the program the service runs: %s" % ", ".join(found[:3]))
    else:
        point("program", True, "only root may change the program the service runs, the Python it runs on and where it takes modules from")
    try:
        admin = sorted(groups_of(this_user()) & set(ADMIN_GROUPS))
    except KeyError:
        admin = []
    admin = ["the group %s" % admin[0]] if admin else (["sudo"] if _may_sudo() else [])
    point("admin", not admin, "through %s this user can administer this machine: an agent that runs as it can wait for its "
          "owner's password, and then the service keeps nothing from it" % admin[0] if admin
          else "this user is in no group that can administer this machine, and sudo lets it run nothing")
    try:
        sudo = subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        sudo = False
    point("sudo", not sudo, "this user can become root without a password: then the service keeps nothing from it" if sudo
          else "this user cannot become root without a password")
    ok = facts is not None and all(x["ok"] is True or (x["check"] == "limits" and x["ok"] is None) for x in points)
    return {"ok": ok, "checks": points}
