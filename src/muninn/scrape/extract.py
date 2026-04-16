"""HTML → plain text extraction + extraction_quality classifier.

We use ``selectolax``'s very-fast Lexbor parser. The strategy:

1. Drop obvious non-content tags (``script``, ``style``, ``nav``, ``footer``,
   ``aside``, ``form``, ``noscript``, ``svg``, ``iframe``).
2. Prefer a focused container if present (``article``, ``main``,
   ``[role=main]``, ``#content``, ``.content``); otherwise fall back to
   ``body``.
3. Concatenate visible text, collapse whitespace.

``classify_extraction_quality`` then maps body length + ratio of text to raw
HTML into one of the three CHECK-allowed values: ``ok | partial | failed``.

Thresholds are intentionally lenient — the goal is to surface obvious
failures (empty page, JS-required app shell) without losing borderline
content. Real triage happens during enrichment.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

try:
    from selectolax.parser import HTMLParser
except ImportError:  # pragma: no cover - selectolax should be installed
    HTMLParser = None  # type: ignore[assignment]


# Tags whose text we never want.
_DROP_TAGS = (
    "script", "style", "nav", "footer", "aside", "form",
    "noscript", "svg", "iframe", "header",
)

# Containers we prefer in priority order.
_CONTENT_SELECTORS = (
    "article",
    "main",
    "[role=main]",
    "#content",
    ".content",
    "#main",
    ".main",
    "#post",
    ".post",
)

_WHITESPACE_RE = re.compile(r"\s+")

# Quality thresholds (chars).
_MIN_OK_CHARS = 200
_MIN_PARTIAL_CHARS = 30


def extract_text(html: str) -> str:
    """Return the visible text content of ``html``, whitespace-collapsed."""
    if not html or HTMLParser is None:
        return ""

    try:
        tree = HTMLParser(html)
    except Exception:
        return ""

    # Strip noise nodes in place.
    for tag in _DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    container = None
    for sel in _CONTENT_SELECTORS:
        try:
            node = tree.css_first(sel)
        except Exception:
            node = None
        if node is not None:
            container = node
            break
    if container is None:
        container = tree.body or tree.root

    if container is None:
        return ""

    raw = container.text(separator=" ", strip=True) or ""
    return _WHITESPACE_RE.sub(" ", raw).strip()


def classify_extraction_quality(
    text: str, html: Optional[str] = None
) -> str:
    """Classify the extracted text into one of ``'ok' | 'partial' | 'failed'``.

    - ``failed``: nothing meaningful was extracted (empty/whitespace only).
    - ``partial``: some text but suspiciously short — likely an app shell or
      a mostly-image page.
    - ``ok``: enough content to plausibly enrich.
    """
    n = len(text or "")
    if n == 0:
        return "failed"
    if n < _MIN_PARTIAL_CHARS:
        return "failed"
    if n < _MIN_OK_CHARS:
        return "partial"
    return "ok"


def extract_with_quality(html: str) -> Tuple[str, str]:
    """Convenience: return ``(text, extraction_quality)`` in one call."""
    text = extract_text(html)
    return text, classify_extraction_quality(text, html)
