"""Research Intake evidence-packet consumer (contract ``muninn-research-intake``).

Stdlib-only: importing this package must not pull in Qdrant, embeddings,
DuckDB, Parquet, MCP or Jinja (ADR-009); ``jsonschema`` is imported only
when a record is validated. See ``canonical`` for the digest and identity
rules, ``records`` for the database-row to record mappings, ``schemas``
for validation and ``export`` for the exporter.
"""

from muninn.consumers.packets.canonical import (
    TEXT_NORMALIZATION,
    CanonicalizationError,
    canonical_json,
    content_digest,
    is_ulid,
    link_item_id,
    locator_source_id,
    new_ulid,
    normalize_text,
    packet_ref,
    record_digest,
    sha256_hex,
    x_article_item_id,
    x_post_item_id,
)
from muninn.consumers.packets.export import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    WITHDRAWAL_REASONS,
    ExportedPacket,
    ExportRun,
    PacketResolution,
    build_packet,
    export_packets,
    make_tombstone,
    packet_errors,
    packet_integrity_errors,
    packet_path,
    parse_packet_ref,
    resolve_packet,
    withdraw_exported_packet,
    withdraw_packet,
    write_packet,
)

__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "TEXT_NORMALIZATION",
    "WITHDRAWAL_REASONS",
    "CanonicalizationError",
    "ExportRun",
    "ExportedPacket",
    "PacketResolution",
    "build_packet",
    "canonical_json",
    "content_digest",
    "export_packets",
    "is_ulid",
    "link_item_id",
    "locator_source_id",
    "make_tombstone",
    "new_ulid",
    "normalize_text",
    "packet_errors",
    "packet_integrity_errors",
    "packet_path",
    "packet_ref",
    "parse_packet_ref",
    "record_digest",
    "resolve_packet",
    "sha256_hex",
    "withdraw_exported_packet",
    "withdraw_packet",
    "write_packet",
    "x_article_item_id",
    "x_post_item_id",
]
