"""Model price table and integer cost arithmetic for budgeted enrichment.

Costs are integer micro-USD (contract §2: no floats). Rates are integer
micro-USD per million tokens, so ``cost = ceil(tokens * rate / 1e6)`` stays
exact. The table is Anthropic's first-party list price as of 2026-10-21;
a usage record computed from it carries ``cost_basis = 'estimated'``.

Cache writes are the 5-minute TTL rate (1.25x input). A model missing from
the table cannot be budgeted, so a budgeted run refuses to start with it.
"""

from __future__ import annotations

from dataclasses import dataclass

_MTOK = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """Integer micro-USD per million tokens."""

    input: int
    output: int
    cache_write: int
    cache_read: int


def _usd(input_: str, output: str, cache_write: str, cache_read: str) -> ModelPrice:
    def micro(usd: str) -> int:
        whole, _, frac = usd.partition(".")
        return int(whole) * _MTOK + int((frac + "000000")[:6])

    return ModelPrice(micro(input_), micro(output), micro(cache_write), micro(cache_read))


# USD per MTok: input, output, cache write (5m), cache read.
PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": _usd("1.00", "5.00", "1.25", "0.10"),
    "claude-haiku-4-5-20251001": _usd("1.00", "5.00", "1.25", "0.10"),
    "claude-sonnet-4-6": _usd("3.00", "15.00", "3.75", "0.30"),
    "claude-opus-4-6": _usd("5.00", "25.00", "6.25", "0.50"),
    "claude-sonnet-5-5": _usd("2.00", "10.00", "2.50", "0.20"),
    "claude-opus-5-5": _usd("4.00", "20.00", "5.00", "0.20"),
}


class UnknownModelPrice(KeyError):
    """No price for this model, so its spend cannot be bounded."""


def price_for(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelPrice(
            f"no price for model {model!r} in muninn.enrich.pricing.PRICES; "
            f"add it before running a budgeted task"
        ) from None


def _ceil_div(n: int, d: int) -> int:
    return -(-n // d)


def cost_micro_usd(
    price: ModelPrice,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> int:
    """Cost of one call from its measured usage, rounded up to a micro-USD."""
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_write_tokens * price.cache_write
        + cache_read_tokens * price.cache_read
    )
    return _ceil_div(total, _MTOK)


def worst_case_micro_usd(price: ModelPrice, *, input_tokens: int, max_output_tokens: int) -> int:
    """Upper bound for a call not yet made: every input token billed at the
    dearer of the input and cache-write rates, and the full output cap."""
    in_rate = max(price.input, price.cache_write)
    return _ceil_div(input_tokens * in_rate + max_output_tokens * price.output, _MTOK)


def usd_to_micro(usd: str | float) -> int:
    """Parse a dollar amount (``"1"``, ``"0.25"``) into integer micro-USD."""
    from decimal import Decimal, InvalidOperation, ROUND_DOWN

    try:
        value = Decimal(str(usd))
    except InvalidOperation as exc:
        raise ValueError(f"not a dollar amount: {usd!r}") from exc
    if value < 0 or not value.is_finite():
        raise ValueError(f"not a dollar amount: {usd!r}")
    return int((value * _MTOK).to_integral_value(rounding=ROUND_DOWN))


def format_usd(micro: int) -> str:
    return f"${micro / _MTOK:.4f}"
