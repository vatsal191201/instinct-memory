"""Keyword retrieval over the instinct vault.

No embeddings, no vector index, no BM25 library — deliberately. Instinct retrieves by
keyword matching over files, and the aliases on each record are what make that work.
This module is the scoring half of that contract.

Scoring shape: each query token contributes ``weight * idf`` where the weight depends on
*where* it matched (alias > name > id > heading > fact > prose) and idf down-weights tokens
that appear in most records, so "the"/"and"-style noise from a long query cannot drown a
rare, high-signal token such as a project codename.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import Record

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'&.+-]*")

# Stopwords are intentionally tiny: pruning too hard is worse than one wasted scan,
# because an alias like "the pm" or "vp" is often exactly the high-signal token.
STOPWORDS = frozenset(
    """a an the and or but if then than that this these those is are was were be been being
    do does did doing have has had having i me my we our you your he she it they them their
    of in on at to for with from by as not no yes so very just about into over after before
    what when where which who whom how why can could should would will shall may might
    tell me give show find look up remember know think please""".split()
)

# Where a token matched -> its weight. Tuned against the seeded vault.
W_ALIAS = 6.0
W_ALIAS_EXACT = 9.0  # whole-query == alias, e.g. "the pm" or "jj"
W_NAME = 5.0
W_ID = 4.0
W_HEADING = 3.0
W_FACT = 2.0
W_PROSE = 1.0

DEFAULT_PREFETCH_RECORDS = 4
MAX_PREFETCH_CHARS = 2400


def tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) > 1]


def query_tokens(query: str, *, keep_stopwords: bool = False) -> List[str]:
    tokens = tokenize(query)
    if keep_stopwords:
        return tokens
    filtered = [t for t in tokens if t not in STOPWORDS]
    return filtered


def _fact_lines(rec: Record) -> List[str]:
    return [f.text.lower() for f in rec.facts]


def _headings(rec: Record) -> List[str]:
    return [line.lstrip("#").strip().lower() for line in rec.body.splitlines() if line.startswith("#")]


def _alias_terms(rec: Record) -> List[str]:
    return [a for a in rec.aliases if a]


def score(rec: Record, tokens: Sequence[str], *, idf: Optional[Dict[str, float]] = None) -> float:
    """Weighted keyword score for one record. Zero means no token matched anywhere."""
    if not tokens:
        return 0.0
    alias_terms = _alias_terms(rec)
    name_l = rec.name.lower()
    id_l = rec.id.lower()
    slug_l = id_l.split("-", 1)[-1].replace("-", " ")
    headings = " ".join(_headings(rec))
    fact_text = " ".join(_fact_lines(rec))
    prose = (rec.prose or "").lower()
    alias_blob = " ".join(alias_terms)

    total = 0.0
    for token in tokens:
        weight = 0.0
        if token in alias_terms:
            weight = W_ALIAS_EXACT
        elif alias_blob and re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", alias_blob):
            weight = W_ALIAS
        elif re.search(rf"(?<![a-z0-9]){re.escape(token)}", name_l):
            weight = W_NAME
        elif token in slug_l:
            weight = W_ID
        elif token in headings:
            weight = W_HEADING
        elif token in fact_text:
            weight = W_FACT
        elif token in prose:
            weight = W_PROSE
        if weight:
            total += weight * (idf.get(token, 1.0) if idf else 1.0)
    return total


def build_idf(records: Iterable[Record]) -> Dict[str, float]:
    """``log(1 + N/df)`` over the vault. Cosmetic at a few hundred records, decisive at
    the point where several records share a common word."""
    records = list(records)
    n = max(len(records), 1)
    df: Dict[str, int] = {}
    for rec in records:
        blob = " ".join(_alias_terms(rec)) + " " + rec.name.lower() + " " + " ".join(_fact_lines(rec)) + " " + (rec.prose or "").lower()
        for token in set(tokenize(blob)):
            df[token] = df.get(token, 0) + 1
    return {token: math.log(1 + n / count) for token, count in df.items()}


def best_fact_lines(rec: Record, tokens: Sequence[str], limit: int = 3) -> List[str]:
    """The record's most relevant fact lines, current facts first.

    Superseded lines are only surfaced when nothing current matches — showing a retracted
    fact next to its correction is fine, but showing it *instead* of the correction is not.
    """
    scored: List[Tuple[float, int, str, bool]] = []
    for idx, fact in enumerate(rec.facts):
        lower = fact.text.lower()
        hits = sum(1 for token in tokens if token in lower)
        if hits:
            scored.append((float(hits), -idx, fact.render(), fact.is_current()))
    current = [s for s in scored if s[3]]
    pool = current or scored
    pool.sort(reverse=True)
    return [line for _, _, line, _ in pool[:limit]]


def render_snippet(rec: Record, tokens: Sequence[str], *, max_lines: int = 3) -> str:
    """One record rendered for the prefetch context block."""
    header = f"### {rec.name} ({rec.id}, {rec.type})"
    lines = best_fact_lines(rec, tokens, limit=max_lines)
    if not lines and rec.prose:
        lines = [rec.prose]
    if rec.links:
        lines.append(f"links: {' '.join('[[' + l + ']]' for l in rec.links[:6])}")
    # Fact.render() already carries its own "- "; prose does not. Normalise once here.
    body = "\n".join("- " + (l[2:] if l.startswith("- ") else l) for l in lines)
    return header + "\n" + body


@dataclass
class Hit:
    record: Record
    score: float

    def to_dict(self, tokens: Sequence[str], max_lines: int = 3) -> Dict[str, object]:
        return {
            "id": self.record.id,
            "name": self.record.name,
            "type": self.record.type,
            "aliases": self.record.aliases,
            "score": round(self.score, 3),
            "lines": best_fact_lines(self.record, tokens, limit=max_lines),
            "path": self.record.path,
            "updated": self.record.updated,
        }


def search(
    records: Sequence[Record],
    query: str,
    *,
    limit: int = 8,
    record_type: Optional[str] = None,
    idf: Optional[Dict[str, float]] = None,
    min_score: float = 0.0,
) -> Tuple[List[Hit], List[str]]:
    """Rank records against ``query``. Returns ``(hits, tokens_used)``.

    A record with an empty Facts section is still a valid hit (it may be a stub the user
    asked about by name), but only when the name or an alias matched.
    """
    tokens = query_tokens(query)
    if not tokens:
        return [], []
    pool = [r for r in records if record_type is None or r.type == record_type]
    hits = [Hit(record=r, score=score(r, tokens, idf=idf)) for r in pool]
    hits = [h for h in hits if h.score > min_score]
    hits.sort(key=lambda h: (-h.score, h.record.id))
    return hits[: max(limit, 0)], tokens
