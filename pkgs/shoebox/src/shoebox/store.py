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


@dataclass
class Note:
    id: str
    title: str
    body: str
    cat: str
    date: str
    pinned: bool
    archived: bool
    had_h1: bool

    def public(self) -> dict:
        d = asdict(self)
        d.pop("had_h1")
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
        had_h1=had_h1,
    )


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    Same-directory is deliberate: the space is one filesystem, so os.replace is
    atomic, and a reader (SilverBullet's indexer, the agent, a git autosave)
    never sees a half-written page.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".shoebox-", suffix=".tmp")
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


def listing(space: Path) -> list[Note]:
    out: list[Note] = []
    for d in paths.CARD_DIRS:
        root = space / d
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            rel = paths.relative(space, p)
            if paths.in_scope(rel):
                out.append(read(space, rel))
    for f in paths.CARD_FILES:
        if (space / f).is_file():
            out.append(read(space, f))
    out.sort(key=lambda n: (n.date, n.id), reverse=True)
    return out
