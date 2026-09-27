"""The instinct vault: a git-tracked directory of markdown records.

This is the storage layer. It owns paths, the id/alias index, atomic writes, and the
append-only raw ingest log. It deliberately does NOT own retrieval ranking (see
``retrieval.py``) or the provider lifecycle (see ``provider.py``).

Two invariants are enforced here rather than left to callers:

1. **Atomic writes.** Every record write goes to a temp file in the same directory and is
   then ``os.replace``d into place, under a per-record ``flock``. A crash mid-write leaves
   the previous record intact. This is the whole reason a markdown directory survives
   concurrent writers.
2. **No silent history loss.** Every dated fact must survive unchanged or with a
   superseded annotation and a dated correction. No caller can bypass this invariant.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import fcntl
import json
import logging
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from .schema import (
    DEFAULT_MAX_RECORD_CHARS,
    build_fact,
    RECORD_TYPES,
    Fact,
    Record,
    RecordError,
    id_type,
    parse_record,
    render_record,
    slugify,
    validate_record,
    validate_vault,
)

logger = logging.getLogger(__name__)

RAW_DIR = "raw"
TIMELINE_DIR = "timeline"
RECORDS_DIR = "records"

PROFILE_FILE = "PROFILE.md"
ONEPAGER_FILE = "ONEPAGER.md"
TASKS_FILE = "TASKS.md"
INDEX_FILE = "INDEX.md"


def today() -> str:
    return date.today().isoformat()


@dataclass
class JsonlReadResult:
    """One input scan: every nonblank physical line is valid or failed."""

    path: Path
    entries: List[Dict[str, Any]]
    errors: List[str]

    @property
    def nonblank(self) -> int:
        return len(self.entries) + len(self.errors)

    def summary(self) -> str:
        return (f"{self.path.name}: {self.nonblank} nonblank = "
                f"{len(self.entries)} valid + {len(self.errors)} failed")


class JsonlError(ValueError):
    """A JSONL read was incomplete; partial entries must not look like success."""

    def __init__(self, result: JsonlReadResult):
        self.result = result
        super().__init__(result.summary() + "\n" + "\n".join(result.errors))


def _jsonl_object(line: bytes) -> Dict[str, Any]:
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


@dataclass(frozen=True)
class VaultLayout:
    root: Path

    @property
    def records(self) -> Path:
        return self.root / RECORDS_DIR

    @property
    def raw(self) -> Path:
        return self.root / RAW_DIR

    @property
    def daily(self) -> Path:
        return self.root / TIMELINE_DIR / "daily"

    @property
    def weekly(self) -> Path:
        return self.root / TIMELINE_DIR / "weekly"

    @property
    def locks(self) -> Path:
        return self.root / ".locks"

    def type_dir(self, record_type: str) -> Path:
        return self.records / record_type


class Vault:
    """Read/write access to one instinct vault."""

    def __init__(self, root: Path, *, max_record_chars: int = DEFAULT_MAX_RECORD_CHARS, cache_ttl: float = 2.0):
        self.root = Path(os.path.expanduser(str(root)))
        self.layout = VaultLayout(self.root)
        self.max_record_chars = max_record_chars
        self._cache_ttl = cache_ttl
        self._cache: Optional[List[Record]] = None
        self._cache_at: float = 0.0
        self._stamp: Optional[Tuple] = None

    # ------------------------------------------------------------------ layout

    def ensure(self) -> None:
        """Create the skeleton. Safe and cheap to call repeatedly."""
        for path in [self.layout.records, self.layout.raw, self.layout.daily, self.layout.weekly, self.layout.locks]:
            path.mkdir(parents=True, exist_ok=True)
        for record_type in RECORD_TYPES:
            self.layout.type_dir(record_type).mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.layout.records.is_dir()

    def record_paths(self) -> List[Path]:
        if not self.layout.records.is_dir():
            return []
        return sorted(p for p in self.layout.records.glob("*/*.md") if p.is_file())

    def _stamp_now(self) -> Tuple:
        """Cheap change-detector: newest record mtime + file count. Avoids re-parsing on
        every turn while still noticing edits made by the reconciler."""
        newest = 0.0
        count = 0
        for path in self.record_paths():
            count += 1
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
        return (count, round(newest, 3))

    # ------------------------------------------------------------------- locks

    @contextlib.contextmanager
    def lock(self, name: str, *, blocking: bool = True) -> Iterator[bool]:
        """Advisory ``flock`` on ``.locks/<name>.lock``. Yields False when ``blocking=False``
        and the lock is already held (callers use that to skip duplicate work).
        All other acquisition errors propagate before entering the caller's body.
        """
        self.layout.locks.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        path = self.layout.locks / f"{safe}.lock"
        handle = open(path, "a+")
        acquired = False
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
                acquired = True
            except OSError as exc:
                if blocking or exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
            yield acquired
        finally:
            try:
                if acquired:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    # ------------------------------------------------------------------- reads

    def load(self, *, force: bool = False) -> List[Record]:
        """All records as **copies**, cached on ``(file count, newest mtime)``.

        Returning copies is load-bearing, not defensive style. If callers got the cached
        objects, the usual ``rec = vault.find(id); rec.facts = [...]; vault.write(rec)``
        would mutate the cache itself — and the history guard in ``write()`` compares against
        the previous record, so it would be comparing the record to ITSELF and never fire.
        That is a silent data-loss path, so reads are isolated and ``write()`` re-reads from
        disk independently (see ``_disk_record``).
        """
        return [copy.deepcopy(rec) for rec in self.load_refs(force=force)]

    def load_refs(self, *, force: bool = False) -> List[Record]:
        """The cached records WITHOUT copying — read-only consumers only (scoring, index
        rendering). Mutating anything returned here corrupts the cache for the whole process."""
        stamp = self._stamp_now()
        fresh = (time.monotonic() - self._cache_at) < self._cache_ttl
        if not force and self._cache is not None and fresh and stamp == self._stamp:
            return self._cache
        records: List[Record] = []
        for path in self.record_paths():
            try:
                records.append(parse_record(path.read_text(encoding="utf-8"), path=str(path)))
            except (RecordError, OSError) as exc:
                logger.warning("instinct: skipping unparseable record %s: %s", path, exc)
        self._cache = records
        self._cache_at = time.monotonic()
        self._stamp = stamp
        return records

    def _disk_record(self, record_id: str) -> Optional[Record]:
        """Authoritative parse straight off disk, bypassing the cache entirely.

        This is what ``write()`` checks against. Using ``find()`` here would let a caller that
        mutated a cached object walk straight past the history guard.
        """
        previous = None
        # Probe known paths directly: glob/is_file may hide inspection failures.
        # Only an absent path is absence; unreadable or corrupt data must stop writes.
        for record_type in RECORD_TYPES:
            path = self.layout.type_dir(record_type) / f"{record_id}.md"
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            text = path.read_text(encoding="utf-8")
            rec = parse_record(text, path=str(path))
            problems = validate_record(rec)
            if rec.id != record_id or rec.type != record_type:
                problems.append("path does not match record id/type")
            if previous is not None:
                problems.append("duplicate record id")
            if problems:
                raise RecordError(f"{path}: " + "; ".join(problems))
            previous = rec
        return previous

    def index(self) -> Dict[str, Record]:
        return {r.id: r for r in self.load_refs() if r.id}

    def alias_map(self) -> Dict[str, List[str]]:
        """alias -> record ids. One alias can legitimately point at several records."""
        out: Dict[str, List[str]] = {}
        for rec in self.load_refs():
            for alias in rec.aliases:
                out.setdefault(alias, []).append(rec.id)
            slug = rec.name.lower()
            out.setdefault(slug, []).append(rec.id)
        return out

    def find(self, id_or_name: str) -> Optional[Record]:
        """Resolve by exact id, then id case-insensitively, then name/alias."""
        if not id_or_name:
            return None
        needle = id_or_name.strip()
        records = self.load()
        for rec in records:
            if rec.id == needle:
                return rec
        low = needle.lower()
        for rec in records:
            if rec.id.lower() == low:
                return rec
        for rec in records:
            if rec.name.lower() == low:
                return rec
        matches = [rec for rec in records if low in rec.aliases]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return sorted(matches, key=lambda r: r.updated, reverse=True)[0]
        slug = slugify(needle)
        if slug:
            guess_type = id_type(needle.upper())
            for rec in records:
                if rec.id.endswith("-" + slug) and (guess_type is None or rec.type == guess_type):
                    return rec
        return None

    def read(self, id_or_name: str) -> Optional[Record]:
        return self.find(id_or_name)

    def render_index_markdown(self) -> str:
        """Compact one-line-per-record index. This is what gets injected, so it must stay
        dense: id, type, name, aliases."""
        records = sorted(self.load_refs(), key=lambda r: (r.type, r.id))
        lines = ["# Memory Index", ""]
        for record_type in RECORD_TYPES:
            group = [r for r in records if r.type == record_type]
            if not group:
                continue
            lines.append(f"## {record_type} ({len(group)})")
            for rec in group:
                aliases = ", ".join(a for a in rec.aliases if a != rec.name.lower())[:120]
                lines.append(f"- `{rec.id}` — {rec.name}" + (f" | aliases: {aliases}" if aliases else ""))
            lines.append("")
        if len(lines) == 2:
            lines.append("_No records yet._")
        return "\n".join(lines).rstrip() + "\n"

    def render_profile(self, *, max_chars: int = 24000) -> str:
        """The injected block: PROFILE.md, then INDEX.md, truncated from the index end."""
        self.ensure()
        profile_path = self.root / PROFILE_FILE
        profile = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""
        index = self.render_index_markdown()
        contract = (
            "## How to use this memory\n"
            "- This memory is **READ-ONLY** to you. A nightly process owns every write.\n"
            "- Before answering anything about the user, people, projects, or past decisions, "
            "call `memory_search` (by nickname too — aliases exist for that reason) or "
            "`memory_read`.\n"
            "- Do not guess from this profile alone: it is a summary, the records are the source of truth.\n"
            "- To flag something for durable memory, use `memory_note`. It is a proposal, not a write.\n"
        )
        header = "# Memory (instinct vault)\n"
        tail = "\n" + contract
        budget = max(max_chars - len(header) - len(tail), 200)
        body = profile + "\n" + index
        if len(body) > budget:
            body = body[:budget].rstrip() + "\n\n_(index truncated — use memory_index for the full list)_\n"
        return header + body + tail

    # ------------------------------------------------------------------ writes

    def path_for(self, rec: Record) -> Path:
        if rec.type not in RECORD_TYPES:
            raise RecordError(f"unknown record type {rec.type!r} for id {rec.id}")
        return self.layout.type_dir(rec.type) / f"{rec.id}.md"

    def _atomic_write(self, path: Path, text: Union[str, bytes]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".md")
        try:
            options = {} if isinstance(text, bytes) else {"encoding": "utf-8"}
            with os.fdopen(fd, "wb" if isinstance(text, bytes) else "w", **options) as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def write(self, rec: Record, *, force: bool = False) -> Path:
        """Persist a record atomically.

        Refuses to drop any dated fact line, including when ``force=True`` is supplied
        by an older caller. Corrections preserve and annotate the original line. Refuses to write a record whose body exceeds the size cap — the
        reconciler must shorten and link out instead, exactly like Instinct does.
        """
        rendered = render_record(rec)
        candidate = parse_record(rendered)
        problems = validate_record(candidate)
        if [f.render() for f in candidate.facts] != [f.render() for f in rec.facts]:
            problems.append("fact text must stay on a single dated line")
        if problems:
            raise RecordError(f"{rec.id}: " + "; ".join(problems))
        if len(candidate.body) > self.max_record_chars:
            raise RecordError(
                f"{rec.id}: body is {len(candidate.body)} chars (cap {self.max_record_chars}). "
                "Shorten it and push detail into a linked note instead of raising the cap."
            )
        rec.updated = today()
        rec.created = rec.created or today()
        path = self.path_for(rec)
        with self.lock(f"record-{rec.id}", blocking=True):
            previous = self._disk_record(rec.id)
            if previous is not None:
                new_lines = {f.render() for f in rec.facts}
                for old in previous.facts:
                    if old.render() in new_lines:
                        continue
                    # A correction may append an annotation to the exact old text;
                    # a correction date alone never authorizes dropping that text.
                    preserved = any(
                        f.superseded_on and not old.superseded_on
                        and f.render() == old.render() + f" [!superseded {f.superseded_on}]"
                        and any(c.corrects_on == old.date and c.date == f.superseded_on
                                for c in rec.facts)
                        for f in rec.facts
                    )
                    if not preserved:
                        raise RecordError(
                            f"{rec.id}: write would drop a dated fact from {old.date}. "
                            "Keep the old line, mark it superseded, and add a dated correction."
                        )
            self._atomic_write(path, render_record(rec))
        self._cache = None
        return path

    def create(
        self,
        record_type: str,
        name: str,
        *,
        aliases: Optional[Sequence[str]] = None,
        prose: str = "",
        facts: Optional[Sequence[Fact]] = None,
        links: Optional[Sequence[str]] = None,
        sources: Optional[Sequence[str]] = None,
    ) -> Record:
        from .schema import make_id, normalise_aliases

        record_id = make_id(record_type, name)
        record = Record(
            id=record_id,
            name=name,
            type=record_type,
            aliases=normalise_aliases(name, list(aliases or [])),
            created=today(),
            updated=today(),
            sources=list(sources or []),
            facts=list(facts or []),
            links=list(links or []),
            prose=prose,
        )
        self.write(record)
        return record

    def add_fact(self, id_or_name: str, text: str, *, when: Optional[str] = None, source: Optional[str] = None) -> Record:
        """Append a dated fact. This is the reconciler's normal path."""
        rec = self.find(id_or_name)
        if rec is None:
            raise RecordError(f"no such record: {id_or_name}")
        rec.facts.append(build_fact(when or today(), text))
        if source and source not in rec.sources:
            rec.sources.append(source)
        self.write(rec)
        return rec

    def correct_fact(
        self,
        id_or_name: str,
        old_text_substring: str,
        new_text: str,
        *,
        when: Optional[str] = None,
    ) -> Record:
        """The only sanctioned way to retract a fact: annotate the old line in place and add
        a dated correction that points back at it."""
        stamp = when or today()
        rec = self.find(id_or_name)
        if rec is None:
            raise RecordError(f"no such record: {id_or_name}")
        target = None
        for fact in rec.facts:
            if old_text_substring.lower() in fact.text.lower() and fact.superseded_on is None:
                target = fact
                break
        if target is None:
            raise RecordError(f"{rec.id}: no current fact matching {old_text_substring!r}")
        target.text = f"{target.text} [!superseded {stamp}]"
        target.superseded_on = stamp
        rec.facts.append(build_fact(stamp, f"Correction: {new_text} [corrects {target.date}]"))
        self.write(rec)
        return rec

    def validate(self) -> List[str]:
        records, problems = [], []
        for path in self.record_paths():
            try:
                rec = parse_record(path.read_text(encoding="utf-8"), path=str(path))
                records.append(rec)
                if path.stem != rec.id or path.parent.name != rec.type:
                    problems.append(f"{path.name}: path does not match record id/type")
                if len(rec.body) > self.max_record_chars:
                    problems.append(f"{rec.id}: body exceeds {self.max_record_chars} chars")
            except (RecordError, OSError) as exc:
                problems.append(f"{path.name}: {exc}")
        return problems + validate_vault(records)

    # -------------------------------------------------------------------- raw

    def append_raw(self, name: str, payload: Dict[str, Any]) -> None:
        """Append one JSON line under the same lock used by inbox consumption."""
        self.layout.raw.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        path = self.layout.raw / name
        with self.lock("raw-" + name.replace(".jsonl", ""), blocking=True):
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)

    def raw_daily_path(self, day: Optional[str] = None) -> Path:
        return self.layout.raw / f"{day or today()}.jsonl"

    def read_raw(self, day: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.read_jsonl(self.raw_daily_path(day).name)

    def read_jsonl(self, name: str) -> List[Dict[str, Any]]:
        """Read JSON objects, raising with line diagnostics on incomplete input."""
        result = self.inspect_jsonl(name)
        if result.errors:
            raise JsonlError(result)
        return result.entries

    def inspect_jsonl(self, name: str) -> JsonlReadResult:
        """Scan without changing bytes or silently replacing invalid UTF-8.

        Callers using this diagnostic API must check errors before processing entries.
        Missing files are empty inputs; other I/O failures propagate.
        """
        path = self.layout.raw / name
        result = JsonlReadResult(path, [], [])
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            if path.is_symlink():
                raise
            return result
        with handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    result.entries.append(_jsonl_object(line))
                except (UnicodeDecodeError, ValueError) as exc:
                    result.errors.append(f"{path}:{number}: {exc}")
        return result

    def consume_jsonl(
        self, name: str, *, archive_name: str = "inbox.processed.jsonl",
        entries: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> int:
        """Archive only committed snapshot entries, preserving later appends and other days.

        Call only after a successful commit. The same lock as append_raw covers the
        read/archive/rewrite. Malformed lines remain queued for operator inspection.
        """
        path = self.layout.raw / name
        wanted = None if entries is None else Counter(
            json.dumps(entry, sort_keys=True, ensure_ascii=False) for entry in entries
        )
        lock_name = "raw-" + name.replace(".jsonl", "")
        with self.lock(lock_name, blocking=True):
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                if path.is_symlink():
                    raise
                return 0
            consumed, remaining = [], []
            # Split only at physical LF boundaries; retain CRLF and undecodable bytes.
            lines = content.split(b"\n")
            for index, part in enumerate(lines):
                line = part + b"\n" if index < len(lines) - 1 else part
                try:
                    key = json.dumps(_jsonl_object(line), sort_keys=True, ensure_ascii=False)
                except (UnicodeDecodeError, ValueError):
                    remaining.append(line)
                    continue
                if wanted is None or wanted[key] > 0:
                    consumed.append(line if line.endswith(b"\n") else line + b"\n")
                    if wanted is not None:
                        wanted[key] -= 1
                else:
                    remaining.append(line)
            if not consumed:
                return 0
            with open(self.layout.raw / archive_name, "ab") as handle:
                handle.writelines(consumed)
                handle.flush()
                os.fsync(handle.fileno())
            self._atomic_write(path, b"".join(remaining))
        return len(consumed)

    # --------------------------------------------------------------- timeline

    def timeline_days(self, limit: int = 7) -> List[str]:
        if not self.layout.daily.is_dir():
            return []
        days = sorted((p.stem for p in self.layout.daily.glob("*.md")), reverse=True)
        return days[:limit]

    def read_timeline(self, period: str = "daily", *, day: Optional[str] = None, limit: int = 7) -> List[Dict[str, str]]:
        if period == "daily":
            if day:
                path = self.layout.daily / f"{day}.md"
                return [{"name": path.stem, "text": path.read_text(encoding="utf-8")}] if path.exists() else []
            out = []
            for stem in self.timeline_days(limit):
                path = self.layout.daily / f"{stem}.md"
                out.append({"name": stem, "text": path.read_text(encoding="utf-8", errors="replace")})
            return out
        directory = self.layout.weekly
        if not directory.is_dir():
            return []
        stems = sorted((p.stem for p in directory.glob("*.md")), reverse=True)[:limit]
        return [{"name": s, "text": (directory / f"{s}.md").read_text(encoding="utf-8", errors="replace")} for s in stems]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
