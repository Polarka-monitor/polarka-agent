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

## For sellers from outside (from 0.2.3)

Not the gate's own facilitator opened up, but a second process of the same code, so that nothing an outside
caller does reaches the gate's payments, its lock or its memory:

    useradd --system --user-group --no-create-home --shell /usr/sbin/nologin x402pub
    install -d -o x402pub -g x402pub -m 0700 /var/lib/polarka-x402-public
    # its RPC user x402pub and /etc/polarka-rpc/x402pub: ../rpc-users.md
    cp polarka-x402-public.service /etc/systemd/system/ && systemctl enable --now polarka-x402-public
    curl -s http://127.0.0.1:9571/supported

`--public` makes it count requests by the address the web server writes into X-Forwarded-For (an IPv6 address
by its /64, and also by the /48 it is in): a payment to `/verify` or `/settle` at most `--per-minute` times a
minute from one address (60), twice that from one /48, and ten times that from all of them together; more is
answered `429` with `Retry-After: 60`. Only a request that would ask the node counts; `/supported` and a
malformed request do not. It forgets an address a minute after its last request and writes none down; when it
holds 10,000 addresses all the same, the one heard from longest ago makes room, so a newcomer is never turned
away for a full table. It keeps no catalog (`--bazaar-origin` is refused with `--public`), so what others sell
is never listed in our name. It takes no new payment once it keeps `--most-payments` (20000), and one place (an
IPv4 address, an IPv6 /48) brings at most a twentieth of that in a day: `/verify` and `/settle` then answer
`settlement_failed`, which tells a seller to ask again later, while a payment it already has is still settled.
Its folder then wants looking at.

What these bounds do not stop (review 10, M2): a sender with many addresses in many places can still use up the
requests of all together, and twenty places can fill what is kept, at the price of the fees of the payments they
bring. A payment it already has is counted like any other request, so while that lasts it waits too. The bounds
keep the node and the gate's own facilitator out of it; they do not make this one always available.

The web server passes it `/facilitator/supported`, `/facilitator/verify` and `/facilitator/settle`
(`../postage/gate.caddy`); a seller names `https://gate.polarka.org/facilitator` as its facilitator. Check
through the web server that an X-Forwarded-For the caller writes counts for nothing: from one machine, the 61st
request within a minute is answered `429`, whatever address it names.

A payment it settles pays the seller directly: the facilitator checks it and hands the buyer's own signed
transaction to the network, as any node does. It asks no fee, holds nothing, and is an experiment that may stop.
