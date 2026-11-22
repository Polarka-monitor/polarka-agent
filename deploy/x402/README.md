# An x402 facilitator for NORTH, and the postage gate over x402

The facilitator does for sellers what x402 asks of one (`GET /supported`, `POST /verify`, `POST /settle`):
it checks that a payment pays enough to the seller, hands it to its own node and says when it is in a block.
It keeps no coins and signs nothing. What it took is kept in `x402-used.jsonl`, so one payment buys once.

    useradd --system --home /var/lib/polarka-x402 --create-home --shell /usr/sbin/nologin x402
    python3 -m venv /opt/polarka-x402/venv && /opt/polarka-x402/venv/bin/pip install polarka-agent==0.2.2
    # its own RPC user, allowed only the calls it makes: see ../rpc-users.md
    cp polarka-x402.service /etc/systemd/system/ && systemctl enable --now polarka-x402
    curl -s http://127.0.0.1:9570/supported

A seller is answered only when the payment is one block deep (`--confirmations 1`). `--instant-below` lets
small amounts through as soon as the node takes them, before a block; such a payment can still be replaced
(the node is built on Bitcoin Core 30, which replaces waiting transactions by fee), so use it only where the
price is there to keep robots away.

The postage gate sells its lot over x402 too, when it is told where the facilitator is:

    polarka-agent --dir /var/lib/polarka-gate/wallet postage-serve ... --x402-facilitator http://127.0.0.1:9570

A buyer: `polarka-agent fetch https://gate.example.org/v1/unit --most 0.05`. The first call pays and waits
for the block; the claim in the payment is then the token for the rest of the lot, and `fetch` uses it by
itself for the next calls to the same place.

To offer the facilitator to other sellers, put it behind the web server under its own name. Whoever settles
payments for others has duties of their own; this one is an experiment and says so.
