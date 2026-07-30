"""Token and cost accounting for the gutter.

The reason this is its own module rather than three lines inline: cached tokens are billed
differently from fresh ones, and an agent run is mostly cache reads. Adding
`input_tokens + output_tokens` and multiplying by an input rate overstates a long agent session
several times over, because it prices tens of millions of cache-read tokens at the full input
rate. That is the single easiest way to publish a wrong number here.

The rates are public list prices in dollars per million tokens, and they change. They live in
one dict, the source and the date they were taken are recorded, and `unknown_models` is reported
so a run priced with a fallback rate is never presented as though it were exact.
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
# Family-level rates, used when the exact model id is unknown but its family is not. A run on
# claude-opus-5 is priced at opus rates and reported as INFERRED, never as exact. Inventing a
# specific published price for a model whose pricing has not been verified would be worse than
# saying the number is an estimate.
FAMILY = {
    "opus":   {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_write": 1.25,  "cache_read": 0.10},
}
# Used when neither the id nor the family is recognised. Deliberately the most expensive tier,
# so an unknown model produces an over-estimate rather than a flattering under-estimate.
FALLBACK = {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50}
RATE_SOURCE = "Anthropic public list pricing, recorded 2026-07-26"

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
    unknown_models: set = field(default_factory=set)
    priced_calls: int = 0
    unpriced_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write + self.cache_read


def rate_for(model: str):
    """Return (rates, confidence). Confidence is exact, inferred, or unknown.

    Longest match wins, so claude-sonnet-4-5-20250101 finds claude-sonnet-4-5 rather than
    claude-sonnet-4.
    """
    if not model:
        return None, "unknown"
    m = model.lower()
    best = None
    for key, r in RATES.items():
        if key in m and (best is None or len(key) > len(best[0])):
            best = (key, r)
    if best:
        return best[1], "exact"
    for fam, r in FAMILY.items():
        if fam in m:
            return r, "inferred"
    return None, "unknown"


def price(usage: dict, model: str) -> tuple[float, str]:
    """Dollars for one assistant turn, and the confidence in the rate used."""
    if not usage:
        return 0.0, "exact"
    if model == SYNTHETIC:
        return 0.0, "exact"
    r, confidence = rate_for(model)
    r = r or FALLBACK
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    read = usage.get("cache_read_input_tokens", 0) or 0
    write = usage.get("cache_creation_input_tokens", 0) or 0

    # Split the cache write across its two lifetimes when the record says so.
    detail = usage.get("cache_creation") or {}
    w5 = detail.get("ephemeral_5m_input_tokens")
    w1h = detail.get("ephemeral_1h_input_tokens")
    if w5 is not None or w1h is not None:
        w5 = w5 or 0
        w1h = w1h or 0
        write_cost = (w5 * r["cache_write"] + w1h * r["cache_write"] * CACHE_1H_MULTIPLIER) / 1e6
    else:
        write_cost = write * r["cache_write"] / 1e6

    total = (inp * r["input"] + out * r["output"] + read * r["cache_read"]) / 1e6 + write_cost
    return total, confidence


def accumulate(events) -> Cost:
    c = Cost()
    seen = set()
    for e in events:
        if not e.usage:
            continue
        # One usage record covers a whole assistant turn, and that turn can produce several
        # events (text plus tool calls). Counting it once per event would multiply the bill by
        # the number of blocks in the message.
        key = (e.uuid, e.model)
        if key in seen:
            continue
        seen.add(key)
        u = e.usage
        c.input_tokens += u.get("input_tokens", 0) or 0
        c.output_tokens += u.get("output_tokens", 0) or 0
        c.cache_read += u.get("cache_read_input_tokens", 0) or 0
        c.cache_write += u.get("cache_creation_input_tokens", 0) or 0
        d, confidence = price(u, e.model)
        c.dollars += d
        if confidence == "exact":
            c.priced_calls += 1
        else:
            c.unpriced_calls += 1
            if e.model and e.model != SYNTHETIC:
                c.unknown_models.add(f"{e.model} ({confidence})")
    return c


def running(events):
    """Cumulative dollars and tokens after each event, for the gutter."""
    out = []
    total_d = 0.0
    total_t = 0
    seen = set()
    for e in events:
        if e.usage:
            key = (e.uuid, e.model)
            if key not in seen:
                seen.add(key)
                u = e.usage
                d, _c = price(u, e.model)
                total_d += d
                total_t += ((u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0)
                            + (u.get("cache_read_input_tokens", 0) or 0)
                            + (u.get("cache_creation_input_tokens", 0) or 0))
        out.append((total_d, total_t))
    return out
