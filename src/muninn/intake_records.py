"""Map stage-native rows onto muninn-research-intake 0.1 contract records.

Neutral ground between the writers (``muninn ingest x``, ``muninn scrape``)
and the readers (the packet exporter): one place that knows how a
``bookmarks`` / ``item_links`` / ``scrape_history`` / ``usage_records`` row
becomes a ``source-item`` / ``item-link`` / ``content-revision`` /
``bookmark-observation`` / ``usage-record`` dict, per
``docs/contracts/MUNINN-RESEARCH-INTAKE.md`` §3, §4 and §10.1.

Stdlib only (plus the stdlib-only packets canonical module), so any stage
may import it. Rows may be ``sqlite3.Row`` or plain mappings. Times in the
database are epoch seconds; records carry ISO-8601 UTC with a ``Z``.

The functions build records; they do not validate them. Tests validate the
output against ``schemas/json/intake/``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from muninn.consumers.packets.canonical import link_item_id, x_article_item_id, x_post_item_id

# bookmarks.source values written by the X adapter.
SOURCE_X_POST = "x"
SOURCE_X_ARTICLE = "x_article"
SOURCE_X_LINK = "x_link"
X_SOURCES = (SOURCE_X_POST, SOURCE_X_ARTICLE, SOURCE_X_LINK)
# Sources whose own text comes from the X API, never from `muninn scrape`.
API_CONTENT_SOURCES = (SOURCE_X_POST, SOURCE_X_ARTICLE)

_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_X_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")


def iso(epoch: int | None) -> str | None:
    """Epoch seconds → ``2026-10-20T08:00:00Z`` (None stays None)."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def row_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    meta = _json(_get(row, "source_metadata"), {})
    return meta if isinstance(meta, dict) else {}


# ── Identity ─────────────────────────────────────────────────────────

def item_ref_for(
    source: str, source_id: str, url: str | None = None, metadata: Mapping[str, Any] | None = None
) -> str:
    """The typed item ID (``scrape_history.item_ref``, export ``item_id``) of a bookmarks row.

    - ``x`` → ``x:post:<source_id>``; ``x_article`` → ``x:article:<source_id>``
    - ``x_link`` → ``github:repo:<owner>/<name>`` or ``web:<source_id>`` (the
      ``item_id`` the writer recorded in ``source_metadata``, else derived
      from the sanitized locator)
    - any other source with a locator (browser bookmarks) → the web/github
      identity of its sanitized URL, so the same page shares one item
    - otherwise ``<source>:<source_id>``, an internal-only reference that is
      not a contract item ID
    """
    if source == SOURCE_X_POST:
        return x_post_item_id(source_id)
    if source == SOURCE_X_ARTICLE:
        return x_article_item_id(source_id)
    if source == SOURCE_X_LINK:
        recorded = (metadata or {}).get("item_id")
        if isinstance(recorded, str) and recorded:
            return recorded
        return link_item_id(url) if url else f"web:{source_id}"
    if url and url.startswith(("http://", "https://")):
        return link_item_id(url)
    return f"{source}:{source_id}"


def item_ref_for_row(row: Mapping[str, Any]) -> str:
    return item_ref_for(
        _get(row, "source"), _get(row, "source_id"), _get(row, "url"), row_metadata(row)
    )


def item_kind(item_id: str) -> str:
    if item_id.startswith("x:post:"):
        return "x_post"
    if item_id.startswith("x:article:"):
        return "x_article"
    if item_id.startswith("github:repo:"):
        return "github_repo"
    return "web_page"


# ── Records ──────────────────────────────────────────────────────────

def _author(meta: Mapping[str, Any]) -> dict[str, Any] | None:
    """``source_metadata.author`` (``{id, username, name}``, the X API's own
    names) → the record's ``{platform_id, handle, display_name}``."""
    raw = meta.get("author")
    if not isinstance(raw, Mapping):
        return None
    platform_id = str(raw.get("id") or raw.get("platform_id") or "")
    if not _X_ID_RE.match(platform_id):
        return None
    out: dict[str, Any] = {"platform_id": platform_id}
    handle = raw.get("username") or raw.get("handle")
    if isinstance(handle, str) and _HANDLE_RE.match(handle):
        out["handle"] = handle
    name = raw.get("name") or raw.get("display_name")
    if isinstance(name, str) and 0 < len(name) <= 100:
        out["display_name"] = name
    return out


def source_item_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """A ``bookmarks`` row → ``source-item`` record."""
    meta = row_metadata(row)
    item_id = item_ref_for_row(row)
    kind = item_kind(item_id)
    title = _get(row, "title")
    if isinstance(title, str):
        title = title[:1000] or None
    record: dict[str, Any] = {
        "record_type": "source-item",
        "item_id": item_id,
        "kind": kind,
        "locator": _get(row, "url"),
        "title": title,
        "published_at": iso(meta.get("published_at")),
        "redaction": {
            "param_names": list(dict.fromkeys(_json(_get(row, "redacted_param_names"), []))),
            "path_redacted": bool(_get(row, "path_redacted", 0)),
            "userinfo_redacted": bool(meta.get("userinfo_redacted", False)),
        },
    }
    language = meta.get("lang") or meta.get("language")
    if isinstance(language, str) and _LANGUAGE_RE.match(language):
        record["language"] = language
    if kind in ("x_post", "x_article") and "author" in meta:
        record["author"] = _author(meta)
    if kind == "x_post":
        x_post: dict[str, Any] = {"is_long_post": bool(meta.get("is_long_post", False))}
        if _X_ID_RE.match(str(meta.get("conversation_id") or "")):
            x_post["conversation_id"] = str(meta["conversation_id"])
        if isinstance(meta.get("media_keys"), list):
            x_post["has_media"] = bool(meta["media_keys"])
        if isinstance(meta.get("possibly_sensitive"), bool):
            x_post["possibly_sensitive"] = meta["possibly_sensitive"]
        record["x_post"] = x_post
    if kind == "github_repo":
        owner, _, name = item_id.removeprefix("github:repo:").partition("/")
        record["repository"] = {"owner": owner, "name": name}
    return record


def is_bookmarked(row: Mapping[str, Any]) -> bool:
    """True for rows that record one of the operator's bookmarks.

    X posts written only because a bookmarked post quotes, replies to or
    links them carry ``source_metadata.bookmarked = false``; linked items
    (``x_link``) and articles are never bookmarks themselves.
    """
    source = _get(row, "source")
    if source in (SOURCE_X_ARTICLE, SOURCE_X_LINK):
        return False
    if source == SOURCE_X_POST:
        return row_metadata(row).get("bookmarked", True) is not False
    return True


def bookmark_observation_record(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``bookmarks`` row → ``bookmark-observation``, or None if it is no bookmark."""
    if not is_bookmarked(row):
        return None
    source = _get(row, "source")
    meta = row_metadata(row)
    if source == SOURCE_X_POST:
        record: dict[str, Any] = {
            "record_type": "bookmark-observation",
            "item_id": item_ref_for_row(row),
            "source": "x",
            "observed_at": iso(_get(row, "captured_at")),
            "observed_at_basis": "first_seen_by_sync",
        }
        if meta.get("last_observed_at") is not None:
            record["last_observed_at"] = iso(meta["last_observed_at"])
        if meta.get("first_sync_run_id"):
            record["sync_run_id"] = meta["first_sync_run_id"]
        if isinstance(meta.get("folder_ids"), list):
            record["folder_ids"] = list(dict.fromkeys(meta["folder_ids"]))
        if isinstance(meta.get("list_position"), int):
            record["list_position"] = meta["list_position"]
        return record
    if source == "bookmarks_html" and _get(row, "url") and _get(row, "captured_at"):
        return {
            "record_type": "bookmark-observation",
            "item_id": item_ref_for_row(row),
            "source": "bookmarks_html",
            "observed_at": iso(_get(row, "captured_at")),
            "observed_at_basis": "source_reported",
        }
    return None


def item_link_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """An ``item_links`` row → ``item-link`` record."""
    record: dict[str, Any] = {
        "record_type": "item-link",
        "parent_item_id": _get(row, "parent_ref"),
        "child_item_id": _get(row, "child_ref"),
        "relation": _get(row, "relation"),
        "position": int(_get(row, "position", 0)),
        "locator_resolution": _get(row, "locator_resolution") or "api_reference",
        "first_seen_at": iso(_get(row, "first_seen_at")),
    }
    status = _get(row, "entity_status")
    record["entity_status"] = int(status) if status is not None else None
    return record


def content_revision_record(row: Mapping[str, Any], *, include_text: bool = False) -> dict[str, Any]:
    """A ``scrape_history`` row → ``content-revision`` record.

    ``include_text`` adds the normalized ``text`` when the row still holds it
    (withdrawn rows do not).
    """
    method = _get(row, "retrieval_method")
    record: dict[str, Any] = {
        "record_type": "content-revision",
        "revision_id": _get(row, "revision_ulid"),
        "item_id": _get(row, "item_ref"),
        "content_sha256": _get(row, "content_sha256"),
        "normalization": _get(row, "normalization"),
        "char_count": int(_get(row, "char_count", 0)),
        "coverage": _get(row, "coverage"),
        "retrieval_method": method,
        "freshness": "historical" if method == "archive" else "current",
        "extractor": {
            "name": _get(row, "extractor_name"),
            "version": _get(row, "extractor_version"),
        },
        "published_at": iso(_get(row, "published_at")),
        "retrieved_at": iso(_get(row, "retrieved_at")),
        "observed_at": iso(_get(row, "observed_at")),
    }
    scrape_pass = _get(row, "scrape_pass")
    if scrape_pass is not None and method != "x_api":
        record["scrape_pass"] = scrape_pass
    if _get(row, "http_status") is not None:
        record["http_status"] = int(row["http_status"])
    if _get(row, "final_locator") is not None:
        record["final_locator"] = row["final_locator"]
    if method == "archive":
        record["archive_snapshot_at"] = iso(_get(row, "archive_snapshot_at"))
    if include_text and _get(row, "withdrawn_at") is None:
        text = _get(row, "content_text")
        if text is not None or record["coverage"] in ("blocked", "deleted", "empty"):
            record["text"] = text or ""
    return record


def usage_record(row: Mapping[str, Any], *, sync_run_id: str | None = None) -> dict[str, Any]:
    """A ``usage_records`` row → ``usage-record`` (``sync_run_id`` is the run's ULID)."""
    record: dict[str, Any] = {
        "record_type": "usage-record",
        "usage_id": _get(row, "usage_ulid"),
        "kind": _get(row, "kind"),
        "provider": _get(row, "provider"),
        "operation": _get(row, "operation"),
        "request_count": int(_get(row, "request_count", 0)),
        "cost_micro_usd": int(_get(row, "cost_micro_usd", 0)),
        "cost_basis": _get(row, "cost_basis"),
        "occurred_at": iso(_get(row, "occurred_at")),
    }
    if _get(row, "resource_count") is not None:
        record["resource_count"] = int(row["resource_count"])
    if record["kind"] == "model":
        record["model"] = _get(row, "model")
        for col, key in (
            ("input_token_count", "input_tokens"),
            ("output_token_count", "output_tokens"),
            ("cache_read_token_count", "cache_read_tokens"),
        ):
            if _get(row, col) is not None:
                record[key] = int(row[col])
    if sync_run_id:
        record["sync_run_id"] = sync_run_id
    return record


__all__ = [
    "API_CONTENT_SOURCES",
    "SOURCE_X_ARTICLE",
    "SOURCE_X_LINK",
    "SOURCE_X_POST",
    "X_SOURCES",
    "bookmark_observation_record",
    "content_revision_record",
    "is_bookmarked",
    "iso",
    "item_kind",
    "item_link_record",
    "item_ref_for",
    "item_ref_for_row",
    "row_metadata",
    "source_item_record",
    "usage_record",
]
