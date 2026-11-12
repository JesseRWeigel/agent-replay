import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from agent_replay import cli, minecraft, render

FIXTURE = Path(__file__).resolve().parents[1] / 'examples/minecraft-failure.json'

class MinecraftTests(unittest.TestCase):
    def setUp(self):
        self.trial = json.loads(FIXTURE.read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'input.json'
        self.out = Path(self.tmp.name) / 'replay.html'

    def parse(self, trial=None):
        self.path.write_text(json.dumps(self.trial if trial is None else trial))
        return minecraft.parse_trial(self.path)

    def test_failure_fixture(self):
        events, stats = self.parse()
        self.assertEqual(len(events), 10)
        self.assertEqual(stats['provenance'], 'synthetic')
        self.assertIn('SYNTHETIC', events[0].text)
        self.assertIn('NOT acquired', events[-1].text)
        self.assertTrue(events[-1].is_error)
        self.assertTrue(events[6].is_error)

    def test_no_raw_fields_at_any_level(self):
        for scope in ('root', 'pair', 'action', 'reply', 'observation', 'item'):
            trial = copy.deepcopy(self.trial)
            targets = {'root': trial, 'pair': trial['actions'][1],
                       'action': trial['actions'][1]['action'],
                       'reply': trial['actions'][1]['reply'],
                       'observation': trial['actions'][1]['reply']['observation'],
                       'item': trial['actions'][1]['reply']['observation']['nearbyDroppedItems'][0]}
            targets[scope]['prompt'] = 'PRIVATE-CONTENT'
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.parse(trial)

    def test_unknown_action_rejected(self):
        self.trial['actions'][0]['action']['kind'] = 'shell'
        with self.assertRaises(ValueError): self.parse()

    def test_no_success_from_inventory_or_completed_action(self):
        self.trial['actions'][-1]['reply']['observation']['inventory'] = [{'name':'oak_log','count':1}]
        events, _ = self.parse()
        self.assertIn('NOT acquired', events[-1].text)

    def test_unqualified_endpoint_cannot_claim_success(self):
        self.trial['outcome'] = {'infrastructure':'failed', 'acquired':True}
        events, _ = self.parse()
        self.assertIn('NOT established', events[-1].text)
        self.assertTrue(events[-1].is_error)

    def test_success_only_reported_as_supplied(self):
        self.trial['provenance'] = 'reviewed-derived'
        self.trial['outcome']['acquired'] = True
        events, _ = self.parse()
        self.assertIn('not independently verified', events[0].text)
        self.assertIn('Supplied independent endpoint: acquired', events[-1].text)
        self.assertFalse(events[-1].is_error)

    def test_finite_numeric_values_only(self):
        for value in (True, float('nan'), float('inf'), 'PRIVATE', 1e99):
            self.trial['actions'][0]['action']['x'] = value
            with self.subTest(value=value), self.assertRaises(ValueError): self.parse()

    def test_oversized_integer_rejected_cleanly(self):
        for value in (10**1000, -(10**1000)):
            self.trial['actions'][0]['action']['x'] = value
            with self.subTest(sign=value > 0), self.assertRaises(ValueError):
                self.parse()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main([str(self.path), '--format', 'minecraft-trial',
                                           '-o', str(self.out)]), 2)
            self.assertFalse(self.out.exists())

    def test_duplicate_keys_rejected_at_all_depths(self):
        original = json.dumps(self.trial)
        for field in ('provenance', 'acquired', 'kind', 'status', 'name'):
            duplicate = original.replace('"' + field + '":',
                                         '"' + field + '": null, "' + field + '":', 1)
            self.path.write_text(duplicate)
            with self.subTest(field=field), self.assertRaises(ValueError):
                minecraft.parse_trial(self.path)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main([str(self.path), '--format', 'minecraft-trial',
                                           '-o', str(self.out)]), 2)
            self.assertFalse(self.out.exists())

    def test_status_and_boolean_validation(self):
        self.trial['outcome']['acquired'] = 'false'
        with self.assertRaises(ValueError): self.parse()

    def test_size_and_event_limits(self):
        self.path.write_bytes(b' ' * (minecraft.MAX_BYTES + 1))
        with self.assertRaises(ValueError): minecraft.parse_trial(self.path)
        self.trial['actions'] = self.trial['actions'][:1] * 257
        with self.assertRaises(ValueError): self.parse()
        self.trial['actions'] = []
        with self.assertRaises(ValueError): self.parse()

    def test_cli_export_and_unknown_accounting(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([str(FIXTURE), '--format', 'minecraft-trial', '-o', str(self.out)]), 0)
        html = self.out.read_text()
        self.assertIn('SYNTHETIC ILLUSTRATION', html)
        self.assertIn('No price estimate is made', html)
        self.assertIn('"usd":null', html)
        self.assertNotIn('<b>$0.00</b>', html)
        self.assertIn('navigation_blocked', html)

    def test_invalid_input_no_write_or_content_in_error(self):
        self.path.write_text('{"raw":"PRIVATE-CONTENT"}')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main([str(self.path), '--format', 'minecraft-trial', '-o', str(self.out)]), 2)
        self.assertFalse(self.out.exists())
        self.assertNotIn('PRIVATE-CONTENT', stderr.getvalue())

    def test_explicit_path_and_no_thinking(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['--format', 'minecraft-trial'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main([str(FIXTURE), '--format', 'minecraft-trial', '--include-thinking'])

    def test_bad_item_and_error_text_rejected(self):
        for invalid in ('<script>', 'arbitrary private text'):
            self.trial['actions'][2]['reply']['error']['code'] = invalid
            with self.assertRaises(ValueError): self.parse()

if __name__ == '__main__': unittest.main()
