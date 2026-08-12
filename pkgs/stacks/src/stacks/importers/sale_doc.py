"""Parse 'Books to look for at sale.docx' into standing want rules.

This document is not a wishlist of titles. It is 50 lines of hand-written
standing instructions, and its grammar — while inconsistent — is regular enough
to extract:

    Any books by Seymour Simon - science books        -> author rule
    DK books / Usborne / Landmark books               -> publisher rule
    Rangers apprentice:missing 10                     -> series, missing [10]
    Hardy boys:have; 1,2,3,4,6,10,12,...              -> series, have [1,2,...]
    Cornerstones of freedom: have 50: The Alamo, ...  -> series, have [titles]
    Addy Saves the Day by Connie Rose Porter (1994)   -> specific work

The have-lists are the valuable part and the reason a plain wishlist cannot do
this job: at a sale, seeing a Cornerstones of Freedom title is only useful if
you know which fifty are already on the shelf.

Classification is heuristic and **deliberately admits when it is unsure** —
every rule carries ``needs_review`` and its verbatim source line. Fifty lines is
small enough for a human to correct in a sitting, and a wrong rule that fires
silently at every sale is worse than one flagged for a look.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from stacks.models import WantKind
from stacks.normalize import normalize_title

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Imprints and publishers named in the document.
_PUBLISHERS = re.compile(
    r"\b(dk|usborne|landmark|christian liberty press|memoria press|time life|"
    r"scholastic|benchmark|golden book|abeka|pathway)\b",
    re.I,
)

#: "Any books by X", "X books", "X - <description>" all name a person.
_AUTHOR_PREFIX = re.compile(r"^(?:any\s+books?\s+by|books?\s+by)\s+(.+)$", re.I)
_AUTHOR_SUFFIX = re.compile(r"^(.+?)\s+books?\b", re.I)

#: A capitalised personal name: 1-4 words, allowing initials and particles.
_LOOKS_LIKE_NAME = re.compile(
    r"^[A-Z][\w'’.\-]*(?:\s+[A-Z]?[\w'’.\-]+){0,3}$"
)

_HAVE = re.compile(r"\b(?:already\s+have|have)\b\s*\d*\s*[:;]?", re.I)
_MISSING = re.compile(r"\bmissing\b\s*[:;]?", re.I)
_SUGGESTED = re.compile(r"suggested titles?\s*[:;]?", re.I)


@dataclass
class ParsedWant:
    label: str
    kind: WantKind
    raw: str
    index: int
    have: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    positions_have: list[int] = field(default_factory=list)
    positions_missing: list[int] = field(default_factory=list)
    needs_review: bool = False
    note: str | None = None


def _paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    assert body is not None
    out = []
    for p in body.iter(f"{W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{W}t"))
        if text.strip():
            out.append(text.strip())
    return out


def _split_list(blob: str) -> tuple[list[str], list[int]]:
    """Split a have/missing blob into labels and numeric positions."""
    labels: list[str] = []
    positions: list[int] = []
    # Strip a trailing parenthetical aside before splitting.
    blob = re.sub(r"\(([^)]*)\)\s*$", "", blob).strip()
    for part in re.split(r"[,;]", blob):
        part = part.strip(" .#")
        if not part:
            continue
        if re.fullmatch(r"#?\d{1,3}", part):
            positions.append(int(part.lstrip("#")))
        elif len(part) > 2:
            labels.append(part)
    return labels, positions


def _classify(head: str, raw: str) -> tuple[WantKind, bool]:
    """Guess what a line is about. Returns (kind, needs_review)."""
    if _PUBLISHERS.search(head):
        return WantKind.publisher, False

    m = _AUTHOR_PREFIX.match(head)
    if m:
        return WantKind.author, False

    # "by <Name>" naming a specific title
    if re.search(r"\bby\s+[A-Z]", raw):
        return WantKind.work, False

    # A bare personal name on its own line.
    stripped = _AUTHOR_SUFFIX.match(head)
    candidate = (stripped.group(1) if stripped else head).strip()
    if _LOOKS_LIKE_NAME.match(candidate) and len(candidate.split()) <= 4:
        # Ambiguous: "Magic School Bus" and "Tom Swift" look like names but are
        # series. Anything with have/missing data is a series; otherwise flag.
        if re.search(r"\b(have|missing)\b", raw, re.I):
            return WantKind.series, False
        return WantKind.author, True

    if re.search(r"\b(have|missing|series)\b", raw, re.I):
        return WantKind.series, False

    return WantKind.topic, True


#: Words the label picks up from the surrounding sentence but which are not
#: part of the series/author name: "Animal ark have", "Janette oke missing".
_LABEL_TAIL = re.compile(r"\s+(have|missing|books?|series)\s*$", re.I)


def _clean_label(label: str) -> str:
    # A trailing parenthetical is an aside, not part of the name:
    # "Nature's children or other animal books (have bighorn sheep, ...)".
    # An unclosed one means the head split landed mid-list; drop from the "(".
    label = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()
    if label.count("(") > label.count(")"):
        label = label[: label.index("(")].strip()

    prev = None
    while prev != label:
        prev = label
        label = _LABEL_TAIL.sub("", label).strip(" .,:;-")
    return label


def _join_continuations(paras: list[str]) -> list[str]:
    """Fold wrapped lines back into the rule they belong to.

    A paragraph beginning with a comma is a continuation — the Love Comes
    Softly entry spans two, and treating the second as its own rule invents a
    want named ", When Breaks the Dawn, ...".
    """
    out: list[str] = []
    for p in paras:
        if out and (p.startswith(",") or p.startswith(";")):
            out[-1] = out[-1].rstrip(" ,;") + ", " + p.lstrip(" ,;")
        else:
            out.append(p)
    return out


def parse(path: Path) -> list[ParsedWant]:
    out: list[ParsedWant] = []

    for i, raw in enumerate(_join_continuations(_paragraphs(path))):
        # The label is everything before the first ':' or ' - ' separator.
        head = re.split(r"\s*[:;]\s*|\s+-\s+", raw, maxsplit=1)[0].strip()
        if not head:
            continue

        have_labels: list[str] = []
        have_pos: list[int] = []
        miss_labels: list[str] = []
        miss_pos: list[int] = []

        # Everything after "missing" is a missing-list; after "have", a have-list.
        mm = _MISSING.search(raw)
        if mm:
            miss_labels, miss_pos = _split_list(raw[mm.end():])

        hm = _HAVE.search(raw)
        if hm:
            tail = raw[hm.end():]
            # A "suggested titles:" aside after a have-list names wants.
            sm = _SUGGESTED.search(tail)
            if sm:
                want_labels, _ = _split_list(tail[sm.end():])
                miss_labels.extend(want_labels)
                tail = tail[: sm.start()]
            if mm and mm.start() > hm.start():
                tail = tail[: mm.start() - hm.end()]
            have_labels, have_pos = _split_list(tail)

        kind, review = _classify(head, raw)

        m = _AUTHOR_PREFIX.match(head)
        label = _clean_label(m.group(1).strip() if m else head) or head

        out.append(
            ParsedWant(
                label=label[:300],
                kind=kind,
                raw=raw,
                index=i,
                have=have_labels,
                missing=miss_labels,
                positions_have=have_pos,
                positions_missing=miss_pos,
                needs_review=review,
            )
        )

    return out


def match_key_for(label: str) -> str:
    return normalize_title(label)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


@dataclass
class WantStats:
    rules: int = 0
    entries_have: int = 0
    entries_missing: int = 0
    linked_author: int = 0
    linked_series: int = 0
    needs_review: int = 0
    overridden: int = 0
    ignored: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        kinds = ", ".join(f"{k}:{v}" for k, v in sorted(self.by_kind.items()))
        return (
            f"{self.rules} want rules ({kinds}); "
            f"{self.entries_have} 'have' entries, {self.entries_missing} 'missing'; "
            f"linked {self.linked_author} authors / {self.linked_series} series; "
            f"{self.overridden} overridden, {self.ignored} ignored; "
            f"{self.needs_review} need review"
        )


#: Default location of the hand-curated classification corrections.
DEFAULT_OVERRIDES = Path(__file__).resolve().parents[3] / "data" / "want-overrides.toml"


def load_overrides(path: Path | None = None) -> dict[str, dict]:
    """Read the classification corrections, keyed by normalised label.

    Returns an empty mapping when the file is absent — the import must still
    work on a fresh checkout, just with more rules flagged for review.
    """
    import tomllib

    path = path or DEFAULT_OVERRIDES
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    return {r["match"]: r for r in doc.get("rule", []) if r.get("match")}


def load(session, path: Path, overrides_path: Path | None = None) -> WantStats:
    """Create want rules, linking to existing authors and series where possible."""
    from sqlalchemy import func, select

    from stacks.models import Author, Series, WantEntry, WantRule, WantSource
    from stacks.normalize import normalize_author

    stats = WantStats()
    overrides = load_overrides(overrides_path)

    for pw in parse(path):
        key = match_key_for(pw.label)

        ov = overrides.get(key)
        if ov:
            if ov.get("ignore"):
                stats.ignored += 1
                continue
            if ov.get("kind"):
                pw.kind = WantKind(ov["kind"])
            if ov.get("label"):
                pw.label = ov["label"]
                key = match_key_for(pw.label)
            pw.needs_review = False
            pw.note = ov.get("why")
            stats.overridden += 1
        rule = WantRule(
            kind=pw.kind,
            source=WantSource.sale_doc,
            label=pw.label,
            match_key=key,
            raw_text=pw.raw,
            needs_review=pw.needs_review,
            notes=pw.note,
        )

        if pw.kind is WantKind.author:
            sort_name = normalize_author(pw.label)
            author = session.scalar(select(Author).where(Author.sort_name == sort_name))
            if author is None:
                # Trigram fallback — the sale doc spells names loosely
                # ("Joanna spyri", "D 'aulaire", "Janette oke").
                sim = func.similarity(Author.sort_name, sort_name)
                row = session.execute(
                    select(Author, sim).where(sim > 0.55).order_by(sim.desc()).limit(1)
                ).first()
                author = row[0] if row else None
            if author is not None:
                rule.author_id = author.id
                stats.linked_author += 1

        elif pw.kind is WantKind.series:
            series = session.scalar(select(Series).where(Series.name.ilike(pw.label)))
            if series is None:
                sim = func.similarity(Series.name, pw.label)
                row = session.execute(
                    select(Series, sim).where(sim > 0.55).order_by(sim.desc()).limit(1)
                ).first()
                series = row[0] if row else None
            if series is not None:
                rule.series_id = series.id
                stats.linked_series += 1

        session.add(rule)
        session.flush()

        for label in pw.have:
            session.add(WantEntry(rule_id=rule.id, have=True, label=label[:300],
                                  match_key=match_key_for(label)))
            stats.entries_have += 1
        for pos in pw.positions_have:
            session.add(WantEntry(rule_id=rule.id, have=True, label=str(pos),
                                  match_key=str(pos), position=pos))
            stats.entries_have += 1
        for label in pw.missing:
            session.add(WantEntry(rule_id=rule.id, have=False, label=label[:300],
                                  match_key=match_key_for(label)))
            stats.entries_missing += 1
        for pos in pw.positions_missing:
            session.add(WantEntry(rule_id=rule.id, have=False, label=str(pos),
                                  match_key=str(pos), position=pos))
            stats.entries_missing += 1

        stats.rules += 1
        stats.by_kind[pw.kind.value] = stats.by_kind.get(pw.kind.value, 0) + 1
        if pw.needs_review:
            stats.needs_review += 1

    session.flush()
    return stats
