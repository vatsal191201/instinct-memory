---
name: instinct-memory
description: Recall durable memory through read-only vault tools and propose changes for the reconciler.
version: 1.0.0
author: The instinct-memory authors
license: Apache-2.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [memory, instinct, vault, aliases, reconciler, read-only, markdown]
    category: productivity
---

# Instinct Memory

Durable memory lives in git-tracked Markdown records with dated facts, lowercase aliases,
and links. Records are the source of truth. A profile and index are injected once per
conversation; verify details with a record before answering.

## When to use

- Search before answering about the user, their people, projects, preferences, or past decisions.
- Resolve nicknames and abbreviations through aliases.
- When the user says "remember this", propose a durable fact with `memory_note`.

## Tools

- `memory_search(query, limit?, type?)`: ranked keyword and alias matches.
- `memory_read(id_or_name)`: full record, including superseded facts and corrections.
- `memory_index(type?)`: ids, names, aliases, update dates, and fact counts.
- `memory_timeline(period?, date?, limit?)`: daily or weekly chronology.
- `memory_note(text, about?)`: append a proposal to the reconciler inbox.

There is no record-write tool. Do not create, edit, or delete vault records as part of a
conversation. The nightly reconciler owns those writes. `memory_note` does not immediately
change a record; do not claim that it does.

If `keep_holographic` is enabled, `fact_store` and `fact_feedback` operate on a separate
SQLite store. Hermes's built-in memory tool is also separate. They can provide immediate
memory, but do not directly edit the vault.

## Search by the user's words

The example vault is fictional. These queries illustrate alias retrieval:

```text
memory_search(query="jj")            -> PERS-june
memory_search(query="the pm")        -> PERS-june
memory_search(query="sam's coffee")  -> PREF-coffee
memory_search(query="api cutover")   -> WS-lighthouse
memory_search(query="the office")    -> ORG-tidewater-labs
```

After a match, use `memory_read(id_or_name="PERS-june")` for the full record.
A search miss does not establish that a fact is absent: synonyms outside the aliases will
not match. Inspect `memory_index`, then search a stored term or read the record directly.
Use `memory_timeline` for questions about what happened on a day or during a week.

## Record contract

```markdown
---
id: PERS-june
name: June
aliases:
- june
- jj
- the pm
type: person
created: '2026-08-02'
updated: '2026-08-02'
sources:
- chat:2026-08-02
---

# June

PM on Lighthouse.

## Facts
- (2026-08-02) PM for Lighthouse. Runs the Thursday sync.

## Links
- [[WS-lighthouse]]
- [[ORG-tidewater-labs]]
```

Ids use `<PREFIX>-<lowercase-kebab-slug>`. Prefixes are `PERS` (person), `PREF`
(preference), `ORG` (organization), and `WS` (workstream). Aliases must be lowercase.
Every fact bullet begins `- (YYYY-MM-DD)`. Links must resolve to existing records.

Corrections preserve history:

```markdown
- (2026-03-14) Oat flat white, extra hot. [!superseded 2026-09-26]
- (2026-09-26) Correction: Decaf oat flat white, extra hot. [corrects 2026-03-14]
```

The writer rejects deletion of dated facts, even with `force=True`. It validates the new
serialized content and enforces the body size cap. A structured plan that fails validation
is rejected before publication.

## Store and configuration

```text
$HERMES_HOME/memory-vault/
  PROFILE.md                      operator-maintained profile
  INDEX.md                        generated record index
  ONEPAGER.md, TASKS.md            optional semantic summaries; not injected
  records/<type>/<ID>.md           durable facts
  timeline/daily/<YYYY-MM-DD>.md   daily rollups
  timeline/weekly/<YYYY-Www>.md    weekly rollups
  raw/<YYYY-MM-DD>.jsonl           captured turns
  raw/inbox.jsonl                 pending proposals
  raw/inbox.processed.jsonl       archived, replayable proposals
  raw/memory_tool_writes.jsonl    built-in memory write notifications
```

`HERMES_HOME` defaults to `~/.hermes`. After the installer creates the activation alias,
set `memory.provider` to `instinct`. A manual copy without that alias uses `instinct-memory`.
Provider settings live under `plugins.instinct-memory`: `vault_path`, `profile_max_chars`,
`prefetch_records`, `max_record_chars`, and `keep_holographic`.

## Operator reconciliation

Run the reconciler when requested as an operator task; ordinary conversation proposals
should use `memory_note`.

```bash
# Offline preview: validate and build projections in a temporary copy.
"${HERMES_HOME:-$HOME/.hermes}/scripts/instinct_reconcile.sh" --no-llm --dry-run --verbose

# Preview including a Claude Code structured edit plan.
"${HERMES_HOME:-$HOME/.hermes}/scripts/instinct_reconcile.sh" --dry-run --verbose

# Full run for one day.
"${HERMES_HOME:-$HOME/.hermes}/scripts/instinct_reconcile.sh" --date 2026-09-26 --verbose
```

For a custom `vault_path`, set `INSTINCT_VAULT` to the same location. The script does not
read provider configuration. `INSTINCT_CLAUDE_BIN` can select the Claude Code executable.

The deterministic layer builds timelines, rebuilds the index, validates, and commits.
The semantic layer requests JSON with tools disabled, validates the plan in a copy,
applies atomic writes, and commits. A dry run leaves the target vault untouched.

Notes are archived only after committed work, and only for the selected snapshot/day.
`--no-llm` archives notes as timeline input without updating records. Archived notes remain
available to a later full run of the same date. Other dates and newly arriving notes remain
queued. A full run without a valid Claude plan retains pending notes.

Exit codes: `0` completed, `1` validation failure, `2` lock already held, `3` runtime error.
Review commits and record corrections after a run. Never rewrite published history.
