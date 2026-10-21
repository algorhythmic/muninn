# Research Intake — muninn development record

Research Intake is a workflow inside muninn. It captures
selected external material (initially the operator's X bookmarks and their
linked articles and repositories), assesses its suitability, and exports
versioned evidence packets. Odin consumes those packets for project
applicability review, proposals and human decisions. Muninn owns capture,
archive and assessment; Odin owns proposals and decisions.

This directory is muninn's own development record for that work.

- **Design.** Research Intake is a workflow over muninn's existing pipeline
  stages, not a separate module (ADR-009). It consists of:
  - an X ingest adapter
  - a suitability enrichment task
  - an evidence-packet export
  - append-only history tables
- **Sequencing.** X bookmarks are muninn's v1 (ADR-012).
- **Origin.** The originating Odin design handoff is kept privately, in the
  dotprivate sidecar. It originates in Odin's `docs/design/`.

## Status

| Ticket | State | Record |
|---|---|---|
| RI-01 extension spike and decision | Spike complete; decision **accepted 2026-10-21** in revised stage-native form | [RI-01-extension-spike.md](RI-01-extension-spike.md), ADR-009 |
| P1 dependency fixes | In progress on branch `adr-reset-x-first` | [work plan §P](RI-02-RI-03-work-plan.md#p--prerequisite-maintenance-muninn-hygiene-not-ri-scope) |
| P2 schema migrator | Not started; next after P1 | [work plan §P2](RI-02-RI-03-work-plan.md#p2--schema-migrator-next-after-p1) |
| RI-02 X access qualification | Desk research done; live qualification (operator-run) and synthetic fixtures not started | [work plan §RI-02](RI-02-RI-03-work-plan.md#ri-02--qualify-x-access-and-capture-strategy) |
| RI-03 records and producer contract | Not started | [work plan §RI-03](RI-02-RI-03-work-plan.md#ri-03--specify-records-and-the-producer-interface) |
| RI-04 – RI-08 | Not started | Handoff §7 |
| RI-09 | Odin-owned; not tracked here | — |

Nothing is implemented. No live X access, model evaluation, Odin
integration or deployment has occurred. Update this table with test
receipts as work lands. The handoff document is design, not runtime state.

## Privacy

This repository's GitHub remote is public. Fixtures here are synthetic.
Live X responses, bookmark content and account details go only to private
stores: the dotprivate sidecar at `.private/muninn/`.
