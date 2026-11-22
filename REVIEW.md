# What an independent reviewer found, and what was done

An independent AI reviewer, which had not seen the code being written, read version 0.1.0.dev0 (commit
`e30816c`). It is not a human security expert, and a review of this kind finds some faults and does not show
that there are no others. Its report is in `review/report-0.1.0.dev0.md`, unchanged. Its verdict: the source
may be public as a clearly labelled work in progress; it is not to be used with real coins yet.

| ID | Severity | Finding in short | State |
|---|---|---|---|
| F1 | High | A server that falsely reports a small payment as in a block made the wallet drop the refused payment it stood against from the day's sum; repeated, the day's limit did not hold | Mended: a payment counts into its day whatever became of it, only the owner's `forget` takes one out. Test: `test_a_server_that_lies_about_a_block_does_not_free_the_day` |
| F2 | High | The wallet looks at 200 keys of a kind, yet sent change to key 200 and handed out address 200 | Mended: no address beyond is handed out (`TOO_MANY_ADDRESSES`), and change stays within the keys looked at; since the human review of 22 November it uses them again in turn rather than the last one for every payment. Two tests |
| F3 | Medium | Text chosen by the server reaches the agent through MCP results | Mended: a refusal is passed on as one of a few fixed words, an error as its HTTP status, and a receiver that has not the shape of an address is refused; the words are shown at the command line only. Two tests. A memo (F17) is still shown as it was given |
| F4 | Medium | The bounds hold against the MCP tools only, not against a program with a shell or file access as the same user | The README says so. On main for 0.2.3: a signing service (`signer.py`, `signer-serve`) that holds the words and `limits.json` under a user of its own, which the agent, as a user of its own that cannot administer the machine, only asks over a socket. Review 10 read it on 22 November; what it found is mended on main (`docs/review-guide.md`). Tests: `tests/test_signer.py`, `tests/test_review10.py` |
| F5 | Medium | One output of amount 0 on an address of the wallet stopped every command | Mended: such an output is no coin and is passed over. Test |
| F6 | Low | A payment that did not go through and is asked for again after 24 hours is not counted in that day | Mended on main on 22 November (raised again by the human review): shown again a day or more later, it counts for that day; and since review 10 (H1), one that stayed open for a day or more and then goes out, sent by anybody, counts for the day it goes out. Tests: `test_a_payment_shown_again_a_day_later_counts_for_that_day`, `tests/test_review10.py` |
| F7 | Low | "In a block" is final at one confirmation | Open |
| F8 | Low | A lying server can stop the wallet until its owner acts and can hold the folder's lock for long | Open; the README now says so |
| F9 | Low | A deeply nested JSON line ends the MCP server | Mended on main on 22 November: a line is read with a bound and deep JSON is answered as not JSON. Test: `test_a_line_is_read_with_a_bound_and_deep_json_is_refused` |
| F10 | Low | Some inputs ended the command line with a traceback | Mended: whatever else goes wrong is one JSON object, `INTERNAL` |
| F11 | Low | Journal, state, lock and limits are readable by others on the machine | Mended on main on 22 November for the journal, the state and the lock (made 0600 whatever the umask); limits.json is the owner's to write. Test: `test_the_wallets_files_are_its_owners_alone` |
| F12 | Low | The test of the tests counted any failing run as caught and had no run of the unchanged code | Mended: it runs the code as it stands first, and counts only tests that ran and failed |
| F13 | Low | `received` and `wait` report what sits unspent on an address, not that one payment arrived | Open |
| F14 | Note | The day is counted by the machine's clock and the journal | Open, by design for now |
| F15 | Note | Plain http is allowed to private ranges | Open |
| F16 | Note | The wording about a stamp's fee holds at the default fee rate only | Open |
| F17 | Note | A memo is stored and later shown to agents unfiltered | Open |

Nothing here has been checked by the reviewer again after the mending.
