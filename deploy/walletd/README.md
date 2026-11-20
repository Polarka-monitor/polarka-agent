# walletd on the server

`polarka_agent/walletd.py` is a wallet server with the API of Northbound (version 1) in front of the server's
own TrueNorth node. The pool pays through it, so that its payouts rest on its own node and not on somebody
else's server. It listens on 127.0.0.1 only and must never be put behind Caddy or any other way in from outside.

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
