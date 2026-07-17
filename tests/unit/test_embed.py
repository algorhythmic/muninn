"""Unit tests for the pluggable embedding backends (muninn.vector.embed)."""

from __future__ import annotations

import math
import sys

import pytest

from muninn.vector import embed


def test_hash_backend_deterministic_and_normalized(monkeypatch):
    monkeypatch.setenv("MUNINN_EMBEDDING_BACKEND", "hash")
    v1 = embed.embed_document("hello world")
    v2 = embed.embed_document("hello world")
    assert v1 == v2
    assert len(v1) == embed.VECTOR_DIM
    assert math.isclose(sum(x * x for x in v1), 1.0, rel_tol=1e-6)
    assert embed.embed_document("something else") != v1


def test_hash_backend_is_symmetric(monkeypatch):
    # The placeholder has no asymmetric prompts: query == document vector.
    monkeypatch.setenv("MUNINN_EMBEDDING_BACKEND", "hash")
    assert embed.embed_query("quokka") == embed.embed_document("quokka")


def test_text_to_vector_is_document_alias(monkeypatch):
    monkeypatch.setenv("MUNINN_EMBEDDING_BACKEND", "hash")
    assert embed.text_to_vector("x") == embed.embed_document("x")


def test_missing_sentence_transformers_raises_actionable_error(monkeypatch):
    monkeypatch.setenv("MUNINN_EMBEDDING_BACKEND", "sentence-transformers")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    monkeypatch.setattr(embed, "_st_model", None)
    with pytest.raises(embed.EmbeddingBackendError) as exc:
        embed.embed_document("some text")
    assert "muninn[embeddings]" in str(exc.value)


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("MUNINN_EMBEDDING_BACKEND", "banana")
    with pytest.raises(embed.EmbeddingBackendError):
        embed.embed_query("some text")
