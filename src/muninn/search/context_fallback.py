"""Degraded lexical excerpt bundles from the complete legacy search path.

This path admits whole, already ordered bundles. It does not emulate Braid's
graph closures, diversity ranking, or context packing engine.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3

EXCERPT_CHAR_LIMIT = 512
OPTIONAL_ROOT_LIMIT = 12


def _candidate_limit(request):
    return min(50, max(12, request.optional_roots * 2 + len(request.required_sources)))


def _lexical_span(body: str, query: str) -> tuple[int, int]:
    """Return exact normalized-body coordinates; never use folded offsets."""
    if not body:
        return 0, 0
    terms = re.findall(r"\w+", query)[:16]
    matches = [match.start() for term in terms
               if (match := re.search(re.escape(term), body, flags=re.IGNORECASE)) is not None]
    position = min(matches, default=0)
    start = max(0, position - 128)
    end = min(len(body), start + EXCERPT_CHAR_LIMIT)
    return start, end


@dataclass(frozen=True)
class _Candidate:
    document_id: str
    namespace: str
    source_ref: str
    source_revision: str
    title: str
    start: int
    end: int
    text_digest: str
    excerpt: str
    matched_stages: tuple[str, ...]
    trusted_ref: str = ""


def _candidates(request, db_path):
    """Resolve candidate IDs through current canonical sources, never hit text."""
    from muninn.config import load_paths
    from muninn.consumers.mcp import tools
    from muninn.consumers.mcp.vault_fs import NAMESPACES, VaultAccessError
    from muninn.db import connect
    from muninn.index.authored import resolve_authored
    from muninn.index.registry import ensure_corpus, source_epoch
    from muninn.index.sources import resolve_captured
    from muninn.search.fts5_qdrant import Fts5QdrantBackend

    backend = Fts5QdrantBackend(db_path=db_path)
    lanes, omissions = [], []
    scopes = (None,) if set(request.namespaces) == set(NAMESPACES) else request.namespaces
    for namespace in scopes:
        response = json.loads(tools.search(request.query, namespace=namespace, top_k=_candidate_limit(request),
                                           db_path=db_path, backend=backend))
        if response.get("error"):
            omissions.append({"reason": "legacy_scope_unavailable", "namespace": namespace})
        lanes.append(response.get("results", []))
        if response.get("warnings"):
            omissions.append({"reason": "legacy_scan_bounded", "namespace": namespace})
    ordered = [lane[i] for i in range(max((len(lane) for lane in lanes), default=0))
               for lane in lanes if i < len(lane)]
    explicit = []
    for ref in (*request.required_sources, *request.anchors):
        if ref.startswith("bookmark:"):
            explicit.append({"namespace": "bookmarks", "bookmark_id": int(ref.split(":", 1)[1])})
        else:
            path = ref.removeprefix("vault:")
            explicit.append({"namespace": path.split("/")[1], "path": path})
    ordered = explicit + ordered
    conn = connect(db_path)
    paths = load_paths()
    candidates, authored_checks, seen = [], [], set()
    try:
        conn.execute("BEGIN")
        corpus_id, epoch = ensure_corpus(conn), source_epoch(conn)

        def visible(bid):
            return conn.execute("SELECT 1 FROM bookmarks WHERE bookmark_id=? AND content_visible=1", (bid,)).fetchone() is not None

        for item in ordered:
            namespace = item.get("namespace")
            if namespace not in request.namespaces:
                continue
            if namespace == "bookmarks":
                bid = item.get("bookmark_id")
                if type(bid) is not int:
                    continue
                source = resolve_captured(conn, corpus_id, bid)
                if source is None or source.body is None:
                    continue
                identifier, reference, revision = source.document_id, source.body.source_reference, source.projection_revision
                body, title = source.body.text, source.title or ""
                trusted_ref = f"bookmark:{bid}"
            else:
                if paths.vault_dir is None or not isinstance(item.get("path"), str):
                    continue
                try:
                    source = resolve_authored(paths.vault_dir, paths.personal_vault_dir,
                                              item["path"], row_visible=visible)
                except (OSError, ValueError, VaultAccessError):
                    source = None
                if source is None or source.namespace != namespace:
                    continue
                identifier = "authored:" + hashlib.sha256(json.dumps([corpus_id, source.path],
                    sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                reference, revision = "vault:" + source.path, source.file_digest
                body, title = source.body, source.title
                trusted_ref = reference
                authored_checks.append((source.path, source.file_digest))
            if identifier in seen or not body:
                continue
            seen.add(identifier)
            start, end = _lexical_span(body, request.query)
            excerpt = body[start:end]
            candidates.append(_Candidate(identifier, namespace, reference, revision, title,
                start, end, hashlib.sha256(excerpt.encode()).hexdigest(), excerpt,
                tuple(stage for stage in item.get("matched", ()) if isinstance(stage, str)), trusted_ref))
        conn.rollback()
        if source_epoch(conn) != epoch:
            raise RuntimeError("legacy_source_changed")
        for path, digest in authored_checks:
            try:
                source = resolve_authored(paths.vault_dir, paths.personal_vault_dir, path,
                                          row_visible=visible, expected_digest=digest)
            except (OSError, ValueError, VaultAccessError) as exc:
                raise RuntimeError("legacy_source_changed") from exc
            if source is None:
                raise RuntimeError("legacy_source_changed")
        if source_epoch(conn) != epoch:
            raise RuntimeError("legacy_source_changed")
        return candidates, {"corpus_id": corpus_id, "source_epoch": epoch}, omissions
    finally:
        conn.close()


def _bundle(candidate):
    from muninn.index.projection import attribution_id
    from muninn.search.documents import ContextEntry, CONTEXT_ESTIMATOR

    citation_id = attribution_id(candidate.document_id, candidate.source_revision)
    common = {"document_id": candidate.document_id, "namespace": candidate.namespace,
              "source_ref": candidate.source_ref, "source_revision": candidate.source_revision}
    citation = ContextEntry(citation_id, "attribution", "", {
        **common, "citation": {**common, "title": candidate.title},
        "cost_estimator": CONTEXT_ESTIMATOR})
    span = {"start": candidate.start, "end": candidate.end,
            "span_coordinate_system": "normalized_body_unicode_codepoints",
            "text_digest": candidate.text_digest}
    identifier = "legacy-excerpt:" + hashlib.sha256(json.dumps(
        [candidate.document_id, candidate.source_revision, span],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    excerpt = ContextEntry(identifier, "context_excerpt", candidate.excerpt, {
        **common, **span, "title": candidate.title, "attribution_id": citation_id,
        "matched_stages": list(candidate.matched_stages), "selection": "legacy_lexical_excerpt"})
    return citation, excerpt


def legacy_context(request, db_path=None, reason="braid_unavailable"):
    """Admit complete lexical source bundles, with separately labelled limits.

    This is a degraded ordered admission path, not Braid graph/diversity packing.
    Required source bundles are resolved independently of query recall.
    """
    from muninn.search.context import context_json, estimate_entry_cost, finalize_context
    from muninn.index.registry import RegistryError
    from muninn.consumers.mcp.vault_fs import VaultAccessError

    coverage = {"mode": "legacy_lexical_excerpts", "graph": False, "diversity": False,
                "authored_recall": "keyword_only", "candidate_limit_per_search": _candidate_limit(request),
                "candidate_scope": "combined" if len(request.namespaces) == 5 else "per_namespace",
                "optional_root_limit": request.optional_roots}
    def finish(entries=(), **kwargs):
        return finalize_context(entries, request, backend="fts5-qdrant", coverage=coverage,
                                fallback_reason=reason, **kwargs)

    if request.expected_generation is not None:
        return finish(state="unavailable", reason="expected_generation_unavailable")
    for attempt in range(2):
        try:
            candidates, boundary, omissions = _candidates(request, db_path)
            break
        except (sqlite3.Error, RegistryError, VaultAccessError, OSError):
            return finish(state="unavailable", reason="legacy_source_unavailable")
        except RuntimeError as exc:
            if str(exc) != "legacy_source_changed":
                raise
            if attempt:
                return finish(state="unavailable", reason="source_changed")
    required = set(request.required_sources)
    present = {candidate.trusted_ref for candidate in candidates}
    if required - present:
        return finish(generation=boundary, state="abstained", reason="required_source_unavailable",
                      omissions=({"reason": "required_source_unavailable"},))
    if set(request.anchors) - present:
        omissions.append({"reason": "anchor_unavailable"})
    candidates.sort(key=lambda item: item.trusted_ref not in required)
    entries, ids, optional_count = [], set(), 0
    for candidate in candidates:
        mandatory = candidate.trusted_ref in required
        if not mandatory and optional_count >= request.optional_roots:
            omissions.append({"reason": "optional_root_limit"})
            continue
        bundle = [entry for entry in _bundle(candidate) if entry.id not in ids]
        proposed = [*entries, *bundle]
        if (len(context_json(proposed)) > request.max_bytes
                or sum(estimate_entry_cost(entry) for entry in proposed) > request.budget):
            if mandatory:
                return finish(generation=boundary, state="abstained", reason="required_source_infeasible",
                              omissions=({"reason": "required_source_infeasible"},))
            omissions.append({"reason": "optional_bundle_infeasible"})
            continue
        entries.extend(bundle)
        ids.update(entry.id for entry in bundle)
        if not mandatory:
            optional_count += 1
    return finish(entries, generation=boundary, omissions=omissions,
                  warnings=("degraded_legacy_context",))
