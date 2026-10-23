"""Spend caps for the synthesis API runner (ADR-013).

Two caps, both in integer micro-USD (``muninn.enrich.pricing``):

* **per run**: one CLI invocation (``--max-cost-usd``). A
  ``synthesize deep-pass --pending`` run shares it across its tasks.
* **monthly**: ``MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD``, checked against
  the synthesis ``usage_records`` rows of the current UTC calendar month,
  read fresh before every call. It is required: without it the API
  runner refuses to spend.

Before each call the runner's worst-case cost for it must fit in what is
left of both caps; a call that would not fit is never made and the
attempt is recorded as ``cap_hit``. After each call the measured cost is
written to ``usage_records`` and charged to the run.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from muninn.config import SYNTHESIS_MONTHLY_BUDGET_ENV
from muninn.enrich.pricing import format_usd, usd_to_micro

# usage_records.operation for a synthesis call is "synthesize:<task_type>".
OPERATION_PREFIX = "synthesize:"
DEFAULT_RUN_CAP_MICRO_USD = 3_000_000  # $3.00 per run


class SynthesisConfigError(RuntimeError):
    """Synthesis cannot spend: a cap or the model's price is not configured."""


def month_start(now: int) -> int:
    t = datetime.fromtimestamp(now, timezone.utc)
    return int(datetime(t.year, t.month, 1, tzinfo=timezone.utc).timestamp())


def monthly_spent_micro_usd(conn: sqlite3.Connection, now: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_micro_usd), 0) FROM usage_records "
        "WHERE kind = 'model' AND operation LIKE ? AND occurred_at >= ?",
        (OPERATION_PREFIX + "%", month_start(now)),
    ).fetchone()
    return int(row[0])


def monthly_cap_from_env() -> int:
    raw = os.environ.get(SYNTHESIS_MONTHLY_BUDGET_ENV)
    if raw is None or not raw.strip():
        raise SynthesisConfigError(
            f"{SYNTHESIS_MONTHLY_BUDGET_ENV} is not set: the synthesis API runner needs a "
            "monthly budget in USD before it spends anything (or use --emit-task to run "
            "the task in another harness)"
        )
    try:
        return usd_to_micro(raw.strip())
    except ValueError as exc:
        raise SynthesisConfigError(f"{SYNTHESIS_MONTHLY_BUDGET_ENV}: {exc}") from exc


@dataclass
class BudgetCheck:
    fits: bool
    worst_case_micro_usd: int
    run_remaining_micro_usd: int
    month_remaining_micro_usd: int

    @property
    def limit(self) -> str:
        return "monthly" if self.month_remaining_micro_usd < self.run_remaining_micro_usd else "per-run"

    def evidence(self) -> dict[str, object]:
        return {
            "budget": f"{self.limit} cap",
            "worst_case": format_usd(self.worst_case_micro_usd),
            "run_remaining": format_usd(max(0, self.run_remaining_micro_usd)),
            "month_remaining": format_usd(max(0, self.month_remaining_micro_usd)),
        }


class Budget:
    """Per-run and monthly caps. The monthly cap is read from the
    environment on the first check unless given, so building a Budget
    never fails; checking one without a monthly cap does."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        run_cap_micro_usd: int = DEFAULT_RUN_CAP_MICRO_USD,
        monthly_cap_micro_usd: int | None = None,
        clock=None,
    ) -> None:
        self.conn = conn
        self.run_cap_micro_usd = run_cap_micro_usd
        self._monthly_cap = monthly_cap_micro_usd
        self.clock = clock or (lambda: int(time.time()))
        self.spent_micro_usd = 0

    @property
    def monthly_cap_micro_usd(self) -> int:
        if self._monthly_cap is None:
            self._monthly_cap = monthly_cap_from_env()
        return self._monthly_cap

    def check(self, worst_case_micro_usd: int) -> BudgetCheck:
        run_left = self.run_cap_micro_usd - self.spent_micro_usd
        month_left = self.monthly_cap_micro_usd - monthly_spent_micro_usd(self.conn, self.clock())
        fits = worst_case_micro_usd <= run_left and worst_case_micro_usd <= month_left
        return BudgetCheck(fits, worst_case_micro_usd, run_left, month_left)

    def charge(self, cost_micro_usd: int) -> None:
        self.spent_micro_usd += cost_micro_usd


def record_usage(
    conn: sqlite3.Connection,
    *,
    task_type: str,
    model: str,
    usage: dict[str, int] | None,
    cost_micro_usd: int,
    now: int,
) -> None:
    """Append one ``usage_records`` row for a synthesis API call and commit.

    Mirrors the suitability ledger: ``input_token_count`` is every
    non-cache-read input token (the table has no cache-write column; the
    cost carries the split), and ``cost_basis`` is ``estimated`` because the
    cost comes from muninn's price table, not from an invoice.
    """
    from muninn.consumers.packets.canonical import new_ulid  # stdlib-only

    conn.execute(
        """
        INSERT INTO usage_records (
            usage_ulid, kind, provider, operation, model, request_count,
            input_token_count, output_token_count, cache_read_token_count,
            cost_micro_usd, cost_basis, occurred_at
        ) VALUES (?, 'model', 'anthropic', ?, ?, 1, ?, ?, ?, ?, 'estimated', ?)
        """,
        (
            new_ulid(now * 1000),
            OPERATION_PREFIX + task_type,
            model,
            (usage["uncached_input_tokens"] + usage["cache_write_tokens"]) if usage else None,
            usage["output_tokens"] if usage else None,
            usage["cache_read_tokens"] if usage else None,
            cost_micro_usd,
            now,
        ),
    )
    conn.commit()
