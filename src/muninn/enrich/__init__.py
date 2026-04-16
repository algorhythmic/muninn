"""Per-bookmark enrichment via Anthropic Haiku.

Public API:

- ``compute_content_hash`` / ``would_skip`` — idempotency helpers
  (see :mod:`muninn.enrich.idempotency`).
- ``enrich_bookmark`` / ``EnrichmentResult`` — single-bookmark Haiku call
  (see :mod:`muninn.enrich.haiku`).
- ``enrich_all`` / ``EnrichmentStats`` — bulk pipeline orchestration
  (see :mod:`muninn.enrich.pipeline`).
"""

from muninn.enrich.haiku import EnrichmentResult, enrich_bookmark
from muninn.enrich.idempotency import compute_content_hash, would_skip
from muninn.enrich.pipeline import EnrichmentStats, enrich_all

__all__ = [
    "EnrichmentResult",
    "EnrichmentStats",
    "compute_content_hash",
    "enrich_all",
    "enrich_bookmark",
    "would_skip",
]
