# Compiled cross-references

The vault compiler turns bookmark relationships stored in SQLite into
Obsidian links on captured pages under `wiki/bookmarks/`. The
`cross_references` table is the durable source for these relationships;
the compiler only derives display links from it. A row records a directed
`source_bookmark_id` → `target_bookmark_id` relationship, with optional
`relationship` and `rationale` text and a required `created_by` value.
Deep-pass synthesis can add rows to this table. Its output is validated
before persistence, and the writer records `created_by='deep_pass'`.

## What appears on a page

For a compiled bookmark, the compiler reads both outgoing rows (the page
is the source) and incoming rows (the page is the target). Each eligible
neighbor appears as a `[[slug]]` link in **Cross-references** on that
page. The stored relationship remains directed: showing the link at both endpoints
does not create a reverse database row or reverse the meaning of its
metadata.

Only bookmarks that are visible and have a compiled page in the current
run can supply or receive a Cross-references link. The compiler resolves
links from its current `bookmark_id` → slug map, so a hidden or missing target
cannot leave a dangling wikilink. A policy change that hides a bookmark
also removes its compiled page on the next normal compile.

The compiler omits self-references and renders each eligible neighbor
once, even if rows exist in both directions or under multiple creators.
It sorts the resolved slugs for a stable result and does not cap the
number of persisted links. This section is absent when there are no
eligible stored references.

The existing **Related** section remains for same-era and shared-tag
suggestions. It excludes neighbors already shown in Cross-references,
so persisted relationships take precedence over the heuristic list.
Era and tag matches do not write `cross_references` rows.

X item pages also have **Links** and **Linked from** sections based on
`item_links`. Those sections describe source-item links. A neighbor
already linked there is not repeated in heuristic **Related**, but an
eligible persisted reference still appears in **Cross-references**. The
same cross-reference visibility and priority rules apply to both X and
browser bookmark pages.

## Metadata boundary

`relationship`, `rationale`, and `created_by` remain stored data, not
rendered instructions. The captured-page templates emit only resolved
wikilinks for Cross-references; they do not place the free-text rationale
or relationship in page prose, link labels, frontmatter, or HTML comments.
Consumers that need the directed meaning should read the database row,
including its source and target IDs, instead of inferring it from the
bidirectional vault links.

The compiler owns only captured pages in `wiki/bookmarks/`. Curated
concept, infrastructure, people, and reference-project notes belong to
the authored lane and are outside this compilation path.
