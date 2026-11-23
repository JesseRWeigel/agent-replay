import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_replay import cli


class OutputPathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "input.jsonl"
        self.original = (json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "Keep this transcript."}]},
        }) + "\n").encode()
        self.source.write_bytes(self.original)

    def run_cli(self, output, *options):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main([str(self.source), "-o", str(output), *options])
        return code, err.getvalue()

    def test_export_rejects_source_and_aliases(self):
        symlink = self.root / "symbolic.html"
        symlink.symlink_to(self.source)
        hardlink = self.root / "hard.html"
        os.link(self.source, hardlink)
        for path in (self.source, symlink, hardlink):
            with self.subTest(output=path.name):
                self.source.write_bytes(self.original)
                code, err = self.run_cli(path)
                self.assertEqual(self.source.read_bytes(), self.original)
                self.assertEqual(code, 2)
                self.assertIn("same file", err)

    def test_audit_only_never_writes_even_with_matching_paths(self):
        code, _ = self.run_cli(self.source, "--audit-only")
        self.assertEqual(code, 0)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_export_still_replaces_an_existing_distinct_output(self):
        output = self.root / "replay.html"
        output.write_text("old output", encoding="utf-8")
        code, _ = self.run_cli(output)
        self.assertEqual(code, 0)
        self.assertIn("Keep this transcript.", output.read_text(encoding="utf-8"))
        self.assertEqual(self.source.read_bytes(), self.original)
