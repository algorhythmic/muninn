"""MCP recall over the whole vault: ``search``, ``get_page``, ``list_namespace``.

A synthetic tmp vault (both lanes) plus a canonical-schema DB. The tool
callables are tested directly, with a fake search backend where the
captured-lane ranking matters.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from muninn.consumers.mcp import tools, vault_fs
from muninn.search import RRF_K, SearchHit, SearchUnavailable

HIDDEN_URL = "https://intranet.example/private/page"


class _FakeBackend:
    name = "fake"

    def __init__(self, *, keyword=(), semantic=(), semantic_error=None):
        self._keyword = [SearchHit(b, 1.0, "fake-kw") for b in keyword]
        self._semantic = [SearchHit(b, 0.5, "fake-vec") for b in semantic]
        self._semantic_error = semantic_error
        self.calls: list[tuple[str, str, int]] = []

    def keyword(self, query, limit=10):
        self.calls.append(("keyword", query, limit))
        return self._keyword[:limit]

    def semantic(self, query, limit=10):
        self.calls.append(("semantic", query, limit))
        if self._semantic_error is not None:
            raise self._semantic_error
        return self._semantic[:limit]


def _seed_db(db_path: Path) -> None:
    now = int(time.time())
    conn = sqlite3.connect(str(db_path))
    try:
        rows = [
            (1, "Quokka rescue", "https://example.com/quokka", 1),
            (2, "Transformer paper", "https://example.org/attention", 1),
            (3, "Hidden thing", HIDDEN_URL, 0),
        ]
        for bid, title, url, visible in rows:
            conn.execute(
                "INSERT INTO bookmarks (bookmark_id, source, source_id, captured_at, title, url, "
                "  era_label, domain, content_visible, enrichment_source) "
                "VALUES (?, 'netscape', ?, ?, ?, ?, 'era-a', 'example.com', ?, 'at_capture')",
                (bid, f"s{bid}", now, title, url, visible),
            )
        conn.executemany(
            "INSERT INTO enriched (bookmark_id, summary, tags, enrichment_model, "
            "  enrichment_prompt_version, content_hash, enriched_at) VALUES (?, ?, '[]', 'm', 'v1', ?, ?)",
            [(1, "A synthetic summary about quokkas", "h1", now), (2, "Attention is all you need", "h2", now)],
        )
        # Bookmark 1 links to the hidden row 3 (as an X post links an article).
        conn.execute(
            "INSERT INTO item_links (parent_bookmark_id, child_bookmark_id, parent_ref, child_ref, "
            "  relation, position, first_seen_at) VALUES (1, 3, 'ref:1', 'ref:3', 'links_to', 0, ?)",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()


def _compiled(bid: int, title: str, body: str = "") -> str:
    return (
        f"---\npage_type: bookmark\nbookmark_id: {bid}\ntitle: {json.dumps(title)}\n"
        f"source: netscape\ntags: []\n---\n\n# {title}\n\n{body}"
    )


PAGES = {
    "bookmarks/quokka-rescue-1.md": _compiled(
        1,
        "Quokka rescue",
        f"Links to {HIDDEN_URL} and [[hidden-thing-3|Hidden thing]].\n\n"
        "## Related\n\n- [[transformer-paper-2]]\n- [[gone-page-9]]\n",
    ),
    "bookmarks/transformer-paper-2.md": _compiled(2, "Transformer paper", "quokka-free"),
    "bookmarks/hidden-thing-3.md": _compiled(3, "Hidden thing", "quokka secret"),
    "bookmarks/handwritten.md": "# Not compiled\n\nquokka notes in the wrong place\n",
    "concepts/marsupials.md": (
        "---\ntitle: Marsupials\nsummary: Pouched mammals, quokka included.\n---\n\n"
        "# Marsupials\n\nThe quokka is a marsupial. Quokka quokka.\n"
    ),
    "concepts/sub/consensus.md": "# Consensus\n\nAgreement among replicas.\n",
    "concepts/stale-copy.md": _compiled(3, "Hidden thing", "quokka copy"),
    "infra/qdrant.md": "---\ntitle: 'Qdrant host'\n---\n\nVector store on the homelab.\n",
    "people/ada.md": "# Ada\n\nWrote about quokka habitats.\n",
    "projects/muninn.md": (
        "# Muninn\n\n> Reference, not canonical — Odin owns commitments and decisions.\n\n"
        "Personal archive of saved material.\n"
    ),
}


@pytest.fixture
def vault(tmp_path: Path, db_path: Path, monkeypatch) -> Path:
    _seed_db(db_path)
    root = tmp_path / "vault"
    for rel, text in PAGES.items():
        file = root / "wiki" / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "diary.md").write_text("# Private\n\nquokka diary\n", encoding="utf-8")
    monkeypatch.setenv("MUNINN_VAULT_DIR", str(root))
    monkeypatch.setenv("MUNINN_PERSONAL_VAULT_DIR", str(personal))
    monkeypatch.setenv("MUNINN_DB_PATH", str(db_path))
    return root


def _search(db_path, query="quokka", namespace=None, top_k=10, **backend):
    fake = _FakeBackend(**backend)
    out = json.loads(tools.search(query, namespace, top_k, db_path=db_path, backend=fake))
    return out, fake


# ── search ──────────────────────────────────────────────────────────


def test_search_all_namespaces_interleaves_lanes(vault, db_path):
    out, fake = _search(db_path, keyword=[1], semantic=[2, 1])
    got = [(r["namespace"], r["path"]) for r in out["results"]]
    # captured lane: 1 (both lists) then 2; authored lane: marsupials
    # (title + body matches) then people/ada. Rank 1 of each lane first.
    assert got == [
        ("bookmarks", "wiki/bookmarks/quokka-rescue-1.md"),
        ("concepts", "wiki/concepts/marsupials.md"),
        ("bookmarks", "wiki/bookmarks/transformer-paper-2.md"),
        ("people", "wiki/people/ada.md"),
    ]
    first, page = out["results"][0], out["results"][1]
    assert first["bookmark_id"] == 1 and first["title"] == "Quokka rescue"
    assert first["summary"] == "A synthetic summary about quokkas"
    assert first["matched"] == ["fake-kw", "fake-vec"]
    assert first["score"] == pytest.approx(1 / (RRF_K + 1))
    assert page == {
        "namespace": "concepts",
        "path": "wiki/concepts/marsupials.md",
        "title": "Marsupials",
        "summary": "Pouched mammals, quokka included.",
        "matched": ["vault-scan"],
        "score": pytest.approx(1 / (RRF_K + 1)),
    }
    assert out["namespace"] is None and out["warnings"] == []
    assert [c[0] for c in fake.calls] == ["keyword", "semantic"]


def test_search_hybrid_order_within_captured_lane(vault, db_path):
    out, _ = _search(db_path, namespace="bookmarks", keyword=[1, 2], semantic=[2])
    assert [r["bookmark_id"] for r in out["results"]] == [2, 1]
    assert out["results"][0]["matched"] == ["fake-kw", "fake-vec"]


def test_search_semantic_unavailable_falls_back_to_keyword(vault, db_path):
    out, fake = _search(
        db_path, namespace="bookmarks", keyword=[2, 1], semantic_error=SearchUnavailable("down")
    )
    assert [r["bookmark_id"] for r in out["results"]] == [2, 1]
    assert all(r["matched"] == ["fake-kw"] for r in out["results"])
    assert [c[0] for c in fake.calls] == ["keyword", "semantic"]


def test_search_bookmarks_namespace_skips_vault_scan(vault, db_path):
    out, _ = _search(db_path, namespace="bookmarks", keyword=[1])
    assert {r["namespace"] for r in out["results"]} == {"bookmarks"}


@pytest.mark.parametrize(
    "namespace, expected",
    [
        ("concepts", ["wiki/concepts/marsupials.md"]),
        ("people", ["wiki/people/ada.md"]),
        ("infra", []),
    ],
)
def test_search_authored_namespace_only(vault, db_path, namespace, expected):
    out, fake = _search(db_path, namespace=namespace, keyword=[1], semantic=[2])
    assert [r["path"] for r in out["results"]] == expected
    assert fake.calls == []  # captured lane not queried


def test_search_scan_needs_every_term_and_reads_frontmatter(vault, db_path):
    out, _ = _search(db_path, query="vector homelab", namespace="infra")
    assert [(r["path"], r["title"]) for r in out["results"]] == [("wiki/infra/qdrant.md", "Qdrant host")]
    out, _ = _search(db_path, query="vector quokka", namespace="infra")
    assert out["results"] == []


def test_search_never_surfaces_hidden_rows_or_their_pages(vault, db_path):
    out, _ = _search(db_path, keyword=[3, 1, 424242], semantic=[3])
    text = json.dumps(out)
    assert [r.get("bookmark_id") for r in out["results"] if r["namespace"] == "bookmarks"] == [1]
    # The stale copy of the hidden row's page in concepts/ is withheld too,
    # and wiki/bookmarks/ is never scanned as files.
    assert "stale-copy" not in text and "hidden-thing" not in text
    assert "Hidden thing" not in text and "handwritten" not in text
    assert "diary" not in text  # the personal vault is never read


def test_search_respects_top_k(vault, db_path):
    out, _ = _search(db_path, top_k=1, keyword=[1, 2])
    assert len(out["results"]) == 1


def test_search_without_vault_returns_bookmarks_with_warning(vault, db_path, monkeypatch):
    monkeypatch.delenv("MUNINN_VAULT_DIR")
    out, _ = _search(db_path, keyword=[1])
    assert [(r["bookmark_id"], r["path"]) for r in out["results"]] == [(1, None)]
    assert any("MUNINN_VAULT_DIR is not set" in w for w in out["warnings"])
    err, _ = _search(db_path, namespace="concepts")
    assert err["code"] == "vault_unconfigured"


@pytest.mark.parametrize("namespace", ["sessions", "wiki", "../concepts", ""])
def test_search_unknown_namespace(vault, db_path, namespace):
    out, fake = _search(db_path, namespace=namespace, keyword=[1])
    assert out["code"] == "unknown_namespace"
    assert fake.calls == []


def test_search_empty_query(vault, db_path):
    out, _ = _search(db_path, query="  ")
    assert out["code"] == "invalid_query"


def test_search_scan_is_bounded(vault, db_path):
    result = vault_fs.scan_namespaces(
        vault / "wiki", vault_fs.AUTHORED_NAMESPACES, "quokka", visible=lambda _: True,
        max_files=1,
    )
    assert result.truncated


def test_search_reports_truncated_scan(vault, db_path, monkeypatch):
    real = vault_fs.scan_namespaces
    monkeypatch.setattr(
        vault_fs, "scan_namespaces", lambda *a, **kw: real(*a, **{**kw, "max_files": 1})
    )
    out, _ = _search(db_path, namespace="concepts")
    assert any("bound" in w for w in out["warnings"])


# ── get_page ────────────────────────────────────────────────────────


def _page(path, db_path):
    return json.loads(tools.get_page(path, db_path=db_path))


def test_get_page_authored(vault, db_path):
    out = _page("wiki/concepts/sub/consensus.md", db_path)
    assert out["namespace"] == "concepts"
    assert out["title"] == "Consensus"
    assert out["bookmark_id"] is None
    assert out["content"] == PAGES["concepts/sub/consensus.md"]
    assert out["size_bytes"] == len(out["content"].encode())


def test_get_page_compiled_masks_hidden_neighbours(vault, db_path):
    out = _page("wiki/bookmarks/quokka-rescue-1.md", db_path)
    assert out["bookmark_id"] == 1 and out["title"] == "Quokka rescue"
    content = out["content"]
    assert HIDDEN_URL not in content and "hidden-thing-3" not in content
    assert "[[transformer-paper-2]]" in content  # visible compiled page: kept
    assert "gone-page-9" not in content  # no such page: fails closed
    assert content.count("[hidden link]") == 3


def test_get_page_hidden_row_indistinguishable_from_missing(vault, db_path):
    hidden = _page("wiki/bookmarks/hidden-thing-3.md", db_path)
    missing = _page("wiki/bookmarks/never-existed.md", db_path)
    assert hidden["code"] == missing["code"] == "not_found"
    assert hidden["error"].replace("hidden-thing-3", "X") == missing["error"].replace(
        "never-existed", "X"
    )
    assert "secret" not in json.dumps(hidden)
    # The same page copied into an authored namespace is withheld too.
    assert _page("wiki/concepts/stale-copy.md", db_path)["code"] == "not_found"


def test_get_page_withholds_uncompiled_and_deleted_bookmark_pages(vault, db_path):
    assert _page("wiki/bookmarks/handwritten.md", db_path)["code"] == "not_found"
    (vault / "wiki/bookmarks/deleted-99.md").write_text(_compiled(99, "Deleted"), encoding="utf-8")
    assert _page("wiki/bookmarks/deleted-99.md", db_path)["code"] == "not_found"


def test_get_page_withholds_bookmark_pages_when_db_unavailable(vault, tmp_path):
    missing_db = tmp_path / "no-such.db"
    assert _page("wiki/bookmarks/quokka-rescue-1.md", missing_db)["code"] == "not_found"
    # Authored pages don't need the DB.
    assert _page("wiki/people/ada.md", missing_db)["title"] == "Ada"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "/wiki/concepts/marsupials.md",
        "~/wiki/concepts/marsupials.md",
        "C:/wiki/concepts/marsupials.md",
        "concepts/marsupials.md",
        "raw/notes.md",
        "wiki",
        "wiki/",
        "wiki/../wiki/concepts/marsupials.md",
        "wiki/concepts/../../outside.md",
        "wiki/concepts/./marsupials.md",
        "wiki//concepts/marsupials.md",
        "wiki/concepts\\marsupials.md",
        "wiki/.obsidian/workspace.md",
        "wiki/concepts/marsupials.txt",
        "wiki/concepts/marsupials.md.bak",
        "wiki/concepts/.md",
        "wiki/concepts/marsupials\x00.md",
        "wiki/" + "a" * 600 + ".md",
    ],
)
def test_get_page_rejects_unsafe_paths(vault, db_path, path):
    out = _page(path, db_path)
    assert out["code"] == "invalid_path", out
    assert str(vault) not in out["error"]  # no host paths in errors


def test_get_page_symlinks(vault, db_path, tmp_path):
    wiki = vault / "wiki"
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (wiki / "concepts/escape.md").symlink_to(outside)
    (wiki / "concepts/personal.md").symlink_to(tmp_path / "personal/diary.md")
    (wiki / "concepts/alias.md").symlink_to(wiki / "concepts/marsupials.md")
    (wiki / "concepts/text.md").symlink_to(wiki / "notes.txt")
    (wiki / "notes.txt").write_text("plain text\n", encoding="utf-8")
    (wiki / "concepts/to-hidden.md").symlink_to(wiki / "bookmarks/hidden-thing-3.md")
    (wiki / "linked").symlink_to(tmp_path, target_is_directory=True)

    assert _page("wiki/concepts/escape.md", db_path)["code"] == "invalid_path"
    assert _page("wiki/concepts/personal.md", db_path)["code"] == "invalid_path"
    assert _page("wiki/linked/outside.md", db_path)["code"] == "invalid_path"
    assert _page("wiki/concepts/text.md", db_path)["code"] == "not_found"
    assert _page("wiki/concepts/to-hidden.md", db_path)["code"] == "not_found"
    alias = _page("wiki/concepts/alias.md", db_path)
    assert alias["title"] == "Marsupials"  # inside wiki/: served


def test_get_page_wiki_symlinked_outside_vault_is_refused(tmp_path, db_path, monkeypatch):
    root = tmp_path / "v2"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "concepts").mkdir(parents=True)
    (elsewhere / "concepts/x.md").write_text("# X\n", encoding="utf-8")
    (root / "wiki").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setenv("MUNINN_VAULT_DIR", str(root))
    monkeypatch.delenv("MUNINN_PERSONAL_VAULT_DIR", raising=False)
    assert _page("wiki/concepts/x.md", db_path)["code"] == "vault_unconfigured"


def test_get_page_size_cap(vault, db_path):
    big = vault / "wiki/concepts/big.md"
    big.write_bytes(b"# Big\n" + b"x" * vault_fs.MAX_PAGE_BYTES)
    out = _page("wiki/concepts/big.md", db_path)
    assert out["code"] == "too_large"
    exact = vault / "wiki/concepts/exact.md"
    exact.write_bytes(b"x" * vault_fs.MAX_PAGE_BYTES)
    assert _page("wiki/concepts/exact.md", db_path)["size_bytes"] == vault_fs.MAX_PAGE_BYTES


def test_get_page_refuses_unset_vault(vault, db_path, monkeypatch):
    monkeypatch.delenv("MUNINN_VAULT_DIR")
    assert _page("wiki/concepts/marsupials.md", db_path)["code"] == "vault_unconfigured"


@pytest.mark.parametrize("relation", ["same", "personal-inside-vault", "vault-inside-personal"])
def test_get_page_refuses_vault_overlapping_personal(vault, db_path, monkeypatch, relation):
    if relation == "same":
        monkeypatch.setenv("MUNINN_PERSONAL_VAULT_DIR", str(vault))
    elif relation == "personal-inside-vault":
        (vault / "personal").mkdir()
        monkeypatch.setenv("MUNINN_PERSONAL_VAULT_DIR", str(vault / "personal"))
    else:
        monkeypatch.setenv("MUNINN_PERSONAL_VAULT_DIR", str(vault.parent))
    out = _page("wiki/concepts/marsupials.md", db_path)
    assert out["code"] == "vault_conflict"
    assert str(vault) not in out["error"]
    # search and list_namespace refuse the vault the same way.
    assert json.loads(tools.list_namespace("concepts", db_path=db_path))["code"] == "vault_conflict"
    out, _ = _search(db_path, namespace="concepts")
    assert out["code"] == "vault_conflict"


# ── list_namespace ──────────────────────────────────────────────────


def _list(namespace, db_path, **kw):
    return json.loads(tools.list_namespace(namespace, db_path=db_path, **kw))


def test_list_namespace_summaries(vault, db_path):
    out = _list("concepts", db_path)
    assert out == {
        "namespace": "concepts",
        "pages": [
            {
                "path": "wiki/concepts/marsupials.md",
                "title": "Marsupials",
                "summary": "Pouched mammals, quokka included.",
                "bookmark_id": None,
            },
            {
                "path": "wiki/concepts/sub/consensus.md",
                "title": "Consensus",
                "summary": "Agreement among replicas.",
                "bookmark_id": None,
            },
        ],
        "next_cursor": None,
    }
    project = _list("projects", db_path)["pages"][0]
    assert project["summary"] == "Personal archive of saved material."  # banner skipped
    assert _list("infra", db_path)["pages"][0]["title"] == "Qdrant host"


def test_list_namespace_bookmarks_privacy_and_sqlite_text(vault, db_path):
    out = _list("bookmarks", db_path)
    assert [(p["path"], p["bookmark_id"], p["summary"]) for p in out["pages"]] == [
        ("wiki/bookmarks/quokka-rescue-1.md", 1, "A synthetic summary about quokkas"),
        ("wiki/bookmarks/transformer-paper-2.md", 2, "Attention is all you need"),
    ]
    assert "hidden" not in json.dumps(out).lower()
    assert "handwritten" not in json.dumps(out)


def test_list_namespace_pagination(vault, db_path):
    wiki = vault / "wiki/people"
    for i in range(5):
        (wiki / f"p{i}.md").write_text(f"# Person {i}\n\nline {i}\n", encoding="utf-8")
    full = [p["path"] for p in _list("people", db_path, limit=100)["pages"]]
    assert len(full) == 6

    seen, cursor, calls = [], None, 0
    while True:
        out = _list("people", db_path, limit=2, cursor=cursor)
        calls += 1
        assert len(out["pages"]) <= 2
        seen += [p["path"] for p in out["pages"]]
        cursor = out["next_cursor"]
        if cursor is None:
            break
    assert seen == full and calls == 3


def test_list_namespace_skips_hidden_pages_without_short_pages(vault, db_path):
    """Hidden pages don't count towards ``limit``."""
    first = _list("bookmarks", db_path, limit=1)
    assert [p["bookmark_id"] for p in first["pages"]] == [1]
    second = _list("bookmarks", db_path, limit=1, cursor=first["next_cursor"])
    assert [p["bookmark_id"] for p in second["pages"]] == [2]
    assert second["next_cursor"] is None  # transformer-paper-2.md is the last file


def test_list_namespace_examination_bound(vault, db_path, monkeypatch):
    """A run of withheld pages ends a call early, with a cursor to resume."""
    monkeypatch.setattr(vault_fs, "LIST_MAX_EXAMINED", 2)
    first = _list("bookmarks", db_path, limit=5)  # handwritten, hidden-thing-3
    assert first["pages"] == [] and first["next_cursor"] == "wiki/bookmarks/hidden-thing-3.md"
    second = _list("bookmarks", db_path, limit=5, cursor=first["next_cursor"])
    assert [p["bookmark_id"] for p in second["pages"]] == [1, 2]
    assert second["next_cursor"] is None


def test_list_namespace_skips_symlinks_and_dotfiles(vault, db_path, tmp_path):
    wiki = vault / "wiki/concepts"
    (wiki / "alias.md").symlink_to(wiki / "marsupials.md")
    (wiki / ".hidden.md").write_text("# Hidden\n", encoding="utf-8")
    (wiki / "linked").symlink_to(tmp_path / "personal", target_is_directory=True)
    paths = [p["path"] for p in _list("concepts", db_path)["pages"]]
    assert paths == ["wiki/concepts/marsupials.md", "wiki/concepts/sub/consensus.md"]


def test_list_namespace_errors(vault, db_path, monkeypatch):
    assert _list("sessions", db_path)["code"] == "unknown_namespace"
    assert _list("concepts", db_path, cursor="wiki/people/a.md")["code"] == "invalid_cursor"
    assert _list("concepts", db_path, cursor="../x")["code"] == "invalid_cursor"
    os.rename(vault / "wiki/infra", vault / "wiki/infra-moved")
    assert _list("infra", db_path) == {"namespace": "infra", "pages": [], "next_cursor": None}
    monkeypatch.delenv("MUNINN_VAULT_DIR")
    assert _list("concepts", db_path)["code"] == "vault_unconfigured"


# ── the FastMCP wrappers ────────────────────────────────────────────


def test_server_wrappers_pass_through(vault, db_path):
    from muninn.consumers.mcp import server

    fake = _FakeBackend(keyword=[1])
    server.set_search_backend(fake)
    try:
        found = json.loads(server.search("quokka", "bookmarks", 3))
    finally:
        server.set_search_backend(None)
    assert [r["bookmark_id"] for r in found["results"]] == [1]
    # top_k 3, doubled for hidden-row headroom, then each list fetched
    # DEPTH_FACTOR deeper for fusion.
    assert fake.calls[0] == ("keyword", "quokka", 12)
    page = json.loads(server.get_page(found["results"][0]["path"]))
    assert page["bookmark_id"] == 1
    listing = json.loads(server.list_namespace("concepts", 1))
    assert len(listing["pages"]) == 1 and listing["next_cursor"]


def test_server_registers_new_tools():
    import asyncio

    from muninn.consumers.mcp import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "search", "get_page", "list_namespace",
        "semantic_search", "fts_search", "get_bookmark", "get_era", "list_eras",
    } <= names


# ── the X lane, through the real compiler ───────────────────────────


@pytest.fixture
def x_vault(tmp_path: Path, db_path: Path, monkeypatch):
    from muninn.consumers.vault.compiler import compile_vault
    from muninn.db import connect
    from tests import x_corpus as xc

    conn = connect(db_path)
    try:
        corpus = xc.seed_x_corpus(conn, hidden_at_ingest=False)
    finally:
        conn.close()
    root = tmp_path / "x-vault"
    compile_vault(db_path=db_path, output_dir=root, personal_dir=None)
    monkeypatch.setenv("MUNINN_VAULT_DIR", str(root))
    monkeypatch.delenv("MUNINN_PERSONAL_VAULT_DIR", raising=False)
    return corpus, root


def test_x_search_hits_carry_their_compiled_page(x_vault, db_path):
    from tests import x_corpus as xc

    corpus, _ = x_vault
    fake = _FakeBackend(keyword=[corpus.hidden, corpus.post, corpus.repo])
    out = json.loads(tools.search("retrieval", "bookmarks", db_path=db_path, backend=fake))
    got = [(r["bookmark_id"], r["path"], r["item_kind"]) for r in out["results"]]
    assert got == [
        (corpus.post, f"wiki/bookmarks/x-synth-author-a-{xc.POST_ID}.md", "x_post"),
        (corpus.repo, "wiki/bookmarks/github-synthetic-org-synthetic-repo.md", "github_repo"),
    ]
    page = json.loads(tools.get_page(got[0][1], db_path=db_path))
    assert page["bookmark_id"] == corpus.post
    assert xc.HIDDEN_URL not in page["content"]


def test_x_pages_hidden_after_compile_are_withheld_and_unlinked(x_vault, db_path):
    """A policy change hides the web page; the vault has not been recompiled."""
    from muninn.db import connect
    from tests import x_corpus as xc

    corpus, root = x_vault
    listing = json.loads(tools.list_namespace("bookmarks", db_path=db_path))
    (web_path,) = [p["path"] for p in listing["pages"] if p["bookmark_id"] == corpus.web]
    web_slug = Path(web_path).stem
    post_path = f"wiki/bookmarks/x-synth-author-a-{xc.POST_ID}.md"
    before = (root / post_path).read_text(encoding="utf-8")
    assert f"[[{web_slug}|" in before and xc.WEB_URL in before

    conn = connect(db_path)
    conn.execute("UPDATE bookmarks SET content_visible = 0 WHERE bookmark_id = ?", (corpus.web,))
    conn.commit()
    conn.close()

    assert (root / web_path).exists()  # still on disk until the next compile
    assert json.loads(tools.get_page(web_path, db_path=db_path))["code"] == "not_found"
    after = json.loads(tools.list_namespace("bookmarks", db_path=db_path))
    assert web_path not in [p["path"] for p in after["pages"]]
    post = json.loads(tools.get_page(post_path, db_path=db_path))["content"]
    assert web_slug not in post and xc.WEB_URL not in post
    assert "[[github-synthetic-org-synthetic-repo|" in post  # visible links kept
