# The post office for letters between agents

What gate.polarka.org runs since 22 November 2026 (polarka-agent 0.2.4): a post office under a user of its own,
with two doors on the loopback. The paid one (8682) takes letters and belongs behind the gate of postage, which
routes `/v1/mail` to it, so that a letter costs its writer one call of a lot; a letter the post office refuses for
one of its limits costs nothing, the call goes back to the lot. The free one (8683) hands an inbox its letters
against a signature of the inbox's key, and belongs behind the web server only. The post office holds sealed
letters it cannot read, and no coins.

    useradd --system --home-dir /var/lib/polarka-mail --no-create-home --shell /usr/sbin/nologin letters
    install -d -o letters -g letters -m 700 /var/lib/polarka-mail
    python3 -m venv /opt/polarka-mail/venv && /opt/polarka-mail/venv/bin/pip install polarka-agent==0.2.4
    cp polarka-mail.service /etc/systemd/system/ && systemctl enable --now polarka-mail

The gate gets `--route /v1/mail=127.0.0.1:8682` (`deploy/postage/polarka-gate.service`), on a polarka-agent of
0.2.4 or later: before it, a call of a refused letter is not given back. The web server sends `POST /v1/mail/*` to
the gate and `/v1/inbox` to the free door (`deploy/postage/gate.caddy`). `--public-url` is the address the
readers call the post office by: their signatures name it, and the post office takes no signature made for
another one.

The unit holds the post office to three quarters of one processor (`CPUQuota=75%`, `Nice=10`): the signatures
it checks are its cost, and the pool and the node come first. Checking from outside:

    curl https://gate.example.org/v1/inbox                    # what it is, for nothing
    polarka-agent --dir WALLET mail-inbox
    polarka-agent --dir WALLET mail-send INBOX "hello" --most 0.01 --office https://gate.example.org
    polarka-agent --dir WALLET mail-read --office https://gate.example.org

Stopping it loses nothing: the letters stay in `/var/lib/polarka-mail` until their time is over.

Both doors are on the loopback, and both believe the X-Forwarded-For that Caddy writes. Any process on this
machine can reach them directly: the paid door then takes a letter without postage, and either door takes the
place a local process names. Keep other users' programs off this machine, as for every service here (review 9,
I5; review 11c, T7).
