"""The X slice end to end, through the real CLI commands.

`muninn ingest x` (respx-served connector fixtures) → `muninn scrape
--source x_link` (respx-served synthetic pages, empty Wayback CDX so no
archive fetch) → `muninn enrich` and `muninn enrich --task suitability`
(``anthropic.Anthropic`` replaced by a fake client, Qdrant by an
in-process instance) → `muninn export packets` → `muninn vault compile` →
the MCP search tools. Then a policy change hides one more link, and a
re-sync and recompile withdraw it from the vault and from search.

Privacy is checked across every output: the link domain policy hides at
ingest never reaches a vault page, a packet, the FTS index, a model call or
a search result, and no secret (OAuth tokens, client secret, pagination
cursors, a credential-shaped query value in a link or a redirect) appears
in the database dump, the vault, the packets or the command output.

No network (respx refuses unmocked requests), no model, no credentials.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
import respx
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from muninn.consumers.cli.main import cli
from muninn.consumers.mcp import tools
from muninn.consumers.packets import packet_integrity_errors
from muninn.ingest.x import JsonFileTokenStore, XTokens
from muninn.scrape.client import IA_CDX_BASE
from muninn.scrape.domain_policy import DomainPolicy
from muninn.scrape.rate_limiter import RateLimiter
from muninn.vector.qdrant import ensure_collection, get_point_payloads
from tests.integration.intake_fakes import judgement
from tests.integration.intake_fakes import response as model_response

qdrant_client = pytest.importorskip("qdrant_client")

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "x"
SCHEMA_DIR = ROOT / "schemas" / "json" / "intake"
API = "https://api.x.com/2"
BOOKMARKS = f"{API}/users/9000000001/bookmarks"
PAGES = {None: "bookmarks_page1", "synthetic_next_token_page2": "bookmarks_page2",
         "synthetic_next_token_page3": "bookmarks_page3"}

PLAIN_POST = "1111111111111111101"  # links the guide page (redirected below)
LONG_POST = "1111111111111111102"   # links the repository and the hidden blog post
DOCS_POST = "1111111111111111108"   # links the credential-carrying docs page
HIDDEN_HOST = "blog.example.org"
HIDDEN_URL = "https://blog.example.org/posts/write-up"
LATER_HIDDEN_HOST = "docs.example.com"
GUIDE_URL = "https://www.example.com/articles/synthetic-guide?id=42"
GUIDE_LANDING = "https://www.example.com/articles/synthetic-guide-v2"
REDIRECT_CANARY = "synthetic-redirect-CANARY-0003"
SECRETS = (
    "synthetic-access-token-0001",
    "synthetic-refresh-token-0001",
    "synthetic-client-secret-CANARY-0004",
    "synthetic_next_token",          # pagination cursors
    "synthetic-not-a-real-token",    # access_token value in a fixture link
    REDIRECT_CANARY,                 # access_token value in a redirect target
)


def fx(name: str) -> httpx.Response:
    meta = json.loads((FIX / f"{name}.headers.json").read_text())
    return httpx.Response(meta["status"], headers=meta["headers"],
                          json=json.loads((FIX / f"{name}.json").read_text()))


def page_html(url: str) -> str:
    """A synthetic article page; its words name the URL's path, so each
    page is findable by keyword."""
    words = " ".join(w for w in urlsplit(url).path.replace("-", " ").replace("/", " ").split() if w)
    sentence = f"Synthetic linked page about {words or 'nothing'} with the marlinspike method. "
    return f"<html><body><article><h1>{words}</h1><p>{sentence * 12}</p></article></body></html>"


def serve_page(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == GUIDE_URL:
        return httpx.Response(302, headers={
            "location": f"{GUIDE_LANDING}?id=42&access_token={REDIRECT_CANARY}&utm_source=x"})
    return httpx.Response(200, text=page_html(url), headers={"content-type": "text/html"})


class FakeAnthropic:
    """Stands in for ``anthropic.Anthropic()`` in both enrichment tasks:
    a per-bookmark call gets a summary built from its content, a
    suitability call a schema-valid judgement. Every call is recorded."""

    calls: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        FakeAnthropic.calls.append(kwargs)
        user = kwargs["messages"][0]["content"]
        if user.startswith("SUBJECT ITEM"):
            return model_response(judgement("candidate"), input_tokens=1200, output_tokens=150)
        content = user.split("Content:\n", 1)[1].strip()
        body = {
            "summary": "Summary: " + " ".join(content.split()[:12]),
            "tags": ["retrieval-evaluation"],
            "entities": ["Synthetic Entity"],
            "content_type": "article",
            "language": "en",
        }
        return model_response(body, input_tokens=300, output_tokens=60)


async def _no_wait(self, *args, **kwargs) -> None:
    return None


def invoke(env: SimpleNamespace, *args: str):
    result = CliRunner().invoke(cli, ["--db", str(env.db), *args])
    env.outputs.append(result.output)
    assert result.exit_code == 0, (args, result.output)
    return result


def _bid(conn: sqlite3.Connection, source: str, source_id: str) -> int:
    return conn.execute("SELECT bookmark_id FROM bookmarks WHERE source = ? AND source_id = ?",
                        (source, source_id)).fetchone()[0]


def _link_bid(conn: sqlite3.Connection, host: str) -> int:
    return conn.execute("SELECT bookmark_id FROM bookmarks WHERE source = 'x_link' AND domain = ?",
                        (host,)).fetchone()[0]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("x_e2e")
    env = SimpleNamespace(
        tmp=tmp, db=tmp / "data" / "muninn.db", vault=tmp / "vault", packets=tmp / "packets",
        outputs=[], policy=DomainPolicy(domain_patterns=(HIDDEN_HOST,)),
    )
    (tmp / "data").mkdir()
    (tmp / "personal").mkdir()
    token_file = tmp / "data" / "x-tokens.json"
    JsonFileTokenStore(token_file).save(XTokens(
        access_token=SECRETS[0], refresh_token=SECRETS[1], expires_at=4_000_000_000))
    qdrant = qdrant_client.QdrantClient(":memory:")
    ensure_collection(qdrant)
    env.qdrant = qdrant
    FakeAnthropic.calls = []

    with pytest.MonkeyPatch.context() as mp:
        for var, value in {
            "MUNINN_DATA_DIR": str(tmp / "data"),
            "MUNINN_RAW_DIR": str(tmp / "raw"),
            "MUNINN_DB_PATH": str(env.db),
            "MUNINN_X_CLIENT_ID": "synthetic-client-id",
            "MUNINN_X_CLIENT_SECRET": SECRETS[2],
            "MUNINN_X_TOKEN_FILE": str(token_file),
            "MUNINN_EMBEDDING_BACKEND": "hash",
        }.items():
            mp.setenv(var, value)
        for var in ("ANTHROPIC_API_KEY", "MUNINN_VAULT_DIR", "MUNINN_PERSONAL_VAULT_DIR",
                    "MUNINN_PACKETS_DIR", "MUNINN_INTEREST_PROFILE", "MUNINN_SUITABILITY_MODEL"):
            mp.delenv(var, raising=False)
        # Domain policy is read from the repo's domain_policy.yml by default.
        mp.setattr("muninn.ingest.x_store.load_domain_policy", lambda *a, **k: env.policy)
        mp.setattr(RateLimiter, "wait_for_domain", _no_wait)
        mp.setattr(RateLimiter, "wait_for_global", _no_wait)
        mp.setattr("anthropic.Anthropic", FakeAnthropic)
        mp.setattr("muninn.enrich.pipeline.get_client", lambda *a, **k: qdrant)

        from muninn.db import init_db

        init_db(env.db).close()

        with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{API}/users/me").mock(side_effect=lambda req: fx("users_me"))
            mock.get(BOOKMARKS).mock(
                side_effect=lambda req: fx(PAGES[req.url.params.get("pagination_token")]))
            mock.get(IA_CDX_BASE).mock(
                return_value=httpx.Response(200, json=[["urlkey", "timestamp", "original"]]))
            env.archive = mock.get(url__startswith="https://web.archive.org/web/")
            env.hidden_page = mock.get(HIDDEN_URL)
            env.pages = mock.route(method="GET").mock(side_effect=serve_page)

            invoke(env, "ingest", "x", "--all")
            invoke(env, "scrape", "--source", "x_link")
            invoke(env, "enrich")
            env.calls_enrich = len(FakeAnthropic.calls)
            invoke(env, "enrich", "--task", "suitability",
                   "--profile", str(ROOT / "interest_profile.example.yml"))
            invoke(env, "export", "packets", "--out", str(env.packets))
            invoke(env, "vault", "compile", "--vault-dir", str(env.vault),
                   "--personal-dir", str(tmp / "personal"))
            env.pages_before = sorted(p.name for p in (env.vault / "wiki" / "bookmarks").glob("*.md"))

            conn = sqlite3.connect(env.db)
            env.docs_link = _link_bid(conn, LATER_HIDDEN_HOST)
            conn.close()
            env.docs_hits_before = [r["bookmark_id"] for r in json.loads(
                tools.fts_search("share", db_path=env.db))]

            # A policy change hides the docs page: a full re-sync applies it,
            # the recompile withdraws its page.
            env.policy = DomainPolicy(domain_patterns=(HIDDEN_HOST, LATER_HIDDEN_HOST))
            invoke(env, "ingest", "x", "--all", "--full")
            env.recompile = invoke(env, "vault", "compile", "--vault-dir", str(env.vault),
                                   "--personal-dir", str(tmp / "personal"))
        yield env
    qdrant.close()


# ── helpers over the outputs ────────────────────────────────────────


def _conn(env) -> sqlite3.Connection:
    conn = sqlite3.connect(env.db)
    conn.row_factory = sqlite3.Row
    return conn


def _vault_pages(env) -> dict[str, str]:
    return {p.name: p.read_text() for p in (env.vault / "wiki" / "bookmarks").glob("*.md")}


def _packet_texts(env) -> dict[str, str]:
    return {str(p.relative_to(env.packets)): p.read_text() for p in sorted(env.packets.rglob("*.json"))}


def _model_text() -> str:
    return "\n".join(json.dumps(c, default=str) for c in FakeAnthropic.calls)


def _ids(results_json: str) -> list[int]:
    return [r["bookmark_id"] for r in json.loads(results_json)]


# ── the pipeline ran ────────────────────────────────────────────────


def test_ingest_and_scrape_wrote_the_x_rows(run) -> None:
    conn = _conn(run)
    counts = dict(conn.execute("SELECT source, COUNT(*) FROM bookmarks GROUP BY source").fetchall())
    assert counts == {"x": 11, "x_article": 1, "x_link": 7}
    assert "completed (end_of_list)" in run.outputs[0]
    # Every visible link was fetched live; the hidden one never; no archive fetch.
    live = conn.execute(
        "SELECT b.domain, sr.scrape_status FROM scrape_results sr JOIN bookmarks b USING (bookmark_id) "
        "WHERE b.source = 'x_link' AND sr.pass = 'live'").fetchall()
    assert len(live) == 6 and {r["scrape_status"] for r in live} == {"ok"}
    assert HIDDEN_HOST not in {r["domain"] for r in live}
    assert not run.hidden_page.called and not run.archive.called
    # X posts are not scraped: their live row is still the API text.
    methods = {r[0] for r in conn.execute(
        "SELECT DISTINCT sh.retrieval_method FROM scrape_history sh JOIN bookmarks b USING (bookmark_id) "
        "WHERE b.source = 'x'")}
    assert methods == {"x_api"}


def test_redirected_final_url_is_sanitized(run) -> None:
    conn = _conn(run)
    guide = conn.execute("SELECT bookmark_id FROM bookmarks WHERE url = ?", (GUIDE_URL,)).fetchone()[0]
    final = conn.execute("SELECT final_url FROM scrape_results WHERE bookmark_id = ? AND pass = 'live'",
                         (guide,)).fetchone()[0]
    assert final == f"{GUIDE_LANDING}?id=42"


def test_enrichment_and_suitability_ran(run) -> None:
    conn = _conn(run)
    post = _bid(conn, "x", PLAIN_POST)
    enriched = {r[0] for r in conn.execute("SELECT bookmark_id FROM enriched")}
    assert post in enriched and run.calls_enrich == len(enriched)
    assert post in get_point_payloads(run.qdrant)
    dispositions = dict(conn.execute(
        "SELECT item_ref, disposition FROM assessments").fetchall())
    assert dispositions[f"x:post:{PLAIN_POST}"] == "candidate"
    assert "Suitability complete:" in run.outputs[3]


def test_packets_validate_against_the_contract_schema(run) -> None:
    schemas = {p.name.removesuffix(".schema.json"): json.loads(p.read_text())
               for p in SCHEMA_DIR.glob("*.schema.json")}
    registry = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas.values())
    validator = Draft202012Validator(schemas["evidence-packet"], registry=registry)
    packets = _packet_texts(run)
    assert packets, run.outputs[4]
    subjects = set()
    for name, text in packets.items():
        packet = json.loads(text)
        errors = [e.message for e in validator.iter_errors(packet)]
        assert not errors, (name, errors)
        assert not packet_integrity_errors(packet), name
        subjects.add(json.dumps(packet, sort_keys=True))
    assert any(f"x:post:{PLAIN_POST}" in s for s in subjects)
    assert any(f"x:post:{LONG_POST}" in s for s in subjects)


def test_vault_has_the_x_pages(run) -> None:
    pages = _vault_pages(run)
    (plain,) = [t for n, t in pages.items() if n.endswith(f"-{PLAIN_POST}.md")]
    assert "item_kind: x_post" in plain and "source: x\n" in plain
    assert "> A plain synthetic post with a fully unwound link" in plain
    assert "assessment_disposition: candidate" in plain
    (long_post,) = [t for n, t in pages.items() if n.endswith(f"-{LONG_POST}.md")]
    assert "[hidden link]" in long_post and "github-example-org-example-repo" in long_post


def test_mcp_search_returns_the_x_post(run) -> None:
    conn = _conn(run)
    post = _bid(conn, "x", PLAIN_POST)
    hits = json.loads(tools.fts_search("unwound", db_path=run.db))
    (hit,) = [h for h in hits if h["bookmark_id"] == post]
    assert hit["source"] == "x" and hit["item_kind"] == "x_post"
    assert hit["title"].startswith("@example_author: A plain synthetic post")
    semantic = json.loads(tools.semantic_search("plain synthetic post", limit=50, db_path=run.db,
                                                client=run.qdrant))
    assert post in [h["bookmark_id"] for h in semantic] and all("score" in h for h in semantic)
    full = json.loads(tools.get_bookmark(post, db_path=run.db))
    assert full["item_kind"] == "x_post" and full["source"] == "x"


# ── privacy ─────────────────────────────────────────────────────────


def test_hidden_link_never_reaches_any_output(run) -> None:
    conn = _conn(run)
    hidden = _link_bid(conn, HIDDEN_HOST)
    assert conn.execute("SELECT content_visible FROM bookmarks WHERE bookmark_id = ?",
                        (hidden,)).fetchone()[0] == 0
    fts = "\n".join(" ".join(str(v) for v in r) for r in conn.execute("SELECT rowid, * FROM fts_bookmarks"))
    surfaces = {
        "vault": "\n".join(_vault_pages(run).values()),
        "packets": "\n".join(_packet_texts(run).values()),
        "fts": fts,
        "model calls": _model_text(),
        "stored post text": "\n".join(r[0] or "" for r in conn.execute(
            "SELECT content_text FROM scrape_results sr JOIN bookmarks b USING (bookmark_id) "
            "WHERE b.source = 'x'")),
    }
    for name, text in surfaces.items():
        assert HIDDEN_HOST not in text, name
        assert "posts/write-up" not in text, name
    assert hidden not in {r[0] for r in conn.execute("SELECT bookmark_id FROM enriched")}
    assert hidden not in get_point_payloads(run.qdrant)
    for query in ("write", "blog", "posts", "marlinspike"):
        assert hidden not in _ids(tools.fts_search(query, limit=50, db_path=run.db)), query
        assert hidden not in _ids(tools.semantic_search(query, limit=50, db_path=run.db,
                                                        client=run.qdrant)), query
    assert "error" in json.loads(tools.get_bookmark(hidden, db_path=run.db))


def test_secrets_appear_nowhere(run) -> None:
    conn = sqlite3.connect(run.db)
    dump = "\n".join(conn.iterdump())
    surfaces = {
        "db": dump,
        "vault": "\n".join(_vault_pages(run).values()),
        "packets": "\n".join(_packet_texts(run).values()),
        "model calls": _model_text(),
        "command output": "\n".join(run.outputs),
    }
    # The scrape caches too (the token file is the one place tokens belong).
    data = run.tmp / "data"
    for path in sorted(data.rglob("*.json")):
        if path.name != "x-tokens.json":
            surfaces[str(path.relative_to(data))] = path.read_text(errors="replace")
    assert any(name.startswith("http-cache") for name in surfaces)
    for name, text in surfaces.items():
        for secret in SECRETS:
            assert secret not in text, (name, secret)
        assert "access_token=" not in text, name


def test_policy_change_withdraws_the_docs_page(run) -> None:
    docs = run.docs_link
    assert docs in run.docs_hits_before  # findable before the policy change
    conn = _conn(run)
    assert conn.execute("SELECT content_visible FROM bookmarks WHERE bookmark_id = ?",
                        (docs,)).fetchone()[0] == 0
    removed = [n for n in run.pages_before if n not in _vault_pages(run)]
    assert len(removed) == 1 and removed[0].startswith("docs-example-com-share-")
    assert f"removed stale page wiki/bookmarks/{removed[0]}" in run.recompile.output
    assert LATER_HIDDEN_HOST not in "\n".join(_vault_pages(run).values())
    assert docs not in _ids(tools.fts_search("share", db_path=run.db))
    assert docs not in _ids(tools.semantic_search("share", limit=50, db_path=run.db, client=run.qdrant))
    # The re-sync rewrote the post's text with the link masked; its summary,
    # enriched before the change, is masked on the way out until the next
    # `muninn enrich` replaces it.
    (docs_page_post,) = [t for n, t in _vault_pages(run).items() if n.endswith(f"-{DOCS_POST}.md")]
    assert "> A link whose destination carries a credential-shaped param [hidden link]" in docs_page_post
    assert "Summary: A link whose destination carries a credential-shaped param [hidden link]" in docs_page_post
    assert 'credential-shaped param [hidden link]"\n' in docs_page_post  # the title's truncated locator
    post = _bid(conn, "x", DOCS_POST)
    results = json.loads(tools.fts_search("credential", db_path=run.db))
    results.append(json.loads(tools.get_bookmark(post, db_path=run.db)))
    assert any(r["bookmark_id"] == post for r in results[:-1])
    assert "docs.example" not in json.dumps(results)


def test_resync_retitles_a_post_whose_link_became_hidden(run) -> None:
    conn = _conn(run)
    title = conn.execute("SELECT title FROM bookmarks WHERE source = 'x' AND source_id = ?",
                         (DOCS_POST,)).fetchone()[0]
    assert "docs.example" not in title
