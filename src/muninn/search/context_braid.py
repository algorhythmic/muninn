"""Generation-pinned Braid packing; canonical hydration never expands exports."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from muninn.db import connect, SchemaVersionError
from muninn.index import registry
from muninn.index.braid_client import BraidError
from muninn.index.runtime import open_context
from muninn.index.embedding_profile import ProfileUnavailable, query_embedding, validate_vector
from muninn.index.manifest import Manifest
from muninn.index.paths import load_index_paths
from muninn.index.projection import ATTRIBUTION_SCHEMA, PROJECTION_SCHEMA, attribution_id
from muninn.consumers.packets.canonical import TEXT_NORMALIZATION
from muninn.index.publisher import _inventory, PublishError
from muninn.index.sources import resolve_captured
from muninn.search.context import context_json, estimate_entry_cost, finalize_context
from muninn.search.documents import (
    CONTEXT_ESTIMATOR, CONTEXT_EXCERPT_ATTRS, CONTEXT_EXPORT_ATTRS,
    ContextEntry, DocumentBackendUnavailable,
)


# No summaries, titles, tags, embedding prefixes, or index-owned URLs enter
# the model payload. Citation URLs are checked against canonical sources.
CONTEXT_ATTRS = CONTEXT_EXPORT_ATTRS


class BraidContextBackend:
    def __init__(self, db_path=None, binary="braid", client_factory=None,
                 query_encoder=query_embedding):
        self.db_path, self.binary = db_path, Path(binary)
        self.client_factory = client_factory
        self.query_encoder = query_encoder

    def build(self, request):
        # Retry the entire pinned planning/hydration read once, never a partial
        # bundle or a mutation. Explicit generation preconditions survive retry.
        for attempt in range(2):
            try:
                return self._build(request)
            except DocumentBackendUnavailable as exc:
                if exc.reason != "stale_index" or attempt:
                    raise

    def _build(self, request):
        conn = client = None
        try:
            if self.client_factory is None or self.client_factory is open_context:
                from muninn.index.pilot import require_pilot_enabled
                require_pilot_enabled()
            conn = connect(self.db_path)
            conn.execute("BEGIN")
            generation = registry.active_generation(conn)
            if generation is None or generation.state != "ready":
                raise DocumentBackendUnavailable("generation_unavailable")
            if request.expected_generation is not None and generation.generation_id != request.expected_generation:
                return finalize_context((), request, backend="braid", state="conflict", reason="generation_conflict")
            path = load_index_paths().manifest_path(generation.generation_id)
            if Path(generation.manifest_path).resolve() != path.resolve():
                raise DocumentBackendUnavailable("generation_path_mismatch")
            manifest = Manifest.read(path, expected_digest=generation.manifest_digest)
            if (manifest.generation_id != generation.generation_id or manifest.corpus_id != generation.corpus_id
                    or manifest.projection_version != "muninn-braid-projection/2"
                    or not set(request.namespaces).issubset(manifest.namespaces)):
                raise DocumentBackendUnavailable("scope_unavailable")
            if registry.source_epoch(conn) != manifest.source_epoch:
                raise DocumentBackendUnavailable("stale_index")
            inventory = _inventory(conn, manifest.namespaces)
            if manifest.authored_inventory_digest is not None and inventory.digest != manifest.authored_inventory_digest:
                raise DocumentBackendUnavailable("stale_index")
            if manifest.braid.get("context_schema") != "muninn-context/1":
                raise DocumentBackendUnavailable("context_generation_unavailable")
            profile = manifest.profile()
            factory = self.client_factory or open_context
            runtime_options = {"db_path": self.db_path} if factory is open_context else {}
            client = factory(binary=self.binary, generation_dir=path.parent,
                             dataset_id=manifest.dataset_id, profile_id=profile.profile_id, **runtime_options)
            if client.state != manifest.braid["state"]:
                raise DocumentBackendUnavailable("dataset_mismatch")
            if client.identity != manifest.braid["identity"]:
                raise DocumentBackendUnavailable("binary_identity_mismatch")
            required_caps = {"query_embedding", "required_context", "context_export", "get_many", "allowed_ids", "read_revision"}
            if not required_caps.issubset(client.capabilities):
                raise DocumentBackendUnavailable("capability_mismatch")
            vector = self.query_encoder(request.query, profile, capabilities=client.capabilities, protocol_version=1)
            if not isinstance(vector, dict) or vector.get("model") != profile.profile_id:
                raise DocumentBackendUnavailable("embedding_model_mismatch")
            vector = {"model": profile.profile_id, "vector": validate_vector(vector.get("vector", ()), profile.dimension)}
            base = {"text": request.query, "dataset_id": manifest.dataset_id,
                    "expected_revision": client.state["revision"], "query_embedding": vector,
                    "weights": {"lexical": 1, "dense": 1, "graph": 0, "temporal": 0},
                    "filters": {"types": ["chunk"], "predicate": {"op": "in", "path": "namespace", "values": list(request.namespaces)}}}
            authored = {s.path: s for s in inventory.sources}

            def trusted_document(ref):
                if ref.startswith("bookmark:"):
                    if "bookmarks" not in request.namespaces:
                        return None
                    source = resolve_captured(conn, manifest.corpus_id, int(ref.split(":")[1]))
                    return (source.document_id, source.body.text) if source is not None and source.body else None
                source = authored.get(ref.removeprefix("vault:"))
                if source is None or source.namespace not in request.namespaces:
                    return None
                return ("authored:" + hashlib.sha256(json.dumps([manifest.corpus_id, source.path], sort_keys=True,
                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(), source.body)

            def shortlist(size, document=None, source_text=None):
                if not size:
                    return []
                query = {**base, "budget": {"max": size, "unit": "muninn_candidate", "estimator": "node-cost-one/1"}}
                if document is not None:
                    query["text"] = source_text[:2048]
                    query["weights"] = {"lexical": 1, "dense": 0, "graph": 0, "temporal": 0}
                    query.pop("query_embedding", None)
                    query["filters"] = {"types": ["chunk"], "predicate": {"op": "and", "children": [base["filters"]["predicate"],
                        {"op": "eq", "path": "document_id", "value": document}]}}
                result = client.request("query", timeout_ms=2000, query=query)
                if result.get("dataset") != client.state or not isinstance(result.get("items"), list) or len(result["items"]) > size:
                    raise DocumentBackendUnavailable("protocol_fault")
                for item in result["items"]:
                    attrs = item.get("attrs", {})
                    if (item.get("type") != "chunk" or attrs.get("namespace") not in request.namespaces
                            or attrs.get("profile_id") != profile.profile_id
                            or (document is not None and attrs.get("document_id") != document)
                            or not isinstance(item.get("id"), str)):
                        raise DocumentBackendUnavailable("protocol_fault")
                return result["items"]

            mandatory, roots = [], {}
            for ref in dict.fromkeys((*request.required_sources, *request.anchors)):
                doc = trusted_document(ref)
                if ref not in request.required_sources and sum("context:" + key not in mandatory for key in roots) >= request.optional_roots:
                    continue
                matches = shortlist(1, doc[0], doc[1]) if doc is not None else []
                if not matches:
                    return finalize_context((), request, backend="braid", state="abstained", reason="required_context_unavailable")
                node = matches[0]
                roots[node["id"]] = node
                if ref in request.required_sources:
                    mandatory.append("context:" + node["id"])
            optional = shortlist(request.optional_roots)
            for node in optional:
                if sum("context:" + key not in mandatory for key in roots) >= request.optional_roots:
                    break
                roots[node["id"]] = node
            ids, requires = [], {}
            for node in roots.values():
                attrs = node["attrs"]
                excerpt = attrs.get("context_excerpt_id")
                citation = attribution_id(attrs["document_id"], attrs["source_revision"])
                if excerpt != "context:" + node["id"] or attrs.get("attribution_id") != citation:
                    raise DocumentBackendUnavailable("context_generation_unavailable")
                ids.extend((excerpt, citation))
                requires[excerpt] = [citation]
            query = {**base, "requires": requires,
                     "mandatory_ids": list(dict.fromkeys(mandatory)),
                     "filters": {"types": ["context_excerpt", "attribution"], "allowed_ids": list(dict.fromkeys(ids)), "predicate": base["filters"]["predicate"]},
                     "budget": {"max": request.budget, "unit": "estimated_text_cost", "estimator": CONTEXT_ESTIMATOR}}
            result = client.request("export_context", timeout_ms=2000,
                export={"query": query, "output": {"fields": ["id", "type", "text", "attrs"], "attrs": list(CONTEXT_ATTRS), "max_bytes": request.max_bytes}})
            if result.get("dataset") != client.state or not isinstance(result.get("context"), list):
                raise DocumentBackendUnavailable("protocol_fault")
            rows = result["context"]
            if type(result.get("context_bytes")) is not int or not 2 <= result["context_bytes"] <= request.max_bytes:
                raise DocumentBackendUnavailable("export_bound_mismatch")
            if result.get("abstained") and rows:
                raise DocumentBackendUnavailable("protocol_fault")
            entries, seen, canonical_citations = [], set(), {}
            for row in rows:
                if (not isinstance(row, dict) or set(row) != {"id", "type", "text", "attrs"}
                        or row["id"] not in ids or row["id"] in seen):
                    raise DocumentBackendUnavailable("protocol_fault")
                seen.add(row["id"])
                entries.append(ContextEntry(**row))
            for entry in entries:
                attrs = entry.attrs
                if not isinstance(attrs, dict) or attrs.get("namespace") not in request.namespaces:
                    raise DocumentBackendUnavailable("protocol_fault")
                if entry.type == "context_excerpt":
                    original = roots.get(entry.id.removeprefix("context:"))
                    if original is None or requires[entry.id][0] not in seen:
                        raise DocumentBackendUnavailable("protocol_fault")
                    original_attrs = original["attrs"]
                    expected_attrs = {**original_attrs, "kind": "context_excerpt", "requires": requires[entry.id], "cost_estimator": CONTEXT_ESTIMATOR}
                    expected_attrs = {k: v for k, v in expected_attrs.items() if k in CONTEXT_EXCERPT_ATTRS}
                    if attrs != expected_attrs or entry.text != original["text"]:
                        raise DocumentBackendUnavailable("stale_index")
                    if any(attrs.get(k) != original_attrs.get(k) for k in ("document_id", "source_ref", "source_revision", "start", "end", "text_digest")):
                        raise DocumentBackendUnavailable("stale_index")
                    if (attrs.get("cost_estimator") != CONTEXT_ESTIMATOR or attrs.get("kind") != "context_excerpt"
                            or attrs.get("projection_schema") != PROJECTION_SCHEMA or attrs.get("profile_id") != profile.profile_id
                            or attrs.get("normalization") != TEXT_NORMALIZATION
                            or attrs.get("span_coordinate_system") != "normalized_body_unicode_codepoints"
                            or attrs.get("embedding_prefix_role") != "embedding_only_not_cited_body"):
                        raise DocumentBackendUnavailable("context_generation_unavailable")
                    if attrs["namespace"] == "bookmarks":
                        if type(attrs.get("bookmark_id")) is not int:
                            raise DocumentBackendUnavailable("protocol_fault")
                        source = resolve_captured(conn, manifest.corpus_id, attrs["bookmark_id"], expected_revision=attrs["source_revision"])
                        body = source.body.text if source is not None and source.body and source.document_id == attrs["document_id"] else None
                        if attrs.get("path") is not None:
                            raise DocumentBackendUnavailable("stale_index")
                        if source is not None and source.body and attrs.get("source_ref") != source.body.source_reference:
                            raise DocumentBackendUnavailable("stale_index")
                    else:
                        source = authored.get(attrs["path"])
                        body = source.body if source is not None and source.file_digest == attrs["source_revision"] else None
                        expected_id = "authored:" + hashlib.sha256(json.dumps(
                            [manifest.corpus_id, attrs["path"]], sort_keys=True, ensure_ascii=False,
                            separators=(",", ":")).encode()).hexdigest()
                        if attrs["document_id"] != expected_id:
                            raise DocumentBackendUnavailable("stale_index")
                        if source is not None and (attrs.get("source_ref") != "vault:" + source.path
                                or attrs.get("bookmark_id") != source.bookmark_id):
                            raise DocumentBackendUnavailable("stale_index")
                    citation = {"schema": ATTRIBUTION_SCHEMA, "document_id": attrs["document_id"],
                                "namespace": attrs["namespace"], "source_ref": attrs["source_ref"],
                                "source_revision": attrs["source_revision"], "normalization": TEXT_NORMALIZATION}
                    if attrs["namespace"] == "bookmarks" and source is not None and source.body:
                        values = {"url": source.url, "coverage": source.body.coverage,
                                  "source_digest": source.body.source_digest, "text_digest": source.body.text_digest,
                                  "revision_ulid": source.body.revision_ulid, "scrape_pass": source.body.scrape_pass,
                                  "retrieved_at": source.body.retrieved_at}
                    elif source is not None:
                        values = {"path": source.path, "coverage": "full", "body_digest": source.body_digest,
                                  "normalization": source.normalization}
                    else:
                        values = {}
                    citation.update({k: v for k, v in values.items() if v is not None})
                    canonical_citations[requires[entry.id][0]] = citation
                    start, end = attrs["start"], attrs["end"]
                    if (body is None or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body)
                            or body[start:end] != entry.text or hashlib.sha256(entry.text.encode()).hexdigest() != attrs["text_digest"]):
                        raise DocumentBackendUnavailable("stale_index")
                elif entry.type != "attribution" or entry.text != "" or attrs.get("cost_estimator") != CONTEXT_ESTIMATOR:
                    raise DocumentBackendUnavailable("protocol_fault")
            # Citation rows must be paired with an admitted excerpt, and must
            # name exactly that excerpt's pinned source boundary.
            for entry in entries:
                if entry.type != "attribution":
                    continue
                dependents = [e for e in entries if e.type == "context_excerpt" and requires[e.id] == [entry.id]]
                citation = entry.attrs.get("citation", {})
                expected_citation = canonical_citations.get(entry.id)
                expected_attrs = {"projection_schema": PROJECTION_SCHEMA, "document_id": citation.get("document_id"),
                    "source_revision": citation.get("source_revision"), "namespace": citation.get("namespace"),
                    "source_ref": citation.get("source_ref"), "kind": "attribution", "attribution_schema": ATTRIBUTION_SCHEMA,
                    "citation": expected_citation, "cost_estimator": CONTEXT_ESTIMATOR}
                if not dependents or citation != expected_citation or entry.attrs != expected_attrs:
                    raise DocumentBackendUnavailable("protocol_fault")
            if not set(mandatory).issubset(seen) and not result.get("abstained"):
                raise DocumentBackendUnavailable("protocol_fault")
            if len(context_json(entries)) > request.max_bytes or sum(estimate_entry_cost(e) for e in entries) > request.budget:
                raise DocumentBackendUnavailable("export_bound_mismatch")
            conn.rollback()
            if registry.source_epoch(conn) != manifest.source_epoch or (manifest.authored_inventory_digest is not None
                    and _inventory(conn, manifest.namespaces).digest != manifest.authored_inventory_digest):
                raise DocumentBackendUnavailable("stale_index")
            omissions = []
            for rejection in result.get("rejections", []):
                if not isinstance(rejection, dict):
                    raise DocumentBackendUnavailable("protocol_fault")
                identifier, reason = rejection.get("id"), rejection.get("reason")
                if identifier in requires and reason in {"budget", "required_context_unavailable", "eligibility", "evidence_policy"}:
                    omissions.append({"id": identifier, "reason": reason})
            # Generation manifests may contain excluded hidden/private paths.
            # Report aggregate scope coverage, never those source identifiers.
            coverage_omissions = {}
            for omission in manifest.omissions:
                reason = omission.get("reason")
                if reason in {"hidden", "symlink", "not_markdown", "not_visible", "excluded_symlink", "enrichment_withheld"}:
                    continue
                namespace = omission.get("namespace")
                if namespace is not None and namespace not in request.namespaces:
                    continue
                safe_reason = reason if reason in {"body_partial", "body_unavailable", "metadata_embedding_exceeds_token_budget",
                    "archive_unavailable", "authored_inventory_incomplete", "content_unavailable"} else "source_omitted"
                coverage_omissions[safe_reason] = coverage_omissions.get(safe_reason, 0) + 1
            return finalize_context(entries, request, backend="braid", generation={"generation_id": manifest.generation_id,
                "corpus_id": manifest.corpus_id, "source_epoch": manifest.source_epoch, **client.state},
                accounting={"diversity_lambda": 0.7}, coverage={"namespaces": list(request.namespaces),
                "candidate_roots": len(roots), "optional_root_limit": request.optional_roots, "complete": not bool(coverage_omissions),
                "omission_counts": coverage_omissions},
                state="abstained" if result.get("abstained") else "ok", reason=result.get("abstention_reason"),
                omissions=omissions)
        except DocumentBackendUnavailable:
            raise
        except (BraidError, ProfileUnavailable) as exc:
            raise DocumentBackendUnavailable(getattr(exc, "code", "embedding_unavailable")) from exc
        except (OSError, sqlite3.Error, SchemaVersionError, registry.RegistryError, PublishError,
                ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DocumentBackendUnavailable("generation_unavailable") from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except (BraidError, OSError):
                    pass
            if conn is not None:
                conn.close()
