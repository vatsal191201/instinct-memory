---
name: instinct-memory
description: Use when recalling durable memory. The vault is read-only.
version: 1.0.0
author: The instinct-memory authors
license: Apache-2.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [memory, instinct, vault, aliases, reconciler, read-only, markdown]
    category: productivity
    related_skills: [agent-memory-curation, hermes-agent]
---

# Instinct Memory

Durable memory as git-tracked markdown: interlinked records with dated facts, an injected
profile, and alias-driven keyword retrieval (no embeddings). Its defining property is that
**you read it, you never write it** — a nightly reconciler is the only writer. The records
are the source of truth; the injected profile is a summary of them.

## When to Use

- Before answering anything about the user, their people, projects, preferences, or past
  decisions — `memory_search` first, don't answer from the profile alone.
- When a nickname, abbreviation, or "who/what is X" comes up — resolve it against the vault.
- When the user says "remember this" or states something durable — propose it with
  `memory_note`.
- When operating or validating the store by hand — running the reconciler, adding a record,
  or correcting a fact.

For choosing *which* memory store a fact belongs in (flat memory vs fact store vs vault vs
skills), see the `agent-memory-curation` skill. This skill is about operating the vault.

## Overview — read-only to you, reconciler-owned

The agent surface is five tools, all read-only except a proposal queue:

- `memory_search` — alias-aware keyword recall; returns id, type, and best dated fact lines.
- `memory_read` — one full record: frontmatter, every fact (incl. superseded + corrections),
  links.
- `memory_index` — every record's id / name / aliases / updated / fact count.
- `memory_timeline` — daily / weekly rollups of what happened.
- `memory_note` — a **proposal** to the reconciler's inbox. Not a write.

You cannot create, edit, or delete a record through any tool. Durable writes happen only when
the reconciler runs (nightly via cron, or by hand). Trust the records over the profile: when
the injected summary and a record disagree, the record wins.

## Paths and layout

```
~/.hermes/memory-vault/            # the vault (git repo; $HERMES_HOME/memory-vault)
  PROFILE.md                       # injected at session start (life context / autonomy / style)
  INDEX.md                         # generated one-line-per-record index, injected with PROFILE
  ONEPAGER.md                      # "what's going on right now" — refreshed nightly
  TASKS.md                         # open loops — injected
  records/<type>/<ID>.md           # type ∈ preference | person | organization | workstream
  timeline/daily/<YYYY-MM-DD>.md   # reconciler rollups
  timeline/weekly/<YYYY-Www>.md
  raw/<YYYY-MM-DD>.jsonl           # append-only turn capture (reconciler input; never rewritten)
  raw/inbox.jsonl                  # memory_note proposals, consumed each reconcile
  raw/memory_tool_writes.jsonl     # mirror of built-in memory-tool writes
~/.hermes/plugins/instinct-memory/ # the MemoryProvider
~/.hermes/scripts/instinct_reconcile.sh   # reconciler (cron wrapper) + instinct_reconcile.py
```

Config lives in `~/.hermes/config.yaml`: `memory.provider: instinct-memory` (the activation
key is the plugin **directory** name), and `plugins.instinct-memory.*`
(`vault_path`, `profile_max_chars`, `prefetch_records`, `max_record_chars`, `keep_holographic`).
Inspect the vault directly with `read_file` / `search_files` against `records/`.

## The record contract

Frontmatter is YAML starting at byte 0, then a `# Name`, one-line prose, `## Facts`, `## Links`:

```markdown
---
id: PERS-june
name: June
aliases:
- june
- jj
- the pm
type: person
created: '2026-01-15'
updated: '2026-02-02'
sources:
- seed:memory
---

# June

One-line prose describing who/what this is.

## Facts
- (2026-01-15) A durable, dated fact.
- (2026-01-15) Old value [!superseded 2026-02-02]
- (2026-02-02) Correction: new value [corrects 2026-01-15]

## Links
- [[PERS-sam]]
- [[PREF-coffee]]
```

Rules enforced mechanically:

- **id = `<PREFIX>-<kebab-slug>`**, PREFIX ∈ `PREF` (preference), `PERS` (person),
  `ORG` (organization), `WS` (workstream). The prefix must match `type`.
- **aliases are lowercase** and are the single most important field — they are what makes
  keyword search find a record under the user's own wording.
- **every fact line is dated: `- (YYYY-MM-DD) text`.** An undated bullet fails validation.
- **corrections never delete.** The old line is annotated in place with `[!superseded <date>]`
  and a new dated line carries `Correction: … [corrects <old-date>]`.
- **body size is capped** (`max_record_chars`, ~8000). Over it, the reconciler shortens the
  record and links detail out — you cannot just grow one.

## Searching well — aliases are the point

Aliases exist so you can search the **user's own words**, not the formal name. Always try
nicknames, abbreviations, and alternative spellings, and let the alias resolve:

```
memory_search(query="the pm")            # -> PERS-june   (also "jj", "jj")
memory_search(query="the office")       # -> ORG-tidewater-labs    (also "work", "tidewater")
memory_search(query="api cutover")         # -> WS-lighthouse
memory_search(query="lighthouse")          # -> WS-lighthouse
```

Punctuation-heavy handles resolve too (`sam`, `june`, `lh`). Then
`memory_read(id_or_name="June")` for the full record.

**When a search comes back empty, do not conclude "not in memory."** Retrieval is keyword +
alias only (no embeddings), so a synonym the record doesn't list won't match. Call
`memory_index` to see every record with its aliases, find the right term, and search again —
or read the record directly. The empty-result hint says exactly this. Use `memory_timeline`
for "what did we do last week" chronology.

## memory_note vs the built-in memory tool vs fact_store

Three durable stores, routed by *when you need the fact back*, not by topic:

- **`memory_note(text, about?)`** — appends to `raw/inbox.jsonl` as a proposal. It does **not**
  take effect this turn and **cannot be read back** until the next reconcile. Use it for
  durable, long-term facts about the user's world (people, projects, preferences, decisions):
  *"remember that …"*.
- **built-in `memory` tool** — the always-on flat memory (injected every turn). Immediate and
  readable this session. Use it for something you need **right now** or standing operational
  state.
- **`fact_store`** (holographic delegate, kept working via `keep_holographic`) — structured
  SQLite facts with entity resolution and compositional retrieval (`probe`, `reason`,
  `contradict`). Immediate; a separate store from the vault. Rate facts with `fact_feedback`.

Rule of thumb: need it this turn → `memory` or `fact_store`; want it in the durable narrative
record → `memory_note`.

## Operating the reconciler

The reconciler is the only sanctioned writer. Two layers: **A** (deterministic — roll up raw
turns to daily/weekly, rebuild `INDEX.md`, validate, commit) and **B** (ask a coding CLI for a
structured edit plan, validate it against the contract, apply it through the vault's writer
functions — the model never touches a file directly).

```bash
# Validate only — fast, deterministic, no LLM. Use this to check the vault is well-formed.
~/.hermes/scripts/instinct_reconcile.sh --no-llm --dry-run --verbose

# Dry-run including the LLM plan (still writes nothing durable, makes no commit).
~/.hermes/scripts/instinct_reconcile.sh --dry-run --verbose

# Real run (the writer). Normally cron does this nightly; run by hand only deliberately.
~/.hermes/scripts/instinct_reconcile.sh

# Reconcile a specific day.
~/.hermes/scripts/instinct_reconcile.sh --date 2026-02-01
```

Exit codes: `0` ok, `1` validation problems, `2` nothing to do, `3` error. A `--dry-run`
regenerates the `INDEX.md`/timeline projections but makes no commit and edits no records.
Each real run is a git commit — `git -C ~/.hermes/memory-vault log --oneline` is the receipt.

**Adding a record safely.** You do not create records directly. Either `memory_note` the fact
(the reconciler creates or updates the record on its next run), or let a real reconcile
compose the `create`/`add_facts`/`link` ops from the day's activity. If an operator must create
one by hand, write `records/<type>/<PREFIX>-<slug>.md` to the contract above, then run
`instinct_reconcile.sh --no-llm --dry-run --verbose` and confirm `validation: 0 problems`
before committing — but prefer the reconciler ops over raw editing.

**Correcting a fact.** Never overwrite or delete a dated line. Supersede it and add a dated
correction — the reconciler's `correct_fact` op does this: it annotates the old line
`[!superseded <date>]` and appends `- (<date>) Correction: <new> [corrects <old-date>]`. The
writer **refuses** any write that drops a dated fact without a matching `[corrects <date>]`.

## Common Pitfalls

- **Hand-editing a record to reword or drop a dated fact without a `[corrects <date>]` line**
  breaks the correction/versioning invariant — the writer raises `RecordError` and rejects it.
  Add the correction line; do not route around the guard.
- **`force=True` is not an escape hatch.** It exists solely for the reconciler's sanctioned
  `shorten` op, which still keeps every superseded and correction line. Never use it to push a
  normal edit past the history guard.
- **A search miss is not "unknown."** The fact is probably stored under a different alias — use
  `memory_index`, then search the term you find.
- **`memory_note` is not immediate** — it won't read back this turn; it lands at the next
  reconcile. If you need the fact this session, also use the built-in `memory` tool.
- **Don't treat the profile as ground truth.** `PROFILE.md` / `ONEPAGER.md` / `INDEX.md` are
  generated summaries; verify at the record before acting on anything load-bearing.
- **Don't hand-edit the projections.** `INDEX.md`, `timeline/`, and `PROFILE.md` are
  regenerated by the reconciler; edits are lost. Change records, not projections.
- **Leave the git tree clean.** The vault is a git repo — commit or revert your changes, and
  never rewrite published history.

## Verification Checklist

- `memory_search` on a nickname the user actually uses returns the right record.
- If a search is empty, `memory_index` shows the record under some alias; a re-search on that
  alias hits.
- `instinct_reconcile.sh --no-llm --dry-run --verbose` exits `0` or `2` and prints
  `validation: 0 problems`.
- After a real reconcile, `git -C ~/.hermes/memory-vault log --oneline` shows a new commit.
- A fact proposed with `memory_note` appears as a record **only after** the next reconcile —
  not the same turn.
