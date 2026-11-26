# The USDC facilitator of the gate (from 0.2.9)

A small service that settles x402 payments in USDC on Base for one gate, to one address: the scheme `exact`, with
an authorization of transfer (EIP-3009 `transferWithAuthorization`). It is built on the official x402 SDK for
Python (`x402[evm]==2.29.0`, Apache-2.0), in an environment of its own, so that polarka-agent itself keeps no
dependencies. The service and these files are MIT, like the rest.

## What it does, and what it does not

- It listens on 127.0.0.1 only. The gate is its only caller: `GET /supported`, `GET /health`, `POST /verify`,
  `POST /settle`, shaped as in x402.
- It settles only USDC of its network, only to `--pay-to`, from 1 unit to `--max-amount` (1 USDC by default), only
  x402 version 2 with the scheme `exact` and an EIP-3009 authorization, only from a plain address (an address with
  code, an EIP-7702 delegation among them, is refused) and only with a canonical signature. Anything else is refused
  with a reason of its own before the SDK sees it.
- It holds no USDC: the transfer goes from the payer straight to `--pay-to`. Its gas key pays Base's network fee,
  about 100,000 gas a settlement, which in November 2026 was about a tenth of a cent. Keep only a few dollars of
  ETH there; it writes an alarm into its log when the key falls below a floor.
- It writes every payment (payer, nonce, the signed transfer) into a journal in `--state-dir` before anything goes
  to the chain. After a restart it reads the journal back and only ever asks about what it sent; it never sends a
  payment anew from there.
- A payment counts as paid only when the token's `AuthorizationUsed(payer, nonce)` is followed by its
  `Transfer(payer, pay-to, the whole amount)`. Any other use of that nonce is `authorization_used_elsewhere`: a payer
  can sign a second authorization with the same nonce, to itself, and get it into a block first.
- It keeps its own count of the gas key's nonces. A nonce whose transfer the chain lost is filled with a transfer of
  nothing to itself, which charges nobody, at most four times today's fee. While such a nonce stays open longer
  than a minute, new payments are refused (nothing is sent) and an alarm is written every ten minutes.
- A payment whose outcome it still cannot find six hours after its end is given up as failed for the gate
  (`settlement_unknown`), for a person to look at.

## Running it, as ours runs

1. A user of its own and its folder:

       useradd --system --home-dir /var/lib/polarka-usdc --shell /usr/sbin/nologin usdc
       install -d -m 700 -o usdc -g usdc /var/lib/polarka-usdc

2. An environment with the SDK (check the wheel's SHA-256 against PyPI), and the service itself:

       python3 -m venv /opt/polarka-usdc/venv
       /opt/polarka-usdc/venv/bin/pip install "x402[evm]==2.29.0"
       install -m 644 usdc_facilitator.py rpc_checks.py logs_control.py /opt/polarka-usdc/

3. The gas key, made on the server and never shown; only its address is printed:

       runuser -u usdc -- /opt/polarka-usdc/venv/bin/python -c "import os; from eth_account import Account; \
         a = Account.create(); fd = os.open('/var/lib/polarka-usdc/gas.key', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); \
         os.write(fd, (a.key.hex() + '\n').encode()); print(a.address)"

   Send a few dollars of ETH on Base to that address.

4. An RPC for Base that takes a key. Its address goes into a file only user usdc may read, never onto a command line
   and never on the screen, for example from the clipboard: `pbpaste | ssh root@server 'umask 077; cat > /var/lib/polarka-usdc/rpc-url &&
   chown usdc:usdc /var/lib/polarka-usdc/rpc-url'`. Ours is Coinbase's CDP Node
   (`https://api.developer.coinbase.com/rpc/v1/base/` and the project's Client API key). The public
   `https://mainnet.base.org` is rate limited and answered by many nodes behind a balancer. Providers whose free
   tier limits `eth_getLogs` to a few blocks do not fit: the facilitator asks 500 blocks at a time, up to 3000.

5. Check the RPC before it carries a payment, as user usdc:

       runuser -u usdc -- /opt/polarka-usdc/venv/bin/python /opt/polarka-usdc/rpc_checks.py
       runuser -u usdc -- /opt/polarka-usdc/venv/bin/python /opt/polarka-usdc/logs_control.py https://base-rpc.publicnode.com

   The first shows the chain, the gas balance, `estimate_gas` at the latest block and `eth_getLogs` over 500 and
   3000 blocks. With `--send`, once the gas key has ETH (stop the facilitator meanwhile), it sends one transfer of
   nothing to itself and shows what the RPC answers when the same transfer comes again, when another comes under the
   same nonce, and when the first is in a block. The second asks the same settled blocks of the provider and of a
   public node: the logs must be the same, so the provider leaves nothing out. Neither prints the RPC's address.

   What CDP answered on 26 November 2026: a waiting transfer is found by its hash; the same transfer again, another
   under the same nonce and the same one after its block all get `nonce too low`, never `already known` or
   `replacement transaction underpriced`. The facilitator takes `nonce too low` as a nonce used before its transfer
   was handed over, and after an answer lost on the way as its own transfer, which may be in.

6. The unit: `polarka-usdc.service` from here, with `PAY_TO=0x...` (the seller's own address) in
   `/etc/polarka-usdc/env`. Then `systemctl enable --now polarka-usdc` and `curl -s http://127.0.0.1:8402/health`.

7. The gate: `polarka-gate-usdc.conf` as a drop-in of `polarka-gate.service`. It adds `--usdc-facilitator`,
   `--usdc-pay-to`, `--usdc-path /v1/stamp` and `--free-look /v1/stamp`. From outside, a 402 of the gate then names
   NORTH first and USDC second, and `/_look` paths are refused.

8. Back: remove the drop-in, `systemctl daemon-reload`, restart the gate. Stop the facilitator only when nothing waits
   (`"waiting": 0` in `/health`): its journal is what answers a payment that comes back late.

## Tests

- `test_offline.py`: 33 tests without a chain, in this folder with the SDK installed:
  `python -m unittest test_offline`.
- On a copy of Base: `run_fork_tests.sh` starts anvil (Foundry 1.10.0) as a fork of Base, mints USDC to a test payer
  through USDC's `masterMinter` on the fork, and runs the self-test (29 checks: settlement, the attacks of the review,
  payments at once), a late block (4), the facilitator killed while it waits (4), and the gate end to end with the
  SDK's standard buyer (8). Nothing is sent to Base itself. Test keys: `python gen_keys.py FOLDER` prints only their
  addresses. Settings: `PY` (a Python with `x402[evm]==2.29.0`), `FOUNDRY_BIN`, `KEYS` (the folder of the keys),
  `POLARKA_AGENT` (this repository, for the gate) and `RUN_DIR`.

What independent reviewers found in five readings, and what was done about it: `docs/review-guide.md`.
