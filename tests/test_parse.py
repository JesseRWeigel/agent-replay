import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_replay.parse import parse_transcript


def user(text, ts="2026-11-19T10:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


class ParseShapeTests(unittest.TestCase):
    def test_mixed_log_keeps_good_events_and_counts_bad_records(self):
        rows = [user("later", "2026-11-19T10:02:00Z"), None, [], 5, "junk",
                user("bad timestamp", 123),
                {"type": "user", "timestamp": "2026-11-19T10:01:00Z",
                 "message": ["not a message"]},
                {"type": ["not a type"]},
                user("first"),
                {"type": "future-record", "timestamp": "2026-11-19T10:01:00Z"},
                user("same timestamp, file order"),
                {"type": "ai-title"}]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "test.jsonl"
            log.write_text("\n".join(map(json.dumps, rows)) + "\n{\n", encoding="utf-8")
            events, stats = parse_transcript(log)

        self.assertEqual(stats["lines"], 13)
        self.assertEqual(stats["unparsable"], 8)
        self.assertEqual(stats["ignored"], 1)
        self.assertEqual(stats["events"], 4)
        self.assertEqual([(event.kind, event.text) for event in events], [
            ("user", "first"), ("user", "same timestamp, file order"),
            ("unknown", "[record of type 'future-record' is not rendered]"), ("user", "later"),
        ])

    def test_cli_reports_bad_record_and_exports_good_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "test.jsonl"
            out = Path(tmp) / "replay.html"
            log.write_text("null\n" + json.dumps(user("ordinary replay message")) + "\n",
                           encoding="utf-8")
            run = subprocess.run([sys.executable, "-m", "agent_replay.cli", str(log),
                                  "-o", str(out)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            page = out.read_text(encoding="utf-8")
            self.assertIn("ordinary replay message", page)
            self.assertIn("1 unparsable", page)

    def test_only_bad_records_does_not_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "test.jsonl"
            out = Path(tmp) / "replay.html"
            log.write_text("null\n[]\n", encoding="utf-8")
            out.write_text("last good replay", encoding="utf-8")
            run = subprocess.run([sys.executable, "-m", "agent_replay.cli", str(log),
                                  "-o", str(out)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 2, run.stderr)
            self.assertIn("no events from 2 lines", run.stderr)
            self.assertEqual(out.read_text(encoding="utf-8"), "last good replay")
