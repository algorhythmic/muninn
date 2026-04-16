"""Ingest adapters: raw source files → `bookmarks` rows.

v1 ships only the Netscape Bookmark HTML adapter (`bookmarks_html`). The
schema is source-agnostic — future adapters (Evernote, Apple Notes,
YouTube history, Spotify, …) drop in alongside, upserting via
`(source, source_id)`.
"""

from muninn.ingest.pipeline import IngestStats, ingest_html

__all__ = ["IngestStats", "ingest_html"]
