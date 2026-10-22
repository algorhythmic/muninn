"""Map stage-native rows onto muninn-research-intake 0.1 contract records.

The one implementation of the row-to-record mapping, shared by the writers
(``muninn ingest x``, ``muninn scrape``), the suitability task and the
packet exporter: how a ``bookmarks`` / ``item_links`` / ``scrape_history`` /
``assessments`` / ``usage_records`` row becomes a ``source-item`` /
``item-link`` / ``bookmark-observation`` / ``content-revision`` /
``assessment`` / ``usage-record`` dict, per
``docs/contracts/MUNINN-RESEARCH-INTAKE.md`` §3, §4 and §10.1.

Optional keys: a key the schema *requires* is always present, null where
the schema allows null and the value is unknown. A key the schema leaves
*optional* is omitted when there is no value, and never written as null.
The one exception is ``source-item.author``, where null is a stated fact
(the X API returned no author) and absence means the item has no author.

Stdlib only (plus the stdlib-only packets canonical module), so any stage
may import it. Rows may be ``sqlite3.Row`` or plain mappings. Times in the
database are epoch seconds; records carry ISO-8601 UTC with a ``Z``.

The functions build records; they do not validate them. Tests validate the
output against ``schemas/json/intake/``, and the exporter validates every
packet before it writes it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from muninn.consumers.packets.canonical import (
    content_digest,
    is_ulid,
    link_item_id,
    normalize_text,
    record_digest,
    x_article_item_id,
    x_post_item_id,
)

# bookmarks.source values written by the X adapter.
SOURCE_X_POST = "x"
SOURCE_X_ARTICLE = "x_article"
SOURCE_X_LINK = "x_link"
X_SOURCES = (SOURCE_X_POST, SOURCE_X_ARTICLE, SOURCE_X_LINK)
# Sources whose own text comes from the X API, never from `muninn scrape`.
API_CONTENT_SOURCES = (SOURCE_X_POST, SOURCE_X_ARTICLE)
# Coverages that carry no readable content (contract §4.4).
NO_CONTENT_COVERAGE = ("blocked", "deleted", "empty")

ITEM_ID_RE = re.compile(
    r"^(x:post:[1-9][0-9]{0,19}|x:article:[1-9][0-9]{0,19}"
    r"|github:repo:[a-z0-9][a-z0-9-]{0,38}/[a-z0-9._-]{1,100}|web:[0-9a-f]{64})$"
)
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_X_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_FOLDER_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


# ── Times and row access ─────────────────────────────────────────────

def iso(epoch: int | None) -> str | None:
    """Epoch seconds → ``2026-10-20T08:00:00Z`` (None stays None)."""
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
      not a contract item ID (:func:`export_item_id` refuses it)
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


def export_item_id(row: Mapping[str, Any]) -> str:
    """The contract ``item_id`` of a ``bookmarks`` row, or ValueError when the
    row has no exportable identity (no locator and no X ID)."""
    try:
        item_id = item_ref_for_row(row)
    except ValueError as exc:
        raise ValueError(f"bookmark {_get(row, 'bookmark_id')} has no exportable identity") from exc
    if not ITEM_ID_RE.match(item_id):
        raise ValueError(f"bookmark {_get(row, 'bookmark_id')} has no exportable identity")
    return item_id


def item_kind(item_id: str) -> str:
    if item_id.startswith("x:post:"):
        return "x_post"
    if item_id.startswith("x:article:"):
        return "x_article"
    if item_id.startswith("github:repo:"):
        return "github_repo"
    return "web_page"


# ── Items, links, observations ───────────────────────────────────────

def _author(raw: Any) -> dict[str, Any] | None:
    """``source_metadata.author`` (``{id, username, name}``, the X API's own
    names) → the record's ``{platform_id, handle, display_name}``."""
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
    if isinstance(name, str) and name.strip():
        out["display_name"] = name.strip()[:100]
    return out


def source_item_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """A ``bookmarks`` row → ``source-item`` record."""
    meta = row_metadata(row)
    item_id = item_ref_for_row(row)
    kind = item_kind(item_id)
    url = _get(row, "url")
    title = (_get(row, "title") or "").strip()
    record: dict[str, Any] = {
        "record_type": "source-item",
        "item_id": item_id,
        "kind": kind,
        "locator": url if isinstance(url, str) and url.startswith(("http://", "https://")) else None,
        "title": title[:1000] or None,
        "published_at": iso(to_epoch(meta.get("published_at"))),
        "redaction": {
            "param_names": list(dict.fromkeys(
                str(n) for n in _json(_get(row, "redacted_param_names"), []) or []
            )),
            "path_redacted": bool(_get(row, "path_redacted", 0)),
            "userinfo_redacted": bool(meta.get("userinfo_redacted", False)),
        },
    }
    language = meta.get("lang") or meta.get("language")
    if isinstance(language, str) and _LANGUAGE_RE.match(language):
        record["language"] = language
    if kind in ("x_post", "x_article") and "author" in meta:
        record["author"] = _author(meta["author"])
    if kind == "x_post":
        x_post: dict[str, Any] = {"is_long_post": bool(meta.get("is_long_post", False))}
        if _X_ID_RE.match(str(meta.get("conversation_id") or "")):
            x_post["conversation_id"] = str(meta["conversation_id"])
        if isinstance(meta.get("media_keys"), list):
            x_post["has_media"] = bool(meta["media_keys"])
        elif isinstance(meta.get("has_media"), bool):
            x_post["has_media"] = meta["has_media"]
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
    if not is_bookmarked(row) or _get(row, "captured_at") is None:
        return None
    source = _get(row, "source")
    if source == SOURCE_X_POST:
        meta = row_metadata(row)
        record: dict[str, Any] = {
            "record_type": "bookmark-observation",
            "item_id": item_ref_for_row(row),
            "source": "x",
            "observed_at": iso(_get(row, "captured_at")),
            "observed_at_basis": "first_seen_by_sync",
        }
        last = to_epoch(meta.get("last_observed_at"))
        if last is not None:
            record["last_observed_at"] = iso(last)
        if is_ulid(meta.get("first_sync_run_id")):
            record["sync_run_id"] = meta["first_sync_run_id"]
        folders = meta.get("folder_ids")
        if isinstance(folders, list):
            clean = [str(f) for f in folders if _FOLDER_RE.match(str(f))]
            if clean:
                record["folder_ids"] = list(dict.fromkeys(clean))
        position = meta.get("list_position")
        if isinstance(position, int) and not isinstance(position, bool) and position >= 0:
            record["list_position"] = position
        return record
    if source == "bookmarks_html":
        try:
            item_id = export_item_id(row)
        except ValueError:
            return None
        return {
            "record_type": "bookmark-observation",
            "item_id": item_id,
            "source": "bookmarks_html",
            "observed_at": iso(_get(row, "captured_at")),
            "observed_at_basis": "source_reported",
        }
    return None


def item_link_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """An ``item_links`` row → ``item-link`` record."""
    relation = _get(row, "relation")
    resolution = _get(row, "locator_resolution")
    if resolution is None and relation != "links_to":
        resolution = "api_reference"  # referenced by ID; a links_to link must say how
    record: dict[str, Any] = {
        "record_type": "item-link",
        "parent_item_id": _get(row, "parent_ref"),
        "child_item_id": _get(row, "child_ref"),
        "relation": relation,
        "position": int(_get(row, "position", 0)),
        "locator_resolution": resolution,
        "first_seen_at": iso(_get(row, "first_seen_at")),
    }
    status = _get(row, "entity_status")
    if status is not None:
        record["entity_status"] = int(status)
    return record


# ── Hidden locators ──────────────────────────────────────────────────
#
# A visible item can still quote the locator of an item that domain policy
# hides: a post's stored text, title or enrichment written before the
# policy change. These helpers find and mask such locators the same way in
# every consumer (the model context, packets, the vault, MCP, Qdrant).
# Matching ignores case (a repository locator is stored lowercase, the
# post text keeps the author's case) and accepts the locator with or
# without its scheme and ``www.``.

HIDDEN_LINK_TEXT = "[hidden link]"
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_URL_CHAR_BEFORE = r"(?<![A-Za-z0-9_.\-/])"
_URL_CHAR_AFTER = r"(?![A-Za-z0-9_\-])"


def locator_core(url: str) -> str:
    """``url`` without scheme, leading ``www.`` and trailing ``/``, lowercased."""
    core = _SCHEME_RE.sub("", url.strip()).lower()
    core = core.removeprefix("www.")
    return core.rstrip("/")


def _locator_regex(urls: set[str] | frozenset[str]) -> re.Pattern[str] | None:
    cores = sorted({c for c in (locator_core(u) for u in urls if u) if c}, key=len, reverse=True)
    if not cores:
        return None
    alternatives = "|".join(
        re.escape(c) + (r"(?:\.git)?" if c.startswith("github.com/") else "") + "/?"
        for c in cores
    )
    return re.compile(
        rf"{_URL_CHAR_BEFORE}(?:https?://)?(?:www\.)?(?:{alternatives}){_URL_CHAR_AFTER}", re.I
    )


def mentions_locator(text: str | None, urls: set[str] | frozenset[str]) -> bool:
    """True if ``text`` quotes any of these locators (see the note above)."""
    if not text or not urls:
        return False
    pattern = _locator_regex(urls)
    return bool(pattern and pattern.search(text))


def mask_locators(text: str, urls: set[str] | frozenset[str]) -> str:
    """Replace every quoted locator in ``text`` with ``[hidden link]``."""
    if not text or not urls:
        return text
    pattern = _locator_regex(urls)
    return pattern.sub(HIDDEN_LINK_TEXT, text) if pattern else text


def mask_title(title: str, urls: set[str] | frozenset[str]) -> str:
    """:func:`mask_locators` for a title. A post title is the start of the
    post text cut to about 80 characters, so it can end in a truncated
    locator (``… https://intra…``): a final token that is a prefix of a
    hidden locator is masked too, once it says more than a bare scheme."""
    title = mask_locators(title, urls)
    if not urls or not title.endswith(("…", "...")):
        return title
    head, _, last = title.rpartition(" ")
    stem = last.rstrip(".…")
    has_scheme = bool(_SCHEME_RE.match(stem))
    stem_core = _SCHEME_RE.sub("", stem).lower().removeprefix("www.")
    if not stem_core or not (has_scheme or "." in stem_core or "/" in stem_core):
        return title
    if any(locator_core(u).startswith(stem_core) for u in urls if u):
        return f"{head} {HIDDEN_LINK_TEXT}" if head else HIDDEN_LINK_TEXT
    return title


def hidden_child_locators(conn: sqlite3.Connection, bookmark_ids: Any) -> dict[int, set[str]]:
    """``{parent bookmark_id: locators of its hidden linked items}`` for these
    parents (only parents with at least one hidden child appear)."""
    ids = sorted({int(b) for b in bookmark_ids if b is not None})
    if not ids or conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'item_links'"
    ).fetchone() is None:
        return {}
    out: dict[int, set[str]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for parent, url in conn.execute(
            f"""
            SELECT l.parent_bookmark_id, b.url FROM item_links l
            JOIN   bookmarks b ON b.bookmark_id = l.child_bookmark_id
            WHERE  l.parent_bookmark_id IN ({','.join('?' * len(chunk))})
              AND  b.content_visible = 0 AND b.url IS NOT NULL AND b.url != ''
            """,
            chunk,
        ):
            out.setdefault(parent, set()).add(url)
    return out


# ── Content revisions ────────────────────────────────────────────────

def revision_text(row: Mapping[str, Any]) -> str | None:
    """The revision's normalized text, if it is stored, readable and matches
    its digest (withdrawn and no-content revisions have none)."""
    if _get(row, "withdrawn_at") is not None or _get(row, "coverage") in NO_CONTENT_COVERAGE:
        return None
    raw = _get(row, "content_text")
    if raw is None:
        return None
    text = normalize_text(raw)
    if not text or content_digest(text) != _get(row, "content_sha256"):
        return None
    return text


def content_revision_record(row: Mapping[str, Any], *, include_text: bool = False) -> dict[str, Any]:
    """A ``scrape_history`` row → ``content-revision`` record.

    ``include_text`` adds the normalized ``text`` when the row still holds
    readable text that matches its digest (see :func:`revision_text`).
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
    if include_text:
        text = revision_text(row)
        if text is not None:
            record["text"] = text
    return record


def current_revision_row(
    conn: sqlite3.Connection, item_ref: str, bookmark_id: int | None = None
) -> sqlite3.Row | None:
    """The item's current content revision, or None.

    Revisions are a content-addressed set, not a timeline (contract §5),
    so the non-withdrawn revision whose digest matches the item's current
    ``scrape_results`` live row wins. That match may be a revision first
    recorded from an archive capture: the revision key leaves out the
    retrieval method, so a live fetch identical to an earlier archive
    capture adds no row of its own, yet its text is the item's current
    text (contract §3.2). An archive revision is accepted only through
    such a match with readable live text. Otherwise the most recently
    retrieved non-withdrawn ``x_api`` or ``live`` revision is the fallback
    (archive revisions are historical, contract §8).
    """
    rows = conn.execute(
        """
        SELECT * FROM scrape_history
        WHERE  item_ref = ? AND withdrawn_at IS NULL
        ORDER  BY retrieved_at DESC, scrape_history_id DESC
        """,
        (item_ref,),
    ).fetchall()
    if not rows:
        return None
    current = [r for r in rows if r["retrieval_method"] in ("x_api", "live")]
    if bookmark_id is not None:
        live = conn.execute(
            "SELECT content_text FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
            (bookmark_id,),
        ).fetchone()
        if live is not None:
            want = content_digest(live["content_text"])
            for row in current:
                if row["content_sha256"] == want:
                    return row
            if normalize_text(live["content_text"]):
                for row in rows:  # an archive revision with the same readable text
                    if row["content_sha256"] == want and row["coverage"] not in NO_CONTENT_COVERAGE:
                        return row
    return current[0] if current else None


# ── Profiles, assessments, usage ─────────────────────────────────────

def profile_record(conn: sqlite3.Connection, profile_sha256: str) -> dict[str, Any]:
    """The stored ``interest-profile`` revision with this digest."""
    row = conn.execute(
        "SELECT body_json FROM interest_profiles WHERE profile_sha256 = ?", (profile_sha256,)
    ).fetchone()
    if row is None:
        raise LookupError(f"interest profile {profile_sha256} is not recorded")
    return json.loads(row["body_json"])


def assessment_record(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    cache: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """An ``assessments`` row → ``assessment`` record (with its prior's digest).

    ``evidence`` is present only when the row stores model-cited evidence
    (``evidence_json``), already checked against the cited revisions when
    the assessment was written.
    """
    cache = cache if cache is not None else {}
    if row["assessment_id"] in cache:
        return cache[row["assessment_id"]]
    rev = conn.execute(
        "SELECT revision_ulid FROM scrape_history WHERE scrape_history_id = ?",
        (row["scrape_history_id"],),
    ).fetchone()
    prof = conn.execute(
        "SELECT profile_id FROM interest_profiles WHERE profile_sha256 = ?", (row["profile_sha256"],)
    ).fetchone()
    record: dict[str, Any] = {
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
        "matched_interests": _json(row["matched_interests"], []),
        "unresolved_questions": _json(row["unresolved_questions"], []),
        "assessed_at": iso(row["assessed_at"]),
    }
    evidence = _json(_get(row, "evidence_json"), None)
    if evidence:
        record["evidence"] = evidence
    if row["prior_assessment_id"] is not None:
        prior = conn.execute(
            "SELECT * FROM assessments WHERE assessment_id = ?", (row["prior_assessment_id"],)
        ).fetchone()
        prior_record = assessment_record(conn, prior, cache)
        record["prior_assessment"] = {
            "ref": prior_record["assessment_id"], "digest": record_digest(prior_record),
        }
    cache[row["assessment_id"]] = record
    return record


def usage_record(
    row: Mapping[str, Any], *, sync_run_id: str | None = None, assessment_id: str | None = None
) -> dict[str, Any]:
    """A ``usage_records`` row → ``usage-record``.

    ``sync_run_id`` and ``assessment_id`` are the run's and the assessment's
    ULIDs. When not passed they are read from ``run_ulid`` /
    ``assessment_ulid`` columns joined onto the row, if present.
    """
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
    sync_run_id = sync_run_id or _get(row, "run_ulid")
    if sync_run_id:
        record["sync_run_id"] = sync_run_id
    assessment_id = assessment_id or _get(row, "assessment_ulid")
    if assessment_id:
        record["assessment_id"] = assessment_id
    return record


__all__ = [
    "API_CONTENT_SOURCES",
    "HIDDEN_LINK_TEXT",
    "ITEM_ID_RE",
    "NO_CONTENT_COVERAGE",
    "SOURCE_X_ARTICLE",
    "SOURCE_X_LINK",
    "SOURCE_X_POST",
    "X_SOURCES",
    "assessment_record",
    "bookmark_observation_record",
    "content_revision_record",
    "current_revision_row",
    "export_item_id",
    "hidden_child_locators",
    "is_bookmarked",
    "iso",
    "item_kind",
    "item_link_record",
    "item_ref_for",
    "item_ref_for_row",
    "locator_core",
    "mask_locators",
    "mask_title",
    "mentions_locator",
    "profile_record",
    "revision_text",
    "row_metadata",
    "source_item_record",
    "to_epoch",
    "usage_record",
]
