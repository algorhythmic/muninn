# Enrichment prompts

Each `.md` file here is a system prompt sent to the LLM during enrichment.

## Naming convention

Files are named `<task>_v<N>.md`, where:

- `<task>` is a short snake_case identifier for what the prompt does
  (e.g., `per_bookmark`, `era_narrative`, `deep_pass`).
- `<N>` is a monotonically increasing integer.

The filename stem (without `.md`) is the value written to the
`enrichment_prompt_version` column on the `enriched`, `eras`, and
`analyses` tables, e.g. `per_bookmark_v1`. This column is part of the
idempotency triple `(enrichment_model, enrichment_prompt_version,
content_hash)` — bumping the version bypasses the cache and forces
re-enrichment of every row.

## When to bump the version

Bump (i.e. create a new `<task>_v<N+1>.md`) when the **prompt text
changes meaningfully**. Editing whitespace or fixing a typo can stay on
the existing version. Anything that affects the JSON schema, the
content-type taxonomy, the tagging guidance, or the model's behavior
on the same input must be a new file with a new integer.

After bumping, update the corresponding constant in `muninn.config`
(e.g. `PER_BOOKMARK_PROMPT_VERSION`) so the pipeline picks it up.

## Current prompts

- `per_bookmark_v1.md` — Haiku per-bookmark enrichment. Produces
  `summary`, `tags`, `entities`, `content_type`, `language` as JSON.
  Used by `muninn.enrich.haiku`.
