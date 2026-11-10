"""Who is who: sign-in identities, the people they belong to, and their names.

Three different strings get confused with each other the moment more than one
sign-in source exists, so they are kept apart here:

  * an **identity** is what an authentication source calls someone —
    ``ana``, ``ana@example.com``, ``github:aparker``. There can be
    several per person and they are not chosen by us.
  * a **person** is one canonical slug. This is the ONLY one that ever
    reaches a note: ``archived_by: ana``. It must be stable, because it is
    written across every file someone has touched — renaming yourself must
    never rewrite history.
  * a **display name** is a label. Free text, changeable on a whim, never
    stored in a note.

The mapping lives in a markdown table in the space, because this project has
no database and should not grow one for a lookup table. It is meant to be
edited by hand as readily as from the Users page::

    | Identity | Person | Display name | First seen |
    | --- | --- | --- | --- |
    | sam | sam | the owner | 2026-07-09 |
    | ana | ana | the other person Parker | 2026-11-09 |
    | github:aparker | ana |  | 2026-11-10 |

A row whose Person is its own Identity IS that person. A row pointing at
another row's Identity is an alias — that is how one human signs in with
Google on their phone and GitHub on a laptop and still archives a note once.
A row with an empty Person has been seen but not claimed, which is what the
Users page exists to resolve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

HEADERS = ("Identity", "Person", "Display name", "First seen")

# An identity is recorded as it came, lightly tidied: a provider prefix and an
# email domain are what tell two sign-ins apart, so unlike a person slug they
# must survive.
_IDENT_BAD = re.compile(r"[^a-z0-9_.:@+-]+")
# A person slug goes into frontmatter, so it stays narrow on purpose.
_SLUG_BAD = re.compile(r"[^a-z0-9_-]+")
_SLUG_OK = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

MAX_UNCLAIMED = 200     # a reachable port could otherwise grow this file forever


def ident(raw: str | None) -> str:
    """An identity string, as observed, reduced to something storable."""
    s = _IDENT_BAD.sub("-", (raw or "").strip().lower()).strip("-.:@+")
    return s[:128]


def slug(raw: str | None) -> str | None:
    """A person slug, or None. The local part of an email is the useful half."""
    s = (raw or "").strip().lower().split("@", 1)[0]
    s = _SLUG_BAD.sub("-", s).strip("-")[:32]
    return s if _SLUG_OK.match(s) else None


@dataclass
class Row:
    identity: str
    person: str = ""
    display: str = ""
    first_seen: str = ""


@dataclass
class People:
    rows: list[Row] = field(default_factory=list)
    prologue: str = ""        # whatever the file said before the table
    epilogue: str = ""

    # ---------------------------------------------------------------- lookup
    def _by_identity(self) -> dict[str, Row]:
        return {r.identity: r for r in self.rows}

    def canonical(self, raw: str | None) -> str | None:
        """The person slug for a sign-in identity.

        Unknown identities fall back to slugging the raw string, so the app
        works with an empty (or absent) people file exactly as it did before
        this existed.
        """
        i = ident(raw)
        if not i:
            return None
        row = self._by_identity().get(i)
        if row and row.person:
            return slug(row.person)
        return slug(raw)

    def display_for(self, person: str | None) -> str:
        """The label for a person slug; the slug itself when nothing is set."""
        p = (person or "").strip().lower()
        if not p:
            return ""
        by = self._by_identity()
        own = by.get(p)
        if own and own.display.strip():
            return own.display.strip()
        # An alias row may carry the name even when the canonical row does not
        # — several can, when each provider offered one. Prefer the fullest,
        # since "the other person Parker" from one directory beats "the other person S" from another,
        # and ties break on identity so the answer is at least stable.
        candidates = [r for r in self.rows
                      if r.person.strip().lower() == p and r.display.strip()]
        if candidates:
            best = max(candidates, key=lambda r: (len(r.display.strip()), r.identity))
            return best.display.strip()
        return p

    def people(self) -> list[str]:
        """Every canonical person slug, in a stable order."""
        out: list[str] = []
        for r in self.rows:
            s = slug(r.person) if r.person else None
            if s and s not in out:
                out.append(s)
        return out

    # ---------------------------------------------------------------- change
    def has(self, raw: str | None) -> bool:
        """Have we recorded this identity before?"""
        i = ident(raw)
        return bool(i) and i in self._by_identity()

    def observe(self, raw: str | None, today: str | None = None,
                known: set[str] | None = None) -> bool:
        """Record an identity we have not seen before. True when it was new.

        ``known`` is the set of people the app already has evidence for — a
        row here, or notes already attributed to them. An identity that lands
        on one of those is recorded as theirs, because there is nothing to
        ask: it is what the app would have done by fallback anyway. Anything
        else is left unclaimed, and the app asks whose it is on first load.
        """
        i = ident(raw)
        if not i or i in self._by_identity():
            return False
        if sum(1 for r in self.rows if not r.person) >= MAX_UNCLAIMED:
            return False
        s = slug(raw)
        self.rows.append(Row(
            identity=i,
            person=s if (s and known is not None and s in known) else "",
            first_seen=today or date.today().isoformat(),
        ))
        return True

    def assign(self, identity: str, person: str | None, display: str | None = None) -> None:
        """Point an identity at a person, and/or set that person's label."""
        i = ident(identity)
        by = self._by_identity()
        row = by.get(i)
        if row is None:
            row = Row(identity=i, first_seen=date.today().isoformat())
            self.rows.append(row)
        if person is not None:
            row.person = slug(person) or ""
        if display is not None:
            # A pipe would split the cell in two and silently eat the rest of
            # the row; nothing else in a name needs escaping.
            row.display = " ".join(display.replace("|", "/").split())[:60]


# ------------------------------------------------------------------ the file

_ROW = re.compile(r"^\s*\|(.*)\|\s*$")
_SEP = re.compile(r"^[\s|:-]+$")


def parse(text: str) -> People:
    """Read the first Identity-headed table, keeping the prose around it."""
    lines = text.splitlines()
    start = end = None
    for n, line in enumerate(lines):
        m = _ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if cells and cells[0].lower() == HEADERS[0].lower():
            start = n
            break
    if start is None:
        return People(prologue=text.rstrip("\n"))

    rows: list[Row] = []
    n = start + 1
    while n < len(lines):
        line = lines[n]
        m = _ROW.match(line)
        if not m:
            break
        if not _SEP.match(line):
            cells = [c.strip() for c in m.group(1).split("|")]
            cells += [""] * (4 - len(cells))
            if cells[0]:
                rows.append(Row(ident(cells[0]), (slug(cells[1]) or "") if cells[1] else "",
                                cells[2], cells[3]))
        n += 1
        end = n
    return People(
        rows=rows,
        prologue="\n".join(lines[:start]).rstrip("\n"),
        epilogue="\n".join(lines[end or n:]).strip("\n"),
    )


DEFAULT_PROLOGUE = """# People

Who is who. One row per sign-in identity.

**Person** is the name that gets written into notes (`archived_by: ana`), so
it stays the same even when someone's login changes. Point two identities at
the same person and they ARE the same person — that is how one human signs in
with Google on a phone and GitHub on a laptop and still archives a note once.

**Display name** is only a label. Change it freely; nothing depends on it.

A row with an empty Person has signed in but has not been claimed yet. Edit
this table by hand, or from Users in the side menu."""


def render(p: People) -> str:
    """The whole file, with the table regenerated and the prose left alone."""
    head = p.prologue.strip("\n") or DEFAULT_PROLOGUE
    out = [head, "", "| " + " | ".join(HEADERS) + " |",
           "| " + " | ".join("---" for _ in HEADERS) + " |"]
    for r in sorted(p.rows, key=lambda r: (r.person == "", r.person, r.identity)):
        out.append("| " + " | ".join([r.identity, r.person, r.display, r.first_seen]) + " |")
    if p.epilogue.strip():
        out += ["", p.epilogue.strip("\n")]
    return "\n".join(out) + "\n"


# -------------------------------------------------------- the file, on disk

# Cached on the file's own identity, like the note cache: FOUR parties write
# this space, so a timestamp of our own would not notice a hand edit.
_CACHE: tuple[int, int, People] | None = None


def path_for(space: Path, rel: str) -> Path:
    return space / rel


def load(space: Path, rel: str) -> People:
    """Read the people table, or an empty one. Never raises."""
    global _CACHE
    p = path_for(space, rel)
    try:
        st = p.stat()
    except OSError:
        return People()
    if _CACHE and _CACHE[0] == st.st_mtime_ns and _CACHE[1] == st.st_size:
        return _CACHE[2]
    try:
        parsed = parse(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return People()
    _CACHE = (st.st_mtime_ns, st.st_size, parsed)
    return parsed


def save(space: Path, rel: str, p: People) -> None:
    """Write it back, creating the file (and its directory) if need be."""
    global _CACHE
    target = path_for(space, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render(p), encoding="utf-8")
    tmp.replace(target)
    _CACHE = None
