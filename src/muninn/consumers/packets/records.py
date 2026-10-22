"""Contract records built from muninn's database rows (contract §4, §10).

One function per record type, each a pure mapping from the internal rows
(draft 002 storage plus the 001 ``bookmarks`` / ``scrape_results`` tables)
to the export shape. The suitability task and the packet exporter both use
them, so an assessment's digest is computed the same way everywhere.

Internal shapes assumed (from the contract's §10.1 X adapter mapping):

* ``bookmarks`` rows: ``source`` ``x`` (``source_id`` = post id),
  ``x_article`` (article id) or ``x_link`` (sha256 of the sanitized
  canonical locator, which is ``bookmarks.url``). Browser bookmarks are
  exported as ``web:``/``github:repo:`` items of their sanitized ``url``.
* ``bookmarks.source_metadata`` for X posts: ``author`` (``id``,
  ``username``, ``name``; ``null`` when the API returned none),
  ``published_at`` (epoch seconds or ISO-8601), ``lang``/``language``,
  ``conversation_id``, ``possibly_sensitive``, ``media_keys`` (or
  ``has_media``), ``is_long_post``, ``last_observed_at`` (epoch),
  ``sync_run_id`` (ULID), ``folder_ids``, and optionally
  ``userinfo_redacted``. Never the post text.
* the post text: ``scrape_results`` pass ``live`` (current) and
  ``scrape_history`` rows with ``retrieval_method = 'x_api'`` (history).

Stdlib-only, like the rest of the package.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from muninn.consumers.packets.canonical import (
    SHA256_RE,
    content_digest,
    is_ulid,
    link_item_id,
    normalize_text,
    record_digest,
    x_article_item_id,
    x_post_item_id,
)

_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_X_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_FOLDER_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_GITHUB_ID_RE = re.compile(r"^github:repo:([a-z0-9][a-z0-9-]{0,38})/([a-z0-9._-]{1,100})$")

# Observation sources the contract knows (bookmark-observation.source).
OBSERVATION_BASIS = {"x": "first_seen_by_sync", "bookmarks_html": "source_reported"}


# ── Times ────────────────────────────────────────────────────────────

def iso(epoch: int | None) -> str | None:
    """Epoch seconds to the contract's ISO-8601 UTC form (``...Z``)."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_epoch(value: Any) -> int | None:
    """Epoch seconds from an int or an ISO-8601 string (X ``created_at``)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row["source_metadata"]
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _json_list(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


# ── Items ────────────────────────────────────────────────────────────

def item_id_for(row: Mapping[str, Any]) -> str:
    """The typed export ``item_id`` of a ``bookmarks`` row (contract §3.1)."""
    source, source_id, url = row["source"], row["source_id"], row["url"]
    if source == "x":
        return x_post_item_id(source_id)
    if source == "x_article":
        return x_article_item_id(source_id)
    if url and url.startswith(("http://", "https://")):
        return link_item_id(url)
    if source == "x_link" and SHA256_RE.match(source_id or ""):
        return f"web:{source_id}"
    raise ValueError(f"bookmark {row['bookmark_id']} has no exportable identity")


def _kind(item_id: str) -> str:
    if item_id.startswith("x:post:"):
        return "x_post"
    if item_id.startswith("x:article:"):
        return "x_article"
    if item_id.startswith("github:repo:"):
        return "github_repo"
    return "web_page"


def _author(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    platform_id = str(value.get("platform_id") or value.get("id") or "")
    if not _X_ID_RE.match(platform_id):
        return None
    out: dict[str, Any] = {"platform_id": platform_id}
    handle = value.get("handle") or value.get("username")
    if isinstance(handle, str) and _HANDLE_RE.match(handle):
        out["handle"] = handle
    name = value.get("display_name") or value.get("name")
    if isinstance(name, str) and name.strip():
        out["display_name"] = name.strip()[:100]
    return out


def source_item_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """``source-item`` for a ``bookmarks`` row."""
    item_id = item_id_for(row)
    kind = _kind(item_id)
    md = _metadata(row)
    url = row["url"]
    title = (row["title"] or "").strip() or None
    rec: dict[str, Any] = {
        "record_type": "source-item",
        "item_id": item_id,
        "kind": kind,
        "locator": url if url and url.startswith(("http://", "https://")) else None,
        "title": title[:1000] if title else None,
        "published_at": iso(to_epoch(md.get("published_at"))),
        "redaction": {
            "param_names": list(dict.fromkeys(str(n) for n in _json_list(row["redacted_param_names"]))),
            "path_redacted": bool(row["path_redacted"]),
            "userinfo_redacted": bool(md.get("userinfo_redacted", False)),
        },
    }
    lang = md.get("language", md.get("lang"))
    if isinstance(lang, str) and _LANG_RE.match(lang):
        rec["language"] = lang
    if kind in ("x_post", "x_article") and "author" in md:
        rec["author"] = _author(md["author"])
    if kind == "x_post":
        x_post: dict[str, Any] = {"is_long_post": bool(md.get("is_long_post", False))}
        conv = md.get("conversation_id")
        if conv is not None and _X_ID_RE.match(str(conv)):
            x_post["conversation_id"] = str(conv)
        if "has_media" in md or "media_keys" in md:
            x_post["has_media"] = bool(md.get("has_media") or md.get("media_keys"))
        if isinstance(md.get("possibly_sensitive"), bool):
            x_post["possibly_sensitive"] = md["possibly_sensitive"]
        rec["x_post"] = x_post
    if kind == "github_repo":
        m = _GITHUB_ID_RE.match(item_id)
        assert m is not None
        rec["repository"] = {"owner": m.group(1), "name": m.group(2)}
    return rec


def observation_record(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """``bookmark-observation`` for a bookmarked item, or None for rows that
    are not bookmarks themselves (linked items, unknown sources)."""
    basis = OBSERVATION_BASIS.get(row["source"])
    if basis is None:
        return None
    md = _metadata(row)
    rec: dict[str, Any] = {
        "record_type": "bookmark-observation",
        "item_id": item_id_for(row),
        "source": row["source"],
        "observed_at": iso(row["captured_at"]),
        "observed_at_basis": basis,
    }
    last = to_epoch(md.get("last_observed_at"))
    if last is not None:
        rec["last_observed_at"] = iso(last)
    if is_ulid(md.get("sync_run_id")):
        rec["sync_run_id"] = md["sync_run_id"]
    folders = md.get("folder_ids")
    if isinstance(folders, list):
        clean = [str(f) for f in folders if _FOLDER_RE.match(str(f))]
        if clean:
            rec["folder_ids"] = list(dict.fromkeys(clean))
    return rec


def item_link_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """``item-link`` for an ``item_links`` row."""
    rec: dict[str, Any] = {
        "record_type": "item-link",
        "parent_item_id": row["parent_ref"],
        "child_item_id": row["child_ref"],
        "relation": row["relation"],
        "position": row["position"],
        "locator_resolution": row["locator_resolution"],
        "first_seen_at": iso(row["first_seen_at"]),
    }
    if row["entity_status"] is not None:
        rec["entity_status"] = row["entity_status"]
    return rec


# ── Content revisions ────────────────────────────────────────────────

def revision_text(row: Mapping[str, Any]) -> str | None:
    """The revision's normalized text if it is stored and matches its digest."""
    if row["coverage"] not in ("full", "partial") or row["content_text"] is None:
        return None
    text = normalize_text(row["content_text"])
    if content_digest(text) != row["content_sha256"]:
        return None
    return text


def revision_record(row: Mapping[str, Any], *, include_text: bool = True) -> dict[str, Any]:
    """``content-revision`` for a ``scrape_history`` row."""
    method = row["retrieval_method"]
    rec: dict[str, Any] = {
        "record_type": "content-revision",
        "revision_id": row["revision_ulid"],
        "item_id": row["item_ref"],
        "content_sha256": row["content_sha256"],
        "normalization": row["normalization"],
        "char_count": row["char_count"],
        "coverage": row["coverage"],
        "retrieval_method": method,
        "freshness": "historical" if method == "archive" else "current",
        "extractor": {"name": row["extractor_name"], "version": row["extractor_version"]},
        "http_status": row["http_status"],
        "published_at": iso(row["published_at"]),
        "retrieved_at": iso(row["retrieved_at"]),
        "observed_at": iso(row["observed_at"]),
    }
    if row["scrape_pass"] is not None:
        rec["scrape_pass"] = row["scrape_pass"]
    if row["final_locator"] is not None:
        rec["final_locator"] = row["final_locator"]
    if row["archive_snapshot_at"] is not None:
        rec["archive_snapshot_at"] = iso(row["archive_snapshot_at"])
    if include_text:
        text = revision_text(row)
        if text is not None:
            rec["text"] = text
    return rec


def current_revision_row(
    conn: sqlite3.Connection, item_ref: str, bookmark_id: int | None = None
) -> sqlite3.Row | None:
    """The item's current content revision, or None.

    Current means a non-withdrawn ``x_api`` or ``live`` revision (archive
    revisions are historical, contract §8). Revisions are a
    content-addressed set, not a timeline (contract §5), so the revision
    whose digest matches the item's current ``scrape_results`` live row
    wins; the most recently retrieved one is the fallback.
    """
    rows = conn.execute(
        """
        SELECT * FROM scrape_history
        WHERE  item_ref = ? AND withdrawn_at IS NULL
          AND  retrieval_method IN ('x_api', 'live')
        ORDER  BY retrieved_at DESC, scrape_history_id DESC
        """,
        (item_ref,),
    ).fetchall()
    if not rows:
        return None
    if bookmark_id is not None:
        live = conn.execute(
            "SELECT content_text FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
            (bookmark_id,),
        ).fetchone()
        if live is not None:
            want = content_digest(live["content_text"])
            for row in rows:
                if row["content_sha256"] == want:
                    return row
    return rows[0]


# ── Profiles, assessments, usage ─────────────────────────────────────

def profile_record(conn: sqlite3.Connection, profile_sha256: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT body_json FROM interest_profiles WHERE profile_sha256 = ?", (profile_sha256,)
    ).fetchone()
    if row is None:
        raise LookupError(f"interest profile {profile_sha256} is not recorded")
    return json.loads(row["body_json"])


def assessment_record(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    _cache: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``assessment`` for an ``assessments`` row (with its prior's digest)."""
    cache = _cache if _cache is not None else {}
    if row["assessment_id"] in cache:
        return cache[row["assessment_id"]]
    rev = conn.execute(
        "SELECT revision_ulid FROM scrape_history WHERE scrape_history_id = ?",
        (row["scrape_history_id"],),
    ).fetchone()
    prof = conn.execute(
        "SELECT profile_id FROM interest_profiles WHERE profile_sha256 = ?", (row["profile_sha256"],)
    ).fetchone()
    rec: dict[str, Any] = {
        "record_type": "assessment",
        "assessment_id": row["assessment_ulid"],
        "item_id": row["item_ref"],
        "content_revision": {"ref": rev["revision_ulid"], "digest": row["content_sha256"]},
        "coverage": row["content_coverage"],
        "interest_profile": {"ref": prof["profile_id"], "digest": row["profile_sha256"]},
        "policy_version": row["policy_version"],
        "model": row["model"],
        "prompt_version": row["prompt_version"],
        "disposition": row["disposition"],
        "relevance": row["relevance"],
        "confidence": row["confidence"],
        "rationale": row["rationale"],
        "matched_interests": json.loads(row["matched_interests"]),
        "unresolved_questions": json.loads(row["unresolved_questions"]),
        "assessed_at": iso(row["assessed_at"]),
    }
    if row["prior_assessment_id"] is not None:
        prior = conn.execute(
            "SELECT * FROM assessments WHERE assessment_id = ?", (row["prior_assessment_id"],)
        ).fetchone()
        prior_rec = assessment_record(conn, prior, cache)
        rec["prior_assessment"] = {"ref": prior_rec["assessment_id"], "digest": record_digest(prior_rec)}
    cache[row["assessment_id"]] = rec
    return rec


def usage_record(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    """``usage-record`` for a ``usage_records`` row."""
    rec: dict[str, Any] = {
        "record_type": "usage-record",
        "usage_id": row["usage_ulid"],
        "kind": row["kind"],
        "provider": row["provider"],
        "operation": row["operation"],
        "request_count": row["request_count"],
        "cost_micro_usd": row["cost_micro_usd"],
        "cost_basis": row["cost_basis"],
        "occurred_at": iso(row["occurred_at"]),
    }
    if row["resource_count"] is not None:
        rec["resource_count"] = row["resource_count"]
    if row["kind"] == "model":
        rec["model"] = row["model"]
        for col, key in (
            ("input_token_count", "input_tokens"),
            ("output_token_count", "output_tokens"),
            ("cache_read_token_count", "cache_read_tokens"),
        ):
            if row[col] is not None:
                rec[key] = row[col]
    if row["assessment_id"] is not None:
        a = conn.execute(
            "SELECT assessment_ulid FROM assessments WHERE assessment_id = ?", (row["assessment_id"],)
        ).fetchone()
        rec["assessment_id"] = a["assessment_ulid"]
    if row["sync_checkpoint_id"] is not None:
        s = conn.execute(
            "SELECT run_ulid FROM sync_checkpoints WHERE sync_checkpoint_id = ?",
            (row["sync_checkpoint_id"],),
        ).fetchone()
        if s is not None:
            rec["sync_run_id"] = s["run_ulid"]
    return rec
