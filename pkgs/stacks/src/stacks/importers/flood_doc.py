"""Parse the hand-maintained 'flood destroyed books' .docx.

This document is the only record of what the 2025 flood destroyed. It was
written by hand over months, in two passes that do not agree, and it encodes
meaning in text colour. Everything about it resists clean parsing, so this
module's job is **extraction with provenance**, not interpretation: every record
carries the raw line, its section, and why we classified it as we did, so a
human can adjudicate the ambiguous ones instead of trusting a guess.

Document structure, as measured:

* Paragraphs 0..330 — the **sorted half**: books grouped by reading level and
  series, annotated by hand. This is where the colour lives.
* Paragraph 331 — the divider ``*************Stuff from the pics****``.
* Paragraphs 331..716 — the **photos half**: titles transcribed from
  photographs taken of the damaged shelves. Cleaner, flatter, no colour at all.

Colour: ``0000ff`` (blue) appears on 29 runs, **all of them in the sorted
half**. Blue broadly means "this one is handled", but the mechanism varies and
must not be collapsed — observed variants include a replacement being found, one
being *ordered*, someone else offering their copy, and the book turning out not
to have been lost at all ("there but no pic"). Those are different facts. We
record the flag and the annotation and let a human decide.

``202124`` and ``333333`` are Google Docs' default near-black; they carry no
meaning and are treated as unstyled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET
import zipfile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: The only colour in the document that carries meaning.
BLUE = "0000ff"
#: Google Docs default text colours — noise, not signal.
NEUTRAL_COLORS = {"202124", "202122", "333333", None}

#: Paragraph index where the photo-transcribed section begins.
PICS_DIVIDER = re.compile(r"\*{4,}\s*stuff from the pics", re.I)

_DIVIDER = re.compile(r"^\**\s*\**$")
_GRADE = re.compile(r"\((\d)\)\s*$")
_PARENS = re.compile(r"\(([^)]*)\)")

#: Lines that name a group rather than a book.
_SERIES_HEADER = re.compile(
    r"(need titles|complete series|series\s*:|readers?\s*[:;-]\s*$|^\w[\w\s'.]{0,30}:\s*$)",
    re.I,
)
#: Section headings seen in the document.
_SECTION_WORDS = re.compile(
    r"^(readers all|readers|chapter books|storybooks|kindergarten|"
    r"(1st|2nd|3rd|4th|5th|6th)\s*grade|adult|non[- ]?fiction)\s*$",
    re.I,
)

#: Annotations that mean the book was NOT actually destroyed, or is already
#: resolved. Each is a different fact; we keep the phrase verbatim.
_RESOLUTION_HINTS = (
    ("there but", "not_lost"),
    ("no pic", "no_photo"),
    ("ordered", "ordered"),
    ("has it for you", "offered_by_other"),
    ("you can have", "offered_by_other"),
    ("have:", "partial_have_list"),
)


class LineKind(StrEnum):
    title = "title"
    series_header = "series_header"
    section_header = "section_header"
    divider = "divider"
    empty = "empty"


class Half(StrEnum):
    sorted_by_grade = "sorted_by_grade"
    photos = "photos"


@dataclass(slots=True)
class FloodRecord:
    index: int
    half: Half
    kind: LineKind
    raw: str
    title: str
    section: str | None = None
    series: str | None = None
    grade: int | None = None
    is_blue: bool = False
    resolutions: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    starred: bool = False

    @property
    def needs_review(self) -> bool:
        """Blue with an ambiguous mechanism, or a resolution that contradicts loss."""
        return bool(self.resolutions) or (self.is_blue and self.kind is not LineKind.title)


def _iter_paragraphs(path: Path) -> Iterator[dict]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    assert body is not None
    for p in body.iter(f"{W}p"):
        runs = []
        for r in p.iter(f"{W}r"):
            rpr = r.find(f"{W}rPr")
            color = None
            if rpr is not None:
                c = rpr.find(f"{W}color")
                if c is not None:
                    color = c.get(f"{W}val")
            text = "".join(t.text or "" for t in r.iter(f"{W}t"))
            if text:
                runs.append((text, color))
        yield {"text": "".join(t for t, _ in runs), "runs": runs}


def _clean_title(raw: str) -> str:
    """Strip trailing separators, grade markers and leading list junk."""
    t = raw.strip()
    t = t.lstrip("*").strip()
    t = _GRADE.sub("", t).strip()
    t = re.sub(r"^[-–—•\s]+", "", t)
    t = re.sub(r"[,;:\-\s]+$", "", t)
    return t.strip()


def parse(path: Path) -> list[FloodRecord]:
    paras = list(_iter_paragraphs(path))

    # Locate the photos divider rather than hardcoding an index — the document
    # is still being edited by hand and paragraph numbers will drift.
    pics_at = next(
        (i for i, p in enumerate(paras) if PICS_DIVIDER.search(p["text"])), len(paras)
    )

    records: list[FloodRecord] = []
    section: str | None = None
    series: str | None = None

    for i, p in enumerate(paras):
        raw = p["text"].strip()
        half = Half.sorted_by_grade if i < pics_at else Half.photos

        if not raw:
            continue
        if _DIVIDER.match(raw) or raw.count("*") >= 4:
            records.append(FloodRecord(i, half, LineKind.divider, raw, ""))
            series = None
            continue

        is_blue = any(c == BLUE for _, c in p["runs"] if _.strip())
        starred = raw.lstrip().startswith("*")

        annotations = [m.strip() for m in _PARENS.findall(raw) if m.strip()]
        resolutions = [
            label for phrase, label in _RESOLUTION_HINTS if phrase in raw.lower()
        ]

        gm = _GRADE.search(raw)
        grade = int(gm.group(1)) if gm else None

        if _SECTION_WORDS.match(raw):
            section = raw
            series = None
            records.append(
                FloodRecord(i, half, LineKind.section_header, raw, raw, section=section)
            )
            continue

        if _SERIES_HEADER.search(raw) and len(raw) < 140:
            series = _clean_title(re.split(r"[:;]", raw)[0])
            records.append(
                FloodRecord(
                    i, half, LineKind.series_header, raw, series,
                    section=section, series=series, is_blue=is_blue,
                    resolutions=resolutions, annotations=annotations,
                )
            )
            continue

        title = _clean_title(raw)
        if not title or len(title) < 2:
            continue

        records.append(
            FloodRecord(
                i, half, LineKind.title, raw, title,
                section=section, series=series, grade=grade,
                is_blue=is_blue, resolutions=resolutions,
                annotations=annotations, starred=starred,
            )
        )

    return records


def titles_only(records: list[FloodRecord]) -> list[FloodRecord]:
    return [r for r in records if r.kind is LineKind.title]
