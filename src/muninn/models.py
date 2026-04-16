"""Pydantic models matching the canonical schema rows."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


EnrichmentSource = Literal["at_capture", "recent_archive", "live_fallback", "none"]
ScrapePass = Literal["live", "at_capture", "recent_archive", "playwright", "manual"]
ScrapeStatus = Literal[
    "ok", "partial", "failed", "js_required", "paywall",
    "robots_disallowed", "no_archive", "network_error",
    "timeout", "auth_required",
]
ExtractionQuality = Literal["ok", "partial", "failed"]
SynthesisStatus = Literal[
    "running", "completed", "validation_failed", "container_failed", "cap_hit",
]
SynthesisTaskType = Literal["era_narrative", "deep_pass", "ad_hoc_analysis"]


class Bookmark(BaseModel):
    bookmark_id: Optional[int] = None
    source: str
    source_id: str
    captured_at: int
    title: Optional[str] = None
    url: Optional[str] = None
    folder_path: Optional[list[str]] = None
    era_label: Optional[str] = None
    domain: Optional[str] = None
    source_metadata: Optional[dict] = None
    redacted_param_count: int = 0
    redacted_param_names: Optional[list[str]] = None
    path_redacted: bool = False
    content_visible: bool = True
    enrichment_source: Optional[EnrichmentSource] = None
    ingested_at: Optional[int] = None


class ScrapeResult(BaseModel):
    scrape_result_id: Optional[int] = None
    bookmark_id: int
    pass_: ScrapePass = Field(alias="pass")
    fetched_at: int
    target_timestamp: Optional[int] = None
    actual_snapshot_at: Optional[int] = None
    archive_url: Optional[str] = None
    final_url: Optional[str] = None
    http_status: Optional[int] = None
    scrape_status: ScrapeStatus
    extraction_quality: Optional[ExtractionQuality] = None
    content_text: Optional[str] = None
    content_html: Optional[str] = None
    raw_html_path: Optional[str] = None
    error_detail: Optional[str] = None

    model_config = {"populate_by_name": True}


class Enriched(BaseModel):
    bookmark_id: int
    summary: Optional[str] = None
    tags: Optional[list[str]] = None
    entities: Optional[list[str]] = None
    content_type: Optional[str] = None
    language: Optional[str] = None
    word_count: Optional[int] = None
    enrichment_model: str
    enrichment_prompt_version: str
    content_hash: str
    enriched_at: int
    deep_pass_requested: bool = False
    key_quotes: Optional[list[str]] = None


class Era(BaseModel):
    era_label: str
    inferred_year: Optional[int] = None
    start_date: Optional[int] = None
    end_date: Optional[int] = None
    bookmark_count: int
    dominant_topics: Optional[list[str]] = None
    dominant_domains: Optional[list[str]] = None
    narrative: Optional[str] = None
    enrichment_model: Optional[str] = None
    enrichment_prompt_version: Optional[str] = None
    generated_at: Optional[int] = None


class CrossReference(BaseModel):
    cross_reference_id: Optional[int] = None
    source_bookmark_id: int
    target_bookmark_id: int
    relationship: Optional[str] = None
    rationale: Optional[str] = None
    created_by: str
    created_at: Optional[int] = None


class Analysis(BaseModel):
    analysis_id: Optional[int] = None
    title: str
    prompt: str
    filter_query: Optional[dict] = None
    narrative: Optional[str] = None
    enrichment_model: Optional[str] = None
    enrichment_prompt_version: Optional[str] = None
    generated_at: Optional[int] = None


class SynthesisRun(BaseModel):
    synthesis_run_id: Optional[int] = None
    task_id: str
    task_type: SynthesisTaskType
    attempt: int
    started_at: int
    completed_at: Optional[int] = None
    status: SynthesisStatus
    enrichment_model: Optional[str] = None
    enrichment_prompt_version: Optional[str] = None
    input_token_count: Optional[int] = None
    output_token_count: Optional[int] = None
    validation_errors: Optional[list[dict]] = None
    container_id: Optional[str] = None
