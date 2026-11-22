# Stratum for the pool

pool.polarka.org takes miners over stratum since 22 November 2026 (polarka-agent 0.2.4), on port 3333, beside the
HTTP pool behind the web server. The port is a socket unit of systemd (`polarka-stratum.socket`), which hands its
socket to the pool: the miners' connections then come in under the socket's rules (none: from anywhere), while the
pool's own unit may still speak only with its own machine (`IPAddressDeny=any`, `IPAddressAllow=localhost`).

In the pool's unit:

    [Unit]
    After=truenorthd.service polarka-walletd.service polarka-stratum.socket
    Requires=polarka-stratum.socket

    [Service]
    LimitNOFILE=8192
    Sockets=polarka-stratum.socket
    ExecStart=... pool-serve ... --stratum systemd

and then

    cp polarka-stratum.socket /etc/systemd/system/ && systemctl daemon-reload
    systemctl stop polarka-pool                   # systemd starts no socket whose service runs already
    systemctl enable --now polarka-stratum.socket
    ufw allow 3333/tcp
    systemctl start polarka-pool

Stopping the pool from then on means stopping its socket first, `systemctl stop polarka-stratum.socket
polarka-pool`: with the socket up, a miner who connects to 3333 starts the pool again. Without systemd,
`--stratum :3333` has the pool listen on every address itself. A miner connects with
`./cpuminer -a randomx -o stratum+tcp://pool.example.org:3333 -u ADDRESS -p x` (IronMiner 1.0.8 or later).
