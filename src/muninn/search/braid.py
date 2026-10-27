"""Generation-pinned, fail-closed document search over the shared Braid client."""
from __future__ import annotations

import math
import json
import hashlib
import sqlite3
from pathlib import Path

from muninn.db import connect, SchemaVersionError
from muninn.index import registry
from muninn.index.braid_client import BraidClient, BraidError
from muninn.index.embedding_profile import ProfileUnavailable, query_embedding, validate_vector
from muninn.index.manifest import Manifest
from muninn.index.paths import load_index_paths
from muninn.index.projection import NAMESPACES
from muninn.index.publisher import _inventory, PublishError
from muninn.index.sources import resolve_captured
from muninn.search.documents import DocumentBackendUnavailable, DocumentHit, DocumentSearchResult


def _verify_chunks(body, chunks, items, revision):
    by_id = {item["id"]: item for item in items}
    for chunk in chunks:
        start, end = chunk["start"], chunk["end"]
        if (body is None or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(body) or chunk["source_revision"] != revision):
            raise DocumentBackendUnavailable("stale_index")
        text = body[start:end]
        if text != by_id[chunk["chunk_id"]]["text"] or hashlib.sha256(text.encode()).hexdigest() != chunk["text_digest"]:
            raise DocumentBackendUnavailable("stale_index")


class BraidDocumentBackend:
    def __init__(self, db_path=None, binary="braid", client_factory=BraidClient.open_existing,
                 query_encoder=query_embedding):
        self.db_path, self.binary = db_path, Path(binary)
        self.client_factory, self.query_encoder = client_factory, query_encoder

    def search_documents(self, query, namespaces, limit, options=None):
        if not isinstance(query, str) or not query.strip() or len(query.encode()) > 65536:
            raise ValueError("invalid_query")
        if (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50
                or not isinstance(namespaces, (tuple, list)) or not namespaces
                or any(ns not in NAMESPACES for ns in namespaces)):
            raise ValueError("invalid_scope_or_limit")
        options = {} if options is None else options
        if (not isinstance(options, dict) or set(options) - {"semantic_only", "mode"}
                or options.get("mode", "hybrid") not in ("keyword", "semantic", "hybrid")
                or type(options.get("semantic_only", False)) is not bool):
            raise ValueError("unsupported_search_option")
        conn = client = None
        try:
            conn = connect(self.db_path)
            conn.execute("BEGIN")
            generation = registry.active_generation(conn)
            if generation is None or generation.state != "ready":
                raise DocumentBackendUnavailable("generation_unavailable")
            expected_path = load_index_paths().manifest_path(generation.generation_id)
            if Path(generation.manifest_path).resolve() != expected_path.resolve():
                raise DocumentBackendUnavailable("generation_path_mismatch")
            manifest = Manifest.read(expected_path, expected_digest=generation.manifest_digest)
            if manifest.projection_version != "muninn-braid-projection/2":
                raise DocumentBackendUnavailable("projection_version_mismatch")
            if (manifest.generation_id != generation.generation_id or manifest.corpus_id != generation.corpus_id
                    or not set(namespaces).issubset(manifest.namespaces)):
                raise DocumentBackendUnavailable("scope_unavailable")
            if registry.source_epoch(conn) != manifest.source_epoch:
                raise DocumentBackendUnavailable("stale_index")
            inventory = _inventory(conn, manifest.namespaces)
            if manifest.authored_inventory_digest is not None and inventory.digest != manifest.authored_inventory_digest:
                raise DocumentBackendUnavailable("stale_index")
            profile = manifest.profile()
            client = self.client_factory(binary=self.binary, generation_dir=expected_path.parent,
                dataset_id=manifest.dataset_id, profile_id=profile.profile_id)
            if client.state != manifest.braid["state"]:
                raise DocumentBackendUnavailable("dataset_mismatch")
            if client.identity != manifest.braid["identity"]:
                raise DocumentBackendUnavailable("binary_identity_mismatch")
            capabilities = client.capabilities
            if "query_embedding" not in capabilities:
                raise DocumentBackendUnavailable("capability_mismatch")
            config = json.loads((expected_path.parent / "braid.json").read_text())
            cap = config.get("candidate_limit")
            if type(cap) is not int or not 1 <= cap <= 512 or config.get("defaults", {}).get("mmr_lambda") != 1:
                raise DocumentBackendUnavailable("retrieval_config_mismatch")
            mode = options.get("mode", "semantic" if options.get("semantic_only") else "hybrid")
            vector = self.query_encoder(query, profile, capabilities=capabilities, protocol_version=1) if mode != "keyword" else None
            if vector is not None:
                if not isinstance(vector, dict) or vector.get("model") != profile.profile_id:
                    raise DocumentBackendUnavailable("embedding_model_mismatch")
                vector = {"model": profile.profile_id, "vector": validate_vector(vector.get("vector", ()), profile.dimension)}
            request = {
                "text": query, "dataset_id": manifest.dataset_id,
                "expected_revision": client.state["revision"],
                "filters": {"types": ["document", "chunk"],
                            "predicate": {"op": "in", "path": "namespace", "values": list(namespaces)}},
                "weights": {"lexical": 0 if mode == "semantic" else 1,
                            "dense": 0 if mode == "keyword" else 1, "graph": 0, "temporal": 0},
                "budget": {"max": cap, "unit": "muninn_candidate", "estimator": "node-cost-one/1"},
                "explain": True,
            }
            if vector is not None:
                request["query_embedding"] = vector
            response = client.request("query", timeout_ms=2000, query=request)
            if (not isinstance(response, dict) or response.get("dataset") != client.state
                    or not isinstance(response.get("items"), list) or len(response["items"]) > cap):
                raise DocumentBackendUnavailable("protocol_fault")
            collapsed = {}
            seen_ids = set()
            for item in response["items"]:
                if not isinstance(item, dict) or not isinstance(item.get("attrs"), dict):
                    raise DocumentBackendUnavailable("protocol_fault")
                attrs = item.get("attrs", {})
                if (item.get("type") not in ("document", "chunk") or attrs.get("namespace") not in namespaces
                        or type(item.get("score")) not in (int, float) or not math.isfinite(item["score"])
                        or not isinstance(item.get("id"), str) or item["id"] in seen_ids
                        or attrs.get("kind") != item["type"] or attrs.get("profile_id") != profile.profile_id
                        or not isinstance(attrs.get("document_id"), str)
                        or not isinstance(item.get("why", []), list)):
                    raise DocumentBackendUnavailable("protocol_fault")
                seen_ids.add(item["id"])
                doc_id = attrs["document_id"]
                if item["type"] == "document" and item["id"] != doc_id:
                    raise DocumentBackendUnavailable("protocol_fault")
                prior = collapsed.get(doc_id)
                if prior is None:
                    collapsed[doc_id] = {"item": item, "chunks": [], "stages": set()}
                else:
                    other = prior["item"]["attrs"]
                    if any(attrs.get(key) != other.get(key) for key in
                           ("namespace", "source_ref", "source_revision", "bookmark_id", "path", "profile_id")):
                        raise DocumentBackendUnavailable("stale_index")
                    if item["score"] > prior["item"]["score"]:
                        prior["item"] = item
                collapsed[doc_id]["stages"].update(stage for stage in ("lexical", "dense")
                    if any(reason.startswith(stage + ":") for reason in item.get("why", []) if isinstance(reason, str)))
                if attrs.get("kind") == "chunk":
                    collapsed[doc_id]["chunks"].append({"chunk_id": item["id"], **attrs})
            hits = []
            authored = {s.path: s for s in inventory.sources}
            for doc_id, match in sorted(collapsed.items(), key=lambda pair: (-pair[1]["item"]["score"], pair[0])):
                item, chunks = match["item"], match["chunks"]
                attrs = item["attrs"]
                if attrs["namespace"] == "bookmarks":
                    source = resolve_captured(conn, manifest.corpus_id, attrs["bookmark_id"],
                                              expected_revision=attrs["source_revision"])
                    if source is None or source.document_id != doc_id:
                        raise DocumentBackendUnavailable("stale_index")
                    if attrs["source_ref"] != (source.body.source_reference if source.body else f"bookmarks:{source.bookmark_id}"):
                        raise DocumentBackendUnavailable("stale_index")
                    _verify_chunks(source.body.text if source.body else None, chunks, response["items"], attrs["source_revision"])
                    payload = {"bookmark_id": source.bookmark_id, "title": source.title,
                               "url": source.url, "summary": source.summary, "source": source.source}
                else:
                    source = authored.get(attrs["path"])
                    expected_id = "authored:" + hashlib.sha256(json.dumps([manifest.corpus_id, attrs["path"]], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                    if (source is None or source.file_digest != attrs["source_revision"]
                            or doc_id != expected_id or attrs["source_ref"] != "vault:" + source.path):
                        raise DocumentBackendUnavailable("stale_index")
                    _verify_chunks(source.body, chunks, response["items"], attrs["source_revision"])
                    payload = {"path": source.path, "title": source.title, "summary": None}
                stages = tuple(sorted(match["stages"]))
                safe_chunks = tuple({key: chunk[key] for key in
                    ("chunk_id", "source_ref", "source_revision", "start", "end", "text_digest", "ordinal", "chunker_version")}
                    for chunk in chunks)
                hits.append(DocumentHit(doc_id, attrs["namespace"], attrs["source_ref"],
                    attrs["source_revision"], float(item["score"]), tuple(stages), safe_chunks, payload))
            # Recheck after the backend read: no mixture of a stale canonical
            # source snapshot and a newer epoch may escape hydration.
            conn.rollback()
            if registry.source_epoch(conn) != manifest.source_epoch:
                raise DocumentBackendUnavailable("stale_index")
            if manifest.authored_inventory_digest is not None and _inventory(conn, manifest.namespaces).digest != manifest.authored_inventory_digest:
                raise DocumentBackendUnavailable("stale_index")
            considered = response.get("considered", len(response["items"]))
            if type(considered) is not int or considered < 0:
                raise DocumentBackendUnavailable("protocol_fault")
            warnings = ("candidate_cap_reached",) if max(len(response["items"]), considered) >= cap else ()
            return DocumentSearchResult(tuple(hits[:limit]), generation={"corpus_id": manifest.corpus_id,
                "generation_id": manifest.generation_id, "source_epoch": manifest.source_epoch, **client.state},
                coverage={"namespaces": list(namespaces), "complete": True,
                          "candidate_limit": cap, "top_k_complete": False if warnings else None}, warnings=warnings)
        except DocumentBackendUnavailable:
            raise
        except (BraidError, ProfileUnavailable) as exc:
            raise DocumentBackendUnavailable(getattr(exc, "code", "embedding_unavailable")) from exc
        except (OSError, sqlite3.Error, SchemaVersionError, registry.RegistryError, PublishError, ValueError, KeyError, TypeError, AttributeError, StopIteration) as exc:
            raise DocumentBackendUnavailable("generation_unavailable") from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except (BraidError, OSError):
                    pass
            if conn is not None:
                conn.close()
