# Stamps as a service

Many SHA-256s in one transaction every ten minutes, and a proof for each that anyone can check without
trusting us: `polarka-agent stamp-verify proof.json` (with `--rpc-url` and `--rpc-cookie` it also asks a node
that the block is in the chain).

    useradd --system --home /var/lib/polarka-stamps --create-home --shell /usr/sbin/nologin stamps
    # its own RPC user, allowed only the calls it makes: see ../rpc-users.md
    sudo -u stamps /opt/polarka-x402/venv/bin/polarka-agent --dir /var/lib/polarka-stamps/wallet new --chain main --server http://127.0.0.1:9560
    # write the words down; then limits.json, for example {"per_payment_north": "1", "per_day_north": "5"}, and a few NORTH
    cp polarka-stamps.service /etc/systemd/system/ && systemctl enable --now polarka-stamps

The gate sends `/v1/stamp` there: add `--route /v1/stamp=127.0.0.1:8681` to `postage-serve`. Postage pays for the
calls; the service asks nothing itself.

From an agent: `polarka-agent fetch https://gate.polarka.org/v1/stamp/<sha256> --most 0.05`, then the same again
after the next batch and a block, and keep what comes back: that is the proof.
