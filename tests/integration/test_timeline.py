"""Integration tests for the DuckDB-backed timeline builder."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from muninn.consumers.timeline.builder import build_timeline


def _seed(db_path: Path) -> None:
    now = int(time.time())
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO eras (era_label, narrative, start_date, end_date, "
            "  bookmark_count, dominant_topics) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("early-web", "early days", now - 10000, now - 5000, 2,
                 json.dumps(["web", "history"])),
                ("ai-era", "ai stuff", now - 4000, now, 1, json.dumps(["ai"])),
            ],
        )
        conn.executemany(
            "INSERT INTO bookmarks (source, source_id, captured_at, title, url, "
            "  era_label, domain, content_visible) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("netscape", "1", now - 9000, "A", "https://e.com/a",
                 "early-web", "e.com", 1),
                ("netscape", "2", now - 8000, "B", "https://e.com/b",
                 "early-web", "e.com", 1),
                ("netscape", "3", now - 3000, "C", "https://x.com/c",
                 "ai-era", "x.com", 1),
            ],
        )
        conn.executemany(
            "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, "
            "  scrape_status) VALUES (?, ?, ?, ?)",
            [
                (1, "live", now, "ok"),
                (2, "live", now, "failed"),
                (3, "live", now, "ok"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_timeline_structure(db_path: Path):
    _seed(db_path)
    payload = json.loads(build_timeline(db_path=db_path))
    assert "timeline" in payload
    assert len(payload["timeline"]) == 2


def test_timeline_counts_match_group_by(db_path: Path):
    _seed(db_path)
    payload = json.loads(build_timeline(db_path=db_path))
    counts = {e["era_label"]: e["bookmark_count"] for e in payload["timeline"]}
    assert counts == {"early-web": 2, "ai-era": 1}


def test_timeline_includes_era_metadata(db_path: Path):
    _seed(db_path)
    payload = json.loads(build_timeline(db_path=db_path))
    early = next(e for e in payload["timeline"] if e["era_label"] == "early-web")
    assert early["narrative"] == "early days"
    assert early["dominant_topics"] == ["web", "history"]


def test_timeline_writes_out(db_path: Path, tmp_path: Path):
    _seed(db_path)
    out = tmp_path / "timeline.json"
    payload = build_timeline(db_path=db_path, out_path=out)
    assert out.exists()
    assert json.loads(out.read_text()) == json.loads(payload)


def test_timeline_scrape_counts(db_path: Path):
    _seed(db_path)
    payload = json.loads(build_timeline(db_path=db_path))
    early = next(e for e in payload["timeline"] if e["era_label"] == "early-web")
    assert early["scraped_ok_count"] == 1
    assert early["scraped_failed_count"] == 1
