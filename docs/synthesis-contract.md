# Synthesis task/result contract (`muninn-synthesis/1`)

This is the contract ADR-013 calls for. Synthesis (era narratives, deep passes and ad-hoc analyses) is a file boundary:

- Muninn writes a validated **task file**.
- A runner turns it into a **result file**.
- Muninn validates the result and writes it to the database.

The runner is a deployment choice. Muninn ships one runner, the budgeted direct API call. Any other harness can run the same task file: a local Claude Code session, an Odin-dispatched session or a Saga run. Muninn neither knows nor cares which one ran it.

Code: `src/muninn/synthesis/` (`contract.py`, `orchestrator.py`, `api_runner.py`, `budget.py`). Schemas: `schemas/json/`.

## Commands

```
muninn synthesize era ERA_LABEL            [--emit-task DIR] [--model M] [--max-cost-usd X]
muninn synthesize deep-pass ID | --pending [--emit-task DIR] [--model M] [--max-cost-usd X]
muninn synthesize analyze "PROMPT" [--title T] [--bookmark-id N]...
                                           [--emit-task DIR] [--model M] [--max-cost-usd X]
muninn synthesize ingest-result FILE [--retry-dir DIR]
```

- **No flag** runs the task with the API runner (below).
- **`--emit-task DIR`** writes the task file to `DIR` and runs nothing. It spends nothing and needs no API key. `--model` and `--max-cost-usd` do not apply.
- **`ingest-result FILE`** validates a result file and writes it.
  - A rejected file writes nothing and exits non-zero.
  - Output that fails validation is recorded as `validation_failed` and exits 1. With `--retry-dir`, a first-attempt failure also emits the attempt-2 task file there.

## The task file

`DIR/<task_id>-a<attempt>.task.json` is one JSON object. It validates against `schemas/json/task-input.schema.json`. It is self-contained, so a harness needs nothing from this repository to run it.

| Field | Meaning |
|---|---|
| `contract_version` | `"muninn-synthesis/1"` |
| `task_id` | e.g. `era-3f9c0a1b2c4d`; `[A-Za-z0-9][A-Za-z0-9_.-]*` |
| `task_type` | `era_narrative`, `deep_pass` or `ad_hoc_analysis` |
| `attempt` | `1`, or `2` for the single self-correction retry |
| `task_digest` | `sha256:<hex>`, see below |
| `prompt_version` | e.g. `synthesis_v1`; names `src/muninn/synthesis/prompts/<version>.md` |
| `created_at` | ISO-8601 UTC |
| `instructions` | the system prompt (the text of that prompt file) |
| `result_schema` | the JSON Schema the result's `output` must satisfy (`era-narrative`, `deep-pass` or `ad-hoc-analysis.schema.json`, inlined) |
| `correction_instructions` | `null` on attempt 1. On attempt 2, the previous attempt's validation errors |
| `era_label` / `bookmark_id` / `prompt`, `filter_query`, `title` | the task's subject (per type) |
| `materials` | everything the model may use; the per-type shapes are in `src/muninn/synthesis/tasks/*.py` |

`task_digest` is `"sha256:"` followed by the lowercase hex SHA-256 of the task object's canonical JSON, with the `task_digest` key itself left out. Canonical JSON here means:

- keys sorted
- separators `,` and `:` with no whitespace
- non-ASCII characters kept as UTF-8

In Python that is `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` encoded as UTF-8. A harness never needs to compute the digest; it only echoes it back.

Emitting writes the file twice:

- the copy in `DIR`, for the harness
- muninn's own copy in the **task registry**, `$MUNINN_DATA_DIR/synthesis/tasks/`

It also records a `synthesis_runs` row (`task_id`, `attempt`, status `running`). Materials can carry captured text, so the registry lives beside the database. Treat a handed-out task file as private data, like the database.

## The result file

A result file (by convention `<task_id>-a<attempt>.result.json`) validates against `schemas/json/synthesis-result.schema.json`:

```json
{
  "contract_version": "muninn-synthesis/1",
  "task_id": "era-3f9c0a1b2c4d",
  "task_digest": "sha256:…",
  "attempt": 1,
  "runner": "claude-code-local",
  "output": { "narrative": "…", "inferred_year": 2014, "…": "…" }
}
```

- `task_id`, `task_digest` and `attempt` are copied from the task file.
- `runner` is optional free text, recorded for audit.
- `output` is the model's result object.
  - Its `synthesis_metadata.model` is the model that produced it.
  - Its token counts are the harness's best measurement, or `0`.

## Validation and writing (`ingest-result`)

**Rejected, with nothing written to any table**, when any of these hold:

1. the file is not JSON, or not a valid result envelope
2. there is no registry task for `task_id` and `attempt`
3. the registry copy no longer matches its own digest
4. `task_digest` differs from the registry copy's
5. that attempt's `synthesis_runs` row is not `running` (a result is ingested once)

**Otherwise `output` is validated**, using the same checks as an API run. The task's `result_schema` comes from the registry copy, not the handed-out copy. For deep passes, two more checks apply:

- every `key_quotes` entry must be a verbatim substring of `materials.source_text`
- every cross-reference target must be in `materials.known_bookmark_ids`

The outcome:

- **Valid:** the canonical tables are written and the run row becomes `completed`, in one transaction. The tables are `eras` (upsert), `enriched` + `cross_references` (deep pass) or `analyses` (append). The writes use the registry copy's materials, so a harness that edits its own copy changes nothing. A deep pass keeps the bulk-enrichment idempotency triple (RI-01 D6). It also refreshes FTS, while Qdrant waits for `scripts/reconcile-vector-index.py`.
- **Invalid:** the row becomes `validation_failed` with its errors, and no canonical table is touched. On attempt 1 with `--retry-dir`, an attempt-2 task file is emitted. It has the same task id and materials, plus the correction instructions. Ingesting its result follows the same rules. There is no third attempt.

An ingested result records no `usage_records` row. A harness's spend is not muninn's API spend.

## The default runner: budgeted direct API call

Without `--emit-task`, the orchestrator builds the same task object and hands it to `ApiRunner`:

- The system prompt is the task's `instructions`. The user message is the rest of the task object as JSON.
- Calls go through `client.messages.stream(...)` + `get_final_message()`, with `thinking={"type": "adaptive"}` and `output_config={"effort": …}`, and `max_tokens` 32 000. Thinking counts toward `max_tokens`.
- The model is `--model`, else `MUNINN_SYNTHESIS_MODEL`, else **`claude-opus-5-5`** (ADR-013). The model must be in `muninn.enrich.pricing.PRICES`, or the run refuses to start.
- Effort is `MUNINN_SYNTHESIS_EFFORT` (`low`…`max`), default `high`.
- The runner overwrites `synthesis_metadata` with what it measured: the served model, the task's prompt version, and the input and output token counts from `usage`.

**Budget.** Costs are integer micro-USD, priced from `muninn.enrich.pricing`.

- **Per-run cap:** `--max-cost-usd`, default `3.00`. "Per run" means one CLI invocation, so `deep-pass --pending` shares it across its tasks.
- **Monthly cap:** `MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD`, checked against this UTC calendar month's `usage_records` rows whose `operation` starts with `synthesize:`. It is **required**: without it the runner makes no call and the command fails with a one-line error.
- **Before each attempt:** the worst-case cost must fit in what is left of both caps. Worst case is the request's characters at 2 chars per token, priced at the dearer of the input and cache-write rates, plus `max_tokens` of output. If it does not fit, no call is made. The attempt is recorded as `cap_hit`, with the numbers in `validation_errors`. `--pending` then stops.
- **After each call:** the measured cost from `usage` is appended to `usage_records`: `kind='model'`, `provider='anthropic'`, `operation='synthesize:<task_type>'`, `cost_basis='estimated'`. It is charged to the run. A response without usable `usage` is charged the worst case.

**Outcomes** (`synthesis_runs.status`; the CHECK enum is unchanged):

| Status | When |
|---|---|
| `running` | emitted, awaiting a result file |
| `completed` | valid output written |
| `validation_failed` | output failed validation, did not parse as a JSON object, or was truncated at `max_tokens`. Attempt 1 is retried once with correction instructions |
| `container_failed` | the runner produced no output: an API error or a refusal (`stop_reason="refusal"`). Not retried. The legacy name is kept from the retired container runner |
| `cap_hit` | the budget stopped the attempt before any call |

`synthesis_runs.container_id`, also a legacy name, holds the runner reference: `anthropic-api:<message id>`, or `result:<runner>` for an ingested file.

Not in this version:

- **Server-side refusal fallbacks.** These are deliberately off: a fallback could be served by a model the pre-call bound did not price.
- **Message Batches API.** Batches are 50% off and would suit `--pending` and multi-era runs where latency doesn't matter. This is future work: it needs an asynchronous submit/collect pair, with the budget reserved at submit time.
- **Structured outputs (`output_config.format`).** The result schemas use `minLength`, `maxItems` and similar keywords that the constrained decoder may not support, so validation stays after the call.

## Running a task file in another harness

Any harness can do this:

1. Read the task file. Use `instructions` as the system prompt, or as the opening instruction. Give the model the rest of the file.
2. Have the model reply with one JSON object that validates against `result_schema`. If `correction_instructions` is set, the model must fix every listed error.
3. Write the result envelope: `contract_version`, `task_id`, `task_digest` and `attempt` copied from the task, the model's object as `output`, and optionally `runner`.
4. Return the file to the operator, who runs `muninn synthesize ingest-result FILE`.

A local Claude Code session is the usual case. Point it at the task file, have it write the result file next to it, then ingest it:

```
muninn synthesize era "uni years" --emit-task ~/synthesis/out
# in a Claude Code session: "Run the muninn synthesis task in
#   ~/synthesis/out/era-….task.json and write the result file beside it."
muninn synthesize ingest-result ~/synthesis/out/era-…-a1.result.json --retry-dir ~/synthesis/out
```

An Odin-dispatched session works the same way. Odin carries the task file to its session and returns the result file. Neither side reads the other's database.

## Versioning

- **`contract_version`** names the file layout. A breaking change to either envelope, or to the digest rule, bumps it (`muninn-synthesis/2`), and muninn rejects versions it does not know.
- **`prompt_version`** names the instructions (`prompts/<task>_v<N>.md`). Bump it when the instructions change meaningfully. It is recorded on every run and every canonical row.
- **Result schemas** are inlined in each task file. A task is validated against the schema it was emitted with, even if the repository's schema has changed since.
