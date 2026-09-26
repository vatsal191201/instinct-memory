#!/usr/bin/env python3
"""Nightly reconciler for the instinct memory vault.

Two layers, matching the design of the system this implements (a background process owns
every durable write; the agent only ever reads):

  Layer A — deterministic, always runs, no LLM:
      raw JSONL -> daily rollup -> weekly rollup -> rebuild INDEX -> validate -> commit.
      Idempotent and safe to re-run.

  Layer B — semantic, best-effort, skipped by --no-llm:
      ask a coding CLI for a STRUCTURED EDIT PLAN (never free-form file contents), validate
      it against the record contract, then apply it through the vault's writer functions.
      The model never touches a file directly.

Ordering matters: the inbox is only truncated AFTER the commit succeeds, so a crash cannot
lose a note the agent asked us to remember.

Exit codes: 0 ok, 1 validation problems, 2 nothing to do, 3 error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
_BUNDLED_PLUGIN = Path(__file__).resolve().parents[1] / "plugin" / "instinct-memory"
PLUGIN_DIR = _BUNDLED_PLUGIN if _BUNDLED_PLUGIN.is_dir() else HERMES_HOME / "plugins" / "instinct-memory"
VAULT_ROOT = Path(os.environ.get("INSTINCT_VAULT") or HERMES_HOME / "memory-vault")
LOG_PATH = VAULT_ROOT / "raw" / "reconcile.log"

import importlib.util  # noqa: E402

_pkg_spec = importlib.util.spec_from_loader("instinctmem", loader=None, is_package=True)
_pkg = importlib.util.module_from_spec(_pkg_spec)
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules["instinctmem"] = _pkg
for _name in ("schema", "retrieval", "vault"):
    _spec = importlib.util.spec_from_file_location(f"instinctmem.{_name}", PLUGIN_DIR / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"instinctmem.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
    setattr(_pkg, _name, _mod)

from instinctmem import schema as sch  # noqa: E402
from instinctmem.retrieval import build_idf, query_tokens, score  # noqa: E402
from instinctmem.vault import Vault  # noqa: E402

VERBOSE = False
CLI_CANDIDATES = ("claude", "codex", "grok")


def log(msg: str, *, always: bool = False) -> None:
    if VERBOSE or always:
        print(msg, flush=True)


def log_error(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- Layer A


def notes_for_day(vault: Vault, day: str) -> List[Dict[str, Any]]:
    """Archived notes remain input so deterministic runs and later LLM replays agree."""
    return [n for name in ("inbox.processed.jsonl", "inbox.jsonl")
            for n in vault.read_jsonl(name) if str(n.get("ts", "")).startswith(day)]


def archive_notes(vault: Vault, day: str, notes: List[Dict[str, Any]]) -> None:
    # The caller has already committed the projections (and any semantic edits).
    consumed = vault.consume_jsonl("inbox.jsonl", entries=notes)
    if consumed:
        log(f"  archived {consumed} inbox note(s)")
        commit(vault, f"reconcile {day}: archive {consumed} consumed inbox note(s)")


def rollup_day(vault: Vault, day: str) -> Optional[Path]:
    """Turn one day's raw JSONL into a timeline/daily/<day>.md rollup."""
    turns = vault.read_raw(day)
    notes = notes_for_day(vault, day)
    memories = [
        m for m in vault.read_jsonl("memory_tool_writes.jsonl")
        if str(m.get("ts", "")).startswith(day)
    ]
    if not turns and not notes and not memories:
        return None

    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for turn in turns:
        sessions.setdefault(str(turn.get("session_id") or "unknown"), []).append(turn)

    lines = [f"# {day}", ""]
    lines.append(f"- turns: {len(turns)} across {len(sessions)} session(s)")
    if turns:
        stamps = sorted(str(t.get("ts", "")) for t in turns if t.get("ts"))
        if stamps:
            lines.append(f"- window: {stamps[0][11:19]} → {stamps[-1][11:19]} UTC")
    if notes:
        lines.append(f"- agent-flagged notes: {len(notes)}")
    if memories:
        lines.append(f"- memory-tool writes: {len(memories)}")
    lines.append("")

    for session_id, items in sorted(sessions.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"## {session_id[:24]} — {len(items)} turn(s)")
        lines.append("")
        for item in items[:40]:
            stamp = str(item.get("ts", ""))[11:16]
            user = str(item.get("user", "")).strip().replace("\n", " ")
            assistant = str(item.get("assistant", "")).strip().replace("\n", " ")
            if user:
                lines.append(f"- **{stamp}** {user[:300]}")
            # The assistant's reply carries what was DONE this day — without it the rollup
            # reads like a list of questions and the reconciler has nothing to work from.
            if assistant:
                lines.append(f"  - ↳ {assistant[:240]}")
            if not user and not assistant:
                lines.append(f"- **{stamp}** _(empty turn)_")
        if len(items) > 40:
            lines.append(f"- …{len(items) - 40} more turns")
        lines.append("")

    if notes:
        lines += ["## Flagged for memory", ""]
        for note in notes:
            about = f" (about: {note['about']})" if note.get("about") else ""
            lines.append(f"- {str(note.get('text', ''))[:400]}{about}")
        lines.append("")

    if memories:
        lines += ["## Memory tool writes", ""]
        for mem in memories:
            lines.append(f"- [{mem.get('action')}/{mem.get('target')}] {str(mem.get('content', ''))[:300]}")
        lines.append("")

    path = vault.layout.daily / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    vault._atomic_write(path, "\n".join(lines).rstrip() + "\n")
    log(f"  daily rollup: {path.name} ({len(turns)} turns)")
    return path


def iso_week(day: str) -> str:
    parsed = date.fromisoformat(day)
    year, week, _ = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def rollup_week(vault: Vault, week: str) -> Optional[Path]:
    """Roll that ISO week's dailies into one weekly file."""
    days = sorted(p.stem for p in vault.layout.daily.glob("*.md") if iso_week(p.stem) == week)
    if not days:
        return None
    lines = [f"# {week}", "", f"- days covered: {len(days)} ({days[0]} → {days[-1]})", ""]
    for day in days:
        text = (vault.layout.daily / f"{day}.md").read_text(encoding="utf-8")
        lines += [f"## {day}", ""]
        for line in text.splitlines()[1:]:
            if line.startswith("- **") or line.startswith("- turns") or line.startswith("- window"):
                lines.append("  " + line)
        bullet_notes = [l for l in text.splitlines() if l.startswith("## ") and "Flagged" in l]
        if bullet_notes:
            lines.append("  - flagged notes present")
        lines.append("")
    path = vault.layout.weekly / f"{week}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    vault._atomic_write(path, "\n".join(lines).rstrip() + "\n")
    log(f"  weekly rollup: {path.name} ({len(days)} days)")
    return path


def rebuild_index(vault: Vault) -> None:
    vault._atomic_write(vault.root / "INDEX.md", vault.render_index_markdown())
    log("  INDEX.md rebuilt")


def git(vault: Vault, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=instinct-memory", "-c",
         "user.email=instinct-memory@users.noreply.github.com", *args],
        cwd=vault.root, capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "instinct-memory",
             "GIT_AUTHOR_EMAIL": "instinct-memory@users.noreply.github.com",
             "GIT_COMMITTER_NAME": "instinct-memory",
             "GIT_COMMITTER_EMAIL": "instinct-memory@users.noreply.github.com"},
    )


def ensure_git(vault: Vault) -> None:
    if not (vault.root / ".git").is_dir():
        git(vault, "init", "-q", "-b", "main")
        git(vault, "config", "user.name", "instinct-memory")
        git(vault, "config", "user.email", "instinct-memory@users.noreply.github.com")

    ignore = vault.root / ".gitignore"
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    missing = [p for p in (".locks/", "*.lock", "raw/reconcile.log") if p not in existing.splitlines()]
    if missing:
        vault._atomic_write(ignore, existing.rstrip() + "\n" + "\n".join(missing) + "\n")


def commit(vault: Vault, message: str) -> bool:
    """Stage everything and commit. Returns True when a commit was actually made."""
    git(vault, "add", "-A")
    dirty = git(vault, "status", "--porcelain").stdout.strip()
    if not dirty:
        log("  nothing to commit")
        return False
    result = git(vault, "commit", "-q", "-m", message)
    if result.returncode != 0:
        combined = (result.stdout + result.stderr).strip()
        if "nothing to commit" in combined:
            return False
        raise RuntimeError(f"git commit failed: {combined[:400]}")
    head = git(vault, "log", "-1", "--oneline").stdout.strip()
    log(f"  committed: {head}", always=True)
    return True


# --------------------------------------------------------------------------- Layer B

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "record_edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["create", "add_facts", "correct_fact", "set_prose", "link", "shorten"],
                    },
                    "id": {"type": "string", "description": "Existing record id (required except for create)."},
                    "type": {"type": "string", "enum": ["preference", "person", "organization", "workstream"]},
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "prose": {"type": "string"},
                    "facts": {"type": "array", "items": {"type": "string"}, "description": "Fact text WITHOUT the date prefix."},
                    "old_text": {"type": "string", "description": "Substring identifying the fact being corrected."},
                    "new_text": {"type": "string"},
                    "links": {"type": "array", "items": {"type": "string"}},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["op"],
            },
        },
        "onepager": {"type": "string", "description": "Full replacement text for ONEPAGER.md, or empty to leave it."},
        "tasks": {"type": "string", "description": "Full replacement text for TASKS.md, or empty to leave it."},
        "notes": {"type": "string", "description": "One-line summary of what you changed."},
    },
    "required": ["record_edits"],
}

PROMPT_HEADER = """You are the reconciliation process for a personal memory vault of git-tracked markdown
records. You are NOT chatting with anyone. You produce a structured edit plan and nothing else.

THE CONTRACT (violating it corrupts the vault):
- Records have an id like PERS-june, a name, lowercase aliases, a type, dated facts, and [[links]].
- Facts are stored as bullets, each dated. In your JSON, `facts` entries are PLAIN TEXT —
  do NOT include the date prefix or a leading dash.
- NEVER delete a fact. To correct a fact, use op "correct_fact" with `old_text` (a substring
  of the existing fact) and `new_text` (the corrected statement). The old line gets annotated
  as superseded and a dated correction is added automatically.
- Aliases exist so keyword search finds the record under the user's own wording. When you
  create a record, write aliases for how a person would actually refer to it (nicknames,
  abbreviations, alternative spellings). This is the single most important field.
- Only record things that are DURABLE and about the user's people, projects, preferences,
  decisions, or ongoing work. Never record small talk, one-off questions, or anything the
  assistant itself said as if it were a user fact.
- If the day contains nothing durable, return `{"record_edits": []}`. That is a good answer.
- Prefer editing an existing record over creating a near-duplicate. Prefer adding a fact over
  rewriting prose.

TODAY'S DATE: __TODAY__

The user's current memory in full follows. Read it before deciding anything.
"""


def _render_prompt_header() -> str:
    # Deliberately replace() and not format(): the header embeds literal JSON braces.
    return PROMPT_HEADER.replace("__TODAY__", date.today().isoformat())


def _which(name: str) -> Optional[str]:
    from shutil import which

    return which(name, path=os.pathsep.join([os.environ.get("PATH", ""), str(Path.home() / ".local" / "bin")]))


def _llm_plan(prompt: str, *, timeout: int = 900) -> Tuple[Optional[Dict[str, Any]], str]:
    """Ask an available coding CLI for the edit plan. Returns (plan, backend_note)."""
    schema_json = json.dumps(PLAN_SCHEMA)
    attempts: List[str] = []

    if _which("claude"):
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--json-schema", schema_json,
            "--max-turns", "1",
            "--no-session-persistence",
        ]
        out, err = _exec_cli(cmd, timeout)
        plan = _parse_claude(out)
        if plan is not None:
            return plan, "claude"
        attempts.append(f"claude: {err[:200]}")

    if _which("codex"):
        # Codex has no --json-schema; ask for JSON in-band and extract the first object.
        cmd = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
               prompt + "\n\nReply with ONLY the JSON object. No prose, no code fences."]
        out, err = _exec_cli(cmd, timeout)
        plan = _extract_json(out)
        if plan is not None:
            return plan, "codex"
        attempts.append(f"codex: {err[:200]}")

    if _which("grok"):
        cmd = ["grok", "-p", prompt + "\n\nReply with ONLY the JSON object. No prose, no code fences.",
               "--output-format", "plain"]
        out, err = _exec_cli(cmd, timeout)
        plan = _extract_json(out)
        if plan is not None:
            return plan, "grok"
        attempts.append(f"grok: {err[:200]}")

    return None, "; ".join(attempts) if attempts else "no coding CLI found on PATH"


def _exec_cli(cmd: Sequence[str], timeout: int) -> Tuple[str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return "", f"timed out after {timeout}s"
    except OSError as exc:
        return "", str(exc)


def _looks_like_plan(payload: Any) -> bool:
    """A payload is only a plan if it actually carries the edit list.

    Without this, the CLI's own error envelope (e.g. {"result": "Not logged in"}) parses as
    valid JSON and gets applied as an empty plan — a silent no-op that looks like success.
    """
    return isinstance(payload, dict) and isinstance(payload.get("record_edits"), list)


def _parse_claude(stdout: str) -> Optional[Dict[str, Any]]:
    stdout = (stdout or "").strip()
    if not stdout:
        return None
    for candidate in (stdout, stdout.splitlines()[-1] if stdout.splitlines() else ""):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            if _looks_like_plan(payload.get("structured_output")):
                return payload["structured_output"]
            if _looks_like_plan(payload.get("result")):
                return payload["result"]
            if _looks_like_plan(payload):
                return payload
    extracted = _extract_json(stdout)
    return extracted if _looks_like_plan(extracted) else None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for idx in range(start, len(text)):
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(text[start : idx + 1])
                except json.JSONDecodeError:
                    return None
                return payload if isinstance(payload, dict) else None
    return None


def build_prompt(vault: Vault, day: str, *, max_records: int) -> Tuple[str, str]:
    """Assemble the bounded prompt package; also returns the record ids it included."""
    parts = [_render_prompt_header()]

    for filename in ("PROFILE.md", "ONEPAGER.md", "TASKS.md"):
        path = vault.root / filename
        if path.exists():
            parts.append(f"\n===== {filename} =====\n{path.read_text(encoding='utf-8')}")

    index_path = vault.root / "INDEX.md"
    if index_path.exists():
        parts.append(f"\n===== INDEX.md =====\n{index_path.read_text(encoding='utf-8')}")

    daily_path = vault.layout.daily / f"{day}.md"
    if daily_path.exists():
        parts.append(f"\n===== timeline/daily/{day}.md (TODAY) =====\n{daily_path.read_text(encoding='utf-8')}")
    else:
        parts.append(f"\n===== timeline/daily/{day}.md =====\n(no activity recorded)")

    notes = notes_for_day(vault, day)
    if notes:
        rendered = "\n".join(
            f"- {n.get('text')}" + (f"  [about: {n.get('about')}]" if n.get("about") else "") for n in notes
        )
        parts.append(f"\n===== NOTES THE AGENT FLAGGED FOR MEMORY =====\n{rendered}")

    # Include full bodies only for records the day plausibly touched.
    haystack = ""
    if daily_path.exists():
        haystack = daily_path.read_text(encoding="utf-8")
    haystack += " " + " ".join(str(n.get("text", "")) for n in notes)
    records = vault.load()
    tokens = query_tokens(haystack)
    idf = build_idf(records)
    ranked = sorted(records, key=lambda r: -score(r, tokens, idf=idf))
    touched = [r for r in ranked[:max_records] if score(r, tokens, idf=idf) > 0] or ranked[: min(10, len(ranked))]
    bodies = []
    for rec in touched:
        fact_lines = "\n".join(f.render() for f in rec.facts)
        bodies.append(
            f"----- {rec.id} ({rec.type}) name={rec.name!r} aliases={rec.aliases} "
            f"links={rec.links} updated={rec.updated}\nprose: {rec.prose}\nfacts:\n{fact_lines}"
        )
    parts.append(
        "\n===== CURRENT RECORDS THE DAY TOUCHED (full bodies) =====\n"
        + "\n\n".join(bodies)
        + "\n\n===== END =====\nProduce the JSON edit plan now."
    )
    return "\n".join(parts), [r.id for r in touched]


def apply_plan(vault: Vault, plan: Dict[str, Any], *, dry_run: bool) -> Tuple[int, List[str]]:
    applied, problems = 0, []
    edits = plan.get("record_edits")
    if not isinstance(edits, list):
        return 0, ["plan.record_edits is not a list"]

    for edit in edits:
        if not isinstance(edit, dict):
            problems.append("edit is not an object")
            continue
        op = edit.get("op")
        try:
            if op == "create":
                record_id = f"{sch.RECORD_TYPES[edit['type']]}-{sch.slugify(edit['name'])}"
                if vault.find(record_id) is not None:
                    problems.append(f"create {record_id}: already exists — skipped")
                    continue
                log(f"  [create] {record_id} aliases={edit.get('aliases')}")
                if not dry_run:
                    rec = vault.create(
                        edit["type"], edit["name"],
                        aliases=edit.get("aliases") or [],
                        prose=edit.get("prose", ""),
                        facts=[sch.build_fact(date.today().isoformat(), t) for t in edit.get("facts", [])],
                        links=edit.get("links") or [],
                        sources=edit.get("sources") or [f"reconcile:{date.today().isoformat()}"],
                    )
                    log(f"    -> {rec.id}")

            elif op == "add_facts":
                for text in edit.get("facts", []) or []:
                    text = str(text).strip()
                    if not text:
                        continue
                    log(f"  [add_fact] {edit.get('id')}: {text[:90]}")
                    if not dry_run:
                        vault.add_fact(edit["id"], text, source=f"reconcile:{date.today().isoformat()}")
            elif op == "correct_fact":
                log(f"  [correct] {edit.get('id')}: {str(edit.get('old_text'))[:60]!r} -> {str(edit.get('new_text'))[:60]!r}")
                if not dry_run:
                    vault.correct_fact(edit["id"], edit["old_text"], edit["new_text"])
            elif op in ("set_prose", "link", "shorten"):
                rec = vault.find(edit["id"])
                if rec is None:
                    problems.append(f"{op} {edit.get('id')}: no such record")
                    continue
                if op == "set_prose":
                    log(f"  [prose] {rec.id}")
                    if not dry_run:
                        rec.prose = str(edit.get("prose", "")).strip() or rec.prose
                        vault.write(rec)
                elif op == "link":
                    new_links = [l for l in (edit.get("links") or []) if l not in rec.links]
                    log(f"  [link] {rec.id} += {new_links}")
                    if not dry_run and new_links:
                        rec.links.extend(new_links)
                        vault.write(rec)
                else:  # shorten — sanctioned by the design, but history must survive
                    # The legacy operation may shorten prose, but cannot replace facts.
                    # The writer rejects any supplied facts that would erase history.
                    if not dry_run:
                        if edit.get("facts"):
                            rec.facts = [sch.build_fact(date.today().isoformat(), str(t).strip())
                                         for t in edit["facts"] if str(t).strip()]
                        if edit.get("prose"):
                            rec.prose = str(edit["prose"]).strip()
                        vault.write(rec)
            else:
                problems.append(f"unknown op {op!r}")
                continue
            applied += 1
        except (KeyError, sch.RecordError, ValueError) as exc:
            problems.append(f"{op} {edit.get('id') or edit.get('name')}: {exc}")

    if not dry_run:
        for filename, key in (("ONEPAGER.md", "onepager"), ("TASKS.md", "tasks")):
            text = plan.get(key)
            if isinstance(text, str) and text.strip():
                body = text if text.lstrip().startswith("#") else f"# {filename.split('.')[0].title()}\n\n{text}"
                vault._atomic_write(vault.root / filename, body.rstrip() + "\n")
                log(f"  refreshed {filename}")
    return applied, problems


# ------------------------------------------------------------------------------- main


def main() -> int:
    global VERBOSE, LOG_PATH
    parser = argparse.ArgumentParser(description="Reconcile the instinct memory vault.")
    parser.add_argument("--no-llm", action="store_true", help="Layer A only (deterministic).")
    parser.add_argument("--date", help="Day to reconcile (YYYY-MM-DD). Default: today.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--max-records", type=int, default=25, help="Record bodies included in the LLM prompt.")
    args = parser.parse_args()
    VERBOSE = args.verbose

    day = args.date or date.today().isoformat()
    try:
        date.fromisoformat(day)
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")
    if args.dry_run:
        # Build projections in a throwaway copy, leaving even absent vaults untouched.
        with tempfile.TemporaryDirectory(prefix="instinct-preview-") as temporary:
            preview = Path(temporary) / "vault"
            if VAULT_ROOT.exists():
                shutil.copytree(VAULT_ROOT, preview, ignore=shutil.ignore_patterns(".git", ".locks"))
            vault = Vault(preview)
            vault.ensure()
            old_log_path = LOG_PATH
            LOG_PATH = preview / "raw" / "reconcile.log"
            try:
                return _run(vault, day, args)
            finally:
                LOG_PATH = old_log_path
    vault = Vault(VAULT_ROOT)
    vault.ensure()

    with vault.lock("reconcile", blocking=False) as acquired:
        if not acquired:
            log("another reconcile run holds the lock — exiting", always=True)
            return 2
        return _run(vault, day, args)


def _run(vault: Vault, day: str, args: argparse.Namespace) -> int:
    log(f"reconcile {day} (dry_run={args.dry_run}, no_llm={args.no_llm})", always=True)

    raw_turns = vault.read_raw(day)
    pending_notes = [n for n in vault.read_jsonl("inbox.jsonl") if str(n.get("ts", "")).startswith(day)]
    if not raw_turns and not pending_notes:
        log("no raw activity for this day — validating the vault", always=True)

    # ---- Layer A -----------------------------------------------------------
    log("Layer A (deterministic):")
    daily = rollup_day(vault, day)
    if daily is not None:
        rollup_week(vault, iso_week(day))
    rebuild_index(vault)

    problems = vault.validate()
    if problems:
        for problem in problems:
            log(f"  ! {problem}", always=True)
        log("validation failed — not committing", always=True)
        return 1

    if not args.dry_run:
        ensure_git(vault)
        touched = [f"timeline/daily/{day}.md"] if daily else []
        commit(
            vault,
            f"reconcile {day}: deterministic layer\n\nraw turns: {len(raw_turns)}\nflagged notes: {len(pending_notes)}\n"
            + ("updated: " + ", ".join(touched) if touched else "no rollup written"),
        )

    if args.no_llm:
        log("Layer B skipped (--no-llm)", always=True)
        if not args.dry_run:
            archive_notes(vault, day, pending_notes)
        return 0

    # ---- Layer B -----------------------------------------------------------
    log("Layer B (semantic):")
    prompt, included = build_prompt(vault, day, max_records=args.max_records)
    log(f"  prompt={len(prompt)} chars, {len(included)} record bodies included")
    plan, backend = _llm_plan(prompt)
    if plan is None:
        log_error(f"no edit plan produced by any backend ({backend})")
        return 0  # Layer A already committed; a missing LLM must not fail the nightly job
        # NOTE: the inbox is deliberately NOT consumed here. The agent's flagged notes are
        # still unprocessed, so consuming them would silently drop what it asked us to keep.

    log(f"  backend={backend}  edits={len(plan.get('record_edits') or [])}")
    applied, apply_problems = apply_plan(vault, plan, dry_run=args.dry_run)
    for problem in apply_problems:
        log(f"  ! {problem}", always=True)
    log(f"  applied {applied} edit(s)")

    if args.dry_run:
        log("dry run — nothing written", always=True)
        return 0

    rebuild_index(vault)
    post = vault.validate()
    for problem in post:
        log(f"  ! post-apply: {problem}", always=True)

    if post or apply_problems:
        return 1

    note = str(plan.get("notes", "")).strip()[:200]
    commit(
        vault,
        f"reconcile {day}: semantic layer via {backend}\n\n{applied} edit(s) applied. {note}",
    )

    # Only now is it safe to consume the inbox: the work is committed.
    archive_notes(vault, day, pending_notes)

    return 1 if post else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never leave a half-run silent
        log_error(f"unhandled: {exc!r}")
        raise SystemExit(3)
