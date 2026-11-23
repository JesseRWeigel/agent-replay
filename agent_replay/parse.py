"""Read a Claude Code transcript into a flat list of events.

A transcript is JSON Lines, one record per line, and the records are a linked list: each carries
a `parentUuid`. They are not guaranteed to be in order in the file, and a resumed session can
interleave records from more than one branch, so ordering by file position is wrong. Events are
sorted by timestamp with file position as the tie-break; parent IDs are retained as metadata.

Two decisions worth stating.

Thinking blocks are excluded unless explicitly asked for. They are the model's working notes
rather than its answer, someone sharing a run to report a bug is not necessarily choosing to
publish them, and the safe default for a tool whose output is meant to be posted publicly is the
quieter one.

A record this parser does not understand is kept as an `unknown` event rather than dropped.
Silently skipping is how an exporter produces a replay that looks complete and is not, and the
reader of a bug report has no way to tell that a third of the run is missing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Event:
    index: int
    kind: str                  # user | assistant | tool_use | tool_result | thinking | system | unknown
    role: str
    ts: str
    text: str = ""
    tool: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_id: str = ""
    is_error: bool = False
    usage: dict = field(default_factory=dict)
    model: str = ""
    message_id: str = ""
    uuid: str = ""
    source_row: int = -1
    parent: str = ""
    sidechain: bool = False
    raw_type: str = ""
    hidden: bool = False


# Record types that carry no content a reader of a replay would want. They are counted and
# reported rather than silently dropped, so the totals still add up.
IGNORED_TYPES = {
    "last-prompt", "mode", "permission-mode", "file-history-snapshot", "file-history-delta",
    "queue-operation", "frame-link", "attachment", "ai-title",
}


def _blocks(content):
    """Message content is either a plain string or a list of typed blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


_ISO_SECONDS_FRACTION = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})[.,]([0-9]+)"
)


def _chronological_key(record):
    source_row, rec = record
    raw = rec.get("timestamp")
    if raw:
        try:
            # Python 3.10's fromisoformat does not accept Z or arbitrary fraction widths.
            # Separate ISO fractional seconds so sorting also keeps sub-microsecond precision.
            normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            match = _ISO_SECONDS_FRACTION.match(normalized)
            fraction = match[2].rstrip("0") if match else ""
            if match:
                normalized = match[1] + normalized[match.end():]
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (1, parsed.astimezone(timezone.utc), fraction, source_row)
        except (ValueError, OverflowError):
            pass
    # No reliable time: retain the event, ahead of timed rows, in file order.
    return (0, datetime.min.replace(tzinfo=timezone.utc), "", source_row)


def parse_transcript(path, *, include_thinking: bool = False, include_sidechains: bool = True):
    """Return events and counts, skipping unparseable JSON or invalid record envelopes."""
    records = []
    stats = {"lines": 0, "unparsable": 0, "ignored": 0, "sidechain": 0}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for source_row, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            stats["lines"] += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Counted, not hidden. A truncated final line is normal for a live session.
                stats["unparsable"] += 1
                continue
            # The line can be valid JSON without being a record. Reject incompatible
            # envelopes before sorting or message extraction, without losing the good rows.
            if (not isinstance(rec, dict)
                    or not isinstance(rec.get("timestamp", ""), (str, type(None)))
                    or not isinstance(rec.get("type", ""), str)
                    or not isinstance(rec.get("message"), (dict, type(None)))):
                stats["unparsable"] += 1
                continue
            records.append((source_row, rec))
    # Sort by instant, not timestamp spelling. Keep file order for equal instants.
    records.sort(key=_chronological_key)

    events: list[Event] = []
    i = 0
    for source_row, rec in records:
        rtype = rec.get("type", "")
        if rtype in IGNORED_TYPES:
            stats["ignored"] += 1
            continue
        side = bool(rec.get("isSidechain"))
        if side:
            stats["sidechain"] += 1
            if not include_sidechains:
                continue
        ts = rec.get("timestamp") or ""
        uuid = rec.get("uuid", "")
        parent = rec.get("parentUuid") or ""
        msg = rec.get("message") or {}
        usage = msg.get("usage") or {}
        model = msg.get("model", "")
        message_id = msg.get("id", "")
        if not isinstance(message_id, str):
            message_id = ""
        row_event_start = len(events)

        if rtype in ("user", "assistant"):
            for b in _blocks(msg.get("content")):
                bt = b.get("type")
                if bt == "text":
                    txt = (b.get("text") or "").strip()
                    if not txt:
                        continue
                    events.append(Event(i, rtype, rtype, ts, text=txt, usage=usage,
                                        model=model, uuid=uuid, parent=parent,
                                        sidechain=side, raw_type=rtype))
                elif bt == "thinking":
                    if not include_thinking:
                        continue
                    events.append(Event(i, "thinking", "assistant", ts,
                                        text=(b.get("thinking") or "").strip(), usage=usage,
                                        model=model, uuid=uuid, parent=parent,
                                        sidechain=side, raw_type=rtype))
                elif bt == "tool_use":
                    events.append(Event(i, "tool_use", "assistant", ts,
                                        tool=b.get("name", "?"),
                                        tool_input=b.get("input") or {},
                                        tool_id=b.get("id", ""), usage=usage, model=model,
                                        uuid=uuid, parent=parent, sidechain=side,
                                        raw_type=rtype))
                elif bt == "tool_result":
                    content = b.get("content")
                    if isinstance(content, list):
                        text = "\n".join(
                            c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text")
                    else:
                        text = content if isinstance(content, str) else json.dumps(content)[:4000]
                    events.append(Event(i, "tool_result", "user", ts, text=text or "",
                                        tool_id=b.get("tool_use_id", ""),
                                        is_error=bool(b.get("is_error")), uuid=uuid,
                                        parent=parent, sidechain=side, raw_type=rtype))
                else:
                    events.append(Event(i, "unknown", rtype, ts,
                                        text=f"[unrendered block of type {bt!r}]",
                                        uuid=uuid, parent=parent, sidechain=side, raw_type=rtype))
                i += 1
        elif rtype == "system":
            events.append(Event(i, "system", "system", ts,
                                text=str(rec.get("content") or rec.get("subtype") or "system"),
                                uuid=uuid, parent=parent, sidechain=side, raw_type=rtype))
            i += 1
        else:
            events.append(Event(i, "unknown", "system", ts,
                                text=f"[record of type {rtype!r} is not rendered]",
                                uuid=uuid, parent=parent, sidechain=side, raw_type=rtype))
            i += 1

        row_events = events[row_event_start:]
        if usage and not any(event.usage for event in row_events):
            # A cumulative usage update may have empty content, hidden thinking only, or only
            # block types this parser cannot render. Keep its accounting without inventing a
            # visible transcript row or exposing hidden content.
            events.append(Event(i, "accounting", "assistant", ts, usage=usage, model=model,
                                uuid=uuid, parent=parent, sidechain=side, raw_type=rtype,
                                hidden=True))
            i += 1

        for event in events[row_event_start:]:
            event.message_id = message_id
            event.source_row = source_row

    # Pair each tool_result back to the call that produced it, so the UI can nest them.
    by_id = {e.tool_id: e for e in events if e.kind == "tool_use" and e.tool_id}
    for e in events:
        if e.kind == "tool_result" and e.tool_id in by_id:
            e.tool = by_id[e.tool_id].tool

    stats["events"] = sum(not event.hidden for event in events)
    return events, stats
