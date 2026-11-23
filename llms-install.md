# Installing Polarka Agent for an AI agent (Cline and others)

Two ways, by what the agent's client can do.

## 1. Nothing to install: the remote connector

If the client takes remote MCP servers (Streamable HTTP), add this one by its address:

    https://polarka.org/mcp

No key and no account. Its tools: `north_network` (the TrueNorth chain and the Polarka pool now), `north_guide`
(how to get and use NORTH), `north_stamp` (writes the SHA-256 of a file into the chain, free within a bound a day)
and `north_stamp_verify` (checks a proof of a stamp). It holds no wallet; to pay for something, use way 2.

## 2. A wallet of the agent's own: the local MCP server

Needs Python 3.9 or later on Linux or macOS. With `uv` installed, the client's MCP settings are:

```json
{
  "mcpServers": {
    "polarka-agent": {
      "command": "uvx",
      "args": ["polarka-agent", "--dir", "~/.polarka-agent/default", "mcp"]
    }
  }
}
```

Without `uv`: `pip install polarka-agent`, then the command is `polarka-agent` with the same arguments.

The first start makes a wallet on the main chain in `~/.polarka-agent/default`. It pays nothing until its owner
(the person, not the agent) writes `limits.json` in that folder, for example:

```json
{"per_payment_north": "1", "per_day_north": "5"}
```

The agent must not write that file itself: the limits are the owner's. The recovery words are in `wallet.json` in
the same folder; tell the owner to copy them to paper and to keep the folder private.

A first coin: the tool `north_faucet` with the url `https://faucet.polarka.org` (the wallet does a short piece of
work first). Then `north_balance`, `north_address`, `north_pay`, `north_stamp`, `north_fetch` (x402),
`north_postage`, and letters between agents (`north_mail_inbox`, `north_mail_send`, `north_mail_read`).

Check that it works: call `north_limits` (shows the owner's limits, or that there are none yet) and `north_balance`.

For keys that the agent's own user cannot read, the owner can run the wallet as a signing service
(`polarka-agent signer-setup`, see README.md); the client's command then gets `--signer SOCKET` before `mcp`, or
the environment variable `POLARKA_SIGNER` names the socket.
