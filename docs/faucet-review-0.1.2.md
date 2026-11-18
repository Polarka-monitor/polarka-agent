# The faucet of 0.1.2: what an independent review found, and what was done

On 18 November 2026 the faucet of version 0.1.2 (`polarka_agent/faucet.py` and what it rests on) was read by
a second AI agent that had not seen it being written. It read the code and then wrote 13 small programs that
drove the real code against the repository's own model of a wallet server. This page is a summary of its
report by the faucet's author (Cloe, an AI working with Severka), with what was done about each finding. The
fixes were then read by a second independent AI reviewer (see "The second look" below), and the faucet has
never run in public.

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
| F1 high | The work (plain SHA-256, ten times faster in C) and the count of requests by place do not bound a script; an address costs nothing to make | One place (an IPv4 address or an IPv6 /48) gets two payments a day (`--per-source`). The faucet gives at most a twelfth of the day's sum in an hour. The work now rises with the challenges handed out, paid or not. Not done: another kind of work (scrypt), see below |
| F2 high | With a limit for one payment equal to the amount, nothing is ever paid (the fee counts), and askers are told "come back tomorrow" | The faucet does not start with such limits. A wallet that refuses for a reason only its owner can mend is reported as out of order, not as empty, and the owner gets an `ALARM` line |
| F3 medium | No timeout and no cap on threads: about a thousand requests that never arrive took the faucet off the net | One read of a request has 10 seconds and the whole request 20; 64 requests are answered at one time and the rest get 503 at once; one request a connection; the deployment reads the whole request in the web server first and raises the file-handle limit |
| F4 medium | A solved claim the wallet refused could be sent again without end, each time asking the wallet server, under the one lock that challenges need too | A challenge is used once it reaches the wallet. An empty faucet remembers that for ten minutes and asks nobody. The lock is held for the books only, never while the wallet server is asked |
| F5 medium | With one coin the faucet pays one asker per block and tells the others "come back tomorrow" | "No coin free, try again in a few minutes" is its own answer, and no challenge is given meanwhile. The owner sends many small coins (deployment notes) |
| F6 medium | The questions asked of the wallet server for one claim grow with the payments that wait for a block | Not done in the wallet. The hourly bound keeps the number of waiting payments small, and nothing waits behind a claim any more except other claims |
| F7 medium | The table of places grew without bound and was walked at every request beyond 100000 entries | Cleaned once a minute; an IPv6 /48 is one place; a hard cap, beyond which new places are told to come back |
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

## The second look

On 18 and 19 November 2026 another independent AI reviewer, who had seen neither the first fixes being written
nor this page being written, checked them: it ran the first review's programs again and wrote its own. Its
verdict was "yes after these fixes": the owner's limits held in every run, 11 of the 18 findings were fixed, 5
partly, and two things still stood in the way.

| | Finding of the second look | Done |
|---|---|---|
| S1 high | Questions that cost nothing raised the work for everybody: 128 of them an hour from 7 addresses put the faucet beyond what this program's own asker does, so every ordinary asker was shut out. The first fix had brought this in | Only work that was done raises the work for all. Questions raise the work of the place that asked them, from the fifth in an hour. And a faucet never asks more than the asker does unasked (24 bits), unless its owner sets the quiet work higher |
| S2 medium | An IPv6 /64 counted as a place, so one customer with a /56 was 256 places and took 99.75 of 100 NORTH in twelve hours | A place is an IPv6 /48 |
| S3 low | The 10 seconds were for one read, so a request sent a byte at a time held a thread as long as it liked | The whole request has 20 seconds; the faucet is said to belong behind a web server |
| S4 low | 100000 places filled the table and new places had to wait | Follows from S2: so many /48s are not had for nothing |
| S5 low | After a payment the network did not take, the next paid claim brought a false alarm and a ten-minute rest | That state is "no coin free, try again in a few minutes" and no alarm |
| S6 low | A limit a hair above the amount still started and then alarmed at every claim | The start asks for room for the fee above the amount |
| S7 low | The asker's deadline covered the body of an answer, not its header | The whole exchange has a deadline |
| S8 low | A payment that left but ended in an error was counted for neither the hour nor the place | Counted |
| S9 low | An empty wallet said "try again in a few minutes" for ever and told its owner nothing | After five such answers in a row the owner gets an `ALARM` line and the faucet asks the wallet less often |
| S10 to S14 notes | A refused start looped under systemd; the asker did not read when a challenge runs out and let a newline through in a transaction id; the tool's text promised too short a time; the unit might not let names resolve | Exit code 2 is not started again; the asker stops when the challenge runs out; ids are matched whole; the text is true; the unit allows what resolving needs. Not done: an asker may still be sent to a private https address; two claims under way at the moment the day is spent still ask the wallet |

Not checked by either reviewer: the web server (Caddy) and systemd themselves, and a real Northbound server
under a real load. What was changed after the second look has been read by nobody else.

The tests that hold these fixes are in `tests/test_faucet_bounds.py`.
