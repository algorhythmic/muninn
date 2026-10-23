"""X bookmark sync: connector pages → stage-native tables (RI-04).

Maps each :class:`~muninn.ingest.x.BookmarkPage` onto the 001/002 tables per
``docs/contracts/MUNINN-RESEARCH-INTAKE.md`` §10.1, and drives a resumable,
incremental sync run (:func:`sync_bookmarks`).

Rows written (see the module-level ``*_METADATA`` notes for the JSON shapes):

- ``bookmarks``: ``source='x'`` (``source_id`` = post ID) for bookmarked
  posts *and* for posts they quote, reply to or link (those carry
  ``source_metadata.bookmarked = false``); ``source='x_article'``
  (``source_id`` = article ID); ``source='x_link'`` (``source_id`` = sha256
  of the sanitized canonical locator) for every other linked URL. Every
  locator went through ``sanitize_url``, and ``domain_policy`` sets
  ``content_visible`` as for browser bookmarks (:mod:`muninn.ingest.policy`:
  X host aliases and handle case included). A post's ``url`` is always the
  canonical ``x.com/<handle>/status/<id>``; a hidden post's title is
  ``@handle`` only and a hidden article has none.
- The post (and article) text: the item's ``scrape_results`` row with
  ``pass='live'`` plus an append-only ``scrape_history`` revision with
  ``retrieval_method='x_api'``; ``enrichment_source='live_fallback'``.
- ``item_links`` for ``links_to`` / ``quotes`` / ``replies_to`` /
  ``article``; ``first_seen_at`` is the first sync that saw the link.
- ``errors[]`` problems on posts → ``deleted`` / ``blocked`` revisions.
- One ``sync_checkpoints`` row per run and one ``usage_records`` row per
  request, written in the same transaction as the page they belong to, so a
  crash can always resume from the last committed page without duplicates.

Re-seeing a bookmarked post updates only ``last_observed_at`` /
``last_sync_run_id`` in its metadata (plus ``content_visible``, which follows
the current domain policy); unchanged text creates no revision.

Imports httpx (via the connector), so ``muninn.ingest`` never imports it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from muninn.consumers.packets.canonical import (
    link_item_id,
    locator_source_id,
    new_ulid,
    normalize_text,
    x_article_item_id,
    x_post_item_id,
)
from muninn.db import transaction
from muninn.ingest.x import (
    BookmarkPage,
    XArticle,
    XCheckpoint,
    XClient,
    XError,
    XItemProblem,
    XLink,
    XPost,
)
from muninn.ingest.policy import (
    HIDDEN_LINK,
    X_ARTICLE_EXTRACTOR,
    X_TEXT_EXTRACTOR,
    PolicyApplyReport,
    apply_policy,
    canonical_post_locator,
    domain_of,
    handle_from_locator,
    hidden_child_patterns,
    hidden_post_title,
    link_fallback_title,
    mask_text,
    meta_handle,
    policy_hides,
    post_locator,
    post_title_for,
    row_visible_flag,
    visible_flag,
)
from muninn.intake_records import SOURCE_X_ARTICLE, SOURCE_X_LINK, SOURCE_X_POST, X_SOURCES
from muninn.revisions import record_revision
from muninn.sanitize import sanitize_url
from muninn.scrape.domain_policy import DomainPolicy, load_domain_policy

log = logging.getLogger(__name__)

COLLECTION_BOOKMARKS = "bookmarks"
OPERATION_BOOKMARKS = "GET /2/users/:id/bookmarks"
OPERATION_ME = "GET /2/users/me"
DEFAULT_MAX_PAGES = 10

SyncMode = Literal["incremental", "full"]
SyncStatus = Literal["running", "paused", "exhausted", "failed", "completed"]


@dataclass(frozen=True)
class XPricing:
    """Estimated X API prices. Pay-per-use owned reads are $0.001 per resource
    (RI-02/RI-03 work plan §A, unverified live). Whether expansions (included
    posts and users) are billed is open, so estimates count them: the
    estimate errs high."""

    micro_usd_per_resource: int = 1000

    def cost(self, resources: int) -> int:
        return resources * self.micro_usd_per_resource

    def estimate_posts(self, pages: int, max_results: int) -> int:
        """Upper bound for the bookmarked posts alone over ``pages`` pages."""
        return self.cost(pages * max_results)


def format_usd(micro_usd: int) -> str:
    return f"${micro_usd / 1_000_000:.3f}"


# ── Options and results ─────────────────────────────────────────────────

@dataclass(frozen=True)
class SyncOptions:
    """``max_pages=None`` is unbounded (``--all``). ``mode='incremental'``
    stops after the first non-empty page whose posts were all already known
    bookmarks; ``'full'`` walks to the end of the list. ``resume`` continues
    the newest unfinished run for this account and collection instead of
    starting a new one."""

    max_pages: int | None = DEFAULT_MAX_PAGES
    mode: SyncMode = "incremental"
    resume: bool = False
    collection: str = COLLECTION_BOOKMARKS


@dataclass
class SyncReport:
    run_ulid: str | None
    status: SyncStatus | Literal["not_started"]
    resumed: bool = False
    pages: int = 0
    posts_seen: int = 0
    posts_new: int = 0
    problems: int = 0
    revisions_new: int = 0
    resources: int = 0
    cost_micro_usd: int = 0
    stop_reason: str | None = None
    resume_after: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    cap_reached: bool = False
    policy: PolicyApplyReport | None = None  # the re-apply pass run before the sync


class NothingToResume(RuntimeError):
    """``--resume`` found no unfinished run (or it may not resume yet)."""


@dataclass
class PageResult:
    posts: int = 0
    new_posts: int = 0
    known_posts: int = 0
    revisions_new: int = 0
    links: int = 0
    resources: int = 0
    problems: int = 0


# ── Row helpers ─────────────────────────────────────────────────────────

_domain = domain_of
_visible = visible_flag
_post_locator = post_locator


def _handle(post: XPost | None) -> str | None:
    return post.author.username if post is not None and post.author else None


def post_title(post: XPost, text: str) -> str | None:
    """``@handle: <first ~80 characters>`` so handles are keyword-searchable."""
    return post_title_for(_handle(post), text)


def _valid_x_id(value: str | None) -> bool:
    return bool(value) and value.isdigit() and not value.startswith("0") and len(value) <= 20


def _replace_short_links(
    text: str, links: tuple[XLink, ...], hidden: Callable[[XLink], bool] | None = None
) -> str:
    """Replace each t.co link with its entity's sanitized canonical locator
    (left as-is when sanitization rejected every candidate), or with
    ``[hidden link]`` when domain policy hides the destination: a hidden
    locator must not reach the stored text, FTS or a model."""
    for link in links:
        if not link.short_url or not link.locator:
            continue
        if hidden is not None and hidden(link):
            text = text.replace(link.short_url, HIDDEN_LINK)
        elif link.locator != link.short_url:
            text = text.replace(link.short_url, link.locator)
    return text


def link_locator(link: XLink) -> str | None:
    """The locator the link's child row stores (GitHub roots canonicalized)."""
    if link.locator is None:
        return None
    item_id = link_item_id(link.locator)
    if item_id.startswith("github:repo:"):
        return "https://github.com/" + item_id.removeprefix("github:repo:")
    return link.locator


def post_text(post: XPost, policy: DomainPolicy | None = None) -> str:
    """The text recorded for a post: note_tweet full text when present,
    t.co links replaced, normalized with muninn-text/1."""
    hidden = None
    if policy is not None:
        def hidden(link: XLink) -> bool:
            return policy_hides(policy, link.locator) or policy_hides(policy, link_locator(link))
    return normalize_text(_replace_short_links(post.text or "", post.links, hidden))


def _post_metadata(post: XPost) -> dict[str, Any]:
    """Structured post metadata (never the text)."""
    meta: dict[str, Any] = {"kind": "x_post"}
    if post.author is not None:
        author: dict[str, Any] = {"id": post.author.id}
        if post.author.username:
            author["username"] = post.author.username
        if post.author.name:
            author["name"] = post.author.name
        meta["author"] = author
    elif post.author_id:
        meta["author"] = {"id": post.author_id}
    else:
        meta["author"] = None
    meta["published_at"] = post.published_at
    if post.lang:
        meta["lang"] = post.lang
    if post.conversation_id:
        meta["conversation_id"] = post.conversation_id
    meta["is_long_post"] = post.has_note_tweet
    meta["media_keys"] = list(post.media_keys)
    if post.possibly_sensitive is not None:
        meta["possibly_sensitive"] = post.possibly_sensitive
    if post.referenced:
        meta["referenced"] = [{"type": kind, "id": pid} for kind, pid in post.referenced]
    if post.article is not None:
        meta["article"] = {
            "id": _article_id(post),
            "title": post.article.title,
            "has_body": bool(post.article.body),
        }
    return meta


def _article_id(post: XPost) -> str | None:
    if post.article is not None and _valid_x_id(post.article.id):
        return post.article.id
    for link in post.links:
        if link.kind == "x_article" and _valid_x_id(link.x_id):
            return link.x_id
    return None


def _load_meta(raw: str | None) -> dict[str, Any]:
    try:
        meta = json.loads(raw) if raw else {}
    except ValueError:
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _dump(meta: dict[str, Any]) -> str:
    return json.dumps(meta, sort_keys=True, ensure_ascii=False)


# ── Writer ──────────────────────────────────────────────────────────────

class XPageWriter:
    """Writes parsed pages for one sync run. Call inside a transaction."""

    def __init__(self, conn: sqlite3.Connection, *, run_ulid: str, policy: DomainPolicy) -> None:
        self.conn = conn
        self.run_ulid = run_ulid
        self.policy = policy

    # ── posts ────────────────────────────────────────────────────

    def _find(self, source: str, source_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT bookmark_id, captured_at, title, url, source_metadata FROM bookmarks "
            "WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()

    def _insert(
        self,
        *,
        source: str,
        source_id: str,
        captured_at: int,
        title: str | None,
        url: str | None,
        meta: dict[str, Any],
        redacted: tuple[str, ...] = (),
        path_redacted: bool = False,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO bookmarks (
                source, source_id, captured_at, title, url, domain, source_metadata,
                redacted_param_count, redacted_param_names, path_redacted, content_visible
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source, source_id, captured_at, title, url, _domain(url), _dump(meta),
                len(redacted), json.dumps(list(redacted)) if redacted else None,
                1 if path_redacted else 0, _visible(self.policy, url),
            ),
        )
        return int(cur.lastrowid)

    def ensure_post(
        self,
        post_id: str,
        *,
        observed_at: int,
        bookmarked: bool,
        post: XPost | None = None,
        url: str | None = None,
        list_position: int | None = None,
    ) -> tuple[int, bool]:
        """Upsert the ``source='x'`` row. Returns ``(bookmark_id, newly_bookmarked)``.

        A first sighting as a bookmark sets ``captured_at`` (the bookmark
        observation time, contract §4.3). A post first written as a
        reference (quoted, replied to, linked) is promoted when it is later
        bookmarked; a known bookmark only gets ``last_observed_at``.

        The stored locator is always the canonical one
        (``x.com/<handle>/status/<id>``), never the URL form a link used:
        ``url`` only lends its handle while the API has not named one. A
        hidden post's title is ``@handle`` only.
        """
        row = self._find(SOURCE_X_POST, post_id)
        handle = _handle(post)
        if row is None:
            meta = _post_metadata(post) if post is not None else {"kind": "x_post"}
            meta["bookmarked"] = bookmarked
            if bookmarked:
                meta.update(
                    first_sync_run_id=self.run_ulid,
                    last_sync_run_id=self.run_ulid,
                    last_observed_at=observed_at,
                )
                if list_position is not None:
                    meta["list_position"] = list_position
            locator = _post_locator(post_id, handle or handle_from_locator(url))
            if not _visible(self.policy, locator):
                title = hidden_post_title(handle)
            else:
                title = post_title(post, post_text(post, self.policy)) if post is not None else None
            return (
                self._insert(
                    source=SOURCE_X_POST, source_id=post_id, captured_at=observed_at,
                    title=title, url=locator, meta=meta,
                ),
                bookmarked,
            )

        bookmark_id = int(row["bookmark_id"])
        meta = _load_meta(row["source_metadata"])
        before = _dump(meta)
        captured_at = int(row["captured_at"])
        title, locator = row["title"], row["url"]
        if post is not None and "published_at" not in meta:
            # Written earlier as a bare reference; fill in what the API now gives.
            for key, value in _post_metadata(post).items():
                meta.setdefault(key, value)
        # The API's handle wins (renames included); else keep the handle the
        # stored locator already names. Either way only the canonical form.
        locator = (
            _post_locator(post_id, handle) if handle
            else canonical_post_locator(post_id, meta, locator)
        )
        if not _visible(self.policy, locator):
            title = hidden_post_title(handle or meta_handle(meta))
        elif post is not None:
            # Recompute on every sighting: a policy change may now hide a
            # locator the old title quoted.
            title = post_title(post, self._masked_text(bookmark_id, post))
        newly = False
        if bookmarked:
            if meta.get("bookmarked", True) is False:
                newly = True
                captured_at = observed_at
                meta.update(bookmarked=True, first_sync_run_id=self.run_ulid)
                if list_position is not None:
                    meta["list_position"] = list_position
            meta.setdefault("bookmarked", True)
            meta["last_observed_at"] = observed_at
            meta["last_sync_run_id"] = self.run_ulid
        visible = _visible(self.policy, locator)
        if _dump(meta) != before or (title, locator, captured_at) != (
            row["title"], row["url"], int(row["captured_at"])
        ):
            # A changed locator changes visibility in the same UPDATE.
            self.conn.execute(
                "UPDATE bookmarks SET source_metadata = ?, captured_at = ?, title = ?, url = ?, "
                "domain = ?, content_visible = ? WHERE bookmark_id = ?",
                (_dump(meta), captured_at, title, locator, _domain(locator), visible, bookmark_id),
            )
        else:
            self._apply_policy(bookmark_id, locator)
        return bookmark_id, newly

    def _apply_policy(self, bookmark_id: int, url: str | None) -> None:
        """Recompute visibility as `muninn policy apply` does: a linked
        page whose scrape ended on a hidden locator stays hidden."""
        visible = row_visible_flag(self.conn, self.policy, bookmark_id, url)
        self.conn.execute(
            "UPDATE bookmarks SET content_visible = ? WHERE bookmark_id = ? AND content_visible <> ?",
            (visible, bookmark_id, visible),
        )

    def _observed_at(self, bookmark_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT captured_at, source, source_metadata FROM bookmarks WHERE bookmark_id = ?",
            (bookmark_id,),
        ).fetchone()
        if row is None or row["source"] != SOURCE_X_POST:
            return None
        if _load_meta(row["source_metadata"]).get("bookmarked", True) is False:
            return None
        return int(row["captured_at"])

    # ── content ──────────────────────────────────────────────────

    def write_content(
        self,
        bookmark_id: int,
        item_ref: str,
        *,
        text: str | None,
        coverage: str,
        retrieved_at: int,
        published_at: int | None,
        extractor: tuple[str, str],
        scrape_status: str = "ok",
        final_url: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        """Current text → ``scrape_results(pass='live')``; distinct text →
        a new ``scrape_history`` revision. Returns True if a revision was added."""
        has_content = coverage in ("full", "partial") and bool(text)
        if not has_content and coverage in ("full", "partial"):
            coverage = "empty"
        content_text = text if has_content else None
        quality = ("ok" if coverage == "full" else "partial") if has_content else None
        if not has_content and scrape_status == "ok":
            scrape_status = "partial"

        current = self.conn.execute(
            "SELECT scrape_status, content_text, extraction_quality, error_detail "
            "FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
            (bookmark_id,),
        ).fetchone()
        unchanged = current is not None and (
            current["scrape_status"], current["content_text"],
            current["extraction_quality"], current["error_detail"],
        ) == (scrape_status, content_text, quality, error_detail)
        if not unchanged:
            self.conn.execute(
                """
                INSERT INTO scrape_results (
                    bookmark_id, pass, fetched_at, final_url, http_status, scrape_status,
                    extraction_quality, content_text, content_html, raw_html_path, error_detail
                ) VALUES (?, 'live', ?, ?, NULL, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT (bookmark_id, pass) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    target_timestamp = NULL,
                    actual_snapshot_at = NULL,
                    archive_url = NULL,
                    final_url = excluded.final_url,
                    http_status = NULL,
                    scrape_status = excluded.scrape_status,
                    extraction_quality = excluded.extraction_quality,
                    content_text = excluded.content_text,
                    content_html = NULL,
                    raw_html_path = NULL,
                    error_detail = excluded.error_detail
                """,
                (bookmark_id, retrieved_at, final_url, scrape_status, quality, content_text,
                 error_detail),
            )
        source = "live_fallback" if has_content else "none"
        self.conn.execute(
            "UPDATE bookmarks SET enrichment_source = ? WHERE bookmark_id = ? "
            "AND enrichment_source IS NOT ?",
            (source, bookmark_id, source),
        )
        added = record_revision(
            self.conn,
            bookmark_id=bookmark_id,
            item_ref=item_ref,
            text=content_text,
            coverage=coverage,  # type: ignore[arg-type]
            retrieval_method="x_api",
            extractor_name=extractor[0],
            extractor_version=extractor[1],
            retrieved_at=retrieved_at,
            published_at=published_at,
            observed_at=self._observed_at(bookmark_id),
        )
        return added is not None

    def _hidden(self, bookmark_id: int) -> bool:
        row = self.conn.execute(
            "SELECT content_visible FROM bookmarks WHERE bookmark_id = ?", (bookmark_id,)
        ).fetchone()
        return row is not None and row["content_visible"] == 0

    def _masked_text(self, bookmark_id: int, post: XPost) -> str:
        """:func:`post_text`, then every hidden linked item's locator masked
        (the same masks ``muninn policy apply`` uses), so a link whose own
        locator escapes the policy but whose item is hidden (a
        ``twitter.com/<Handle>/status/<id>`` form, say) is masked too."""
        return mask_text(post_text(post, self.policy), hidden_child_patterns(self.conn, bookmark_id))

    def write_post_content(self, bookmark_id: int, post: XPost, retrieved_at: int) -> bool:
        """Write the post's text (call after :meth:`write_links`, so its
        linked items exist) and the title derived from it."""
        if self._hidden(bookmark_id):
            return False  # hidden by domain policy: like `muninn scrape`, store no content
        text = self._masked_text(bookmark_id, post)
        row = self.conn.execute(
            "SELECT url, title FROM bookmarks WHERE bookmark_id = ?", (bookmark_id,)
        ).fetchone()
        title = post_title(post, text)
        if row is not None and title != row["title"]:
            self.conn.execute(
                "UPDATE bookmarks SET title = ? WHERE bookmark_id = ?", (title, bookmark_id)
            )
        return self.write_content(
            bookmark_id,
            x_post_item_id(post.post_id),
            text=text,
            coverage="full",
            retrieved_at=retrieved_at,
            published_at=post.published_at,
            extractor=X_TEXT_EXTRACTOR,
            final_url=row["url"] if row else None,
        )

    # ── links ────────────────────────────────────────────────────

    def link(
        self,
        *,
        parent_id: int,
        parent_ref: str,
        child_id: int,
        child_ref: str,
        relation: str,
        position: int,
        resolution: str,
        entity_status: int | None,
        seen_at: int,
    ) -> None:
        if parent_ref == child_ref:
            return
        self.conn.execute(
            """
            INSERT INTO item_links (
                parent_bookmark_id, child_bookmark_id, parent_ref, child_ref, relation,
                position, locator_resolution, entity_status, first_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (parent_ref, relation, position) DO NOTHING
            """,
            (parent_id, child_id, parent_ref, child_ref, relation, position, resolution,
             entity_status, seen_at),
        )

    def ensure_article(
        self, article_id: str, *, seen_at: int, post: XPost | None, article: XArticle | None
    ) -> int:
        row = self._find(SOURCE_X_ARTICLE, article_id)
        if row is None:
            meta: dict[str, Any] = {"kind": "x_article"}
            if post is not None:
                post_meta = _post_metadata(post)
                meta["author"] = post_meta["author"]
                meta["published_at"] = post.published_at
                if post.lang:
                    meta["lang"] = post.lang
                meta["via_post_id"] = post.post_id
            if article is not None:
                meta["has_body"] = bool(article.body)
            url = sanitize_url(f"https://x.com/i/article/{article_id}").sanitized_url
            # A hidden article keeps no title: it is the article's own text.
            title = article.title if article is not None and _visible(self.policy, url) else None
            return self._insert(
                source=SOURCE_X_ARTICLE, source_id=article_id, captured_at=seen_at,
                title=title, url=url, meta=meta,
            )
        bookmark_id = int(row["bookmark_id"])
        if not _visible(self.policy, row["url"]):
            if row["title"] is not None:
                self.conn.execute("UPDATE bookmarks SET title = NULL WHERE bookmark_id = ?", (bookmark_id,))
        elif article is not None and article.title and not row["title"]:
            self.conn.execute(
                "UPDATE bookmarks SET title = ? WHERE bookmark_id = ?", (article.title, bookmark_id)
            )
        self._apply_policy(bookmark_id, row["url"])
        return bookmark_id

    def ensure_link_item(self, link: XLink, *, seen_at: int) -> tuple[int, str]:
        """``x_link`` row for a web page or GitHub repository locator."""
        assert link.locator is not None
        item_id = link_item_id(link.locator)
        # One row per repository: the canonical locator of a GitHub repository
        # is its lowercase root, whatever case, ``.git`` or slash the link used.
        locator = link_locator(link) or link.locator
        meta: dict[str, Any] = {"item_id": item_id}
        if item_id.startswith("github:repo:"):
            owner, _, name = item_id.removeprefix("github:repo:").partition("/")
            meta.update(kind="github_repo", repository={"owner": owner, "name": name})
        else:
            meta["kind"] = "web_page"
        if link.userinfo_redacted:
            meta["userinfo_redacted"] = True
        source_id = locator_source_id(locator)  # equals the web: digest for web pages
        row = self._find(SOURCE_X_LINK, source_id)
        if row is not None:
            bookmark_id = int(row["bookmark_id"])
            self._apply_policy(bookmark_id, row["url"])
            # A hidden linked item keeps no title: it is the hidden page's
            # own (its <title>, as X reports it). Visible again, it gets
            # the title back from the link.
            if self._hidden(bookmark_id):
                if row["title"] is not None:
                    self.conn.execute(
                        "UPDATE bookmarks SET title = NULL WHERE bookmark_id = ?", (bookmark_id,)
                    )
            elif not row["title"]:
                title = link.title or link_fallback_title(meta)
                if title:
                    self.conn.execute(
                        "UPDATE bookmarks SET title = ? WHERE bookmark_id = ?", (title, bookmark_id)
                    )
            return bookmark_id, item_id
        bookmark_id = self._insert(
            source=SOURCE_X_LINK, source_id=source_id, captured_at=seen_at,
            title=(link.title or link_fallback_title(meta)) if _visible(self.policy, locator) else None,
            url=locator, meta=meta, redacted=link.redacted_param_names,
            path_redacted=link.path_redacted,
        )
        return bookmark_id, item_id

    def write_links(
        self, parent_id: int, parent_ref: str, post: XPost, *, seen_at: int, parent_post_id: str
    ) -> int:
        written = 0
        for link in post.links:
            if link.kind == "x_media" or link.locator is None:
                continue  # media is metadata (has_media); unsanitizable links have no item
            resolution = {"unwound_url": "unwound", "expanded_url": "expanded", "url": "short"}[
                link.locator_source or "url"
            ]
            if link.kind == "x_post" and _valid_x_id(link.x_id):
                if link.x_id == parent_post_id:
                    continue
                child_id, _ = self.ensure_post(
                    link.x_id, observed_at=seen_at, bookmarked=False, url=link.locator
                )
                child_ref = x_post_item_id(link.x_id)
            elif link.kind == "x_article" and _valid_x_id(link.x_id):
                article = post.article if _article_id(post) == link.x_id else None
                child_id = self.ensure_article(link.x_id, seen_at=seen_at, post=post, article=article)
                child_ref = x_article_item_id(link.x_id)
            else:
                child_id, child_ref = self.ensure_link_item(link, seen_at=seen_at)
            self.link(
                parent_id=parent_id, parent_ref=parent_ref, child_id=child_id, child_ref=child_ref,
                relation="links_to", position=link.position, resolution=resolution,
                entity_status=link.status, seen_at=seen_at,
            )
            written += 1
        return written

    # ── one page ─────────────────────────────────────────────────

    def write_page(self, page: BookmarkPage) -> PageResult:
        seen = page.observed_at
        result = PageResult(posts=len(page.posts), problems=len(page.problems))
        referenced: set[str] = set()
        for post in page.posts:
            ref = x_post_item_id(post.post_id)
            known = self._find(SOURCE_X_POST, post.post_id)
            was_bookmark = known is not None and (
                _load_meta(known["source_metadata"]).get("bookmarked", True) is not False
            )
            bookmark_id, _ = self.ensure_post(
                post.post_id, observed_at=seen, bookmarked=True, post=post,
                list_position=post.list_position,
            )
            if was_bookmark:
                result.known_posts += 1
            else:  # a new row, or a referenced post seen as a bookmark for the first time
                result.new_posts += 1
            # Links first: the text masks the locators of linked items that are hidden.
            result.links += self.write_links(
                bookmark_id, ref, post, seen_at=seen, parent_post_id=post.post_id
            )
            result.revisions_new += self.write_post_content(bookmark_id, post, seen)

            quoted = {q.post_id: q for q in post.quoted_posts}
            for kind, pid in post.referenced:
                relation = {"quoted": "quotes", "replied_to": "replies_to"}.get(kind)
                if relation is None or not _valid_x_id(pid) or pid == post.post_id:
                    continue  # retweets are not modelled (contract §14)
                referenced.add(pid)
                child_id, _ = self.ensure_post(
                    pid, observed_at=seen, bookmarked=False, post=quoted.get(pid)
                )
                if pid in quoted:
                    q = quoted[pid]
                    result.links += self.write_links(
                        child_id, x_post_item_id(pid), q, seen_at=seen, parent_post_id=pid
                    )
                    result.revisions_new += self.write_post_content(child_id, q, seen)
                self.link(
                    parent_id=bookmark_id, parent_ref=ref, child_id=child_id,
                    child_ref=x_post_item_id(pid), relation=relation, position=0,
                    resolution="api_reference", entity_status=None, seen_at=seen,
                )
                result.links += 1

            article_id = _article_id(post)
            if post.article is not None and article_id is not None:
                art_id = self.ensure_article(article_id, seen_at=seen, post=post, article=post.article)
                art_ref = x_article_item_id(article_id)
                self.link(
                    parent_id=bookmark_id, parent_ref=ref, child_id=art_id, child_ref=art_ref,
                    relation="article", position=0, resolution="api_reference",
                    entity_status=None, seen_at=seen,
                )
                result.links += 1
                body = normalize_text(post.article.body)
                preview = normalize_text(post.article.preview_text)
                if (body or preview) and not self._hidden(art_id):
                    result.revisions_new += self.write_content(
                        art_id, art_ref,
                        text=body or preview,
                        coverage="full" if body else "partial",
                        scrape_status="ok" if body else "partial",
                        retrieved_at=seen, published_at=post.published_at,
                        extractor=X_ARTICLE_EXTRACTOR,
                        error_detail=None if body else "article body not returned; preview only",
                    )

        for problem in page.problems:
            result.revisions_new += self.write_problem(problem, seen, referenced)
        result.resources = page_resources(page)
        return result

    # ── errors[] ─────────────────────────────────────────────────

    _PROBLEM_COVERAGE = {
        "resource-not-found": ("deleted", "failed"),
        "not-authorized-for-resource": ("blocked", "auth_required"),
        "resource-unavailable": ("blocked", "auth_required"),
    }

    def write_problem(self, problem: XItemProblem, seen: int, referenced: set[str]) -> bool:
        """A per-post problem → a ``deleted``/``blocked`` revision. Problems on
        users (suspended authors) or of other kinds are only counted."""
        mapped = self._PROBLEM_COVERAGE.get(problem.kind or "")
        pid = problem.resource_id
        if mapped is None or problem.resource_type not in ("tweet", "post") or not _valid_x_id(pid):
            return False
        coverage, status = mapped
        bookmarked = problem.section != "includes" and pid not in referenced
        bookmark_id, _ = self.ensure_post(pid, observed_at=seen, bookmarked=bookmarked)
        return self.write_content(
            bookmark_id, x_post_item_id(pid), text=None, coverage=coverage, scrape_status=status,
            retrieved_at=seen, published_at=None, extractor=X_TEXT_EXTRACTOR,
            error_detail=problem.kind,
        )


# ── Sync runs ───────────────────────────────────────────────────────────

def page_resources(page: BookmarkPage) -> int:
    """Resources a bookmarks page is estimated to bill: its posts plus the
    expanded posts and users (whether X bills expansions is open)."""
    return len(page.posts) + page.included_post_count + page.included_user_count


def _usage_alone(
    conn: sqlite3.Connection,
    report: SyncReport,
    *,
    checkpoint_id: int | None,
    operation: str,
    resources: int,
    pricing: XPricing,
    occurred_at: int,
) -> None:
    """Record one billed request in its own transaction: for a request whose
    page write failed (and was rolled back) or a run that was refused after
    ``/users/me``. Never masks the caller's exception."""
    if conn.in_transaction:
        conn.rollback()
    try:
        with transaction(conn):
            report.cost_micro_usd += _usage(
                conn, checkpoint_id=checkpoint_id, operation=operation, resources=resources,
                pricing=pricing, occurred_at=occurred_at,
            )
        report.resources += resources
    except Exception:  # pragma: no cover - the DB itself is failing
        log.warning("could not record X usage for %s", operation, exc_info=True)


def _usage(
    conn: sqlite3.Connection,
    *,
    checkpoint_id: int,
    operation: str,
    resources: int,
    pricing: XPricing,
    occurred_at: int,
) -> int:
    cost = pricing.cost(resources)
    conn.execute(
        """
        INSERT INTO usage_records (
            usage_ulid, kind, provider, operation, sync_checkpoint_id, request_count,
            resource_count, cost_micro_usd, cost_basis, occurred_at
        ) VALUES (?, 'x_api', 'x', ?, ?, 1, ?, ?, 'estimated', ?)
        """,
        (new_ulid(occurred_at * 1000), operation, checkpoint_id, resources, cost, occurred_at),
    )
    return cost


def _check_resumable(rows: list[sqlite3.Row], now: int) -> None:
    """Raise :class:`NothingToResume` unless some unfinished run in ``rows``
    (newest first) may resume now."""
    if not rows:
        raise NothingToResume("no unfinished X sync run to resume")
    if all(r["resume_after"] is not None and r["resume_after"] > now for r in rows):
        newest = rows[0]
        raise NothingToResume(
            f"run {newest['run_ulid']} is rate-limited until epoch "
            f"{newest['resume_after']}; try again later"
        )


def find_resumable(
    conn: sqlite3.Connection, *, account_ref: str | None, collection: str = COLLECTION_BOOKMARKS
) -> sqlite3.Row | None:
    """Newest unfinished run (any status but ``completed``)."""
    sql = "SELECT * FROM sync_checkpoints WHERE source = 'x' AND collection = ? AND status <> 'completed'"
    params: list[Any] = [collection]
    if account_ref is not None:
        sql += " AND account_ref = ?"
        params.append(account_ref)
    return conn.execute(sql + " ORDER BY sync_checkpoint_id DESC LIMIT 1", params).fetchone()


def _config_json(client: XClient, options: SyncOptions, pricing: XPricing) -> dict[str, Any]:
    cfg = client.config
    return {
        "max_results": cfg.max_results,
        "post_fields_param": cfg.post_fields_param,
        "includes_posts_key": cfg.includes_posts_key,
        "bookmark_cap": cfg.bookmark_cap,
        "mode": options.mode,
        "max_pages": options.max_pages,
        "price_micro_usd_per_resource": pricing.micro_usd_per_resource,
    }


def sync_bookmarks(
    conn: sqlite3.Connection,
    client: XClient,
    *,
    options: SyncOptions = SyncOptions(),
    policy: DomainPolicy | None = None,
    pricing: XPricing = XPricing(),
    clock: Callable[[], float] = time.time,
    writer_factory: Callable[..., XPageWriter] = XPageWriter,
) -> SyncReport:
    """Run (or resume) one bookmark sync.

    Connector failures (auth, 403, transport, API errors) end the run with
    status ``failed`` and are reported, not raised; any other exception
    marks the run ``failed`` and propagates. The page being written when a
    failure hits is rolled back as a unit, so a resume refetches it.
    """
    if policy is None:
        policy = load_domain_policy()
    if options.max_pages is not None and options.max_pages < 1:
        raise ValueError("max_pages must be at least 1 (or None for no bound)")

    now = int(clock())
    # Contract §10.1: policy is re-applied on every sync, to every X row, not
    # only to the rows this (possibly early-stopping) run happens to see.
    policy_report = apply_policy(conn, policy, sources=X_SOURCES, now=now)
    if policy_report.changed:
        log.info("domain policy re-applied to stored X rows: %s", policy_report.summary())
    if options.resume:
        # Refuse before the billed /users/me call whenever the DB alone can tell.
        _check_resumable(
            conn.execute(
                "SELECT run_ulid, resume_after FROM sync_checkpoints WHERE source = 'x' "
                "AND collection = ? AND status <> 'completed' ORDER BY sync_checkpoint_id DESC",
                (options.collection,),
            ).fetchall(),
            now,
        )

    me = client.get_me()
    account = me.id
    resumed_row = None
    if options.resume:
        resumed_row = find_resumable(conn, account_ref=account, collection=options.collection)
        try:
            _check_resumable([resumed_row] if resumed_row is not None else [], now)
        except NothingToResume:
            _usage_alone(
                conn, SyncReport(run_ulid=None, status="not_started"),
                checkpoint_id=int(resumed_row["sync_checkpoint_id"]) if resumed_row else None,
                operation=OPERATION_ME, resources=1, pricing=pricing, occurred_at=now,
            )
            raise

    with transaction(conn):
        if resumed_row is not None:
            checkpoint_id = int(resumed_row["sync_checkpoint_id"])
            run_ulid = resumed_row["run_ulid"]
            config = _load_meta(resumed_row["config_json"])
            mode: SyncMode = config.get("mode", options.mode)
            conn.execute(
                "UPDATE sync_checkpoints SET status = 'running', resume_after = NULL, "
                "error_code = NULL, error_detail = NULL, updated_at = ? WHERE sync_checkpoint_id = ?",
                (now, checkpoint_id),
            )
            start = XCheckpoint(
                user_id=account,
                status="in_progress",
                next_token=resumed_row["next_token"],
                pages_fetched=int(resumed_row["pages_fetched"]),
                posts_seen=int(resumed_row["items_seen"]),
                started_at=int(resumed_row["started_at"]),
            )
        else:
            run_ulid = new_ulid(now * 1000)
            mode = options.mode
            cur = conn.execute(
                """
                INSERT INTO sync_checkpoints (
                    run_ulid, source, account_ref, collection, status, config_json,
                    started_at, updated_at
                ) VALUES (?, 'x', ?, ?, 'running', ?, ?, ?)
                """,
                (run_ulid, account, options.collection,
                 json.dumps(_config_json(client, options, pricing), sort_keys=True), now, now),
            )
            checkpoint_id = int(cur.lastrowid)
            start = XCheckpoint(user_id=account, status="in_progress", started_at=now)
        report = SyncReport(run_ulid=run_ulid, status="running", resumed=resumed_row is not None,
                            policy=policy_report)
        report.cost_micro_usd += _usage(
            conn, checkpoint_id=checkpoint_id, operation=OPERATION_ME, resources=1,
            pricing=pricing, occurred_at=now,
        )
        report.resources += 1

    writer = writer_factory(conn, run_ulid=run_ulid, policy=policy)
    sync = client.iter_bookmark_pages(start, max_pages=options.max_pages)
    stop_reason: str | None = None
    try:
        for page in sync:
            try:
                with transaction(conn):
                    res = writer.write_page(page)
                    report.cost_micro_usd += _usage(
                        conn, checkpoint_id=checkpoint_id, operation=OPERATION_BOOKMARKS,
                        resources=res.resources, pricing=pricing, occurred_at=page.observed_at,
                    )
                    caught_up = (
                        mode == "incremental" and res.posts > 0 and res.known_posts == res.posts
                    )
                    done = page.is_last or caught_up
                    conn.execute(
                        """
                        UPDATE sync_checkpoints SET
                            next_token = ?, pages_fetched = pages_fetched + 1,
                            items_seen = items_seen + ?, items_new = items_new + ?,
                            status = ?, completed_at = ?, updated_at = ?
                        WHERE sync_checkpoint_id = ?
                        """,
                        (
                            None if done else page.next_token, res.posts, res.new_posts,
                            "completed" if done else "running",
                            page.observed_at if done else None, page.observed_at, checkpoint_id,
                        ),
                    )
            except BaseException:
                # The page's rows were rolled back, but the request was billed.
                _usage_alone(
                    conn, report, checkpoint_id=checkpoint_id, operation=OPERATION_BOOKMARKS,
                    resources=page_resources(page), pricing=pricing, occurred_at=page.observed_at,
                )
                raise
            report.pages += 1
            report.posts_seen += res.posts
            report.posts_new += res.new_posts
            report.problems += res.problems
            report.revisions_new += res.revisions_new
            report.resources += res.resources
            report.cap_reached = report.cap_reached or page.checkpoint.cap_reached
            if caught_up and not page.is_last:
                stop_reason = "caught_up"
                break
            if page.is_last:
                stop_reason = "end_of_list"
    except XError as exc:
        cycled = getattr(exc, "page", None)
        if isinstance(cycled, BookmarkPage):  # fetched and billed, then refused
            _usage_alone(
                conn, report, checkpoint_id=checkpoint_id, operation=OPERATION_BOOKMARKS,
                resources=page_resources(cycled), pricing=pricing, occurred_at=cycled.observed_at,
            )
        _finish(conn, checkpoint_id, report, "failed", clock, stop_reason="error",
                error_code=exc.problem or type(exc).__name__, error_detail=str(exc))
        return report
    except BaseException as exc:
        _finish(conn, checkpoint_id, report, "failed", clock, stop_reason="error",
                error_code=type(exc).__name__, error_detail=str(exc)[:500])
        raise

    if stop_reason is not None:
        _finish(conn, checkpoint_id, report, "completed", clock, stop_reason=stop_reason)
    elif sync.outcome == "usage_capped":
        _finish(conn, checkpoint_id, report, "exhausted", clock, stop_reason="usage_capped",
                error_code="usage-capped", error_detail=str(sync.error) if sync.error else None)
    elif sync.outcome == "rate_limited":
        reset = getattr(sync.error, "reset_at", None)
        _finish(conn, checkpoint_id, report, "paused", clock, stop_reason="rate_limited",
                error_code="rate-limit-exceeded", resume_after=reset,
                error_detail=str(sync.error) if sync.error else None)
    elif sync.outcome == "page_budget":
        _finish(conn, checkpoint_id, report, "paused", clock, stop_reason="page_budget")
    else:  # "complete" without a page (resumed a finished cursor)
        _finish(conn, checkpoint_id, report, "completed", clock, stop_reason="end_of_list")
    return report


def _finish(
    conn: sqlite3.Connection,
    checkpoint_id: int,
    report: SyncReport,
    status: SyncStatus,
    clock: Callable[[], float],
    *,
    stop_reason: str,
    error_code: str | None = None,
    error_detail: str | None = None,
    resume_after: int | None = None,
) -> None:
    """Close the run's checkpoint row (its own transaction)."""
    if conn.in_transaction:
        conn.rollback()
    now = int(clock())
    row = conn.execute(
        "SELECT config_json, completed_at FROM sync_checkpoints WHERE sync_checkpoint_id = ?",
        (checkpoint_id,),
    ).fetchone()
    config = _load_meta(row["config_json"])
    config["stop_reason"] = stop_reason
    if report.cap_reached:
        config["cap_reached"] = True
    with transaction(conn):
        conn.execute(
            """
            UPDATE sync_checkpoints SET
                status = ?, completed_at = ?, updated_at = ?, error_code = ?,
                error_detail = ?, resume_after = ?, config_json = ?,
                next_token = CASE WHEN ? = 'completed' THEN NULL ELSE next_token END
            WHERE sync_checkpoint_id = ?
            """,
            (
                status,
                (row["completed_at"] or now) if status == "completed" else None,
                now, error_code, error_detail, resume_after,
                json.dumps(config, sort_keys=True), status, checkpoint_id,
            ),
        )
    log.info(
        "X sync %s: %s (%s), %d page(s), %d post(s), %d new",
        report.run_ulid, status, stop_reason, report.pages, report.posts_seen, report.posts_new,
    )
    report.status = status
    report.stop_reason = stop_reason
    report.error_code = error_code
    report.error_detail = error_detail
    report.resume_after = resume_after


__all__ = [
    "COLLECTION_BOOKMARKS",
    "DEFAULT_MAX_PAGES",
    "NothingToResume",
    "OPERATION_BOOKMARKS",
    "OPERATION_ME",
    "PageResult",
    "SyncOptions",
    "SyncReport",
    "XPageWriter",
    "XPricing",
    "X_ARTICLE_EXTRACTOR",
    "X_TEXT_EXTRACTOR",
    "find_resumable",
    "HIDDEN_LINK",
    "format_usd",
    "link_locator",
    "page_resources",
    "post_text",
    "post_title",
    "sync_bookmarks",
]
