"""Bounded operational status without queries, source text or vectors."""
from dataclasses import asdict
import os
from pathlib import Path
import time

from muninn.db import connect_readonly
from muninn.index import registry
from muninn.index.freshness import assess
from muninn.index.manifest import Manifest
from muninn.index.paths import load_index_paths


def index_status(db_path=None):
    conn = connect_readonly(db_path)
    try:
        active = registry.active_generation(conn)
        pointer = conn.execute("SELECT previous_generation_id,activation_id FROM index_corpus WHERE singleton=1").fetchone()
        ready = conn.execute("SELECT generation_id FROM index_generations WHERE state='ready' ORDER BY ready_at DESC, generation_id DESC LIMIT 1").fetchone()
        from muninn.index.worker import status
        publisher = asdict(status(conn))
        if publisher.get("updated_at") is not None:
            publisher["age_seconds"] = max(0, int(time.time()) - publisher["updated_at"])
        result = {"config_intent": os.environ.get("MUNINN_SEARCH_BACKEND", "fts5-qdrant"),
                  "corpus_id": registry.ensure_corpus(conn), "source_epoch": registry.source_epoch(conn),
                  "ready_build": ready[0] if ready else None, "active_generation": active.generation_id if active else None,
                  "active": asdict(active) if active else None,
                  "previous_generation": pointer[0], "activation_id": pointer[1],
                  "backend_actually_used": None, "backend_observation": "no_request_observation",
                  "publisher": publisher, "inventory": None, "profile": None, "braid": None,
                  "counts": None, "fallback_reason": "generation_unavailable" if active is None else None}
        if active is not None:
            try:
                path = load_index_paths().manifest_path(active.generation_id)
                if Path(active.manifest_path).resolve() != path.resolve():
                    raise ValueError("generation_path_mismatch")
                manifest = Manifest.read(path, expected_digest=active.manifest_digest)
                evidence = assess(conn, manifest)
                result.update(inventory=asdict(evidence), profile=manifest.profile_id,
                              braid=manifest.braid["identity"],
                              counts={"sources": manifest.source_count, "chunks": manifest.chunk_count,
                                      "omissions": len(manifest.omissions)}, fallback_reason=evidence.reason)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                result["fallback_reason"] = "manifest_unavailable"
        return result
    finally:
        conn.close()
