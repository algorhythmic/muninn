"""Research Intake evidence-packet consumer (contract ``muninn-research-intake``).

Stdlib-only: importing this package must not pull in Qdrant, embeddings,
DuckDB, Parquet, MCP or Jinja (ADR-009). See ``canonical`` for the digest
and identity rules and ``export`` for the exporter interface.
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
    PacketResolution,
    export_packets,
    make_tombstone,
    packet_integrity_errors,
    packet_path,
    parse_packet_ref,
    resolve_packet,
    withdraw_packet,
    write_packet,
)

__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "TEXT_NORMALIZATION",
    "WITHDRAWAL_REASONS",
    "CanonicalizationError",
    "ExportedPacket",
    "PacketResolution",
    "canonical_json",
    "content_digest",
    "export_packets",
    "is_ulid",
    "link_item_id",
    "locator_source_id",
    "make_tombstone",
    "new_ulid",
    "normalize_text",
    "packet_integrity_errors",
    "packet_path",
    "packet_ref",
    "parse_packet_ref",
    "record_digest",
    "resolve_packet",
    "sha256_hex",
    "withdraw_packet",
    "write_packet",
    "x_article_item_id",
    "x_post_item_id",
]
