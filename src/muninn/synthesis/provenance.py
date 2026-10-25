"""Immutable deep-pass results, separate from the mutable enrichment projection.

Writers run inside the caller's transaction and never commit. Record before
changing projections and skip those changes when ``inserted`` is false. This
preserves a later enrichment when an old result is ingested a second time.
History is recovery evidence only: these helpers choose no display precedence
and never restore a result into enrichment, FTS, or the vault.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from muninn.models import SynthesisRun
from muninn.synthesis.contract import canonical_bytes


class DeepPassReplayError(ValueError):
    """An existing task/attempt was replayed with different evidence."""


@dataclass(frozen=True)
class DeepPassIdentity:
    task_id: str
    attempt: int
    deep_pass_result_id: int
    inserted: bool


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _material_bookmark_ids(
    materials: dict[str, Any], output: dict[str, Any], bookmark_id: int
) -> list[int]:
    # A model can paraphrase any neighbor it saw, even without citing it.
    # Withhold the entire output if any such bookmark later becomes hidden.
    ids = {bookmark_id}
    ids.update(materials.get("known_bookmark_ids") or [])
    ids.update(n["bookmark_id"] for n in materials.get("candidate_neighbors") or [])
    ids.update(
        ref["target_bookmark_id"]
        for ref in output.get("cross_references") or []
        if ref.get("target_bookmark_id") is not None
    )
    if any(type(bid) is not int or bid <= 0 for bid in ids):
        raise ValueError("deep-pass material bookmark ids must be positive integers")
    return sorted(ids)


def record_deep_pass(
    conn: sqlite3.Connection,
    output: dict[str, Any],
    run: SynthesisRun,
    *,
    bookmark_id: int,
    materials: dict[str, Any],
) -> DeepPassIdentity:
    """Archive the full validated output once per ``(task_id, attempt)``.

    Digest the exact source text the task supplied, including empty text;
    never infer it from current scrapes or the bulk enrichment hash. The
    materials digest binds every input field without retaining source text,
    neighbor summaries, or raw URLs. Full model output remains private data.

    An identical replay returns ``inserted=False``. A changed output or
    provenance under the same identity raises before any projection write.
    Start/completion/recording times describe the first accepted result and
    are not compared against a later ingestion's clock. When the run has no
    completion timestamp yet, completed_at uses acceptance time, before the
    orchestrator finishes the run in the same transaction.
    """
    if run.task_type != "deep_pass":
        raise ValueError("deep-pass provenance requires a deep_pass run")
    if type(bookmark_id) is not int or bookmark_id <= 0:
        raise ValueError("bookmark_id must be a positive integer")
    if not run.task_id or run.attempt <= 0:
        raise ValueError("deep-pass provenance requires task_id and positive attempt")
    if not isinstance(materials, dict) or not isinstance(materials.get("source_text"), str):
        raise ValueError("deep-pass provenance requires the actual source_text material")
    if materials.get("bookmark_id") != bookmark_id:
        raise ValueError("deep-pass materials bookmark_id does not match the result")
    source_pass = materials.get("source_pass")
    if source_pass is not None and not isinstance(source_pass, str):
        raise ValueError("deep-pass source_pass must be text or null")

    meta = output.get("synthesis_metadata") or {}
    model = run.enrichment_model or meta.get("model") or "unknown"
    prompt = run.enrichment_prompt_version or meta.get("prompt_version") or "deep_pass_v1"
    output_bytes = canonical_bytes(output)
    material_ids = _material_bookmark_ids(materials, output, bookmark_id)
    evidence = (
        bookmark_id,
        model,
        prompt,
        source_pass,
        _digest(materials["source_text"].encode("utf-8")),
        _digest(canonical_bytes(materials)),
        canonical_bytes(material_ids).decode("utf-8"),
        output_bytes.decode("utf-8"),
        _digest(output_bytes),
    )
    existing = conn.execute(
        "SELECT deep_pass_result_id, bookmark_id, model, prompt_version, source_pass, "
        "source_content_hash, materials_digest, material_bookmark_ids_json, output_json, output_digest "
        "FROM deep_pass_results WHERE task_id = ? AND attempt = ?",
        (run.task_id, run.attempt),
    ).fetchone()
    if existing is not None:
        if tuple(existing)[1:] != evidence:
            raise DeepPassReplayError(
                f"deep-pass {run.task_id!r} attempt {run.attempt} already has different evidence"
            )
        return DeepPassIdentity(run.task_id, run.attempt, existing[0], False)

    # A task snapshot can outlive a policy change. Refuse a new acceptance
    # if any input or reference is now hidden, before either history or the
    # caller's mutable summary projection can disclose its paraphrased text.
    unavailable = conn.execute(
        "SELECT input.value FROM json_each(?) input "
        "LEFT JOIN bookmarks b ON b.bookmark_id = input.value "
        "WHERE b.bookmark_id IS NULL OR b.content_visible <> 1 LIMIT 1",
        (evidence[6],),
    ).fetchone()
    if unavailable is not None:
        raise LookupError("deep-pass input or reference is missing or hidden by domain policy")

    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO deep_pass_results (task_id, attempt, bookmark_id, model, prompt_version, "
        "source_pass, source_content_hash, materials_digest, material_bookmark_ids_json, "
        "output_json, output_digest, started_at, completed_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run.task_id, run.attempt, *evidence, run.started_at,
         run.completed_at if run.completed_at is not None else now, now),
    )
    return DeepPassIdentity(run.task_id, run.attempt, cur.lastrowid, True)


_VISIBLE = """
    EXISTS (SELECT 1 FROM bookmarks b
            WHERE b.bookmark_id = r.bookmark_id AND b.content_visible = 1)
    AND NOT EXISTS (
        SELECT 1 FROM json_each(r.material_bookmark_ids_json) input
        LEFT JOIN bookmarks b ON b.bookmark_id = input.value
        WHERE b.bookmark_id IS NULL OR b.content_visible <> 1
    )
"""


def _records(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [col[0] for col in cursor.description]
    records = []
    for row in cursor:
        record = dict(zip(columns, row))
        record["output"] = json.loads(record.pop("output_json"))
        record["material_bookmark_ids"] = json.loads(record.pop("material_bookmark_ids_json"))
        records.append(record)
    return records


def get_deep_pass(
    conn: sqlite3.Connection, task_id: str, attempt: int
) -> dict[str, Any] | None:
    """Read recovery evidence by identity, only while all inputs are visible.

    Hidden and missing bookmark records are indistinguishable from absence.
    No output is applied to the mutable projection.
    """
    rows = _records(conn.execute(
        "SELECT r.* FROM deep_pass_results r WHERE r.task_id = ? AND r.attempt = ? AND " + _VISIBLE,
        (task_id, attempt),
    ))
    return rows[0] if rows else None


def list_deep_passes(
    conn: sqlite3.Connection,
    bookmark_id: int,
    *,
    limit: int = 100,
    after_id: int = 0,
) -> list[dict[str, Any]]:
    """List a bounded page of visible recovery evidence in acceptance order.

    Continue with the last returned ``deep_pass_result_id`` as ``after_id``.
    This ordering chooses no display winner and performs no restoration.
    """
    if type(bookmark_id) is not int or bookmark_id <= 0:
        raise ValueError("bookmark_id must be a positive integer")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    if type(after_id) is not int or after_id < 0:
        raise ValueError("after_id must be a nonnegative integer")
    return _records(conn.execute(
        "SELECT r.* FROM deep_pass_results r "
        "WHERE r.bookmark_id = ? AND r.deep_pass_result_id > ? AND " + _VISIBLE +
        " ORDER BY r.deep_pass_result_id LIMIT ?",
        (bookmark_id, after_id, limit),
    ))
