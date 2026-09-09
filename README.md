# agent-replay

Turn an agent transcript into one self-contained HTML file you can attach to a bug report or post
in a thread. Scrubber, collapsible tool calls, inline diffs, a token and cost gutter, and secrets
removed.

**[Read this on the web](https://jesserweigel.github.io/agent-replay/)** ·
**[Open a demo replay](https://jesserweigel.github.io/agent-replay/demo.html)**

Catalog task `AGENT-048`. Part of [722 things to build](https://github.com/JesseRWeigel/722-things-to-build).

Illustrative CLI output using totals from an earlier captured run. Those totals have not been
recomputed under the current response-identity rules:

```
$ agent-replay --list
    15.6 MB  ~/.claude/projects/-home-user-Projects/15f05fff-....jsonl

$ agent-replay ~/.claude/projects/-home-user-Projects/15f05fff-....jsonl -o replay.html
wrote replay.html (2.7 MB, 3,163 events, 1750 secrets removed, 856.0M tokens, API-equivalent USD $1,686.06, not an invoice)
```

## The refusal is the feature

Most redaction is a filter: it removes what it recognises and hands you the result. If it missed
something, you find out when somebody replies to your thread.

This writes nothing until the rendered file has been scanned and come back clean. A survivor is
an error with a nonzero exit code, not a warning:

```
$ agent-replay session.jsonl -o replay.html
REFUSING to write replay.html: 1 credential-shaped string(s) survived redaction.
  aws identifier: AKIAQW… (20 chars) at offset 8232
Nothing was written. This is a bug in the redactor, so please report it with the label above
and no other detail.
```

**The scan is deliberately not the filter.** `find_leaks()` is written from scratch with none of
the redactors' patterns, and it runs on the finished HTML rather than on the transcript. A checker
sharing code with the redactor cannot catch the redactor's mistakes, because a bug in a pattern is
invisible to a check built on that same pattern.

That separation earned its keep three times while building this, and the checker was right every
time. The clearest case was a truncated private key: a `BEGIN OPENSSH PRIVATE KEY` header with
body but no `END` marker. The redactor required a complete block and skipped it. A transcript that
captured the first sixty lines of an `id_rsa` has published enough to matter, so the redactor was
raised to meet the checker rather than the check being relaxed.

## Three layers

| Layer | Catches | Would miss alone |
|---|---|---|
| Known formats | Provider tokens by shape: Anthropic, OpenAI, OpenRouter, GitHub, GitLab, Google, AWS, Slack, Stripe, npm, PyPI, Hugging Face, JWTs, PEM blocks whole or truncated, bearer headers, inline database passwords | `ADMIN_PASSWORD=hunter2`, which matches no format |
| Assignment context | A value is a secret because of what it is called. dotenv, JSON, YAML, CLI flags, request headers | A bare token pasted into prose |
| Entropy near a secret word | A long random-looking string within 90 characters of a word like `api_key`, matching no known prefix | It is the last-resort net rather than the first line |

### Layering hides its own failures

Attacking the verify script found that emptying the known-format list entirely still passed every
check, because the other two layers covered for it. That redundancy is what you want at runtime and
useless for verification, since a dead layer is invisible.

Each layer now has a liveness probe built from a secret only that layer can catch:

```
3b. each redaction mechanism is individually alive
  known-format         alive   -> please use [redacted:github-token] when you push
  assignment-context   alive   -> ADMIN_PASSWORD=[redacted:assigned-secret]
  entropy              alive   -> the api_key we were issued reads [redacted:high-entropy] exactly
```

Emptying any one list now fails the build and names the dead mechanism.

## Over-redacting is also a failure

A tool that blanks everything passes any leak test perfectly and gets uninstalled the same
afternoon, at which point it protects nobody. Roughly as many tests assert that ordinary content
survives.

| | Input | Result |
|---|---|---|
| keep | `DEBUG_TOKEN_PARSER=true` | Untouched. An earlier version redacted the whole line, because the entropy candidate spanned the `=` and swallowed the variable name |
| keep | `await refreshAuthenticationTokenSilently()` | Untouched. Entropy candidates require a digit, since camelCase identifiers have none and real credentials essentially always do |
| keep | `git grep "BEGIN OPENSSH PRIVATE KEY"` | Untouched. A PEM header alone is not key material, so base64 body is required. Without this, any transcript discussing secret detection refuses to export, which teaches people to pass `--allow-leaks` |
| keep | a 40-character git SHA, content hashes, package versions | Untouched |
| remove | `/mnt/c/Users/name/` | Anonymised. WSL, macOS and native Windows shapes, not only the local one |

## The cost gutter

An agent run is almost entirely cache reads, billed at a fraction of the input rate. Adding input
and output tokens and multiplying by the input price overstates a long session several times over:

```
cache-aware  $0.3180
naive        $3.0180     9.5x too high on one synthetic turn
```

An earlier captured 856M-token session illustrated that mistake as roughly **$12,700 instead of
$1,690**. Those historical figures have not been recomputed under the current response-identity
rules and are included only to show the scale of cache-aware pricing errors.

Billing identity comes from the provider's `message.id`. Duplicate transcript rows and streaming
updates for the same response are merged by taking the largest observed cumulative token counter.
The same non-empty message ID remains one response even if its transcript row UUID or model label
changes. If duplicate rows disagree on the model, the first non-empty label sets the price.
If a message ID is absent, the row UUID and model are the fallback identity; if both IDs are absent,
the source row is used. That fallback can overcount repeated rows, but it does not merge two genuine
responses merely because their content is identical.

Pricing confidence is explicit. Only listed model IDs and documented version aliases receive an
exact rate. A recognisable Claude family with an unknown model ID gets a family estimate. An
unrecognised model keeps its tokens in the total while its dollar cost is omitted, and the report
marks the displayed dollar total as incomplete. Every dollar figure is an API-equivalent list-price
estimate in USD based on the dated source shown in the report. It is not an invoice and does not
represent what a subscription user paid.

## Using it

```bash
pip install -e .
agent-replay --list                          # recent transcripts, largest first
agent-replay                                 # export the most recent one
agent-replay run.jsonl -o replay.html
agent-replay run.jsonl --audit-only          # report what would be removed, write nothing
agent-replay run.jsonl --include-thinking    # reasoning blocks, excluded by default
agent-replay run.jsonl --no-sidechains       # drop subagent transcripts
```

Exit `0` success, `1` something survived redaction so nothing was written, `2` bad usage or an
unreadable transcript.

**Thinking blocks are excluded unless you ask.** They are the model's working notes rather than its
answer, and someone sharing a run to report a bug has not necessarily chosen to publish them.

**An empty result is an error.** A transcript producing no events exits 2, because a zero-event
replay looks like a clean run.

**A record the parser does not understand is kept as an `unknown` event**, not dropped. Silently
skipping is how an exporter produces a replay that looks complete and is not.

## The replay itself

One file, no network. Verified in a real browser at 390px and 1280px: **zero requests to any
host**, zero console errors, both themes with `data-theme` overriding the media query in both
directions, and no horizontal page scroll.

- A scrubber that moves through the filtered event list
- Tool calls collapsed by default, errors expanded
- `Edit` and `Write` rendered as inline diffs
- A gutter carrying the running cost and cumulative tokens beside every event
- Filters by kind, by tool, and errors only, plus full-text search
- Redaction markers shown in place, so a reader sees where a value was removed

Event data travels as JSON in a script block and everything is escaped before insertion, so a
transcript containing HTML cannot restyle or script the page displaying it. Transcripts are full of
HTML, since agents read and write web files all day.

## Status

Verified 2026-09-08.

```
$ bash scripts/verify.sh
1. unit suite
  ok    Ran 39 tests passed

2. a synthetic transcript exports end to end
  ok    exported a 17186 byte replay

3. every planted secret is absent from the exported file
  ok    all 15 planted secrets are absent, checked with grep -F against the literal values

3b. each redaction mechanism is individually alive
  known-format         alive   -> please use [redacted:github-token] when you push
  assignment-context   alive   -> ADMIN_PASSWORD=[redacted:assigned-secret]
  entropy              alive   -> the api_key we were issued reads [redacted:high-entropy] exactly
  ok    all three redaction mechanisms independently catch something

4. a transcript with no secrets is NOT mangled
  1 redaction marker(s) in a transcript with no secrets in it
  ok    clean content survives intact, so the tool is not simply blanking everything

5. the exporter REFUSES when something survives
REFUSING to write /tmp/tmp.Drs5lEJETX/refused.html: 1 credential-shaped string(s) survived redaction.
  aws identifier: AKIAQW… (20 chars) at offset 8497
Nothing was written. This is a bug in the redactor, so please report it with the label above and no other detail. Use --allow-leaks only if you have checked the match yourself and it is a false positive.
  exit=1 file_written=False
  ok    a surviving credential blocks the write and exits nonzero

6. the export is genuinely self-contained
  no remote assets, doctype and viewport present, both themes
  ok    self-contained, standards mode, both themes, no personal paths

7. the cost model keeps pricing identity and confidence explicit
  cache-aware $0.3180  naive $3.0180  ratio 9.5x
  ok    cache reads are discounted, aliases are explicit, and unknown rates stay unavailable

8. a real transcript, if one is present
  ok    exported a real transcript (18440840 bytes)

9. no secrets in this repository's own tracked files
  ok    no real home path and no credential-shaped strings (AWS's documented example key allowed)

10 passed, 0 failed
VERIFY OK
```

### The verify was attacked

| Sabotage | Result |
|---|---|
| `KNOWN = []`, the known-format layer emptied | Check 3b names `known-format` DEAD, unit suite fails. Exit 1 |
| `ASSIGNMENTS = []` | Check 3b names `assignment-context` DEAD. Exit 1 |
| `ENTROPY_THRESHOLD = 99.0` | Check 3b names `entropy` DEAD. Exit 1 |
| `find_leaks()` always returns clean | Check 5 fails: the exporter wrote a file containing a credential the checker had flagged. Exit 1 |

The first three passed cleanly before check 3b existed, which is how that check came to exist.

## Limitations

- **Claude Code transcripts only.** The JSONL shape is specific; other agent formats need their
  own parser.
- **A purely alphabetic secret can slip past the entropy layer**, which requires a digit so
  camelCase identifiers survive. The known-format and assignment layers still cover the realistic
  cases, which is what the layering is for.
- **Encryption, reversal and chunking are not attempted.** A secret split across two strings and
  reassembled at runtime will not be found. One layer of base64 is decoded and checked; more is
  not.
- **The cost figure is an API-equivalent USD list-price estimate, not your invoice.** It ignores
  subscription plans, discounts, batch pricing, and spend that happened outside the transcript.
  Calls with no known rate are omitted from dollars, retained in token totals, and disclosed.
- **Long tool outputs are truncated** at `--max-output`, default 6000 characters, with the dropped
  count shown.
- **The diff for `Edit` is not a real LCS.** Equal leading and trailing lines are dropped and the
  differing middle is shown whole, which is right for a single-hunk edit and never claims a line
  is unchanged when it is not.
- **Read the file before you post it.** This makes a leak much less likely and cannot make it
  impossible, and no tool that pattern-matches text can.

## License

MIT.
