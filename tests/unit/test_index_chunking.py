"""Synthetic tokenizers only; these tests make no model-quality claim."""

import hashlib

import pytest

from muninn.consumers.packets.canonical import normalize_text
from muninn.index.chunking import ChunkSettings, chunk_document


class CharacterTokenizer:
    digest = "fake-character-tokenizer/1"

    def count_tokens(self, text):
        return len(text)


def chunks(body, **kwargs):
    return list(chunk_document("document:synthetic", "revision:1", body,
        profile_id="profile:fake", tokenizer=CharacterTokenizer(), **kwargs))


def test_determinism_exact_normalized_spans_and_coverage():
    body = "  Cafe\u0301\r\n\r\n" + "synthetic paragraph. " * 80 + "\n\nLast paragraph.  "
    normalized = normalize_text(body)
    result = chunks(body, title="Synthetic title", heading="Section")
    assert result == chunks(body, title="Synthetic title", heading="Section")
    assert len(result) > 1
    covered = set()
    for ordinal, chunk in enumerate(result):
        assert chunk.ordinal == ordinal
        assert chunk.text == normalized[chunk.start:chunk.end]
        assert chunk.text_digest == hashlib.sha256(chunk.text.encode()).hexdigest()
        assert chunk.embedding_input == chunk.prefix + normalized[chunk.start:chunk.end]
        assert chunk.prefix == "Synthetic title\nSection\n\n"
        assert chunk.token_count <= 768
        covered.update(range(chunk.start, chunk.end))
    assert covered == set(range(len(normalized)))
    assert len({c.chunk_id for c in result}) == len(result)


def test_token_limit_includes_prefix_and_prompt():
    result = chunks("z" * 3000, title="t" * 300,
        settings=ChunkSettings(target_tokens=512, max_tokens=768, prompt_overhead=50))
    assert all(len(c.embedding_input) + 50 == c.token_count <= 768 for c in result)
    assert len(result[0].text) == 416


def test_long_section_overlap_and_unicode_progress():
    body = "🦊" * 1800
    result = chunks(body)
    assert all(c.end > c.start for c in result)
    assert all(a.start < b.start <= a.end for a, b in zip(result, result[1:]))
    assert all(a.end < b.end for a, b in zip(result, result[1:]))
    assert all(a.end - b.start == 64 for a, b in zip(result, result[1:]))
    assert result[0].start == 0 and result[-1].end == len(body)
    assert len(result) == 4


def test_paragraph_preference_and_whole_fence_when_it_fits():
    settings = ChunkSettings(target_tokens=40, max_tokens=100, overlap_tokens=0)
    body = "A" * 20 + "\n\n" + "B" * 50
    assert chunks(body, settings=settings)[0].text == "A" * 20 + "\n\n"
    fence = "```python\n" + "x = 1\n" * 8 + "```\n"
    result = chunks(fence + "\nTail", settings=settings)
    assert result[0].text == fence


def test_small_paragraph_does_not_repeat_with_default_overlap():
    body = "Short paragraph.\n\n" + "x" * 1500
    result = chunks(body)
    assert result[0].text == "Short paragraph.\n\n"
    assert result[1].start == result[0].end
    assert len(result) == 5


def test_chunk_identity_binds_revision_settings_prefix_and_profile():
    first = chunks("body")[0]
    changed_revision = list(chunk_document("document:synthetic", "revision:2", "body",
        profile_id="profile:fake", tokenizer=CharacterTokenizer()))[0]
    changed_profile = list(chunk_document("document:synthetic", "revision:1", "body",
        profile_id="profile:other", tokenizer=CharacterTokenizer()))[0]
    changed_settings = chunks("body", settings=ChunkSettings(target_tokens=400))[0]
    changed_prefix = chunks("body", title="Title")[0]
    assert len({c.chunk_id for c in [first, changed_revision, changed_profile, changed_settings, changed_prefix]}) == 5


def test_empty_body_and_oversized_prefix():
    assert chunks(" \r\n ") == []
    with pytest.raises(ValueError, match="prefix/prompt"):
        chunks("body", title="x" * 768)


def test_zero_tokenizer_still_advances():
    class ZeroTokenizer(CharacterTokenizer):
        def count_tokens(self, text):
            return 0
    result = list(chunk_document("doc", "rev", "x" * 2000,
        profile_id="fake", tokenizer=ZeroTokenizer()))
    assert len(result) == 1


def test_unicode_token_counts_are_not_character_counts():
    class ByteTokenizer(CharacterTokenizer):
        digest = "fake-utf8-byte-tokenizer/1"

        def count_tokens(self, text):
            return len(text.encode("utf-8"))

    result = list(chunk_document("doc", "rev", "🦊" * 600,
        profile_id="fake", tokenizer=ByteTokenizer(), title="Fox"))
    assert all(c.token_count == len(c.embedding_input.encode("utf-8")) <= 768 for c in result)
    assert result[-1].end == 600
    assert all(a.end - b.start == 16 for a, b in zip(result, result[1:]))


def test_context_sensitive_counts_drop_overlap_instead_of_repeating():
    class ContextTokenizer(CharacterTokenizer):
        def count_tokens(self, text):
            return len(text) * (100 if text.startswith("x") else 1)

    result = list(chunk_document("doc", "rev", "A" + "x" * 600,
        profile_id="fake", tokenizer=ContextTokenizer()))
    assert result[0].end == 512
    assert result[1].start == 512
    assert all(a.end < b.end for a, b in zip(result, result[1:]))
    assert result[-1].end == 601


def test_declared_model_max_rejects_oversized_chunk_configuration():
    class LimitedTokenizer(CharacterTokenizer):
        max_seq_length = 256

    with pytest.raises(ValueError, match="max_tokens exceeds tokenizer max_seq_length"):
        list(chunk_document("doc", "rev", "body", profile_id="fake", tokenizer=LimitedTokenizer()))
    result = list(chunk_document("doc", "rev", "x" * 600, profile_id="fake",
        tokenizer=LimitedTokenizer(), settings=ChunkSettings(target_tokens=128, max_tokens=256)))
    assert all(c.token_count <= 256 for c in result)


@pytest.mark.parametrize("limit", [0, -1, True, "256"])
def test_invalid_declared_model_limit(limit):
    tokenizer = CharacterTokenizer()
    tokenizer.max_seq_length = limit
    with pytest.raises(ValueError, match="positive integer"):
        list(chunk_document("doc", "rev", "body", profile_id="fake", tokenizer=tokenizer))


@pytest.mark.parametrize("kwargs", [
    {"target_tokens": 0}, {"target_tokens": 769}, {"overlap_tokens": 512},
    {"overlap_tokens": -1}, {"prompt_overhead": 768},
])
def test_invalid_settings(kwargs):
    with pytest.raises(ValueError):
        ChunkSettings(**kwargs)


def test_character_cannot_fit_reports_omission():
    class LargeTokenizer(CharacterTokenizer):
        def count_tokens(self, text):
            return 1000 if text else 0
    with pytest.raises(ValueError, match="cannot fit"):
        list(chunk_document("doc", "rev", "🦊", profile_id="fake", tokenizer=LargeTokenizer()))
