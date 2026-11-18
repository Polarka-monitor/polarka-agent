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

Trying it from another machine, with a wallet that has a coin and limits:

    curl -i https://gate.example.org/v1/unit                      # twenty times a day for nothing, then 402
    polarka-agent --dir WALLET postage https://gate.example.org --most 0.05
    curl -H "X-Postage: TOKEN" https://gate.example.org/v1/unit   # once the payment is in a block

Whoever runs a service for payment has the duties of one who does. This one is an experiment and says so.
