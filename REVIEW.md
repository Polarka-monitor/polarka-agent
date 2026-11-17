# What an independent reviewer found, and what was done

An independent AI reviewer, which had not seen the code being written, read version 0.1.0.dev0 (commit
`e30816c`). It is not a human security expert, and a review of this kind finds some faults and does not show
that there are no others. Its report is in `review/report-0.1.0.dev0.md`, unchanged. Its verdict: the source
may be public as a clearly labelled work in progress; it is not to be used with real coins yet.

| ID | Severity | Finding in short | State |
|---|---|---|---|
| F1 | High | A server that falsely reports a small payment as in a block made the wallet drop the refused payment it stood against from the day's sum; repeated, the day's limit did not hold | Mended: a payment counts into its day whatever became of it, only the owner's `forget` takes one out. Test: `test_a_server_that_lies_about_a_block_does_not_free_the_day` |
| F2 | High | The wallet looks at 200 keys of a kind, yet sent change to key 200 and handed out address 200 | Mended: change reuses the last key that is looked at, and no address beyond is handed out (`TOO_MANY_ADDRESSES`). Two tests |
| F3 | Medium | Text chosen by the server reaches the agent through MCP results | Open. Said plainly in the README |
| F4 | Medium | The bounds hold against the MCP tools only, not against a program with a shell or file access as the same user | By design; the README now says so |
| F5 | Medium | One output of amount 0 on an address of the wallet stopped every command | Mended: such an output is no coin and is passed over. Test |
| F6 | Low | A payment that did not go through and is asked for again after 24 hours is not counted in that day | Open |
| F7 | Low | "In a block" is final at one confirmation | Open |
| F8 | Low | A lying server can stop the wallet until its owner acts and can hold the folder's lock for long | Open; the README now says so |
| F9 | Low | A deeply nested JSON line ends the MCP server | Open |
| F10 | Low | Some inputs ended the command line with a traceback | Mended: whatever else goes wrong is one JSON object, `INTERNAL` |
| F11 | Low | Journal, state, lock and limits are readable by others on the machine | Open |
| F12 | Low | The test of the tests counted any failing run as caught and had no run of the unchanged code | Mended: it runs the code as it stands first, and counts only tests that ran and failed |
| F13 | Low | `received` and `wait` report what sits unspent on an address, not that one payment arrived | Open |
| F14 | Note | The day is counted by the machine's clock and the journal | Open, by design for now |
| F15 | Note | Plain http is allowed to private ranges | Open |
| F16 | Note | The wording about a stamp's fee holds at the default fee rate only | Open |
| F17 | Note | A memo is stored and later shown to agents unfiltered | Open |

Nothing here has been checked by the reviewer again after the mending.
