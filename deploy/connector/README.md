# The connector: a remote MCP server at https://polarka.org/mcp

`polarka-agent connector-serve` (from 0.2.5) is an MCP server that a chat application reaches over the internet, so
that an assistant there can use it without installing anything: in Claude, Settings > Connectors > Add custom
connector, with the address `https://polarka.org/mcp`; any other client that speaks the Streamable HTTP transport
takes the same address. It needs no account and no key, and it holds no wallet.

Its four tools: `north_network` (the chain and the pool now), `north_guide` (how to get and use NORTH),
`north_stamp` (a SHA-256 into the chain through the stamp service on this machine, free within a bound a day) and
`north_stamp_verify` (checks a proof of a stamp against the node). The stamps cost the service nothing beyond its one
transaction a batch; the bound a day (500 new SHA-256s for everybody together, 100 for one place by default) keeps
the batch of the paying users clear.

What it keeps, in `--dir`: `state.json` (the free stamps used today and the SHA-256s taken in the last seven days, so
that asking again costs nothing) and `usage.json` (per day: how many sessions began and with which client program,
calls per tool, new stamps, checks). No caller's address is written anywhere; places are counted in memory under a
salted hash that dies with the process. Caddy must not write the callers' addresses for this path either (below).

## Set up

    useradd --system --home /var/lib/polarka-connector --shell /usr/sbin/nologin connector
    install -d -o connector -g connector -m 0700 /var/lib/polarka-connector
    python3 -m venv /opt/polarka-connector/venv-0.2.5
    /opt/polarka-connector/venv-0.2.5/bin/pip install polarka-agent==0.2.5
    ln -sfn /opt/polarka-connector/venv-0.2.5 /opt/polarka-connector/venv

A node user of its own (`deploy/rpc-users.md`), allowed the two calls that `north_stamp_verify` makes:

    rpcauth=connector:<salt>$<hmac>
    rpcwhitelist=connector:getblockheader,getrawtransaction

    printf 'connector:%s' "$PASSWORD" > /etc/polarka-rpc/connector
    chown connector:connector /etc/polarka-rpc/connector && chmod 0400 /etc/polarka-rpc/connector

The node reads its configuration only when it starts: restart it, then the services that use it come back by
themselves (the pool pauses while the node is away). Then the unit:

    install -m 0644 polarka-connector.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now polarka-connector

In Caddy, inside the site of polarka.org, before the files of the site:

    @mcp path /mcp /mcp/
    handle @mcp {
        request_body {
            max_size 64KB
        }
        reverse_proxy 127.0.0.1:8696 {
            request_buffers 64KB
        }
    }

`caddy validate` and `systemctl reload caddy`. Keep this path as the other sites of the server are kept: no access
log, or one whose address fields are deleted.

## Check

    curl -s https://polarka.org/mcp -H 'Content-Type: application/json' \
         -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 300
    curl -s -o /dev/null -w '%{http_code}\n' https://polarka.org/mcp          # 405: no stream, as it should be

The bounds can be changed with `--free-per-day`, `--free-per-place` and `--per-minute` (requests a minute from one
place). A chat application reaches it from its own machines: many of its users share one place, so its bound is
the place's bound.
