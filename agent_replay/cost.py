"""Token and cost accounting for the gutter.

The reason this is its own module rather than three lines inline: cached tokens are billed
differently from fresh ones, and an agent run is mostly cache reads. Adding
`input_tokens + output_tokens` and multiplying by an input rate overstates a long agent session
several times over, because it prices tens of millions of cache-read tokens at the full input
rate. That is the single easiest way to publish a wrong number here.

The rates are public API list prices in US dollars per million tokens, and they change. Exact
model IDs and supported version aliases are explicit. Family estimates are labelled, and usage
for a model with no rate remains in token totals while its dollar cost is left unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Dollars per million tokens. Anthropic public pricing as published on 2026-07-26.
# cache_write is the 5-minute rate; the 1-hour rate is higher and is applied when the usage
# record distinguishes them.
RATES = {
    "claude-opus-4":     {"input": 15.0, "output": 75.0, "cache_write": 18.75,  "cache_read": 1.50},
    "claude-opus-4-1":   {"input": 15.0, "output": 75.0, "cache_write": 18.75,  "cache_read": 1.50},
    "claude-sonnet-4":   {"input": 3.0,  "output": 15.0, "cache_write": 3.75,   "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,   "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0,  "cache_write": 1.25,   "cache_read": 0.10},
    "claude-3-5-haiku":  {"input": 0.80, "output": 4.0,  "cache_write": 1.0,    "cache_read": 0.08},
}
# Versioned IDs that intentionally use the published rate of their canonical model. An ID absent
# from this table is never classified as exact by substring resemblance.
MODEL_ALIASES = {
    "claude-opus-4-20250514": "claude-opus-4",
    "claude-opus-4-1-20250805": "claude-opus-4-1",
    "claude-sonnet-4-20250514": "claude-sonnet-4",
    "claude-sonnet-4-5-20250929": "claude-sonnet-4-5",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "claude-3-5-haiku-20241022": "claude-3-5-haiku",
}

# Family-level rates are estimates for IDs that are recognisably in a known Claude family but
# are not an exact ID or alias above.
FAMILY = {
    "opus":   {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_write": 1.25,  "cache_read": 0.10},
}
FAMILY_PREFIXES = {
    "claude-opus-": "opus",
    "claude-sonnet-": "sonnet",
    "claude-haiku-": "haiku",
}
RATE_SOURCE = "Anthropic public API list pricing in USD, recorded 2026-07-26"

# Local, cached or replayed turns carry this in place of a model id and always report zero
# tokens. They are free, and counting them as unpriced would put a spurious accuracy warning on
# a run that is in fact fully priced.
SYNTHETIC = "<synthetic>"

# The 1-hour cache write costs more than the 5-minute one.
CACHE_1H_MULTIPLIER = 2.0 / 1.25


@dataclass
class Cost:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    dollars: float = 0.0
    priced_calls: int = 0
    estimated_calls: int = 0
    unpriced_calls: int = 0
    estimated_models: set = field(default_factory=set)
    unpriced_models: set = field(default_factory=set)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write + self.cache_read


def rate_for(model: str):
    """Return (rates, confidence). Confidence is exact, inferred, or unknown."""
    if not model:
        return None, "unknown"
    m = model.lower()
    canonical = MODEL_ALIASES.get(m, m)
    if canonical in RATES:
        return RATES[canonical], "exact"
    for prefix, family in FAMILY_PREFIXES.items():
        if m.startswith(prefix):
            return FAMILY[family], "inferred"
    return None, "unknown"


def _cache_writes(usage: dict) -> tuple[int, int]:
    """Return total writes and the subset explicitly attributed to the 1-hour tier."""
    reported = usage.get("cache_creation_input_tokens", 0) or 0
    detail = usage.get("cache_creation") or {}
    w5 = detail.get("ephemeral_5m_input_tokens", 0) or 0
    w1h = detail.get("ephemeral_1h_input_tokens", 0) or 0
    # In a complete provider response the aggregate equals the lifetime sum. A replay may
    # instead have partial snapshots; retain whichever count accounts for more writes.
    return max(reported, w5 + w1h), w1h


def price(usage: dict, model: str) -> tuple[float | None, str]:
    """API-equivalent USD for one response, or None when no rate is available."""
    if not usage:
        return 0.0, "exact"
    if model == SYNTHETIC:
        return 0.0, "exact"
    r, confidence = rate_for(model)
    if r is None:
        return None, confidence
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    read = usage.get("cache_read_input_tokens", 0) or 0
    write, w1h = _cache_writes(usage)
    # Apply the existing 5-minute fallback only to writes not identified as 1-hour.
    write_cost = (
        ((write - w1h) + w1h * CACHE_1H_MULTIPLIER) * r["cache_write"] / 1e6
    )

    total = (inp * r["input"] + out * r["output"] + read * r["cache_read"]) / 1e6 + write_cost
    return total, confidence


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass
class _BilledResponse:
    usage: dict
    model: str
    dollars: float | None
    confidence: str


def _response_identity(event):
    """Return a billed identity, with a conservative fallback when message.id is absent.

    Provider message IDs identify one billed response across duplicate transcript rows. Without
    that ID, a row UUID and model identify the response. If the UUID is also absent, the source
    row is used. Distinct fallback rows are counted separately because there is no evidence that
    they represent the same billable response.
    """
    if event.message_id:
        return "message", event.message_id
    if event.uuid:
        return "row", event.uuid, event.model
    if event.source_row >= 0:
        return "source-row", event.source_row, event.model
    return "event", event.index, event.model


def _merged_usage(previous: dict, update: dict) -> dict:
    """Merge cumulative streaming snapshots by keeping each largest observed counter."""
    merged = dict(previous)
    for name in TOKEN_FIELDS:
        merged[name] = max(previous.get(name, 0) or 0, update.get(name, 0) or 0)

    old_detail = previous.get("cache_creation") or {}
    new_detail = update.get("cache_creation") or {}
    if old_detail or new_detail:
        merged["cache_creation"] = {
            "ephemeral_5m_input_tokens": max(
                old_detail.get("ephemeral_5m_input_tokens", 0) or 0,
                new_detail.get("ephemeral_5m_input_tokens", 0) or 0,
            ),
            "ephemeral_1h_input_tokens": max(
                old_detail.get("ephemeral_1h_input_tokens", 0) or 0,
                new_detail.get("ephemeral_1h_input_tokens", 0) or 0,
            ),
        }
    merged["cache_creation_input_tokens"], _ = _cache_writes(merged)
    return merged


def _token_total(usage: dict) -> int:
    return sum(usage.get(name, 0) or 0 for name in TOKEN_FIELDS)


def _update_response(responses: dict, event):
    identity = _response_identity(event)
    previous = responses.get(identity)
    usage = _merged_usage(previous.usage if previous else {}, event.usage)
    # Conflicting labels on duplicate rows are ambiguous. Keep the first non-empty label so
    # pricing is deterministic while the provider message ID still prevents double counting.
    model = previous.model if previous and previous.model else event.model
    dollars, confidence = price(usage, model)
    current = _BilledResponse(usage, model, dollars, confidence)
    responses[identity] = current
    return previous, current


def accumulate(events) -> Cost:
    c = Cost()
    responses = {}
    for e in events:
        if not e.usage:
            continue
        _update_response(responses, e)

    for response in responses.values():
        u = response.usage
        c.input_tokens += u.get("input_tokens", 0) or 0
        c.output_tokens += u.get("output_tokens", 0) or 0
        c.cache_read += u.get("cache_read_input_tokens", 0) or 0
        c.cache_write += u.get("cache_creation_input_tokens", 0) or 0
        if response.dollars is not None:
            c.dollars += response.dollars
        if response.confidence == "exact":
            c.priced_calls += 1
        elif response.confidence == "inferred":
            c.estimated_calls += 1
            if response.model and response.model != SYNTHETIC:
                c.estimated_models.add(response.model)
        else:
            c.unpriced_calls += 1
            if response.model and response.model != SYNTHETIC:
                c.unpriced_models.add(response.model)
    return c


def running(events):
    """Cumulative dollars and tokens after each event, for the gutter."""
    out = []
    total_d = 0.0
    total_t = 0
    responses = {}
    for e in events:
        if e.usage:
            previous, current = _update_response(responses, e)
            total_d += (current.dollars or 0.0) - (
                previous.dollars if previous and previous.dollars is not None else 0.0
            )
            total_t += _token_total(current.usage) - (
                _token_total(previous.usage) if previous else 0
            )
        out.append((total_d, total_t))
    return out
