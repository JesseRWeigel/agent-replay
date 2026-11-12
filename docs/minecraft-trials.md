# Minecraft trial summaries

This adapter shows a deliberately prepared summary of actions and replies. It is **not** a raw
log importer or an autonomous-game evaluator. It never reads model prompts, reasoning,
transcripts, screenshots, environment variables, runtime paths or private evidence directories.

```
python -m agent_replay.cli examples/minecraft-failure.json \
  --format minecraft-trial -o replay.html --title 'Synthetic Minecraft failure walkthrough'
```

The example is invented, not a recording of an actual model run. It demonstrates how an action
can finish without the collection task succeeding. Review and consent are still required before
sharing real evidence: secret scanning is not a privacy guarantee or permission to publish.

## Input contract

The JSON object must contain exactly `schema`, `provenance`, `outcome` and `actions`:

- `schema`: `minecraft-replay-v1`.
- `provenance`: `synthetic` or `reviewed-derived`. The latter means a human prepared and reviewed
  the summary; the viewer does not independently verify this assertion.
- `outcome`: `infrastructure` (`qualified`, `failed`, `unknown`) and `acquired` (`true`, `false`,
  `null`). Populate acquired only from independent endpoint evidence, never participant inventory
  or a successful dig. A failed run's failure-path observation can support `false`, not success.
- `actions`: 1–256 `{action, reply}` pairs in recorded order. No timestamps are synthesized.

Supported actions: `observe`, `dig`, `finish` without parameters; `look` with `yaw`, `pitch`;
`look_at` and `move_to` with `x`, `y`, `z`; `move` with `direction` (`forward`, `back`, `left`,
`right`) and `ticks`. These are display data only; the viewer never executes them.

A reply has `status` (`completed`, `rejected`, `finished`) and optional `error`/`observation`.
An error has an identifier `code` and Boolean `recoverable`. An observation may contain:

- `position`: numeric `x`, `y`, `z`.
- `inventory`: items with `name`, `count`.
- `nearbyDroppedItems`: items with `name`, `count`, `position`.

Unknown keys at every level are rejected, not silently copied. Trim reviewed derived protocol
receipts to this contract manually. Do not point the adapter at raw logs. Files are limited to
1 MB, action pairs to 256, each item list to 64, and numeric/string forms are constrained.
Tokens and prices are deliberately unavailable; zero would incorrectly imply a free run.
Existing redaction and post-render leak refusal still run. A custom title is escaped, and the
post-render scanner checks the complete HTML, but reviewers should avoid personal data in titles.

## Verification

`python -m unittest discover -s tests -t .` includes schema refusal, raw-field exclusion,
outcome interpretation, CLI failure/no-write, finite-number and accounting checks. The normal
`scripts/verify.sh` also checks leak refusal and existing transcript behavior. Its real-transcript
stage requires a user-provided local transcript; do not obtain private transcripts just to make
that optional-environment check green.

Browser checks: open the generated file, inspect synthetic banner and NOT-acquired summary,
expand/collapse actions, filter rejected actions, clear filters, scrub to the end, change theme,
and inspect at desktop and 390px widths. Ensure it makes no network requests. A successful
synthetic rendering is not evidence of successful Minecraft gameplay.
