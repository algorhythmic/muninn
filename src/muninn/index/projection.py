"""Deterministic source-to-Braid records, without inference or publication.

Consume inside the caller's captured snapshot transaction, spool all records,
then release SQLite before embedding. All wire values are plain JSON objects.
Edges can occur before later nodes; publishers must apply all nodes first.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterator

from muninn.consumers.packets.canonical import TEXT_NORMALIZATION
from muninn.consumers.mcp.vault_fs import AUTHORED_NAMESPACES
from muninn.index.authored import Inventory
from muninn.index.chunking import ChunkSettings, PinnedTokenizer, chunk_document
from muninn.index.embedding_profile import EmbeddingProfile
from muninn.index.sources import iter_captured
from muninn.search.context import estimate_entry_cost
from muninn.search.documents import CONTEXT_ESTIMATOR

PROJECTION_SCHEMA = "muninn-braid-projection/2"
ATTRIBUTION_SCHEMA = "muninn-attribution/1"
NAMESPACES = ("bookmarks", *AUTHORED_NAMESPACES)


@dataclass(frozen=True)
class ProjectionRecord:
    kind: str
    value: dict
    embedding_input: str | None = None


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _captured_id(corpus_id: str, bookmark_id: int) -> str:
    return "captured:" + json.dumps([corpus_id, bookmark_id], separators=(",", ":"))


def attribution_id(document_id: str, source_revision: str) -> str:
    """Stable shared citation identity for all chunks of this exact source."""
    return "attribution:" + _hash([ATTRIBUTION_SCHEMA, document_id, source_revision])


def attribution_requirements(records: Iterator[ProjectionRecord]) -> dict[str, list[str]]:
    """Translate projection dependency edges to Braid's explicit requires map.

    Merely storing graph edges does not activate required-context semantics.
    Consumers must pass this mapping on the pinned export/query request.
    """
    result: dict[str, list[str]] = {}
    for record in records:
        if record.kind == "edge" and record.value["type"] == "requires":
            destinations = result.setdefault(record.value["src"], [])
            if record.value["dst"] not in destinations:
                destinations.append(record.value["dst"])
    return result


def _time(epoch: int | float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z") if epoch is not None else None


def _node(identifier: str, kind: str, text: str, attrs: dict, timestamp: str | None = None) -> dict:
    value = {"id": identifier, "type": kind, "text": text, "attrs": attrs}
    if kind in ("document", "chunk"):
        # Document search requests a bounded candidate ranking. Explicit unit
        # cost prevents the context packer's text-length heuristic dropping
        # otherwise eligible documents before Muninn collapses their chunks.
        value["cost"] = 1
    if timestamp is not None:
        value["ts"] = timestamp
    return value


def _edge(src: str, dst: str, kind: str, timestamp: str | None = None) -> ProjectionRecord:
    value = {"src": src, "dst": dst, "type": kind, "weight": 1.0}
    if timestamp is not None:
        value["ts"] = timestamp
    return ProjectionRecord("edge", value)


def _document_records(
    document_id: str, revision: str, title: str, metadata_text: str, body: str,
    attrs: dict, profile: EmbeddingProfile, tokenizer: PinnedTokenizer,
    settings: ChunkSettings, timestamp: str | None,
) -> Iterator[ProjectionRecord]:
    common = {"projection_schema": PROJECTION_SCHEMA, "document_id": document_id,
              "source_revision": revision, "profile_id": profile.profile_id, **attrs}
    embedding_input = metadata_text if metadata_text.strip() else None
    if embedding_input is not None:
        count = tokenizer.count_tokens(embedding_input)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("tokenizer must return a nonnegative integer")
        if count + settings.prompt_overhead > settings.max_tokens:
            embedding_input = None
            yield ProjectionRecord("omission", {"document_id": document_id, "namespace": attrs["namespace"],
                                                "reason": "metadata_embedding_exceeds_token_budget"})
    yield ProjectionRecord("node", _node(document_id, "document", metadata_text,
                           {**common, "kind": "document", "title": title}, timestamp), embedding_input)
    citation_id = attribution_id(document_id, revision)
    citation = {
        "schema": ATTRIBUTION_SCHEMA, "document_id": document_id,
        "namespace": attrs["namespace"], "source_ref": attrs["source_ref"],
        "source_revision": revision, "normalization": attrs.get("normalization", TEXT_NORMALIZATION),
    }
    for key in ("url", "path", "normalization", "coverage", "source_digest", "body_digest",
                "text_digest", "revision_ulid", "scrape_pass", "retrieved_at"):
        if attrs.get(key) is not None:
            citation[key] = attrs[key]
    # The citation is source-level and shared by every excerpt of the source.
    # Exact body offsets/digests remain on each chunk; embedding prefixes are
    # neither copied into citation text nor mistaken for captured body text.
    attribution = _node(citation_id, "attribution", "",
        {"projection_schema": PROJECTION_SCHEMA, "document_id": document_id,
         "source_revision": revision, "namespace": attrs["namespace"],
         "source_ref": attrs["source_ref"], "kind": "attribution", "attribution_schema": ATTRIBUTION_SCHEMA,
         "citation": citation, "cost_estimator": CONTEXT_ESTIMATOR}, timestamp)
    attribution["cost"] = estimate_entry_cost({k: attribution[k] for k in ("id", "type", "text", "attrs")})
    yield ProjectionRecord("node", attribution)
    # Chunk failures abort the disposable build rather than publish partial body
    # coverage as complete. Source-level missing bodies were already explicit.
    for chunk in chunk_document(document_id, revision, body, profile_id=profile.profile_id,
                                tokenizer=tokenizer, title=title, settings=settings):
        chunk_attrs = {**common, "kind": "chunk", "ordinal": chunk.ordinal,
                       "start": chunk.start, "end": chunk.end,
                       "span_coordinate_system": "normalized_body_unicode_codepoints",
                       "normalization": chunk.normalization, "text_digest": chunk.text_digest,
                       "chunker_version": chunk.chunker_version,
                       "tokenizer_digest": chunk.tokenizer_digest,
                       "embedding_prefix": chunk.prefix,
                       "embedding_prefix_role": "embedding_only_not_cited_body",
                       "attribution_id": citation_id,
                       "context_excerpt_id": "context:" + chunk.chunk_id,
                       "embedding_body_start": len(chunk.prefix),
                       "embedding_body_end": len(chunk.prefix) + len(chunk.text),
                       "embedding_coordinate_system": "unicode_codepoints",
                       "token_count": chunk.token_count}
        yield ProjectionRecord("node", _node(chunk.chunk_id, "chunk", chunk.text,
                               chunk_attrs, timestamp), chunk.embedding_input)
        yield _edge(document_id, chunk.chunk_id, "contains", timestamp)
        yield _edge(chunk.chunk_id, document_id, "belongs_to", timestamp)
        # A separate context identity keeps document-search unit costs stable.
        # Dense input is shared verbatim: the profile cache avoids inference
        # twice while Braid can rank context roots without an ID remapping.
        excerpt_attrs = {**chunk_attrs, "kind": "context_excerpt",
                         "requires": [citation_id], "cost_estimator": CONTEXT_ESTIMATOR}
        excerpt = _node("context:" + chunk.chunk_id, "context_excerpt", chunk.text,
                        excerpt_attrs, timestamp)
        excerpt["cost"] = estimate_entry_cost({k: excerpt[k] for k in ("id", "type", "text", "attrs")})
        yield ProjectionRecord("node", excerpt, chunk.embedding_input)
        yield _edge(excerpt["id"], citation_id, "requires", timestamp)


def project(
    conn: sqlite3.Connection, corpus_id: str, inventory: Inventory,
    profile: EmbeddingProfile, tokenizer: PinnedTokenizer, *,
    settings: ChunkSettings = ChunkSettings(), namespaces: tuple[str, ...] = NAMESPACES,
) -> Iterator[ProjectionRecord]:
    """Stream search nodes, costed context bundles and source relationships.

    A complete authored inventory is mandatory whenever authored namespaces are
    in scope. Profile and tokenizer identities must match even in fake tests.
    No live model is imported here; real callers resolve A's document_tokenizer.
    """
    if not corpus_id or not namespaces or len(set(namespaces)) != len(namespaces) or set(namespaces) - set(NAMESPACES):
        raise ValueError("invalid corpus or namespace scope")
    profile.require_complete()
    if tokenizer.digest != profile.tokenizer_digest:
        raise ValueError("profile_tokenizer_mismatch")
    if getattr(tokenizer, "max_seq_length", settings.max_tokens) < settings.max_tokens:
        raise ValueError("chunk_budget_exceeds_model_limit")
    if set(namespaces).intersection(AUTHORED_NAMESPACES) and not inventory.complete:
        raise ValueError("authored_inventory_incomplete")
    if "bookmarks" in namespaces:
        for source in iter_captured(conn, corpus_id):
            body = source.body
            attrs = {"corpus_id": corpus_id, "namespace": "bookmarks",
                     "bookmark_id": source.bookmark_id,
                     "source_ref": body.source_reference if body else f"bookmarks:{source.bookmark_id}",
                     "captured_at": source.captured_at, "source": source.source,
                     "url": source.url, "summary": source.summary, "tags": list(source.tags),
                     "coverage": body.coverage if body else "unavailable", "omissions": list(source.omissions)}
            if body:
                attrs.update({key: value for key, value in asdict(body).items() if key != "text"})
            title = source.title or ""
            metadata_text = "\n".join(part for part in (title, source.summary or "", " ".join(source.tags)) if part)
            yield from _document_records(source.document_id, source.projection_revision, title,
                metadata_text, body.text if body else "", attrs, profile, tokenizer, settings,
                _time(body.retrieved_at if body else source.captured_at))
            for reason in source.omissions:
                yield ProjectionRecord("omission", {"document_id": source.document_id,
                                                    "namespace": "bookmarks", "reason": reason})
            if body and body.coverage != "full":
                yield ProjectionRecord("omission", {"document_id": source.document_id,
                                                    "namespace": "bookmarks", "reason": "body_partial"})
    for source in inventory.sources:
        if source.namespace not in namespaces:
            continue
        document_id = "authored:" + _hash([corpus_id, source.path])
        attrs = {"corpus_id": corpus_id, "namespace": source.namespace, "path": source.path,
                 "source_ref": "vault:" + source.path, "file_digest": source.file_digest,
                 "body_digest": source.body_digest, "source_body_start": source.source_body_start,
                 "normalization": source.normalization, "bookmark_id": source.bookmark_id,
                 "modified_at_ns": source.validation_stamp[3], "coverage": "full"}
        yield from _document_records(document_id, source.file_digest, source.title,
            source.title, source.body, attrs, profile, tokenizer, settings,
            _time(source.validation_stamp[3] / 1_000_000_000))
    for path, reason in inventory.omissions:
        namespace = path.split("/")[1] if len(path.split("/")) > 1 else None
        if namespace in namespaces:
            yield ProjectionRecord("omission", {"path": path, "namespace": namespace, "reason": reason})
    if "bookmarks" not in namespaces:
        return
    # Relationship prose is intentionally omitted: persisted explanations can
    # paraphrase hidden sources. Typed source identities and timestamps survive.
    queries = (
        ("item_links", "item_link_id", "parent_bookmark_id", "child_bookmark_id", "first_seen_at"),
        ("cross_references", "cross_reference_id", "source_bookmark_id", "target_bookmark_id", "created_at"),
    )
    for table, key, src_column, dst_column, time_column in queries:
        sql = (f"SELECT r.* FROM {table} r JOIN bookmarks s ON s.bookmark_id=r.{src_column} "
               f"JOIN bookmarks d ON d.bookmark_id=r.{dst_column} "
               f"WHERE s.content_visible=1 AND d.content_visible=1 ORDER BY r.{key}")
        for row in conn.execute(sql):
            src, dst = _captured_id(corpus_id, row[src_column]), _captured_id(corpus_id, row[dst_column])
            relation = row["relation"] if table == "item_links" else "cross_reference"
            reference = f"{table}:{row[key]}"
            timestamp = _time(row[time_column])
            provenance = {"projection_schema": PROJECTION_SCHEMA, "kind": "relationship",
                          "corpus_id": corpus_id, "source_ref": reference,
                          "src": src, "dst": dst, "relation": relation,
                          "source_timestamp": row[time_column], "source_table": table}
            if table == "item_links":
                provenance.update({"position": row["position"], "locator_resolution": row["locator_resolution"],
                                   "parent_ref": row["parent_ref"], "child_ref": row["child_ref"]})
            else:
                provenance["created_by"] = row["created_by"]
            provenance["source_revision"] = _hash(provenance)
            relation_id = "relationship:" + _hash([corpus_id, reference])
            yield ProjectionRecord("node", _node(relation_id, "relationship", "", provenance, timestamp))
            yield _edge(src, dst, relation, timestamp)
            # Braid edges have no attrs. Keep each distinct source relation
            # graph-connected through its provenance node as well as retaining
            # the direct typed edge (which Braid may deduplicate by endpoints).
            yield _edge(src, relation_id, "source_relation", timestamp)
            yield _edge(relation_id, dst, "target_relation", timestamp)
