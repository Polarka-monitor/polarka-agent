# Running a faucet in public

A faucet is this wallet under its own limits, behind a web server that provides HTTPS. These files are what
our own faucet runs on (Debian 13). Read `polarka_agent/faucet.py` first: you are putting a wallet with
coins in it on the internet.

    apt install python3-venv caddy
    useradd --system --home /var/lib/polarka-faucet --create-home --shell /usr/sbin/nologin faucet
    python3 -m venv /opt/polarka-faucet/venv
    /opt/polarka-faucet/venv/bin/pip install polarka-agent
    sudo -u faucet /opt/polarka-faucet/venv/bin/polarka-agent --dir /var/lib/polarka-faucet/wallet new --chain main

The last command shows the recovery words once. Write them down away from the server. Then the owner's
limits, by hand, in `/var/lib/polarka-faucet/wallet/limits.json` (mode 600, owner `faucet`):

    {"per_payment_north": "0.26", "per_day_north": "100"}

A payment counts with its fee, so the limit for one payment has to be a little above the amount given; with
a limit equal to the amount the faucet does not start. What the faucet may give in a day is `per_day_north`
and nothing else.

Our own faucet gives 0.25 NORTH to an address (enough for a few thousand calls of postage and for trying
payments) and at most 100 NORTH a day, so that the same sum reaches more programs. Set `per_day_north` to a sum you can give away every single day: somebody with many internet addresses can
take all of it, and the faucet's own counting (two payments a day to one place, a twelfth of the day in an
hour) only makes that slower. Start low and raise it when the faucet has run a while.

    cp polarka-faucet.service /etc/systemd/system/ && systemctl enable --now polarka-faucet
    cp Caddyfile /etc/caddy/Caddyfile        # put your own name in place of faucet.example.org
    systemctl reload caddy

Send the wallet some coins (`polarka-agent --dir ... address` gives an address). Each claim spends one coin
and waits for a block before its change can be spent again, so send several small coins and not one large
one: with one coin the faucet pays one asker per block and tells the others to come back in a few minutes.
With two payments an hour for every coin you are on the safe side (`pay-many` from another wallet of this
program sends many small coins in one payment).

Watch the service's journal for lines that begin with `ALARM`: the wallet refused to pay for a reason only
its owner can put right, or payments were not taken by the network. The faucet then rests for ten minutes at
a time and tells askers so.

What is kept: `faucet.jsonl` in the wallet folder with the addresses that were paid, and the wallet's own
journal. The service writes one line per request to the system journal (time, kind of request, outcome), with
no address of either kind. Caddy's access log is switched off above; if you switch it on, you keep internet
addresses.

Checking it from another machine:

    polarka-agent --dir SOME_WALLET faucet https://your.name
