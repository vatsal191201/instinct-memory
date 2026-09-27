# instinct-memory

A memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that stores durable facts in git-tracked Markdown records. Keyword and alias retrieval keep the store inspectable without embeddings; the agent proposes changes and a nightly reconciler writes them. The architecture is modeled on Instinct, an iMessage assistant, as described in [Dhravya Shah's teardown](https://x.com/dhravyashah/status/2101745550752428340).

![instinct-memory demo](docs/demo.gif)

[Full-quality video (22 s, 1080p)](docs/demo.mp4). Every screen in it is real output from this repo's code running against the fictional example vault in `examples/vault`.

## How it works

Records describe people, preferences, organizations, and workstreams. Each has a stable id, lowercase aliases, dated facts, and links to related records. Retrieval scores alias matches above names and body text. A profile and compact index are injected once per conversation and remain byte-stable for that session.

The agent can search and read records. `memory_note` appends a proposal to an inbox, while completed turns are captured as raw JSONL. The reconciler builds daily and weekly timelines, rebuilds the index, validates the vault, and commits. Its optional semantic layer asks Claude Code for a structured JSON edit plan, validates the resulting vault in a temporary copy, then applies changes through atomic writers and commits again. Corrections retain the old fact with a superseded marker and append a dated correction.

```text
PROFILE.md + records -> profile + index -> conversation
                    -> keyword/alias search -> read-only tools
conversation -> raw turns + proposed notes
                    -> nightly reconciler -> timelines + validated records -> git
```

`PROFILE.md` is maintained by the operator. The reconciler generates `INDEX.md` and timelines; its edit plan may also refresh `ONEPAGER.md` and `TASKS.md`. Those two files are available to the reconciler but are not injected by this provider.

## Record format

The included vault is entirely fictional: Sam, June, Tidewater Labs, and Lighthouse.

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

PM on Lighthouse, where Sam owns the API cutover.

## Facts
- (2026-08-02) PM for Lighthouse. Runs the Thursday sync.

## Links
- [[WS-lighthouse]]
- [[ORG-tidewater-labs]]
```

Prefixes are `PERS`, `PREF`, `ORG`, and `WS`. A correction looks like:

```markdown
- (2026-03-14) Oat flat white, extra hot. [!superseded 2026-09-26]
- (2026-09-26) Correction: Decaf oat flat white, extra hot. [corrects 2026-03-14]
```

## Install

Requires Python 3.9+, Git, and Linux or macOS (`flock` is used for locking). Hermes is required to run the provider, but not the reconciler or tests. When PyYAML is absent, a bundled parser supports the flat frontmatter format shown above; install PyYAML separately if you need more YAML syntax.

```bash
./install.sh
hermes config set memory.provider instinct
```

Restart Hermes after changing the provider. Installation is idempotent, uses no sudo, and copies the plugin, scripts, and skill into `${HERMES_HOME:-$HOME/.hermes}`. It leaves configuration and vault data untouched. To target another Hermes home, export `HERMES_HOME` before both commands.

Hermes discovers memory providers by directory name. The installer keeps `plugins/instinct-memory` and adds a relative `plugins/instinct` symlink so the command above works. If copying only `plugin/instinct-memory` by hand, activate it with `hermes config set memory.provider instinct-memory` instead.

The example vault is not installed into your memory store. To try the reconciler on a disposable copy:

```bash
example_dir=$(mktemp -d)
cp -R examples/vault "$example_dir/vault"
INSTINCT_VAULT="$example_dir/vault" ./scripts/instinct_reconcile.sh --no-llm --date 2026-09-26 --verbose
```

## Tools

| Tool | Behavior |
| --- | --- |
| `memory_search(query, limit?, type?)` | Keyword and alias search with ranked dated fact snippets. |
| `memory_read(id_or_name)` | Full record, including superseded facts and links. |
| `memory_index(type?)` | List ids, names, aliases, update dates, and fact counts. |
| `memory_timeline(period?, date?, limit?)` | Read daily or weekly rollups. |
| `memory_note(text, about?)` | Append a proposal to `raw/inbox.jsonl`; records stay unchanged. |
| `fact_store` | Passthrough to the separate holographic store when enabled and available. |
| `fact_feedback` | Passthrough to holographic fact feedback when enabled and available. |

The optional holographic delegate can write its own SQLite store. It has no record-write tool for this vault. A failed delegate leaves the vault tools usable. Hermes's built-in memory tool is separate from this provider.

## Configuration

Set these keys under `plugins.instinct-memory` in your Hermes configuration:

```yaml
plugins:
  instinct-memory:
    vault_path: "$HERMES_HOME/memory-vault"
    profile_max_chars: 24000
    keep_holographic: true
    prefetch_records: 4
    max_record_chars: 8000
```

| Key | Default | Meaning |
| --- | --- | --- |
| `vault_path` | `$HERMES_HOME/memory-vault` | Record store; supports `~`, `$HERMES_HOME`, and `${HERMES_HOME}`. |
| `profile_max_chars` | `24000` | Budget for the injected profile and index; the usage instructions are retained. |
| `keep_holographic` | `true` | Enable the optional holographic delegate. Set `false` for vault tools alone. |
| `prefetch_records` | `4` | Maximum records retrieved automatically for a turn. |
| `max_record_chars` | `8000` | Maximum record body size enforced by the writer. |

The reconciler uses `INSTINCT_VAULT`, defaulting to `$HERMES_HOME/memory-vault`; it does not read Hermes configuration. If you customize `vault_path`, set `INSTINCT_VAULT` to the same location for manual and cron runs.

## Run the reconciler

```bash
# Preview projections and a Claude edit plan in a temporary copy.
./scripts/instinct_reconcile.sh --dry-run --verbose

# Deterministic rollups, validation, git commits, and inbox archival; no LLM.
./scripts/instinct_reconcile.sh --no-llm --verbose

# Full reconciliation of a selected day.
./scripts/instinct_reconcile.sh --date 2026-09-26 --verbose
```

The scripts also work from `$HERMES_HOME/scripts` after installation. Use `--no-llm --dry-run --verbose` for an offline preview. A dry run leaves the target vault untouched.

The semantic layer requires an authenticated [Claude Code CLI](https://code.claude.com/docs/en/cli-reference). It resolves `claude` from `PATH` or `Path.home() / ".local/bin"`. Set `INSTINCT_CLAUDE_BIN` to an executable path to override discovery. Claude receives the bounded prompt package with file tools and MCP tools disabled; it returns JSON, and the Python writer applies the plan. No other LLM CLI is used.

Inbox entries for the selected day are archived only after their rollup or semantic work has committed. Other days and notes arriving during a run remain queued. `--no-llm` archives proposals as durable timeline input; it does not turn them into record facts. Archived proposals remain replayable with a later full `--date` run. If Claude is unavailable or returns no plan during a full run, deterministic work is kept and pending notes remain queued.

Before processing, the reconciler scans the selected day's raw turns, inbox, processed inbox, and memory-tool log. It reports per-file and total nonblank line counts as `valid + failed`; these are input validation counts, including archived and other-day entries, not counts of new facts. Malformed JSON, invalid UTF-8, and non-object JSON values produce file/line diagnostics on stderr and exit `1`, before any projections, commits, model calls, or inbox consumption. Input bytes are preserved; there is no automatic repair. Valid runs use the checked snapshot, leaving later appends for a subsequent run. This replaces the earlier behavior that silently skipped malformed lines and returned success.

Record writes stop if an existing record cannot be inspected, parsed, or validated. Storage writers also stop on lock acquisition errors; only explicit nonblocking contention is a normal skip.

All generated commits use `instinct-memory <instinct-memory@users.noreply.github.com>`. There is no push step. Exit codes: `0` completed (including deterministic-only success when Claude produces no plan), `1` validation failure or incomplete JSONL input, `2` another reconciler holds the lock (also used by argparse for invalid arguments), `3` runtime error, including I/O or lock system failures.

For a nightly run at 22:00 in the cron daemon's timezone:

```cron
0 22 * * * "$HOME/.hermes/scripts/instinct_reconcile.sh"
```

For a custom installation, set `HERMES_HOME` and, when needed, `INSTINCT_VAULT` in the crontab. A run processes its selected calendar day; use `--date` to catch up a missed day or process activity captured after that day's scheduled run.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use stdlib `unittest`, copy the fictional vault into a temporary directory for every test, and need no Hermes installation, network, or LLM. Missing Hermes interfaces are supplied by a small test shim. Tests cover parsing, retrieval, provider tools, correction history, invalid plans, and deterministic reconciliation with real temporary git repositories.

## Limitations

- Designed for one user's vault on a local filesystem. There is no multi-user authorization model.
- Keyword retrieval misses synonyms absent from aliases. Use `memory_index` to discover the stored terms.
- The LLM layer needs Claude Code authentication and access to its service. Semantic judgments still need review; git records what changed.
- Atomicity is per file. A machine failure during a multi-record publish can leave a partial uncommitted set of changes. Review the git diff before retrying. A crash during archive append/truncate can duplicate a proposal; its committed copy remains recoverable.
- Raw logs and history grow without automatic retention. The raw logs contain captured conversation text.
- Large vaults eventually exceed the prompt and per-record budgets. The writer refuses edits that drop facts or exceed the body cap.

## License

[Apache-2.0](LICENSE). Copyright 2026 The instinct-memory authors.
