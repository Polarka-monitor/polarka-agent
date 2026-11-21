# walletd on the server

`polarka_agent/walletd.py` is a wallet server with the API of Northbound (version 1) in front of the server's own
TrueNorth node. The pool and the stamps service pay through it, so that their payments rest on our own node and
not on somebody else's server. It listens on 127.0.0.1 only and must never be put behind Caddy or any other way in
from outside.

It is one file with nothing but the standard library, so it is copied on its own:

```
adduser --system --group --no-create-home walletd
adduser walletd truenorth                     # reads the node's cookie
install -d -m 0755 /opt/polarka-walletd
install -m 0644 polarka_agent/walletd.py /opt/polarka-walletd/walletd.py
install -m 0644 deploy/walletd/polarka-walletd.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now polarka-walletd
curl -s http://127.0.0.1:9560/ready            # "ready": true once the node is up to date
```

At start it reads the whole chain, then prints `listening 9560`. Wallet data (addresses, coins, history,
status) is answered with 503 while the node catches up, has no peers, or has gone `--max-tip-age` seconds
(7200) without a block: a payout then waits, and is never built on an old picture. The chain tip, fees,
validation and broadcast are answered all the same.

A wallet uses it as its server: `polarka-agent --dir FOLDER new --chain main --server http://127.0.0.1:9560`.
Errors of the node and answers that took more than five seconds go to the journal (`journalctl -u polarka-walletd`).

## A second one for the public

node.polarka.org lets phone wallets and agents use our node. It must not lead to the instance above: strangers
could take all its connections while a payout waits (review 8, M2). It leads to a second walletd with a user, a
port and an RPC user of its own, at most 12 connections at once, and no cookie: the node allows its RPC user only
the calls walletd makes (`deploy/rpc-users.md`).

```
adduser --system --group --no-create-home walletpub
# truenorth.conf: rpcauth and rpcwhitelist for walletpub; /etc/polarka-rpc/walletpub, 0400, owned by walletpub;
# then restart the node (deploy/rpc-users.md)
install -m 0644 deploy/walletd/polarka-walletd-public.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now polarka-walletd-public
curl -s http://127.0.0.1:9561/ready
install -m 0644 deploy/walletd/node.caddy /etc/caddy/sites/60-node.caddy
caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
```

Each instance reads the whole chain at start and keeps it in memory (about 110 MB each in November 2026). The
two cannot take each other's connections; they still share the node, which serves 16 RPC calls at a time, so
the public one is held to 12.
