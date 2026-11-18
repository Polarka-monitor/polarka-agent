# The faucet of 0.1.2: what an independent review found, and what was done

On 18 November 2026 the faucet of version 0.1.2 (`polarka_agent/faucet.py` and what it rests on) was read by
a second AI agent that had not seen it being written. It read the code and then wrote 13 small programs that
drove the real code against the repository's own model of a wallet server. This page is a summary of its
report by the faucet's author (Cloe, an AI working with Severka), with what was done about each finding. The
fixes have not been read by a second pair of eyes, and the faucet has never run in public.

## Verdict of the review

"Not as it is." The owner's limits held in every run: no more than `per_day_north` in a day, no more than
`--amount` for a claim, no coin without a valid challenge and the work. But the work and the count by place
did not hold back a script: one internet address took 480 of 500 NORTH in a simulated day for 42 seconds of
one processor core, and 25 addresses took 498 in one burst for 23 seconds. The worst loss of an owner was
therefore the whole day's sum, every day, to whoever scripts it first.

Findings: critical 0, high 2, medium 6, low 7, notes 3.

## Findings and what was done

| | Finding | Done |
|---|---|---|
| F1 high | The work (plain SHA-256, ten times faster in C) and the count of requests by place do not bound a script; an address costs nothing to make | One place (an IPv4 address or an IPv6 /64) gets two payments a day (`--per-source`). The faucet gives at most a twelfth of the day's sum in an hour. The work now rises with the challenges handed out, paid or not. Not done: another kind of work (scrypt), see below |
| F2 high | With a limit for one payment equal to the amount, nothing is ever paid (the fee counts), and askers are told "come back tomorrow" | The faucet does not start with such limits. A wallet that refuses for a reason only its owner can mend is reported as out of order, not as empty, and the owner gets an `ALARM` line |
| F3 medium | No timeout and no cap on threads: about a thousand requests that never arrive took the faucet off the net | A request has 10 seconds to arrive; 64 requests are answered at one time and the rest get 503 at once; one request a connection; the deployment reads the whole request in the web server first and raises the file-handle limit |
| F4 medium | A solved claim the wallet refused could be sent again without end, each time asking the wallet server, under the one lock that challenges need too | A challenge is used once it reaches the wallet. An empty faucet remembers that for ten minutes and asks nobody. The lock is held for the books only, never while the wallet server is asked |
| F5 medium | With one coin the faucet pays one asker per block and tells the others "come back tomorrow" | "No coin free, try again in a few minutes" is its own answer, and no challenge is given meanwhile. The owner sends many small coins (deployment notes) |
| F6 medium | The questions asked of the wallet server for one claim grow with the payments that wait for a block | Not done in the wallet. The hourly bound keeps the number of waiting payments small, and nothing waits behind a claim any more except other claims |
| F7 medium | The table of places grew without bound and was walked at every request beyond 100000 entries | Cleaned once a minute; an IPv6 /64 is one place; a hard cap, beyond which new places are told to come back |
| F8 low | One address was paid twice a day, written in small and in capital letters | Addresses are compared in small letters |
| F9 low | A crash between the payment and the book, or a disk error after the payment left, paid the address again | Address and challenge are taken before the wallet is asked and given back only when nothing was signed. At its start the faucet also reads the wallet's journal, which is written before a payment leaves |
| F10 low | A wallet server whose node takes no transaction made the faucet say "the payment was made" and close the address | "Nothing was paid, try again later"; the address stays open; after three such payments in a row the faucet rests ten minutes at a time and writes `ALARM` |
| F11 low | One broken line in `faucet.jsonl` and the faucet did not start | Such lines are passed over and counted aloud; the book is written with fsync |
| F12 medium | The asker (`faucet URL`) followed redirects | No redirect is followed |
| F13 low | "Reached over https" was a test of how the address begins | The address is taken apart: https anywhere, plain http only to this very machine, no user names |
| F14 low | A dishonest faucet could cost the asker up to hours of work, hold it for days, and pass words of its choosing to whoever reads the answer | `--max-bits` (24 by default, also for the agent's tool), sixteen times the expected work and no more, a deadline for the whole answer, only error words from a fixed list, and the answer says that it is the faucet's word. Not done: an asking that got nothing still uses up a fresh address |
| F15 low | `X-Forwarded-For` was read in a way that some web servers other than Caddy and nginx get wrong | The last element of the last line counts; with `--trust-proxy` a request without the header is not served |
| F16 note | Loose parsing (`+20.` as a challenge, a length that is no number, a trailing newline) | One spelling for a challenge, lengths are digits only, answers are matched whole |
| F17 note | "The more the faucet is asked, the more bits it wants" was not what the code did | The code now does it (challenges count), and the text says what the work is: friction, not a wall |
| F18 note | A broken connection could put the peer's address into the system journal when the faucet listens directly; files were readable by others inside a closed folder | The error line names no peer; the faucet's files are its owner's to read |

## What stays as it is, and why

- **The work is still SHA-256.** A memory-hard function would take away most of the advantage of a fast
  program, but checking an answer would then cost the faucet tens of milliseconds, which is a door of its own
  for whoever wants to keep it busy. The bounds that count are counted, not worked for.
- **Many internet addresses still get many coins.** Nothing in a faucet without accounts stops that. The
  day's limit in `limits.json` is the promise; everything else makes taking it slower.
- **Places are forgotten at a restart** (their salt is new), payments are not.

The tests that hold these fixes are in `tests/test_faucet_bounds.py`.
