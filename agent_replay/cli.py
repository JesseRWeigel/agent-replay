"""Command line interface.

The important behaviour here is the refusal. After the page is rendered, it is scanned by a
checker written independently of the redactors, and if anything credential-shaped survives, the
file is NOT written and the command exits nonzero. Writing a file and printing a warning would
be the wrong shape: the entire purpose is that the output is safe to post, and a warning is
something a person scrolls past.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from . import cost, parse, redact, render, minecraft


def find_transcripts(root=None):
    root = root or os.path.expanduser("~/.claude/projects")
    return sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True),
                  key=lambda p: os.path.getmtime(p), reverse=True)


def scrub_events(events, *, home):
    """Redact every text-bearing field in place, returning combined counts."""
    found: dict = {}

    def merge(res):
        for k, r in res.redactions.items():
            cur = found.setdefault(k, redact.Redaction(k))
            cur.count += r.count
            for s in r.samples:
                if len(cur.samples) < 3:
                    cur.samples.append(s)
        return res.text

    for e in events:
        if e.text:
            e.text = merge(redact.redact(e.text, home=home))
        if e.tool_input:
            # Round-tripping through JSON catches secrets nested at any depth without needing
            # to walk the structure, and keeps the shape intact for rendering.
            try:
                blob = json.dumps(e.tool_input)
            except (TypeError, ValueError):
                blob = str(e.tool_input)
            cleaned = merge(redact.redact(blob, home=home))
            try:
                e.tool_input = json.loads(cleaned)
            except json.JSONDecodeError:
                # Redaction broke the JSON, which can happen if a placeholder lands inside an
                # escape sequence. Keeping the redacted string is right: the alternative is
                # restoring the unredacted original, which would publish the secret.
                e.tool_input = {"_redacted_arguments": cleaned}
    return found


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="agent-replay",
        description="Turn an agent transcript into one self-contained, redacted HTML replay.")
    p.add_argument("transcript", nargs="?",
                   help="path to a .jsonl transcript, or omit to use the most recent")
    p.add_argument("--format", choices=("claude", "minecraft-trial"), default="claude",
                   help="explicit input format; Minecraft requires a prepared summary JSON")
    p.add_argument("-o", "--out", default="replay.html")
    p.add_argument("--title", default=None)
    p.add_argument("--include-thinking", action="store_true",
                   help="include the model's reasoning blocks, which are excluded by default")
    p.add_argument("--no-sidechains", action="store_true",
                   help="drop subagent transcripts")
    p.add_argument("--max-output", type=int, default=6000,
                   help="truncate any single tool output beyond this many characters")
    p.add_argument("--keep-home", action="store_true",
                   help="do not replace the home directory path with ~")
    p.add_argument("--list", action="store_true", help="list recent transcripts and exit")
    p.add_argument("--audit-only", action="store_true",
                   help="report what would be redacted, write nothing")
    p.add_argument("--allow-leaks", action="store_true",
                   help="write the file even if the post-render scan finds something. "
                        "Refuses by default, and you should leave it that way")
    a = p.parse_args(argv)

    if a.format == "minecraft-trial" and (not a.transcript or a.list or a.include_thinking):
        p.error("minecraft-trial requires an explicit file and does not accept --list/--include-thinking")

    if a.list:
        found = find_transcripts()
        if not found:
            sys.stderr.write("no transcripts found under ~/.claude/projects\n")
            return 2
        for f in found[:25]:
            size = os.path.getsize(f)
            print(f"  {size / 1e6:7.1f} MB  {f}")
        return 0

    path = a.transcript
    if not path:
        found = find_transcripts()
        if not found:
            sys.stderr.write("no transcript given and none found under ~/.claude/projects\n")
            return 2
        path = found[0]
        sys.stderr.write(f"using the most recent transcript: {path}\n")
    if not os.path.exists(path):
        sys.stderr.write(f"no such transcript: {path}\n")
        return 2

    try:
        if a.format == "minecraft-trial":
            events, stats = minecraft.parse_trial(path)
        else:
            events, stats = parse.parse_transcript(
                path, include_thinking=a.include_thinking,
                include_sidechains=not a.no_sidechains)
    except (ValueError, OSError, UnicodeError, RecursionError):
        sys.stderr.write("Invalid or unreadable input; no output written.\n")
        return 2
    visible_events = stats["events"]
    if not visible_events:
        sys.stderr.write(
            f"{path} produced no events from {stats['lines']} lines. "
            "That is reported as an error rather than an empty replay, because an empty "
            "output file looks like a clean run.\n")
        return 2

    home = None if a.keep_home else os.path.expanduser("~")
    found = scrub_events(events, home=home)
    red_total = sum(r.count for r in found.values())

    if a.audit_only:
        print(f"{path}")
        print(f"  {stats['lines']:,} lines, {visible_events:,} events, "
              f"{stats['unparsable']} unparsable, {stats['ignored']:,} metadata records")
        if not found:
            print("  nothing to redact")
        for k, r in sorted(found.items()):
            print(f"  {r.count:>5}  {k:<18} e.g. {', '.join(r.samples)}")
        if a.format == "minecraft-trial":
            print("  Token use and cost not supplied")
            return 0
        c = cost.accumulate(events)
        print(f"  {c.total_tokens:,} tokens, API-equivalent USD ${c.dollars:,.2f} "
              f"({c.priced_calls} exact, {c.estimated_calls} family-estimated, "
              f"{c.unpriced_calls} unavailable; not an invoice)")
        return 0

    title = a.title or f"Agent run: {os.path.basename(path)[:8]}"
    subtitle = (f"{visible_events:,} events from {stats['lines']:,} transcript lines"
                + (f", {stats['unparsable']} unparsable" if stats["unparsable"] else "")
                + (f", {stats['sidechain']:,} from subagents" if stats["sidechain"] else ""))
    if a.format == "minecraft-trial":
        subtitle = stats["notice"]
    page = render.build(events, {
        "title": title, "subtitle": subtitle, "redactions": found,
        "thinking": a.include_thinking,
        "accounting_available": a.format != "minecraft-trial",
    }, max_output=a.max_output)

    # The refusal. Scanning the finished page, with a checker that shares no pattern with the
    # redactors, is the only step here that can catch a redactor being wrong.
    leaks = redact.audit(page)
    if leaks:
        sys.stderr.write(
            f"REFUSING to write {a.out}: {len(leaks)} credential-shaped string(s) survived "
            f"redaction.\n")
        for label, excerpt in leaks[:10]:
            sys.stderr.write(f"  {label}: {excerpt}\n")
        if not a.allow_leaks:
            sys.stderr.write(
                "Nothing was written. This is a bug in the redactor, so please report it with "
                "the label above and no other detail. Use --allow-leaks only if you have "
                "checked the match yourself and it is a false positive.\n")
            return 1
        sys.stderr.write("--allow-leaks given, writing anyway.\n")

    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    size = os.path.getsize(a.out)
    if a.format == "minecraft-trial":
        sys.stderr.write(f"wrote {a.out} ({visible_events} events; token use and cost not supplied)\n")
        return 0
    c = cost.accumulate(events)
    incomplete = ", incomplete" if c.unpriced_calls else ""
    sys.stderr.write(
        f"wrote {a.out} ({size / 1e6:.1f} MB, {visible_events:,} events, "
        f"{red_total} secret{'' if red_total == 1 else 's'} removed, "
        f"{c.total_tokens / 1e6:.1f}M tokens, API-equivalent USD ${c.dollars:,.2f}"
        f"{incomplete}, not an invoice)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
