"""Reading and writing the card subset of the SilverBullet space.

Two rules shape everything here:

**Don't reformat Chris's files.** The space is edited by at least three other
writers (SilverBullet itself, the daybook, interactive agent sessions), and
539 of the Keep-import pages carry hand-written frontmatter. So frontmatter is
edited LINE BY LINE — the keys this app owns (``pinned``, ``archived``) are
replaced or appended in place and every other line is passed through verbatim.
A yaml.safe_load/safe_dump round trip would be shorter and would quietly
restyle quoting, key order and dates across 500+ files on first write.

**Deletion moves, it doesn't unlink.** ``trash`` relocates a note under
``Trash/`` so the UI's undo restores a real file, and a mis-tap is recoverable
without reaching for the space's git log.
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

from . import paths

FM_DELIM = "---"
H1 = re.compile(r"^#\s+(.*\S)\s*$")
LEADING_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}[ _-]+")
TRUE = {"true", "yes", "1", "on"}


# Authorship is recorded ONLY on the agent's own notes. Unlabelled means
# Chris's, which is the asymmetry that matters: his 553 notes keep their files
# byte-identical, anything created in this app is his by construction, and the
# agent is the one party that has to declare itself. A text heuristic was tried
# first and misfiled "Queue replies — gromit side" as his, which is exactly the
# kind of quiet wrongness a default filter must not have.
AGENT = "claude"
HUMAN = "chris"

# Enough to overflow a card at the widest column; the wall clips at ~184px.
PREVIEW_CHARS = 700


@dataclass
class Note:
    id: str
    title: str
    body: str
    cat: str
    date: str
    pinned: bool
    archived: bool
    author: str
    had_h1: bool

    def public(self) -> dict:
        d = asdict(self)
        d.pop("had_h1")
        return d

    def summary(self) -> dict:
        """What the wall needs: enough body to fill a card, and nothing more.

        A card clips at a fixed height, so shipping whole notes just to render
        previews cost 546 KB for 586 notes -- 422 KB of it body text the wall
        never shows, including one 49 KB page. The editor fetches the real body
        when a note is actually opened.
        """
        d = self.public()
        body = d.pop("body")
        d["preview"] = body[:PREVIEW_CHARS]
        d["lines"] = sum(1 for line in body.split("\n") if line.strip())
        d["truncated"] = len(body) > PREVIEW_CHARS
        return d


def _split_frontmatter(text: str) -> tuple[list[str], str]:
    """Return (frontmatter lines without delimiters, remaining body)."""
    if not text.startswith(FM_DELIM):
        return [], text
    lines = text.split("\n")
    if lines[0].strip() != FM_DELIM:
        return [], text
    for i in range(1, len(lines)):
        if lines[i].strip() == FM_DELIM:
            return lines[1:i], "\n".join(lines[i + 1:])
    return [], text  # unterminated: treat the whole thing as body


def _fm_get(fm: list[str], key: str) -> str | None:
    pat = re.compile(rf"^{re.escape(key)}\s*:\s*(.*)$", re.IGNORECASE)
    for line in fm:
        m = pat.match(line)
        if m:
            return m.group(1).strip().strip("'\"")
    return None


def _fm_set(fm: list[str], key: str, value: str | None) -> list[str]:
    """Replace, append, or (value=None) drop a single key, leaving the rest."""
    pat = re.compile(rf"^{re.escape(key)}\s*:", re.IGNORECASE)
    out = [line for line in fm if not pat.match(line)]
    if value is not None:
        out.append(f"{key}: {value}")
    return out


def _title_from_name(rel: str) -> str:
    stem = Path(rel).stem
    return LEADING_DATE.sub("", stem).strip() or stem


def _category(rel: str, fm: list[str]) -> str:
    explicit = _fm_get(fm, "category")
    if explicit:
        return explicit
    head = Path(rel).parts[0] if len(Path(rel).parts) > 1 else ""
    return head.lower() if head else "list"


def read(space: Path, rel: str, *, allow_trash: bool = False) -> Note:
    path = paths.resolve(space, rel, allow_trash=allow_trash)
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm, body = _split_frontmatter(raw)

    had_h1 = False
    title = _title_from_name(rel)
    stripped = body.lstrip("\n")
    first, _, rest = stripped.partition("\n")
    m = H1.match(first)
    if m:
        had_h1 = True
        title = m.group(1)
        body = rest

    when = _fm_get(fm, "date")
    if not when:
        when = date.fromtimestamp(path.stat().st_mtime).isoformat()

    return Note(
        id=rel,
        title=title,
        body=body.strip("\n"),
        cat=_category(rel, fm),
        date=when[:10],
        pinned=(_fm_get(fm, "pinned") or "").lower() in TRUE,
        archived=(_fm_get(fm, "archived") or "").lower() in TRUE,
        author=(_fm_get(fm, "author") or HUMAN).strip().lower(),
        had_h1=had_h1,
    )


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    Same-directory is deliberate: the space is one filesystem, so os.replace is
    atomic, and a reader (SilverBullet's indexer, the agent, a git autosave)
    never sees a half-written page.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".notes-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        else:
            os.chmod(tmp, 0o660)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write(
    space: Path,
    rel: str,
    *,
    title: str | None = None,
    body: str | None = None,
    pinned: bool | None = None,
    archived: bool | None = None,
    category: str | None = None,
) -> Note:
    path = paths.resolve(space, rel)
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm, rest = _split_frontmatter(raw)
    current = read(space, rel)

    new_title = current.title if title is None else title.strip()
    had_h1 = current.had_h1 or (new_title != _title_from_name(rel))

    if pinned is not None:
        fm = _fm_set(fm, "pinned", "true" if pinned else None)
    if archived is not None:
        fm = _fm_set(fm, "archived", "true" if archived else None)
    if category is not None:
        # Empty string clears it, which falls the note back to the directory
        # default rather than writing an empty key.
        clean = re.sub(r"[^A-Za-z0-9 _-]+", "", category).strip().lower()[:40]
        fm = _fm_set(fm, "category", clean.replace(" ", "-") or None)

    out: list[str] = []
    if fm:
        out.append(FM_DELIM)
        out.extend(fm)
        out.append(FM_DELIM)
        out.append("")
    if had_h1:
        out.append(f"# {new_title}")
        out.append("")
    new_body = current.body if body is None else body
    out.append(new_body.strip("\n"))
    _atomic_write(path, "\n".join(out).rstrip("\n") + "\n")
    return read(space, rel)


def _slug(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9 _-]+", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:60] or "note"


def create(space: Path, title: str, body: str) -> Note:
    stamp = datetime.now().strftime("%Y-%m-%d")
    base = f"Inbox/{stamp} {_slug(title or 'note')}"
    rel = f"{base}.md"
    n = 2
    while paths.resolve(space, rel).exists():
        rel = f"{base} ({n}).md"
        n += 1
    path = paths.resolve(space, rel)
    out = [f"# {title.strip() or 'Untitled'}", "", body.strip("\n")]
    _atomic_write(path, "\n".join(out).rstrip("\n") + "\n")
    return read(space, rel)


def trash(space: Path, rel: str) -> str:
    """Move a note under Trash/, preserving its path. Returns the new id."""
    src = paths.resolve(space, rel)
    dest_rel = f"{paths.TRASH_DIR}/{rel}"
    dest = paths.resolve(space, dest_rel, allow_trash=True)
    n = 2
    while dest.exists():
        dest_rel = f"{paths.TRASH_DIR}/{Path(rel).with_suffix('')} ({n}).md"
        dest = paths.resolve(space, dest_rel, allow_trash=True)
        n += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    return dest_rel


def restore(space: Path, trash_rel: str) -> str:
    """Move a trashed note back where it came from. Returns the restored id."""
    src = paths.resolve(space, trash_rel, allow_trash=True)
    parts = Path(trash_rel).parts
    if not parts or parts[0] != paths.TRASH_DIR:
        raise paths.PathError("not a trashed note")
    dest_rel = Path(*parts[1:]).as_posix()
    dest = paths.resolve(space, dest_rel)
    if dest.exists():
        raise paths.PathError("a note already lives at that path")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    return dest_rel


# Parsed notes, keyed by path and invalidated by (mtime_ns, size). FOUR parties
# write this space -- this service, SilverBullet, the daybook and interactive
# agent sessions -- so the key is the file's own identity rather than a
# timestamp of our own. A change by any of them invalidates the entry exactly
# as a change by this service does.
_CACHE: dict[str, tuple[int, int, Note]] = {}


def _cached(space: Path, rel: str, st: os.stat_result) -> Note:
    hit = _CACHE.get(rel)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    note = read(space, rel)
    _CACHE[rel] = (st.st_mtime_ns, st.st_size, note)
    return note


def _walk(space: Path):
    for d in paths.CARD_DIRS:
        root = space / d
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            rel = paths.relative(space, p)
            if paths.in_scope(rel):
                yield rel, p
    for f in paths.CARD_FILES:
        p = space / f
        if p.is_file():
            yield f, p


def _word(hay: str, term: str) -> bool:
    return re.search(r"\b" + re.escape(term) + r"\b", hay) is not None


# A term long enough that matching the FRONT of a category name is meaningful.
# "wood" -> woodgas is a real intent; "gas" -> gasket-anything is not, and a
# 3-letter prefix would drag whole categories in by accident.
CAT_PREFIX_MIN = 4


def split_query(query: str | None) -> tuple[list[str], list[str]]:
    """Split a raw query into (cat: filters, ordinary terms).

    ``cat:woodgas truck`` means "inside the woodgas category, anything about
    trucks" -- the cat: part is a filter, the rest is the usual ranked OR.
    """
    cats: list[str] = []
    terms: list[str] = []
    for tok in (query or "").lower().split():
        if tok.startswith("cat:") and len(tok) > 4:
            cats.append(tok[4:].strip())
        elif tok:
            terms.append(tok)
    return cats, terms


def _cat_terms(note: Note, needles: list[str]) -> set[str]:
    """Which query terms this note's CATEGORY answers.

    Chris: "if the category name is a high grade match for the search term you
    should pull in the whole category." So a term that names a category counts
    as satisfied for every note in it, even one whose text never says the word
    -- searching woodgas should return the woodgas notes, not just the ones
    that happen to spell it out.
    """
    cat = (note.cat or "").lower()
    if not cat:
        return set()
    parts = set(cat.split("-"))
    out = set()
    for t in needles:
        if t == cat or t in parts or (len(t) >= CAT_PREFIX_MIN and cat.startswith(t)):
            out.add(t)
    return out


def _score(note: Note, needles: list[str]) -> tuple[int, int, int]:
    """How well a note answers the query. Bigger is better.

    (terms matched anywhere, terms matched as whole words, terms in the title)

    Substring matching is kept -- it is what Keep does, and dropping it would
    lose real hits -- but on this corpus "gas" also matches gasket, gasoline
    and gasbuddy, and "wood" matches plywood, firewood and Woodstock. Measured
    on the live space, "wood gas" is 90 notes by substring and 41 by whole
    word. Rather than choose, rank: whole-word hits rise to the top and the
    in-word ones become a tail you never scroll to.
    """
    title = note.title.lower()
    hay = title + "\n" + note.body.lower()
    cats = _cat_terms(note, needles)
    # A category match is high-grade by definition, so it counts as a
    # whole-word hit rather than a weak substring one.
    anywhere = sum(1 for t in needles if t in hay or t in cats)
    whole = sum(1 for t in needles if _word(hay, t) or t in cats)
    in_title = sum(1 for t in needles if t in title)
    return anywhere, whole, in_title


def census(space: Path) -> dict[str, int]:
    """Every category and how many notes carry it, ignoring any filter.

    The chip row and the search suggestions both need the full set: filtering
    the census by the current query would make a category vanish from the
    suggestions exactly when you start typing its name.
    """
    out: dict[str, int] = {}
    for n in listing(space):
        out[n.cat] = out.get(n.cat, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def listing(space: Path, query: str | None = None) -> list[Note]:
    """All cards, newest first; with a query, only those that match.

    Search runs HERE, over the whole body, because the wall is only sent
    previews. Matching client-side against a truncated preview would miss hits
    inside long notes and present as "no results" -- a silent wrong answer
    rather than a slow one.
    """
    # ANY term, substring, ranked -- which is what Keep does, and what Chris
    # expects from it. Two earlier attempts were both wrong:
    #   literal phrase : "wood gas" found nothing, because no note contains
    #                    that exact run -- they say "woodgas", or have "wood"
    #                    on one line and "gas" on another;
    #   AND of terms   : still missed notes that say only "wood", or only
    #                    "gas", which Keep returns.
    # Keep's own behaviour is visible in a screenshot of it searching
    # "food lion": a note with neither word in full came back because "lion"
    # appears inside "million". So: substring, OR, and let the ranking decide
    # what rises -- a note matching both terms outranks one matching either.
    cat_filters, needles = split_query(query)
    out: list[Note] = []
    seen: set[str] = set()
    for rel, p in _walk(space):
        if rel in seen:
            continue
        seen.add(rel)
        try:
            st = p.stat()
        except OSError:
            continue
        note = _cached(space, rel, st)
        if cat_filters and (note.cat or "").lower() not in cat_filters:
            continue
        if needles and not _score(note, needles)[0]:
            continue
        out.append(note)
    for gone in set(_CACHE) - seen:
        _CACHE.pop(gone, None)
    if needles:  # cat:-only queries keep plain newest-first order
        # Most terms matched first, title hits ahead of body-only hits, then
        # newest. Without this, OR puts a note mentioning "gas" once above the
        # note actually called "Wood gas truck front end parts".
        out.sort(key=lambda n: (_score(n, needles), n.date, n.id), reverse=True)
    else:
        out.sort(key=lambda n: (n.date, n.id), reverse=True)
    return out
