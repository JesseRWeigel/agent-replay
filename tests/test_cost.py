"""Billing identity, cumulative usage, and pricing confidence tests."""
import json
import os
import tempfile
import unittest

from agent_replay.cost import accumulate, price, rate_for, running
from agent_replay.parse import Event, parse_transcript
from agent_replay.render import build


SONNET = "claude-sonnet-4-5"
USAGE = {"input_tokens": 100, "output_tokens": 20}


def assistant_record(row_uuid, message_id, usage=None, model=SONNET, second=0):
    message = {
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "synthetic response"}],
        "usage": usage or USAGE,
    }
    if message_id is not None:
        message["id"] = message_id
    record = {
        "type": "assistant",
        "timestamp": f"2026-09-08T12:00:{second:02d}.000Z",
        "message": message,
    }
    if row_uuid is not None:
        record["uuid"] = row_uuid
    return record


def parse_records(records):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")
        path = stream.name
    try:
        return parse_transcript(path)[0]
    finally:
        os.unlink(path)


class TestBilledResponseIdentity(unittest.TestCase):
    def test_parser_preserves_provider_message_id(self):
        events = parse_records([assistant_record("row-1", "message-1")])
        self.assertEqual(events[0].message_id, "message-1")

    def test_duplicate_rows_for_one_message_are_billed_once(self):
        events = parse_records([
            assistant_record("row-1", "one-billed-response", second=1),
            assistant_record("row-2", "one-billed-response", second=2),
        ])

        total = accumulate(events)
        self.assertEqual(total.priced_calls, 1)
        self.assertEqual(total.total_tokens, 120)
        self.assertEqual(running(events)[-1], (total.dollars, 120))

    def test_cumulative_updates_use_the_largest_usage_snapshot(self):
        events = parse_records([
            assistant_record("row-1", "streamed-response", USAGE, second=1),
            assistant_record(
                "row-2",
                "streamed-response",
                {"input_tokens": 150, "output_tokens": 30},
                second=2,
            ),
        ])

        total = accumulate(events)
        self.assertEqual(total.priced_calls, 1)
        self.assertEqual(total.total_tokens, 180)
        self.assertEqual([tokens for _dollars, tokens in running(events)], [120, 180])
        self.assertEqual(running(events)[-1][0], total.dollars)

    def test_partial_cache_breakdown_keeps_reported_writes_in_price(self):
        usage = {
            "cache_creation_input_tokens": 1000,
            "cache_creation": {"ephemeral_1h_input_tokens": 250},
        }
        dollars, confidence = price(usage, SONNET)
        # 750 unclassified tokens use the existing 5m fallback rate.
        self.assertAlmostEqual(dollars, (750 * 3.75 + 250 * 6.0) / 1e6)
        self.assertEqual(confidence, "exact")

    def test_complete_cache_breakdown_does_not_double_count(self):
        usage = {
            "cache_creation_input_tokens": 1000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 750,
                "ephemeral_1h_input_tokens": 250,
            },
        }
        events = parse_records([assistant_record("row-1", "complete-cache", usage)])
        total = accumulate(events)
        self.assertEqual(total.cache_write, 1000)
        self.assertAlmostEqual(total.dollars, (750 * 3.75 + 250 * 6.0) / 1e6)
        fallback, _ = price({"cache_creation_input_tokens": 1000}, SONNET)
        self.assertAlmostEqual(fallback, 1000 * 3.75 / 1e6)

    def test_cache_breakdown_only_updates_the_replay_token_total(self):
        first = {"cache_creation": {"ephemeral_5m_input_tokens": 500}}
        second = {"cache_creation": {"ephemeral_1h_input_tokens": 200}}
        events = parse_records([
            assistant_record("row-1", "cache-update", first, second=1),
            assistant_record("row-2", "cache-update", second, second=2),
        ])
        total = accumulate(events)
        timeline = running(events)
        self.assertEqual(total.cache_write, 700)
        self.assertEqual([tokens for _, tokens in timeline], [500, 700])
        self.assertAlmostEqual(total.dollars, (500 * 3.75 + 200 * 6.0) / 1e6)
        self.assertAlmostEqual(timeline[-1][0], total.dollars)

    def test_content_free_snapshot_updates_accounting_without_rendered_content(self):
        initial = assistant_record(
            "row-1", "streamed-response", {"input_tokens": 100, "output_tokens": 2}, second=1
        )
        final = assistant_record(
            "row-2", "streamed-response", {"input_tokens": 100, "output_tokens": 10}, second=2
        )
        final["message"]["content"] = []

        events = parse_records([initial, final])
        total = accumulate(events)
        page = build(events, {"title": "Synthetic replay"})
        payload = page.split('<script id="events" type="application/json">', 1)[1]
        payload = json.loads(payload.split("</script>", 1)[0])

        self.assertEqual(total.output_tokens, 10)
        self.assertEqual(running(events)[-1][1], 110)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["kind"], "assistant")
        self.assertEqual(payload[0]["tok"], 110)
        self.assertEqual(payload[0]["usd"], round(total.dollars, 6))

    def test_unknown_only_blocks_still_retain_their_usage(self):
        record = assistant_record("row-1", "future-response")
        record["message"]["content"] = [{"type": "future_block", "value": "opaque"}]

        events = parse_records([record])

        self.assertEqual(accumulate(events).total_tokens, 120)
        self.assertTrue(any(event.kind == "unknown" for event in events))

    def test_hidden_thinking_keeps_usage_without_exposing_thinking_text(self):
        record = assistant_record("row-1", "thinking-response")
        record["message"]["content"] = [
            {"type": "thinking", "thinking": "private synthetic reasoning"}
        ]

        events = parse_records([record])
        page = build(events, {"title": "Synthetic replay"})

        self.assertEqual(accumulate(events).total_tokens, 120)
        self.assertNotIn("private synthetic reasoning", page)

    def test_distinct_message_ids_with_identical_content_are_separate(self):
        events = parse_records([
            assistant_record("row-1", "message-1", second=1),
            assistant_record("row-2", "message-2", second=2),
        ])

        total = accumulate(events)
        self.assertEqual(total.priced_calls, 2)
        self.assertEqual(total.total_tokens, 240)

    def test_missing_ids_fall_back_to_distinct_source_rows(self):
        events = parse_records([
            assistant_record(None, None, second=1),
            assistant_record(None, None, second=2),
        ])

        total = accumulate(events)
        self.assertEqual(total.priced_calls, 2)
        self.assertEqual(total.total_tokens, 240)

    def test_same_message_id_is_one_response_even_when_model_labels_differ(self):
        events = parse_records([
            assistant_record("row-1", "message-1", model=SONNET, second=1),
            assistant_record(
                "row-2", "message-1", model="claude-opus-4", second=2
            ),
        ])

        total = accumulate(events)
        self.assertEqual(total.priced_calls, 1)
        self.assertEqual(total.total_tokens, 120)
        self.assertEqual(total.dollars, price(USAGE, SONNET)[0])


class TestPricingIdentity(unittest.TestCase):
    def test_lookalike_model_id_is_not_exact(self):
        _rates, confidence = rate_for("claude-opus-4-999")
        self.assertEqual(confidence, "inferred")

    def test_supported_ids_and_version_aliases_are_explicitly_exact(self):
        exact_ids = (
            "claude-opus-4",
            "claude-opus-4-20250514",
            "claude-opus-4-1-20250805",
            "claude-sonnet-4-5",
            "claude-sonnet-4-5-20250929",
            "claude-haiku-4-5-20251001",
        )
        for model in exact_ids:
            with self.subTest(model=model):
                self.assertEqual(rate_for(model)[1], "exact")

    def test_unrecognised_model_has_no_invented_dollar_price(self):
        dollars, confidence = price(USAGE, "provider-model-with-no-rate")
        self.assertIsNone(dollars)
        self.assertEqual(confidence, "unknown")

    def test_mixed_priced_and_unpriced_usage_keeps_all_token_totals(self):
        events = [
            Event(0, "assistant", "assistant", "", usage=USAGE,
                  model=SONNET, message_id="priced"),
            Event(1, "assistant", "assistant", "", usage=USAGE,
                  model="provider-model-with-no-rate", message_id="unpriced"),
        ]

        total = accumulate(events)
        self.assertEqual(total.total_tokens, 240)
        self.assertEqual(total.priced_calls, 1)
        self.assertEqual(total.estimated_calls, 0)
        self.assertEqual(total.unpriced_calls, 1)
        self.assertEqual(running(events)[-1], (total.dollars, total.total_tokens))

    def test_report_discloses_api_estimate_currency_source_and_incomplete_total(self):
        events = [
            Event(0, "assistant", "assistant", "", text="priced", usage=USAGE,
                  model=SONNET, message_id="priced"),
            Event(1, "assistant", "assistant", "", text="unknown", usage=USAGE,
                  model="provider-model-with-no-rate", message_id="unpriced"),
        ]

        page = build(events, {"title": "Synthetic replay"})
        self.assertIn("API-equivalent", page)
        self.assertIn("USD", page)
        self.assertIn("not an invoice", page)
        self.assertIn("incomplete", page)
        self.assertIn("Anthropic public API list pricing", page)


if __name__ == "__main__":
    unittest.main()
