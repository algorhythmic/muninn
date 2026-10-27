"""Profile adapter tests use a fake model; no weights, imports or downloads."""

from dataclasses import asdict, replace
import hashlib
import importlib.metadata
import json
from types import SimpleNamespace

import pytest

from muninn.index import embedding_profile as profiles
from muninn.index.embedding_cache import EmbeddingCache
from muninn.vector import embed


class FakeTokenizer:
    model_max_length = 128
    truncation_side = "right"
    padding_side = "right"
    special_tokens_map = {"bos_token": "<synthetic>"}

    def __init__(self):
        self.backend_tokenizer = SimpleNamespace(to_str=lambda: '{"synthetic":true}')


class FakeModel:
    max_seq_length = 128

    def __init__(self):
        self.prompts = {"query": "synthetic-query: ", "document": "synthetic-document: "}
        self.first = SimpleNamespace(
            auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash="a" * 40)),
            tokenizer=FakeTokenizer(),
        )
        self.calls = []

    def _first_module(self):
        return self.first

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        return [[0.6, 0.8] for text in texts]


@pytest.fixture
def model(monkeypatch):
    from muninn import config

    model = FakeModel()
    monkeypatch.setenv("MUNINN_EMBEDDING_BACKEND", "sentence-transformers")
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "synthetic/model")
    monkeypatch.setattr(config, "EMBEDDING_DIM", 2)
    monkeypatch.setattr(embed, "_get_st_model", lambda: model)
    monkeypatch.setattr(profiles.importlib.metadata, "version", lambda package: "synthetic-1")
    return model


def test_profile_digest_is_canonical_and_stable(model):
    profile = profiles.available_profile()
    canonical = json.dumps(asdict(profile), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert profile.profile_id == "muninn-embedding-profile:" + expected
    assert profiles.available_profile() == profile
    assert profile.semantic_eligible
    assert profile.incomplete_reasons == ()
    assert not model.calls


@pytest.mark.parametrize("change", [
    {"model": "different/model"}, {"artifact_revision": "b" * 40}, {"dimension": 3},
    {"query_prompt_digest": "different"}, {"document_prompt_digest": "different"},
    {"tokenizer_digest": "different"}, {"runtime_versions": (("torch", "changed"),)},
    {"backend": "hash"}, {"normalization": "none"},
    {"query_prompt_name": "different"}, {"document_prompt_name": "different"},
    {"adapter_version": "different"}, {"schema": "different"},
])
def test_every_space_identity_change_changes_digest(model, change):
    profile = profiles.available_profile()
    assert replace(profile, **change).profile_id != profile.profile_id


@pytest.mark.parametrize("revision", [None, "main", "latest", "v1", "abcd"])
def test_mutable_or_unresolved_artifact_is_incomplete(model, revision):
    model.first.auto_model.config._commit_hash = revision
    profile = profiles.available_profile()
    assert "artifact_revision" in profile.incomplete_reasons
    assert not profile.semantic_eligible
    with pytest.raises(profiles.ProfileUnavailable, match="incomplete_profile"):
        profile.require_complete()


@pytest.mark.parametrize("name", ["query", "document"])
def test_missing_prompt_never_falls_back_to_unprompted(model, name):
    del model.prompts[name]
    profile = profiles.available_profile()
    assert name + "_prompt_digest" in profile.incomplete_reasons
    with pytest.raises(profiles.ProfileUnavailable, match="incomplete_profile"):
        profiles.query_embedding("input", profile, capabilities=["query_embedding"])
    assert not model.calls


def test_missing_tokenizer_is_reported_incomplete(model):
    model.first.tokenizer = None
    profile = profiles.available_profile()
    assert "tokenizer_digest" in profile.incomplete_reasons
    assert not profile.semantic_eligible


def test_missing_runtime_version_is_incomplete(model, monkeypatch):
    def missing(package):
        raise importlib.metadata.PackageNotFoundError(package)

    monkeypatch.setattr(profiles.importlib.metadata, "version", missing)
    profile = profiles.available_profile()
    assert "runtime_versions" in profile.incomplete_reasons
    assert not profile.semantic_eligible


def test_model_load_failure_is_typed_and_has_no_hash_fallback(model, monkeypatch):
    profile = profiles.available_profile()

    def unavailable():
        raise embed.EmbeddingBackendError("synthetic unavailable encoder")

    monkeypatch.setattr(embed, "_get_st_model", unavailable)
    with pytest.raises(profiles.ProfileUnavailable, match="encoder_unavailable"):
        profiles.available_profile()
    with pytest.raises(profiles.ProfileUnavailable, match="encoder_unavailable"):
        profiles.query_embedding("input", profile, capabilities=["query_embedding"])
    assert not model.calls


def test_runtime_model_load_failure_is_typed(model, monkeypatch):
    profile = profiles.available_profile()

    def unavailable():
        raise RuntimeError("synthetic device failure")

    monkeypatch.setattr(embed, "_get_st_model", unavailable)
    with pytest.raises(profiles.ProfileUnavailable, match="encoder_unavailable"):
        profiles.available_profile()
    with pytest.raises(profiles.ProfileUnavailable, match="encoder_unavailable"):
        profiles.query_embedding("input", profile, capabilities=["query_embedding"])


def test_runtime_inference_failure_is_typed_and_not_cached(model, monkeypatch, tmp_path):
    profile = profiles.available_profile()
    cache = EmbeddingCache(tmp_path)

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic device failure")

    monkeypatch.setattr(model, "encode", unavailable)
    with pytest.raises(profiles.ProfileUnavailable, match="encoder_unavailable"):
        profiles.query_embedding("input", profile, capabilities=["query_embedding"])
    with pytest.raises(profiles.ProfileUnavailable, match="encoder_unavailable"):
        list(profiles.document_embeddings(["input"], profile, cache=cache))
    assert cache.get(profile, "input") is None


def test_tokenizer_behavior_change_changes_space(model):
    before = profiles.available_profile()
    model.first.tokenizer.truncation_side = "left"
    assert profiles.available_profile().profile_id != before.profile_id


def test_query_and_document_keep_distinct_prompts(model):
    profile = profiles.available_profile()
    result = profiles.query_embedding("input", profile, capabilities=["query_embedding"])
    assert result == {"model": profile.profile_id, "vector": [0.6, 0.8]}
    assert list(profiles.document_embeddings(["input"], profile)) == [[0.6, 0.8]]
    assert [kwargs["prompt_name"] for _, kwargs in model.calls] == ["query", "document"]
    assert all(kwargs["normalize_embeddings"] for _, kwargs in model.calls)


def test_exact_profile_refused_even_at_same_dimension(model):
    profile = profiles.available_profile()
    model.prompts["query"] = "changed query prompt"
    with pytest.raises(profiles.ProfileUnavailable, match="profile_mismatch"):
        profiles.query_embedding("input", profile, capabilities=["query_embedding"])
    assert not model.calls


def test_hash_backend_refused_before_loading(model, monkeypatch):
    profile = profiles.available_profile()
    monkeypatch.setenv("MUNINN_EMBEDDING_BACKEND", "hash")
    monkeypatch.setattr(embed, "_get_st_model", lambda: pytest.fail("model must not load"))
    with pytest.raises(profiles.ProfileUnavailable, match="semantic_backend_unavailable"):
        profiles.available_profile()
    with pytest.raises(profiles.ProfileUnavailable, match="semantic_backend_unavailable"):
        profiles.query_embedding("input", profile, capabilities=["query_embedding"])
    with pytest.raises(profiles.ProfileUnavailable, match="semantic_backend_unavailable"):
        list(profiles.document_embeddings(["input"], replace(profile, backend="hash")))


@pytest.mark.parametrize("capabilities,protocol", [([], 1), (["other"], 1), (["query_embedding"], 0), (["query_embedding"], 2)])
def test_capability_negotiated_before_inference(model, monkeypatch, capabilities, protocol):
    profile = profiles.available_profile()
    monkeypatch.setattr(embed, "_get_st_model", lambda: pytest.fail("model must not load"))
    with pytest.raises(profiles.ProfileUnavailable, match="capability_unavailable"):
        profiles.query_embedding("input", profile, capabilities=capabilities,
                                 protocol_version=protocol)


@pytest.mark.parametrize("query", ["", " \n ", "é" * 32769])
def test_query_shape_bounds_before_inference(model, query):
    with pytest.raises(ValueError):
        profiles.query_embedding(query, profiles.available_profile(), capabilities=["query_embedding"])
    assert not model.calls


def test_document_batch_size_bound_and_order(model):
    profile = profiles.available_profile()
    vectors = list(profiles.document_embeddings((str(i) for i in range(7)), profile, batch_size=2))
    assert len(vectors) == 7
    assert [texts for texts, _ in model.calls] == [["0", "1"], ["2", "3"], ["4", "5"], ["6"]]
    assert [kwargs["batch_size"] for _, kwargs in model.calls] == [2, 2, 2, 1]


def test_document_byte_bounds_are_utf8_bytes(model):
    profile = profiles.available_profile()
    assert len(list(profiles.document_embeddings(["éé", "a", "bbb"], profile,
                    max_input_bytes=5, max_batch_bytes=5))) == 3
    assert [texts for texts, _ in model.calls] == [["éé", "a"], ["bbb"]]
    model.calls.clear()
    with pytest.raises(ValueError, match="input exceeds"):
        list(profiles.document_embeddings(["éé"], profile, max_input_bytes=3,
                                         max_batch_bytes=5))
    assert not model.calls


@pytest.mark.parametrize("bounds", [
    {"batch_size": 0}, {"batch_size": 257}, {"max_input_bytes": 0},
    {"max_input_bytes": 6, "max_batch_bytes": 5}, {"max_batch_bytes": 8388609},
])
def test_invalid_batch_bounds_refused(model, bounds):
    with pytest.raises(ValueError, match="encoding bounds"):
        list(profiles.document_embeddings(["input"], profiles.available_profile(), **bounds))
    assert not model.calls


def test_document_cache_reuses_only_completed_vectors(model, tmp_path):
    cache = EmbeddingCache(tmp_path)
    profile = profiles.available_profile()
    cache.put(profile, "cached", [0.8, 0.6])
    result = list(profiles.document_embeddings(["cached", "new", "cached"], profile, cache=cache))
    assert result == [[0.8, 0.6], [0.6, 0.8], [0.8, 0.6]]
    assert [texts for texts, _ in model.calls] == [["new"]]
    model.calls.clear()
    assert list(profiles.document_embeddings(["new"], profile, cache=cache)) == [[0.6, 0.8]]
    assert not model.calls


def test_cancelled_stream_retains_complete_cache_without_encoding_rest(model, tmp_path):
    profile = profiles.available_profile()
    cache = EmbeddingCache(tmp_path)
    consumed = []

    def source():
        for text in ("one", "two", "three", "four", "five"):
            consumed.append(text)
            yield text

    stream = profiles.document_embeddings(source(), profile, cache=cache, batch_size=2)
    assert next(stream) == [0.6, 0.8]
    stream.close()
    assert [texts for texts, _ in model.calls] == [["one", "two"]]
    assert len(consumed) <= 3  # At most one input of lookahead beyond a bounded batch.
    assert cache.get(profile, "one") == [0.6, 0.8]
    assert cache.get(profile, "two") == [0.6, 0.8]
    assert cache.get(profile, "three") is None


@pytest.mark.parametrize("output", [[], [[0, 0]], [[float("nan"), 1]], [[1]], [[1, 2, 3]], [[True, False]]])
def test_bad_encoder_output_refused(model, monkeypatch, output):
    profile = profiles.available_profile()
    monkeypatch.setattr(model, "encode", lambda *args, **kwargs: output)
    with pytest.raises(profiles.ProfileUnavailable, match="invalid_vector"):
        profiles.query_embedding("input", profile, capabilities=["query_embedding"])


def test_array_encoder_output_uses_native_numeric_values(model, monkeypatch):
    class FakeArray:
        def tolist(self):
            return [0.6, 0.8]

    profile = profiles.available_profile()
    monkeypatch.setattr(model, "encode", lambda *args, **kwargs: [FakeArray()])
    assert profiles.query_embedding("input", profile, capabilities=["query_embedding"])["vector"] == [0.6, 0.8]
