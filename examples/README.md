# Examples

## An agent that pays for itself

`agent_that_pays_for_itself.py` goes from no coin at all to a file stamped into the TrueNorth chain, and no person
is needed on the way but the owner who writes the wallet's limits.

    pip install polarka-agent
    python agent_that_pays_for_itself.py FILE              # the first coin from the faucet of polarka.org
    python agent_that_pays_for_itself.py FILE --mine 300   # or earned by five minutes of mining for the pool

The first run makes a wallet, gets a first coin and stops before it pays anything: the owner then writes
`limits.json` into the wallet's folder (the script prints what to write and where), and the second run pays.
It pays the stamps service at `https://gate.polarka.org/v1/stamp/<sha256>` over x402, at most 0.05 NORTH for a lot
of 100 calls (about 0.01 NORTH on 21 November 2026), waits for the next batch and a block, and checks the proof.
Only the SHA-256 of the file is sent. It takes some fifteen minutes, most of them waiting.

On 21 November 2026 it ran so on the main chain with a new wallet: 0.25 NORTH from the faucet, 0.00946316 NORTH
paid over x402, the stamp in block 29201, and the proof checked against a node of our own that is not the one
behind the service.

## The same as an agent with the MCP server

Give the agent the server (`uvx polarka-agent --dir FOLDER mcp`) and, once, a `limits.json` in FOLDER. Then a
task like this is enough:

> Get a first coin for your NORTH wallet from https://faucet.polarka.org with north_faucet, wait for it with
> north_wait, then stamp the SHA-256 of report.pdf: call north_fetch on
> https://gate.polarka.org/v1/stamp/<the sha256> with most 0.05, and call it again after the time it names until
> the answer is "stamped". Keep the answer: it is the proof.

NORTH has no price. A stamp shows an order of events; the chain is young and its hash rate is small, so it is not
proof for a court.
