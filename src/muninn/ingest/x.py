"""X API v2 bookmarks connector: client and page-iterator stub (RI-02b).

Reads the authenticated user's bookmarks (``GET /2/users/:id/bookmarks``)
and bookmark folders with an OAuth 2.0 user-context token, and turns each
response page into placeholder records. It is a transport-and-parse layer
only:

- it never writes to the database: :mod:`muninn.ingest.x_store` maps its
  pages onto the stage-native tables, and ``muninn ingest x`` drives both;
- ``muninn.ingest`` does not import it, so the ingest entry point stays
  free of ``httpx`` (see ``tests/unit/test_import_isolation.py``).

Pagination: only a missing ``meta.next_token`` ends a run. A short page that
still carries a token is not the end. Progress is an :class:`XCheckpoint`,
JSON-serializable and safe to persist (it holds no credentials); resuming
from one continues at the first page the caller had not finished.

Failures (docs.x.com as read on 2026-10-21; RI-02a verifies them live):

- 200 with ``errors[]``: per-item :class:`XItemProblem` on the page, not
  exceptions.
- 401: one refresh with the stored refresh token, then retry. The rotated
  refresh token is handed back to the :class:`TokenStore`.
- 403: :class:`XForbidden` (e.g. ``client-forbidden``).
- 429 ``rate-limit-exceeded``: wait for ``x-rate-limit-reset`` via the
  injected clock/sleeper, within a budget; past it, :class:`XRateLimited`.
- 429 ``usage-capped``: :class:`XUsageCapped`, never retried. A sync run
  turns it into the terminal ``usage_capped`` status.

Secrets (access/refresh tokens, client secret, PKCE verifier, auth code)
never appear in logs, exception messages, reprs or checkpoints. Only
:meth:`XTokens.to_dict`, which a token store calls, emits them.

The record types (:class:`XPost`, :class:`XLink`, :class:`XAuthor`,
:class:`XArticle`, :class:`XMedia`) are **local placeholders pending
RI-03**, which owns the shared record fields. They carry what the API
gives; RI-04 maps them onto the stage-native tables.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from muninn.sanitize.url import sanitize_url

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.x.com/2"
DEFAULT_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
DEFAULT_TOKEN_URL = "https://api.x.com/2/oauth2/token"
DEFAULT_SCOPES = ("tweet.read", "users.read", "bookmark.read", "offline.access")

# Locator preference for a URL entity; the chosen one then goes through
# sanitize_url. A candidate that fails sanitization falls through to the next.
LOCATOR_ORDER = ("unwound_url", "expanded_url", "url")

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


# ── Configuration ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class XConnectorConfig:
    """Everything RI-02a may need to change after the live qualification.

    ``bookmark_cap`` is the historical "800 most recent bookmarks" ceiling.
    It is unverified, so it is a reporting threshold, not a stop: a run that
    reaches it sets ``XCheckpoint.cap_reached`` (older bookmarks may be
    invisible to the API). ``None`` disables the flag.

    ``post_fields_param`` / ``includes_posts_key`` cover the two documented
    spellings (``tweet.fields`` / ``includes.tweets`` versus
    ``post.fields`` / ``includes.posts``).
    """

    api_base: str = DEFAULT_API_BASE
    max_results: int = 100
    post_fields_param: str = "tweet.fields"
    includes_posts_key: str = "tweets"
    post_fields: tuple[str, ...] = (
        "article",
        "attachments",
        "author_id",
        "conversation_id",
        "created_at",
        "entities",
        "lang",
        "note_tweet",
        "possibly_sensitive",
        "referenced_tweets",
    )
    user_fields: tuple[str, ...] = ("name", "username", "protected")
    media_fields: tuple[str, ...] = ("media_key", "type", "url", "preview_image_url", "alt_text")
    expansions: tuple[str, ...] = (
        "author_id",
        "attachments.media_keys",
        "referenced_tweets.id",
        "referenced_tweets.id.author_id",
    )
    # Keys tried, in order, for an X Article's full text (unverified).
    article_body_keys: tuple[str, ...] = ("plain_text", "text")
    bookmark_cap: int | None = 800
    # Refresh proactively when the access token expires within this window.
    refresh_skew_seconds: float = 60.0
    # 429 rate-limit-exceeded: how many waits per request, and the longest
    # single wait; beyond either the request raises XRateLimited.
    max_rate_limit_waits: int = 3
    max_rate_limit_wait_seconds: float = 15 * 60 + 5
    rate_limit_fallback_wait_seconds: float = 60.0
    rate_limit_margin_seconds: float = 1.0
    timeout_seconds: float = 30.0
    # Upper bound on pages of a folder endpoint (folder list, folder posts).
    max_folder_pages: int = 100

    def __post_init__(self) -> None:
        if not 1 <= self.max_results <= 100:
            raise ValueError("max_results must be within 1..100")
        if self.max_folder_pages < 1:
            raise ValueError("max_folder_pages must be at least 1")
        if self.bookmark_cap is not None and self.bookmark_cap < 1:
            raise ValueError("bookmark_cap must be positive or None")

    def bookmark_params(self) -> dict[str, str]:
        return {
            "max_results": str(self.max_results),
            self.post_fields_param: ",".join(self.post_fields),
            "user.fields": ",".join(self.user_fields),
            "media.fields": ",".join(self.media_fields),
            "expansions": ",".join(self.expansions),
        }


# ── Errors ──────────────────────────────────────────────────────────────

class XError(Exception):
    """Base for connector failures. Messages are scrubbed of secrets."""

    def __init__(
        self, message: str, *, status: int | None = None, problem: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.problem = problem  # problem-type slug, e.g. "client-forbidden"


class XAuthError(XError):
    """No usable token: none stored, refresh failed, or 401 after refresh."""


class XForbidden(XError):
    """403. ``reason`` carries e.g. ``client-not-enrolled``."""

    def __init__(self, message: str, *, reason: str | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.reason = reason


class XRateLimited(XError):
    """429 ``rate-limit-exceeded`` past the wait budget. Retry after ``reset_at``."""

    def __init__(
        self,
        message: str,
        *,
        reset_at: int | None = None,
        limit: int | None = None,
        remaining: int | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.reset_at = reset_at
        self.limit = limit
        self.remaining = remaining


class XUsageCapped(XError):
    """429 ``usage-capped``: spend/usage exhausted until the next cycle or a top-up."""

    def __init__(
        self, message: str, *, period: str | None = None, scope: str | None = None, **kw: Any
    ) -> None:
        super().__init__(message, **kw)
        self.period = period
        self.scope = scope


class XApiError(XError):
    """Any other non-200, a malformed body, or a pagination token that didn't advance."""


class XPaginationCycle(XApiError):
    """A ``next_token`` this run already followed (or the one just used): the
    list would loop. ``page`` is the bookmarks page whose token repeated; it
    was fetched (and billed) but is not yielded, so the caller can still
    record its usage. None for the folder endpoints."""

    def __init__(self, message: str, *, page: "BookmarkPage | None" = None, **kw: Any) -> None:
        super().__init__(message, problem="pagination-cycle", **kw)
        self.page = page


class XTransportError(XError):
    """The request never produced a response (DNS, connect, timeout…)."""


def _scrub(text: str, secret_values: set[str | None]) -> str:
    for value in secret_values:
        if value:
            text = text.replace(value, "[redacted]")
    return text


def _problem_slug(type_uri: object) -> str | None:
    """``https://api.twitter.com/2/problems/usage-capped`` → ``usage-capped``.

    Matches on the trailing segment so the api.twitter.com / api.x.com host
    spelling doesn't matter.
    """
    if not isinstance(type_uri, str) or "/problems/" not in type_uri:
        return None
    return type_uri.rstrip("/").rsplit("/", 1)[-1] or None


# ── Tokens and token stores ─────────────────────────────────────────────

@dataclass(frozen=True, repr=False)
class XTokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None  # epoch seconds
    scope: str | None = None

    def __repr__(self) -> str:
        refresh = "<redacted>" if self.refresh_token else None
        return (
            f"XTokens(access_token=<redacted>, refresh_token={refresh}, "
            f"expires_at={self.expires_at!r}, scope={self.scope!r})"
        )

    __str__ = __repr__

    def expires_within(self, now: float, seconds: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at - seconds

    def to_dict(self) -> dict[str, Any]:
        """Serialized form **including secrets**, for token stores only."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> XTokens:
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=data.get("expires_at"),
            scope=data.get("scope"),
        )


class TokenStore(Protocol):
    """Where the client reads tokens and hands back refreshed (rotated) ones."""

    def load(self) -> XTokens | None: ...

    def save(self, tokens: XTokens) -> None: ...


class MemoryTokenStore:
    def __init__(self, tokens: XTokens | None = None) -> None:
        self.tokens = tokens
        self.saves = 0

    def load(self) -> XTokens | None:
        return self.tokens

    def save(self, tokens: XTokens) -> None:
        self.tokens = tokens
        self.saves += 1

    def __repr__(self) -> str:
        return f"MemoryTokenStore(has_tokens={self.tokens is not None})"


class JsonFileTokenStore:
    """Tokens as a 0600 JSON file, replaced atomically. Keep it outside any repo."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> XTokens | None:
        if not self.path.is_file():
            return None
        return XTokens.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, tokens: XTokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".x-tokens-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(tokens.to_dict(), fh)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def __repr__(self) -> str:
        return f"JsonFileTokenStore(path={str(self.path)!r})"


# ── OAuth 2.0 authorization code + PKCE ─────────────────────────────────

@dataclass(frozen=True, repr=False)
class OAuthClientConfig:
    """The developer app's OAuth 2.0 settings. ``client_secret`` is set only
    for a confidential client (it then authenticates with HTTP Basic)."""

    client_id: str
    redirect_uri: str
    client_secret: str | None = None
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    authorize_url: str = DEFAULT_AUTHORIZE_URL
    token_url: str = DEFAULT_TOKEN_URL

    def __repr__(self) -> str:
        secret = "<redacted>" if self.client_secret else None
        return (
            f"OAuthClientConfig(client_id={self.client_id!r}, "
            f"redirect_uri={self.redirect_uri!r}, client_secret={secret}, "
            f"scopes={self.scopes!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, repr=False)
class PKCEPair:
    verifier: str
    challenge: str
    method: str = "S256"

    def __repr__(self) -> str:
        return (
            f"PKCEPair(verifier=<redacted>, challenge={self.challenge!r}, method={self.method!r})"
        )


@dataclass(frozen=True, repr=False)
class AuthorizationRequest:
    """What :func:`start_authorization` returns: open ``url`` in a browser,
    then check ``state`` and pass ``pkce.verifier`` to :func:`exchange_code`."""

    url: str
    state: str
    pkce: PKCEPair

    def __repr__(self) -> str:
        return f"AuthorizationRequest(url={self.url!r}, state=<redacted>, pkce={self.pkce!r})"


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256: base64url(sha256(verifier)), unpadded."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pkce_pair() -> PKCEPair:
    # 64 random bytes → 86 unreserved characters (RFC 7636 allows 43..128).
    verifier = secrets.token_urlsafe(64)
    return PKCEPair(verifier=verifier, challenge=pkce_challenge(verifier))


def build_authorization_url(
    oauth: OAuthClientConfig, *, state: str, code_challenge: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": oauth.client_id,
            "redirect_uri": oauth.redirect_uri,
            "scope": " ".join(oauth.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{oauth.authorize_url}?{query}"


def start_authorization(oauth: OAuthClientConfig) -> AuthorizationRequest:
    pkce = new_pkce_pair()
    state = secrets.token_urlsafe(32)
    url = build_authorization_url(oauth, state=state, code_challenge=pkce.challenge)
    return AuthorizationRequest(url=url, state=state, pkce=pkce)


def exchange_code(
    oauth: OAuthClientConfig,
    *,
    code: str,
    code_verifier: str,
    http: httpx.Client | None = None,
    clock: Clock = time.time,
) -> XTokens:
    """Trade the authorization code from the redirect for tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": oauth.redirect_uri,
        "code_verifier": code_verifier,
    }
    return _token_request(oauth, data, http=http, clock=clock, secret_values={code, code_verifier})


def refresh_access_token(
    oauth: OAuthClientConfig,
    refresh_token: str,
    *,
    http: httpx.Client | None = None,
    clock: Clock = time.time,
) -> XTokens:
    """Use ``refresh_token`` once. If the response rotates it, the new one is
    returned; otherwise the old one is kept so the caller can persist either."""
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    return _token_request(
        oauth, data, http=http, clock=clock, secret_values={refresh_token},
        previous_refresh=refresh_token,
    )


def _token_request(
    oauth: OAuthClientConfig,
    data: dict[str, str],
    *,
    http: httpx.Client | None,
    clock: Clock,
    secret_values: set[str | None],
    previous_refresh: str | None = None,
) -> XTokens:
    secret_values = secret_values | {oauth.client_secret}
    auth: tuple[str, str] | None = None
    if oauth.client_secret:
        auth = (oauth.client_id, oauth.client_secret)
    else:
        data = {**data, "client_id": oauth.client_id}

    client = http or httpx.Client(timeout=30.0)
    try:
        resp = client.post(oauth.token_url, data=data, auth=auth)
    except httpx.HTTPError as exc:
        raise XTransportError(f"token endpoint: {type(exc).__name__}") from None
    finally:
        if http is None:
            client.close()

    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or not isinstance(body, dict) or "access_token" not in body:
        err = body.get("error") if isinstance(body, dict) else None
        desc = body.get("error_description") if isinstance(body, dict) else None
        msg = f"token endpoint returned {resp.status_code}: {err or 'no access_token'}"
        if desc:
            msg += f" ({desc})"
        raise XAuthError(_scrub(msg, secret_values), status=resp.status_code, problem=err)

    expires_in = body.get("expires_in")
    return XTokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token") or previous_refresh,
        expires_at=clock() + float(expires_in) if expires_in is not None else None,
        scope=body.get("scope"),
    )


# ── Placeholder records (pending RI-03) ─────────────────────────────────

LinkKind = Literal["web", "github_repo", "x_article", "x_post", "x_media"]
LocatorSource = Literal["unwound_url", "expanded_url", "url"]


@dataclass(frozen=True)
class XAuthor:
    """PLACEHOLDER pending RI-03."""

    id: str
    username: str | None = None
    name: str | None = None
    protected: bool | None = None


@dataclass(frozen=True)
class XArticle:
    """PLACEHOLDER pending RI-03. ``body`` is None when the API omits it.

    ``id`` is read from the article object when the API supplies one
    (unverified, RI-02a); otherwise the writer takes it from an
    ``x.com/i/article/<id>`` URL entity on the post."""

    title: str | None
    preview_text: str | None = None
    body: str | None = None
    id: str | None = None


@dataclass(frozen=True)
class XMedia:
    """PLACEHOLDER pending RI-03. ``url`` is sanitized."""

    media_key: str
    type: str | None = None
    url: str | None = None
    alt_text: str | None = None


@dataclass(frozen=True)
class XLink:
    """PLACEHOLDER pending RI-03: one URL entity of a post.

    Only the sanitized locator is kept; the raw candidates are not.
    ``status`` is X's unwinding status (e.g. 404), not ours.
    """

    position: int
    locator: str | None
    locator_source: LocatorSource | None
    kind: LinkKind
    status: int | None = None
    github_repo: str | None = None  # "owner/name" for kind == "github_repo"
    x_id: str | None = None  # article or post id for x_article / x_post
    media_key: str | None = None
    title: str | None = None
    redacted_param_names: tuple[str, ...] = ()
    path_redacted: bool = False
    userinfo_redacted: bool = False
    parse_error: str | None = None
    # The entity's t.co ``url`` exactly as it appears in the post text, kept
    # only so the writer can replace it with ``locator`` (contract §10). It
    # is a public short link, never a destination URL; None for non-t.co.
    short_url: str | None = None


@dataclass(frozen=True)
class XPost:
    """PLACEHOLDER pending RI-03: one post as the API returned it.

    Times are epoch seconds. ``published_at`` is the post's ``created_at``;
    ``observed_at`` is when this run saw it in the bookmark list (None for
    quoted posts, which were not bookmarked). ``text`` is the
    ``note_tweet`` full text when present.
    """

    post_id: str
    author_id: str | None
    author: XAuthor | None
    published_at: int | None
    text: str
    has_note_tweet: bool
    lang: str | None
    article: XArticle | None
    referenced: tuple[tuple[str, str], ...]  # (type, post id)
    quoted_post_ids: tuple[str, ...]
    quoted_posts: tuple[XPost, ...]  # resolved from includes, one level deep
    media_keys: tuple[str, ...]
    media: tuple[XMedia, ...]
    links: tuple[XLink, ...]
    observed_at: int | None
    list_position: int | None  # 0-based position across the whole run
    conversation_id: str | None = None
    possibly_sensitive: bool | None = None


@dataclass(frozen=True)
class XItemProblem:
    """One entry of a 200 response's ``errors[]``: a per-item problem
    (deleted, protected, suspended…), not a failed request."""

    kind: str | None  # problem slug: resource-not-found, not-authorized-for-resource, …
    title: str | None
    detail: str | None
    resource_type: str | None = None
    resource_id: str | None = None
    parameter: str | None = None
    section: str | None = None


@dataclass(frozen=True)
class XFolder:
    id: str
    name: str | None


@dataclass(frozen=True)
class RateLimitInfo:
    limit: int | None
    remaining: int | None
    reset_at: int | None  # epoch seconds

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> RateLimitInfo | None:
        def _int(name: str) -> int | None:
            try:
                return int(headers[name])
            except (KeyError, ValueError):
                return None

        info = cls(
            _int("x-rate-limit-limit"), _int("x-rate-limit-remaining"), _int("x-rate-limit-reset")
        )
        return None if info == cls(None, None, None) else info


# ── Checkpoint ──────────────────────────────────────────────────────────

CheckpointStatus = Literal["pending", "in_progress", "complete", "usage_capped", "rate_limited"]
CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class XCheckpoint:
    """Resumable sync state; holds no credentials (PLACEHOLDER for RI-03's
    sync-checkpoint table).

    ``next_token`` is the token of the next page to fetch (None before the
    first page). Persist the checkpoint after the page that produced it is
    durably handled; resuming then never repeats a finished page.
    """

    user_id: str
    status: CheckpointStatus = "pending"
    next_token: str | None = None
    pages_fetched: int = 0
    posts_seen: int = 0
    problems_seen: int = 0
    cap_reached: bool = False
    started_at: int | None = None
    updated_at: int | None = None
    resume_after: int | None = None  # epoch; set when status == "rate_limited"
    version: int = CHECKPOINT_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> XCheckpoint:
        data = json.loads(text)
        if data.get("version") != CHECKPOINT_VERSION:
            raise ValueError(f"unsupported checkpoint version: {data.get('version')!r}")
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown checkpoint keys: {sorted(unknown)}")
        return cls(**data)


# ── Parsing ─────────────────────────────────────────────────────────────

_X_HOSTS = frozenset(
    {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
)
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
# First path segments on github.com that are not repository owners.
_GITHUB_RESERVED = frozenset(
    {
        "about", "apps", "collections", "contact", "enterprise", "events", "explore",
        "features", "issues", "login", "marketplace", "new", "notifications", "orgs",
        "organizations", "pricing", "pulls", "search", "settings", "sponsors", "topics",
        "trending",
    }
)


@dataclass(frozen=True)
class BookmarkPage:
    """One bookmarks response, parsed. ``checkpoint`` is the state to persist
    once this page is handled."""

    index: int  # pages fetched before this one, across resumes
    request_token: str | None
    next_token: str | None
    result_count: int
    posts: tuple[XPost, ...]
    problems: tuple[XItemProblem, ...]
    observed_at: int
    rate_limit: RateLimitInfo | None
    checkpoint: XCheckpoint
    # Expansion counts, for usage estimates (whether X bills them is open).
    included_post_count: int = 0
    included_user_count: int = 0

    @property
    def is_last(self) -> bool:
        return self.next_token is None


def _parse_time(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _classify(locator: str) -> tuple[LinkKind, str | None, str | None]:
    """→ (kind, github_repo, x_id) from a sanitized locator."""
    parts = urlsplit(locator)
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    if host in _GITHUB_HOSTS and len(segs) >= 2 and segs[0].lower() not in _GITHUB_RESERVED:
        name = segs[1].removesuffix(".git")
        return "github_repo", f"{segs[0]}/{name}", None
    if host in _X_HOSTS:
        # /i/article/<id> and /<handle>/article/<id>
        if len(segs) >= 3 and segs[1] == "article":
            return "x_article", None, segs[2]
        # /<handle>/status/<id> and /i/web/status/<id>
        for i in range(len(segs) - 1):
            if segs[i] == "status" and segs[i + 1].isdigit():
                return "x_post", None, segs[i + 1]
    return "web", None, None


def _short_url(value: object) -> str | None:
    """The t.co link as written in the post text, or None if it isn't one."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if (parts.hostname or "").lower() != "t.co" or parts.query or parts.username or parts.password:
        return None
    return value


def parse_url_entity(entity: Mapping[str, Any], position: int) -> XLink:
    """Pick the locator (``unwound_url`` → ``expanded_url`` → ``url``) and sanitize it."""
    status = entity.get("status")
    media_key = entity.get("media_key")
    short_url = _short_url(entity.get("url"))
    first_error: str | None = None
    for source in LOCATOR_ORDER:
        candidate = entity.get(source)
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        result = sanitize_url(candidate)
        if result.sanitized_url is None:
            first_error = first_error or result.parse_error
            continue
        kind, repo, x_id = _classify(result.sanitized_url)
        if media_key:
            kind = "x_media"
        return XLink(
            position=position,
            locator=result.sanitized_url,
            locator_source=source,  # type: ignore[arg-type]
            kind=kind,
            status=status if isinstance(status, int) else None,
            github_repo=repo,
            x_id=x_id,
            media_key=media_key,
            title=entity.get("title"),
            redacted_param_names=tuple(result.redacted_param_names),
            path_redacted=result.path_redacted,
            userinfo_redacted=result.userinfo_redacted,
            short_url=short_url,
        )
    return XLink(
        position=position,
        locator=None,
        locator_source=None,
        kind="x_media" if media_key else "web",
        status=status if isinstance(status, int) else None,
        media_key=media_key,
        title=entity.get("title"),
        parse_error=first_error or "no_locator",
        short_url=short_url,
    )


@dataclass
class _Includes:
    users: dict[str, XAuthor]
    posts: dict[str, Mapping[str, Any]]
    media: dict[str, XMedia]


def _parse_includes(body: Mapping[str, Any], config: XConnectorConfig) -> _Includes:
    inc = body.get("includes") or {}
    users = {
        u["id"]: XAuthor(
            id=u["id"], username=u.get("username"), name=u.get("name"), protected=u.get("protected")
        )
        for u in inc.get("users") or []
        if u.get("id")
    }
    posts = {p["id"]: p for p in inc.get(config.includes_posts_key) or [] if p.get("id")}
    media = {}
    for m in inc.get("media") or []:
        key = m.get("media_key")
        if not key:
            continue
        raw_url = m.get("url") or m.get("preview_image_url")
        media[key] = XMedia(
            media_key=key,
            type=m.get("type"),
            url=sanitize_url(raw_url).sanitized_url if raw_url else None,
            alt_text=m.get("alt_text"),
        )
    return _Includes(users, posts, media)


def _parse_article(raw: object, config: XConnectorConfig) -> XArticle | None:
    if not isinstance(raw, Mapping):
        return None
    body = next(
        (raw[k] for k in config.article_body_keys if isinstance(raw.get(k), str) and raw[k]), None
    )
    raw_id = raw.get("id")
    article_id = str(raw_id) if isinstance(raw_id, (str, int)) and str(raw_id).isdigit() else None
    return XArticle(
        title=raw.get("title"), preview_text=raw.get("preview_text"), body=body, id=article_id
    )


def _parse_post(
    raw: Mapping[str, Any],
    inc: _Includes,
    config: XConnectorConfig,
    *,
    observed_at: int | None,
    position: int | None,
    depth: int = 0,
) -> XPost:
    note = raw.get("note_tweet") or {}
    text = note.get("text") or raw.get("text") or ""

    # The note_tweet entities annotate the full text; the top-level ones the
    # truncated text (and carry media links). Merge, first occurrence wins.
    entities: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    note_urls = (note.get("entities") or {}).get("urls") or []
    top_urls = (raw.get("entities") or {}).get("urls") or []
    for source in (note_urls, top_urls):
        for e in source:
            key = e.get("url") or e.get("expanded_url") or json.dumps(e, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            entities.append(e)

    referenced = tuple(
        (r.get("type") or "", r["id"]) for r in raw.get("referenced_tweets") or [] if r.get("id")
    )
    quoted_ids = tuple(pid for kind, pid in referenced if kind == "quoted")
    quoted_posts: tuple[XPost, ...] = ()
    if depth == 0:
        quoted_posts = tuple(
            _parse_post(inc.posts[q], inc, config, observed_at=None, position=None, depth=1)
            for q in quoted_ids
            if q in inc.posts
        )
    media_keys = tuple((raw.get("attachments") or {}).get("media_keys") or ())
    author_id = raw.get("author_id")
    return XPost(
        post_id=raw["id"],
        author_id=author_id,
        author=inc.users.get(author_id) if author_id else None,
        published_at=_parse_time(raw.get("created_at")),
        text=text,
        has_note_tweet=bool(note.get("text")),
        lang=raw.get("lang"),
        article=_parse_article(raw.get("article"), config),
        referenced=referenced,
        quoted_post_ids=quoted_ids,
        quoted_posts=quoted_posts,
        media_keys=media_keys,
        media=tuple(inc.media[k] for k in media_keys if k in inc.media),
        links=tuple(parse_url_entity(e, i) for i, e in enumerate(entities)),
        observed_at=observed_at,
        list_position=position,
        conversation_id=raw.get("conversation_id"),
        possibly_sensitive=(
            raw["possibly_sensitive"] if isinstance(raw.get("possibly_sensitive"), bool) else None
        ),
    )


def _parse_problems(body: Mapping[str, Any]) -> tuple[XItemProblem, ...]:
    return tuple(
        XItemProblem(
            kind=_problem_slug(e.get("type")),
            title=e.get("title"),
            detail=e.get("detail"),
            resource_type=e.get("resource_type"),
            resource_id=e.get("resource_id") or e.get("value"),
            parameter=e.get("parameter"),
            section=e.get("section"),
        )
        for e in body.get("errors") or []
        if isinstance(e, Mapping)
    )


def parse_bookmark_page(
    body: Mapping[str, Any],
    *,
    checkpoint: XCheckpoint,
    observed_at: int,
    request_token: str | None = None,
    rate_limit: RateLimitInfo | None = None,
    config: XConnectorConfig | None = None,
) -> BookmarkPage:
    """Parse one bookmarks response body and advance ``checkpoint`` past it.

    Pure: no I/O. Items without an ``id`` become ``malformed-item`` problems.
    """
    config = config or XConnectorConfig()
    inc = _parse_includes(body, config)
    problems = list(_parse_problems(body))
    posts: list[XPost] = []
    for raw in body.get("data") or []:
        if not isinstance(raw, Mapping) or not raw.get("id"):
            problems.append(
                XItemProblem("malformed-item", "Malformed item", "data[] entry without an id")
            )
            continue
        position = checkpoint.posts_seen + len(posts)
        posts.append(_parse_post(raw, inc, config, observed_at=observed_at, position=position))

    meta = body.get("meta") or {}
    next_token = meta.get("next_token") or None
    posts_seen = checkpoint.posts_seen + len(posts)
    after = replace(
        checkpoint,
        status="complete" if next_token is None else "in_progress",
        next_token=next_token,
        pages_fetched=checkpoint.pages_fetched + 1,
        posts_seen=posts_seen,
        problems_seen=checkpoint.problems_seen + len(problems),
        cap_reached=config.bookmark_cap is not None and posts_seen >= config.bookmark_cap,
        started_at=checkpoint.started_at if checkpoint.started_at is not None else observed_at,
        updated_at=observed_at,
        resume_after=None,
    )
    return BookmarkPage(
        index=checkpoint.pages_fetched,
        request_token=request_token,
        next_token=next_token,
        result_count=int(meta.get("result_count", len(posts))),
        posts=tuple(posts),
        problems=tuple(problems),
        observed_at=observed_at,
        rate_limit=rate_limit,
        checkpoint=after,
        included_post_count=len(inc.posts),
        included_user_count=len(inc.users),
    )


# ── Client ──────────────────────────────────────────────────────────────

SyncOutcome = Literal["complete", "usage_capped", "rate_limited", "page_budget"]


class XClient:
    """Synchronous X API v2 client for the bookmark endpoints.

    ``clock`` and ``sleep`` are injectable so tests never really wait.
    Pass ``http`` to share (or mock) an ``httpx.Client``; otherwise the
    client owns one.
    """

    def __init__(
        self,
        oauth: OAuthClientConfig,
        token_store: TokenStore,
        *,
        config: XConnectorConfig | None = None,
        http: httpx.Client | None = None,
        clock: Clock = time.time,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self.oauth = oauth
        self.token_store = token_store
        self.config = config or XConnectorConfig()
        self._http = http or httpx.Client(timeout=self.config.timeout_seconds)
        self._owns_http = http is None
        self._clock = clock
        self._sleep = sleep
        self._known_secrets: set[str | None] = {oauth.client_secret}

    def __repr__(self) -> str:
        return f"XClient(api_base={self.config.api_base!r}, client_id={self.oauth.client_id!r})"

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> XClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── endpoints ────────────────────────────────────────────────

    def get_me(self) -> XAuthor:
        body, _ = self._get("/users/me", {"user.fields": ",".join(self.config.user_fields)})
        data = body.get("data") or {}
        if not data.get("id"):
            raise XApiError("/users/me returned no user id")
        return XAuthor(
            id=data["id"], username=data.get("username"), name=data.get("name"),
            protected=data.get("protected"),
        )

    def fetch_bookmark_page(self, checkpoint: XCheckpoint) -> BookmarkPage:
        """Fetch the page ``checkpoint.next_token`` points at (the first page
        if None). Raises on any request failure, including usage-capped."""
        params = self.config.bookmark_params()
        token = checkpoint.next_token
        if token:
            params["pagination_token"] = token
        body, headers = self._get(f"/users/{checkpoint.user_id}/bookmarks", params)
        page = parse_bookmark_page(
            body,
            checkpoint=checkpoint,
            observed_at=int(self._clock()),
            request_token=token,
            rate_limit=RateLimitInfo.from_headers(headers),
            config=self.config,
        )
        if page.next_token is not None and page.next_token == token:
            raise XPaginationCycle("pagination token did not advance", page=page)
        log.debug(
            "X bookmarks page %d: %d posts, %d problems, more=%s",
            page.index, len(page.posts), len(page.problems), not page.is_last,
        )
        return page

    def iter_bookmark_pages(
        self, checkpoint: XCheckpoint, *, max_pages: int | None = None
    ) -> BookmarkSync:
        """A resumable sync from ``checkpoint`` (``XCheckpoint(user_id=…)`` to start)."""
        return BookmarkSync(self, checkpoint, max_pages=max_pages)

    def list_bookmark_folders(self, user_id: str) -> list[XFolder]:
        return [
            XFolder(id=item["id"], name=item.get("name"))
            for item in self._paginate(f"/users/{user_id}/bookmarks/folders", {})
        ]

    def folder_post_ids(self, user_id: str, folder_id: str) -> list[str]:
        """Post IDs in one folder. The endpoint returns IDs only (no fields)."""
        path = f"/users/{user_id}/bookmarks/folders/{folder_id}"
        return [item["id"] for item in self._paginate(path, {})]

    # ── transport ────────────────────────────────────────────────

    def _paginate(self, path: str, params: dict[str, str]) -> Iterator[Mapping[str, Any]]:
        """Items of a paginated folder endpoint. Stops with
        :class:`XPaginationCycle` on any repeated token and with
        :class:`XApiError` past ``config.max_folder_pages`` pages."""
        token: str | None = None
        seen: set[str] = set()
        pages = 0
        while True:
            if pages >= self.config.max_folder_pages:
                raise XApiError(
                    f"GET {path}: more than {self.config.max_folder_pages} pages; "
                    "stopping (raise max_folder_pages if the list is really that long)"
                )
            q = dict(params)
            if token:
                q["pagination_token"] = token
            body, _ = self._get(path, q)
            pages += 1
            for item in body.get("data") or []:
                if isinstance(item, Mapping) and item.get("id"):
                    yield item
            nxt = (body.get("meta") or {}).get("next_token") or None
            if nxt is None:
                return
            if nxt == token or nxt in seen:
                raise XPaginationCycle(f"GET {path}: pagination token repeated")
            seen.add(nxt)
            token = nxt

    def _tokens(self) -> XTokens:
        tokens = self.token_store.load()
        if tokens is None:
            raise XAuthError("no stored X tokens; run the OAuth 2.0 PKCE flow first")
        self._known_secrets |= {tokens.access_token, tokens.refresh_token}
        expiring = tokens.expires_within(self._clock(), self.config.refresh_skew_seconds)
        if tokens.refresh_token and expiring:
            log.info("X access token expires soon; refreshing")
            tokens = self._refresh(tokens)
        return tokens

    def _refresh(self, tokens: XTokens) -> XTokens:
        assert tokens.refresh_token
        new = refresh_access_token(
            self.oauth, tokens.refresh_token, http=self._http, clock=self._clock
        )
        self._known_secrets |= {new.access_token, new.refresh_token}
        self.token_store.save(new)
        rotated = new.refresh_token != tokens.refresh_token
        log.info("X access token refreshed (refresh token rotated: %s)", rotated)
        return new

    def _error(self, cls: type[XError], message: str, **kw: Any) -> XError:
        return cls(_scrub(message, self._known_secrets), **kw)

    def _get(self, path: str, params: Mapping[str, str]) -> tuple[dict[str, Any], httpx.Headers]:
        url = f"{self.config.api_base}{path}"
        tokens = self._tokens()
        refreshed = False
        waits = 0
        while True:
            try:
                resp = self._http.get(
                    url, params=params, headers={"Authorization": f"Bearer {tokens.access_token}"}
                )
            except httpx.HTTPError as exc:
                raise self._error(XTransportError, f"GET {path}: {type(exc).__name__}") from None

            status = resp.status_code
            try:
                body = resp.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                body = {}
            if status == 200:
                if not body:
                    raise self._error(
                        XApiError, f"GET {path}: 200 without a JSON object", status=200
                    )
                return body, resp.headers

            slug = _problem_slug(body.get("type"))
            summary = f"X API {status} on {path}: {body.get('title') or 'error'}"
            if body.get("detail"):
                summary += f" ({body['detail']})"

            if status == 401:
                if not refreshed and tokens.refresh_token:
                    log.info("X API 401 on %s; refreshing access token once", path)
                    tokens = self._refresh(tokens)
                    refreshed = True
                    continue
                raise self._error(XAuthError, summary, status=401, problem=slug)
            if status == 403:
                raise self._error(
                    XForbidden, summary, status=403, problem=slug, reason=body.get("reason")
                )
            if status == 429:
                if slug == "usage-capped" or body.get("title") == "UsageCapExceeded":
                    log.warning("X API usage cap reached on %s; stopping", path)
                    raise self._error(
                        XUsageCapped, summary, status=429, problem="usage-capped",
                        period=body.get("period"), scope=body.get("scope"),
                    )
                cfg = self.config
                info = RateLimitInfo.from_headers(resp.headers) or RateLimitInfo(None, None, None)
                if info.reset_at is not None:
                    wait = max(0.0, info.reset_at - self._clock()) + cfg.rate_limit_margin_seconds
                else:
                    wait = cfg.rate_limit_fallback_wait_seconds
                if waits < cfg.max_rate_limit_waits and wait <= cfg.max_rate_limit_wait_seconds:
                    log.warning("X API rate limit on %s; waiting %.0fs", path, wait)
                    self._sleep(wait)
                    waits += 1
                    continue
                raise self._error(
                    XRateLimited, summary, status=429, problem=slug or "rate-limit-exceeded",
                    reset_at=info.reset_at, limit=info.limit, remaining=info.remaining,
                )
            raise self._error(XApiError, summary, status=status, problem=slug)


class BookmarkSync:
    """Iterate bookmark pages from a checkpoint until there is no ``next_token``.

    After each yielded page, ``self.checkpoint`` is that page's checkpoint;
    persist it once the page is handled. Usage caps and rate limits past the
    wait budget end the iteration with ``outcome`` set and the checkpoint
    still pointing at the unfetched page; any other failure propagates and
    leaves ``checkpoint`` at the last yielded page.
    """

    def __init__(
        self, client: XClient, checkpoint: XCheckpoint, *, max_pages: int | None = None
    ) -> None:
        self.client = client
        self.checkpoint = checkpoint
        self.max_pages = max_pages
        self.outcome: SyncOutcome | None = None
        self.error: XError | None = None
        # Every token this run has requested or been handed: X returning any
        # of them again means the list loops, not just a token that stalls.
        self._seen_tokens: set[str] = {checkpoint.next_token} if checkpoint.next_token else set()

    def __iter__(self) -> Iterator[BookmarkPage]:
        if self.checkpoint.status == "complete":
            self.outcome = "complete"
            return
        fetched = 0
        while True:
            if self.max_pages is not None and fetched >= self.max_pages:
                self.outcome = "page_budget"
                return
            try:
                page = self.client.fetch_bookmark_page(self.checkpoint)
            except XUsageCapped as exc:
                self._stop("usage_capped", exc)
                return
            except XRateLimited as exc:
                self._stop("rate_limited", exc, resume_after=exc.reset_at)
                return
            if page.next_token is not None:
                if page.next_token in self._seen_tokens:
                    raise XPaginationCycle(
                        f"pagination token repeated after {page.index + 1} page(s): the "
                        "bookmark list loops; stopping", page=page,
                    )
                self._seen_tokens.add(page.next_token)
            self.checkpoint = page.checkpoint
            fetched += 1
            yield page
            if page.is_last:
                self.outcome = "complete"
                return

    def _stop(
        self,
        outcome: Literal["usage_capped", "rate_limited"],
        exc: XError,
        resume_after: int | None = None,
    ) -> None:
        self.outcome = outcome
        self.error = exc
        self.checkpoint = replace(
            self.checkpoint,
            status=outcome,
            updated_at=int(self.client._clock()),
            resume_after=resume_after,
        )


__all__ = [
    "BookmarkPage",
    "BookmarkSync",
    "CHECKPOINT_VERSION",
    "DEFAULT_SCOPES",
    "LOCATOR_ORDER",
    "AuthorizationRequest",
    "JsonFileTokenStore",
    "MemoryTokenStore",
    "OAuthClientConfig",
    "PKCEPair",
    "RateLimitInfo",
    "TokenStore",
    "XApiError",
    "XArticle",
    "XAuthError",
    "XAuthor",
    "XCheckpoint",
    "XClient",
    "XConnectorConfig",
    "XError",
    "XFolder",
    "XForbidden",
    "XItemProblem",
    "XLink",
    "XMedia",
    "XPaginationCycle",
    "XPost",
    "XRateLimited",
    "XTokens",
    "XTransportError",
    "XUsageCapped",
    "build_authorization_url",
    "exchange_code",
    "new_pkce_pair",
    "parse_bookmark_page",
    "parse_url_entity",
    "pkce_challenge",
    "refresh_access_token",
    "start_authorization",
]
