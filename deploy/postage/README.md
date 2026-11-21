# A gate that takes postage, and something small behind it

What our own gate runs on, beside the faucet (`deploy/faucet`): a service with two facts about the chain,
and the gate before it. A caller gets twenty calls a day for nothing; after that the gate answers `402`
and asks for 600 hashes' worth of NORTH for a hundred calls. The price in NORTH follows the chain's
difficulty. The gate's wallet only receives: it has no `limits.json`, so the program can pay nothing from it.

    useradd --system --home /var/lib/polarka-gate --create-home --shell /usr/sbin/nologin gate
    sudo -u gate /opt/polarka-faucet/venv/bin/polarka-agent --dir /var/lib/polarka-gate/wallet new --chain main
    cp chain_facts.py /opt/polarka-faucet/
    cp polarka-facts.service polarka-gate.service /etc/systemd/system/
    systemctl enable --now polarka-facts polarka-gate

and in the Caddyfile a second name beside the faucet's:

    gate.example.org {
        reverse_proxy 127.0.0.1:8670
        log {
            output discard
        }
    }

Ours gives every `reverse_proxy` its own connection per request (`transport http { keepalive off }`, see
`gate.caddy`), so that a body a program behind leaves unread is never taken for a next request.

Trying it from another machine, with a wallet that has a coin and limits:

    curl -i https://gate.example.org/v1/unit                      # twenty times a day for nothing, then 402
    polarka-agent --dir WALLET postage https://gate.example.org --most 0.05
    curl -H "X-Postage: TOKEN" https://gate.example.org/v1/unit   # once the payment is in a block

Ours also takes x402 (`--x402-facilitator`, see `deploy/x402`) and passes `/v1/stamp` to the stamps service
(`deploy/stamps`), and since 0.2.1 it says what it sells in the bazaar extension of x402: `--public-url
https://gate.polarka.org --offers /etc/polarka-gate/offers.json`, with `offers.json` from this folder. The
facilitator, started with `--bazaar-origin https://gate.polarka.org`, lists what was paid for here, and
`gate.caddy` lets only its read-only routes out, and only with a release after 0.2.1 (review 8, H1; ours are
off until such a release runs):

    curl https://gate.polarka.org/x402/supported
    curl https://gate.polarka.org/x402/discovery/resources
    curl 'https://gate.polarka.org/x402/discovery/search?query=stamp'

Whoever runs a service for payment has the duties of one who does. This one is an experiment and says so.
