"""Record schema for the instinct memory vault.

The frontmatter contract, the id/alias rules, the dated-fact grammar, and the
validation pass. Everything here is pure (no I/O, no config) so the reconciler and
the provider share one definition of a well-formed record.

Design notes (from the Instinct teardown this implements):
  * facts are a bulleted list, each line dated — even though the container is markdown prose;
  * a correction never deletes history: the superseded line is annotated in place and a new
    dated line carries the correction;
  * aliases exist so keyword retrieval surfaces a record under many phrasings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

# type -> id prefix. Order is the canonical display order.
RECORD_TYPES: Dict[str, str] = {
    "preference": "PREF",
    "person": "PERS",
    "organization": "ORG",
    "workstream": "WS",
}
PREFIX_TO_TYPE: Dict[str, str] = {v: k for k, v in RECORD_TYPES.items()}

ID_RE = re.compile(r"^(PREF|PERS|ORG|WS)-[a-z0-9]+(?:-[a-z0-9]+)*$")
FACT_RE = re.compile(r"^-\s*\((\d{4}-\d{2}-\d{2})\)\s*(.+)$")
SUPERSEDED_RE = re.compile(r"\[!superseded\s+(\d{4}-\d{2}-\d{2})\]")
CORRECTS_RE = re.compile(r"\[corrects\s+(\d{4}-\d{2}-\d{2})\]")
LINK_RE = re.compile(r"\[\[([A-Za-z0-9_-]+)\]\]")

FACTS_HEADING = "## Facts"
LINKS_HEADING = "## Links"

DEFAULT_MAX_RECORD_CHARS = 8000
MIN_ALIAS_LEN = 2


class RecordError(ValueError):
    """Raised when a record cannot be parsed or would violate the contract."""


@dataclass
class Fact:
    """One dated bullet in a record's Facts section."""

    date: str
    text: str
    superseded_on: Optional[str] = None
    corrects_on: Optional[str] = None

    def render(self) -> str:
        return f"- ({self.date}) {self.text}"

    def is_current(self) -> bool:
        return self.superseded_on is None


@dataclass
class Record:
    """A single vault record: frontmatter plus body."""

    id: str
    name: str
    type: str
    aliases: List[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    sources: List[str] = field(default_factory=list)
    facts: List[Fact] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    prose: str = ""
    body: str = ""
    path: Optional[str] = None

    @property
    def prefix(self) -> str:
        return RECORD_TYPES.get(self.type, "PERS")

    def current_facts(self) -> List[Fact]:
        return [f for f in self.facts if f.is_current()]

    def all_terms(self) -> List[str]:
        """Every string a keyword search should be able to hit, lowercased."""
        terms = [self.name, self.id, self.id.split("-", 1)[-1].replace("-", " ")]
        terms.extend(self.aliases)
        for fact in self.facts:
            terms.append(fact.text)
        return [t.lower().strip() for t in terms if t and t.strip()]


# --------------------------------------------------------------------------- ids


def slugify(text: str) -> str:
    """Lowercase kebab slug; non-ascii collapses rather than raising."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-{2,}", "-", text).strip("-")


def make_id(record_type: str, name: str) -> str:
    if record_type not in RECORD_TYPES:
        raise RecordError(f"unknown record type: {record_type!r}")
    slug = slugify(name)
    if not slug:
        raise RecordError(f"cannot build an id from name {name!r}")
    return f"{RECORD_TYPES[record_type]}-{slug}"


def id_type(record_id: str) -> Optional[str]:
    if not record_id or "-" not in record_id:
        return None
    return PREFIX_TO_TYPE.get(record_id.split("-", 1)[0])


def normalise_aliases(name: str, aliases: Any) -> List[str]:
    """Lowercase, dedupe, drop the name's own lowercase form only if it adds nothing,
    and always guarantee the plain slug of the name is present."""
    out: List[str] = []
    seen = set()
    incoming = aliases if isinstance(aliases, (list, tuple)) else ([aliases] if aliases else [])
    for item in list(incoming) + [name]:
        if not isinstance(item, str):
            continue
        value = item.strip().lower()
        if len(value) < MIN_ALIAS_LEN or value in seen:
            continue
        seen.add(value)
        out.append(value)
    slug = slugify(name)
    if slug and slug.replace("-", " ") not in seen:
        out.append(slug.replace("-", " "))
    return out


# ------------------------------------------------------------------- frontmatter


def split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Return ``(frontmatter, body)``; raises RecordError when the block is absent."""
    if not text.startswith("---"):
        raise RecordError("missing frontmatter block (must start at byte 0 with '---')")
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        raise RecordError("unterminated frontmatter block")
    raw_fm = parts[0][3:].strip()
    body = parts[1].lstrip("-\n")
    try:
        try:
            import yaml
        except ImportError:
            from . import _frontmatter as yaml

        data = yaml.safe_load(raw_fm) or {}
    except Exception as exc:  # pragma: no cover - malformed yaml
        raise RecordError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RecordError("frontmatter must be a mapping")
    return data, body


def _section(body: str, heading: str) -> str:
    """Text under ``heading`` up to the next '## ' heading (or end of body).

    The heading must start its own line. Matching it as a bare substring (the previous
    behaviour) let a fact line or prose that merely *mentions* ``## Facts``/``## Links``
    re-anchor the section — silently dropping every fact, or hijacking the record's links
    with whatever ``[[...]]`` followed the mention. The end is already line-anchored via
    ``^##\\s``; the start now matches it.
    """
    match = re.search(r"^" + re.escape(heading), body, re.MULTILINE)
    if match is None:
        return ""
    rest = body[match.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def parse_facts(body: str) -> List[Fact]:
    facts: List[Fact] = []
    for line in _section(body, FACTS_HEADING).splitlines():
        line = line.strip()
        match = FACT_RE.match(line)
        if not match:
            continue
        text = match.group(2).strip()
        superseded = SUPERSEDED_RE.search(text)
        corrects = CORRECTS_RE.search(text)
        facts.append(
            Fact(
                date=match.group(1),
                text=text,
                superseded_on=superseded.group(1) if superseded else None,
                corrects_on=corrects.group(1) if corrects else None,
            )
        )
    return facts


def parse_links(body: str) -> List[str]:
    """Links from the ``## Links`` section, in order, deduped."""
    out: List[str] = []
    for link in LINK_RE.findall(_section(body, LINKS_HEADING)):
        if link not in out:
            out.append(link)
    return out


def parse_record(text: str, path: Optional[str] = None) -> Record:
    fm, body = split_frontmatter(text)
    record_id = str(fm.get("id", "")).strip()
    record_type = str(fm.get("type", "")).strip() or (id_type(record_id) or "")
    prose = ""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        prose = stripped
        break
    return Record(
        id=record_id,
        name=str(fm.get("name", "")).strip(),
        type=record_type,
        aliases=normalise_aliases(str(fm.get("name", "")).strip(), fm.get("aliases")),
        created=str(fm.get("created", "")).strip(),
        updated=str(fm.get("updated", "")).strip(),
        sources=[str(s) for s in (fm.get("sources") or [])] if isinstance(fm.get("sources"), (list, tuple)) else [],
        facts=parse_facts(body),
        links=parse_links(body),
        prose=prose,
        body=body,
        path=path,
    )


def render_record(rec: Record) -> str:
    """Serialise a Record back to markdown. Always emits the canonical shape so a
    rewrite of an untouched record is a no-op diff."""
    try:
        import yaml
    except ImportError:
        from . import _frontmatter as yaml

    fm = {
        "id": rec.id,
        "name": rec.name,
        "aliases": rec.aliases,
        "type": rec.type,
        "created": rec.created or date.today().isoformat(),
        "updated": rec.updated or date.today().isoformat(),
        "sources": rec.sources,
    }
    front = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True).strip()
    chunks = [f"---\n{front}\n---", "", f"# {rec.name}"]
    if rec.prose:
        chunks += ["", rec.prose]
    chunks += ["", FACTS_HEADING]
    for fact in rec.facts:
        chunks.append(fact.render())
    if not rec.facts:
        chunks.append("- (no facts recorded yet)")
    if rec.links:
        chunks += ["", LINKS_HEADING]
        chunks.extend(f"- [[{link}]]" for link in rec.links)
    return "\n".join(chunks).rstrip() + "\n"


# ------------------------------------------------------------------- validation


def validate_record(rec: Record, known_ids: Optional[set] = None) -> List[str]:
    """Hard-contract violations for one record. Empty list means clean.

    Deliberately strict exactly where history is at stake: an undated fact or a
    vanished superseded line destroys the correction/versioning invariant.
    """
    problems: List[str] = []
    if not ID_RE.match(rec.id or ""):
        problems.append(f"id {rec.id!r} does not match <PREFIX>-<kebab-slug>")
    elif id_type(rec.id) != rec.type:
        problems.append(f"id prefix {rec.id.split('-')[0]} does not match type {rec.type!r}")
    if rec.type not in RECORD_TYPES:
        problems.append(f"unknown type {rec.type!r}")
    if not rec.name:
        problems.append("missing name")
    if not rec.aliases:
        problems.append("no aliases (aliases are what make keyword retrieval work)")
    for alias in rec.aliases:
        if alias != alias.lower():
            problems.append(f"alias {alias!r} is not lowercase")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", rec.created or ""):
        problems.append(f"created {rec.created!r} is not YYYY-MM-DD")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", rec.updated or ""):
        problems.append(f"updated {rec.updated!r} is not YYYY-MM-DD")

    section = _section(rec.body, FACTS_HEADING)
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("-") and not FACT_RE.match(stripped):
            problems.append(f"undated fact line (must be '- (YYYY-MM-DD) text'): {stripped[:80]}")
        if SUPERSEDED_RE.search(stripped) and not CORRECTS_RE.search(" ".join(f.text for f in rec.facts)):
            if not any(f.corrects_on for f in rec.facts):
                problems.append("a fact is marked superseded but no correcting fact carries [corrects ...]")

    if known_ids is not None:
        for link in rec.links:
            if link not in known_ids:
                problems.append(f"dangling link [[{link}]] — no such record")
    return problems


def validate_vault(records: List[Record]) -> List[str]:
    """Vault-wide violations: duplicate ids, dangling links, oversize bodies."""
    problems: List[str] = []
    seen: Dict[str, str] = {}
    for rec in records:
        if rec.id in seen:
            problems.append(f"duplicate id {rec.id} in {rec.path} (also {seen[rec.id]})")
        else:
            seen[rec.id] = rec.path or "?"
    known = set(seen)
    for rec in records:
        problems.extend(f"{rec.id}: {p}" for p in validate_record(rec, known_ids=known))
    return problems


def build_fact(when: str, text: str) -> Fact:
    """Construct a Fact and derive its annotation flags from the text.

    Load-bearing: a Fact built here must be byte-identical to the same Fact re-parsed from
    disk. Constructing ``Fact(...)`` directly leaves ``superseded_on``/``corrects_on`` at
    None even when the text carries the annotation — which makes the vault's history guard
    reject its own sanctioned correction path.
    """
    text = (text or "").strip()
    superseded = SUPERSEDED_RE.search(text)
    corrects = CORRECTS_RE.search(text)
    return Fact(
        date=when,
        text=text,
        superseded_on=superseded.group(1) if superseded else None,
        corrects_on=corrects.group(1) if corrects else None,
    )


def parse_fact_line(line: str) -> Optional[Fact]:
    match = FACT_RE.match(line.strip())
    if not match:
        return None
    return build_fact(match.group(1), match.group(2))
