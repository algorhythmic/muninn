"""Synthetic canonical writes exercise the durable registry and epoch triggers."""

import sqlite3

import pytest

from muninn.db import init_db
from muninn.index import registry as r


def bookmark(conn, n=1):
    conn.execute("INSERT INTO bookmarks(bookmark_id,source,source_id,captured_at,title) "
                 "VALUES (?,'synthetic',?,1,'title')", (n, str(n)))


def ready(conn, name, *, lease_token=None):
    r.allocate_generation(conn, name, ("captured",), lease_token=lease_token)
    r.mark_ready(conn, name, "private/manifest.json", "a" * 64, lease_token=lease_token)


def test_corpus_identity_stable_across_connections_and_derived_rebuild(tmp_path):
    path = tmp_path / "synthetic.db"
    conn = init_db(path)
    identity = r.ensure_corpus(conn)
    assert identity.startswith("muninn-corpus:")
    ready(conn, "generation1")
    conn.close()
    conn = init_db(path)
    assert r.ensure_corpus(conn) == identity
    assert r.get_generation(conn, "generation1").state == "ready"
    conn.close()


def test_epoch_transactional_and_noop_update_does_not_invalidate(fresh_db):
    conn = fresh_db
    before = r.source_epoch(conn)
    bookmark(conn)
    assert r.source_epoch(conn) > before
    conn.rollback()
    assert r.source_epoch(conn) == before
    bookmark(conn)
    conn.commit()
    epoch = r.source_epoch(conn)
    conn.execute("UPDATE bookmarks SET title=title")
    conn.commit()
    assert r.source_epoch(conn) == epoch
    conn.execute("UPDATE bookmarks SET content_visible=0")
    assert r.source_epoch(conn) > epoch
    conn.rollback()
    assert r.source_epoch(conn) == epoch
    conn.execute("DELETE FROM bookmarks")
    assert r.source_epoch(conn) > epoch


@pytest.mark.parametrize("update", [
    "UPDATE bookmarks SET title='changed'", "UPDATE bookmarks SET url='https://example.test'",
    "UPDATE bookmarks SET source_metadata='{}'", "UPDATE bookmarks SET captured_at=2",
    "UPDATE bookmarks SET enrichment_source='live_fallback'",
])
def test_bookmark_projection_mutations_invalidate(fresh_db, update):
    bookmark(fresh_db)
    fresh_db.commit()
    before = r.source_epoch(fresh_db)
    fresh_db.execute(update)
    assert r.source_epoch(fresh_db) > before


@pytest.mark.parametrize("insert,update,delete", [
    ("INSERT INTO scrape_results(bookmark_id,pass,fetched_at,scrape_status,content_text) VALUES(1,'live',1,'ok','body')",
     "UPDATE scrape_results SET content_text='changed'", "DELETE FROM scrape_results"),
    ("INSERT INTO enriched(bookmark_id,summary,enrichment_model,enrichment_prompt_version,content_hash,enriched_at) VALUES(1,'summary','fake','1','hash',1)",
     "UPDATE enriched SET summary='changed'", "DELETE FROM enriched"),
    ("INSERT INTO item_links(parent_bookmark_id,child_bookmark_id,parent_ref,child_ref,relation,position,first_seen_at) VALUES(1,2,'one','two','links_to',0,1)",
     "UPDATE item_links SET relation='quotes'", "DELETE FROM item_links"),
    ("INSERT INTO cross_references(source_bookmark_id,target_bookmark_id,relationship,created_by) VALUES(1,2,'related','synthetic')",
     "UPDATE cross_references SET relationship='changed'", "DELETE FROM cross_references"),
    ("INSERT INTO scrape_history(revision_ulid,bookmark_id,item_ref,content_sha256,normalization,char_count,content_text,coverage,retrieval_method,extractor_name,extractor_version,retrieved_at) VALUES('01JAAAAAAAAAAAAAAAAAAAAAAA',1,'one','" + "a" * 64 + "','v1',4,'body','full','live','fake','1',1)",
     "UPDATE scrape_history SET content_text=NULL,withdrawn_at=2,withdrawal_reason='policy_hidden'", "DELETE FROM scrape_history"),
])
def test_source_related_insert_update_delete_invalidate(fresh_db, insert, update, delete):
    conn = fresh_db
    bookmark(conn)
    bookmark(conn, 2)
    conn.commit()
    for statement in (insert, update, delete):
        before = r.source_epoch(conn)
        conn.execute(statement)
        assert r.source_epoch(conn) > before
        conn.commit()


def test_deep_pass_and_lint_provenance_invalidate(fresh_db):
    conn = fresh_db
    bookmark(conn)
    bookmark(conn, 2)
    conn.commit()
    before = r.source_epoch(conn)
    digest = 'sha256:' + 'a' * 64
    conn.execute("INSERT INTO deep_pass_results(task_id,attempt,bookmark_id,model,prompt_version,source_content_hash,materials_digest,material_bookmark_ids_json,output_json,output_digest,started_at,completed_at) "
                 "VALUES('synthetic',1,1,'fake','1',?,?,'[1,2]','{}',?,1,2)", (digest, digest, digest))
    assert r.source_epoch(conn) > before
    before = r.source_epoch(conn)
    conn.execute("INSERT INTO lint_pair_checks(bookmark_id_a,bookmark_id_b,content_hash_a,content_hash_b,model,prompt_version,task_id,checked_at) VALUES(1,2,?,?,'fake','1','synthetic',1)", (digest, digest))
    assert r.source_epoch(conn) > before
    before = r.source_epoch(conn)
    conn.execute("INSERT INTO lint_findings(relation,bookmark_id,other_bookmark_id,content_hash,other_content_hash,rationale,evidence_json,model,prompt_version,task_id,created_at) VALUES('contradicts',1,2,?,?,'synthetic','[]','fake','1','synthetic',1)", (digest, digest))
    assert r.source_epoch(conn) > before


def test_usage_status_and_request_flags_do_not_invalidate(fresh_db):
    conn = fresh_db
    bookmark(conn)
    conn.execute("INSERT INTO enriched(bookmark_id,enrichment_model,enrichment_prompt_version,content_hash,enriched_at) VALUES(1,'fake','1','hash',1)")
    conn.commit()
    before = r.source_epoch(conn)
    conn.execute("UPDATE enriched SET deep_pass_requested=1")
    conn.execute("UPDATE bookmarks SET ingested_at=ingested_at+1")
    conn.execute("INSERT INTO synthesis_runs(task_id,task_type,attempt,started_at,status) VALUES('synthetic','deep_pass',1,1,'running')")
    conn.execute("INSERT INTO usage_records(usage_ulid,kind,provider,operation,cost_micro_usd,cost_basis,occurred_at) VALUES('01JAAAAAAAAAAAAAAAAAAAAAAA','x_api','fake','fake',0,'estimated',1)")
    conn.commit()
    assert r.source_epoch(conn) == before


def test_stale_activation_never_overwrites_active(fresh_db):
    conn = fresh_db
    ready(conn, "first")
    epoch = r.source_epoch(conn)
    r.activate(conn, "first", epoch)
    ready(conn, "second")
    bookmark(conn)
    conn.commit()
    with pytest.raises(r.RegistryError, match="source_epoch_changed"):
        r.activate(conn, "second", epoch)
    assert r.active_generation(conn).generation_id == "first"
    with pytest.raises(r.RegistryError, match="source_epoch_changed"):
        r.activate(conn, "second", r.source_epoch(conn))
    assert r.active_generation(conn).generation_id == "first"


def test_state_machine_and_snapshot_binding(fresh_db):
    conn = fresh_db
    r.allocate_generation(conn, "staged", ("captured", "authored"))
    assert r.get_generation(conn, "staged").scope == ("authored", "captured")
    assert r.active_generation(conn) is None
    with pytest.raises(r.RegistryError, match="not_ready"):
        r.activate(conn, "staged", 0)
    bookmark(conn)
    conn.commit()
    epoch = r.source_epoch(conn)
    r.set_generation_snapshot(conn, "staged", epoch)
    r.mark_ready(conn, "staged", "/private/manifest.json", "sha256:" + "b" * 64)
    r.activate(conn, "staged", epoch)
    assert r.active_generation(conn).source_epoch == epoch
    with pytest.raises(r.RegistryError, match="not_staging"):
        r.mark_failed(conn, "staged")
    r.allocate_generation(conn, "failed", ("captured",))
    r.mark_failed(conn, "failed", "synthetic_failure")
    with pytest.raises(r.RegistryError, match="not_staging"):
        r.mark_ready(conn, "failed", "/private/manifest.json", "a" * 64)
    with pytest.raises(r.RegistryError, match="not_ready"):
        r.activate(conn, "failed", epoch)


def test_registry_never_commits_unrelated_writes(fresh_db):
    bookmark(fresh_db)
    with pytest.raises(r.RegistryError, match="open transaction"):
        r.allocate_generation(fresh_db, "staged", ("captured",))
    assert fresh_db.in_transaction
    fresh_db.rollback()
    assert not fresh_db.execute("SELECT 1 FROM bookmarks").fetchone()


def test_lease_exclusion_expiry_and_fencing(tmp_path):
    path = tmp_path / "synthetic.db"
    first, second = init_db(path), init_db(path)
    with r.publisher_lease(first) as old:
        old.assert_owned()
        with pytest.raises(r.RegistryError, match="lease_busy"):
            with r.publisher_lease(second):
                pytest.fail("second publisher must not acquire live lease")
        first.execute("UPDATE index_corpus SET lease_expires_at=unixepoch()-1")
        first.commit()
        with r.publisher_lease(second) as current:
            with pytest.raises(r.RegistryError, match="lease_lost"):
                old.assert_owned()
            with pytest.raises(r.RegistryError, match="lease_lost"):
                r.allocate_generation(first, "stale", ("captured",), lease_token=old.token)
            ready(second, "valid", lease_token=current.token)
            with pytest.raises(r.RegistryError, match="lease_lost"):
                r.activate(first, "valid", r.source_epoch(first), lease_token=old.token)
            r.activate(second, "valid", r.source_epoch(second), lease_token=current.token)
    assert r.active_generation(first).generation_id == "valid"
    with r.publisher_lease(first) as new:
        new.assert_owned()
    first.close()
    second.close()
