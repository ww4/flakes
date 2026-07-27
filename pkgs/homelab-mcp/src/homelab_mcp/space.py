"""Reading, searching and writing the SilverBullet space.

Writes go straight to the filesystem. The `claude` user already holds recursive
POSIX ACLs on the space (see the `silverbullet` NixOS module), so there is no
credential to hold and no HTTP round-trip. That is deliberate: it means this
process never needs to reach SilverBullet's own API, which is authless and also
exposes `POST /.shell`.

It also means appends are a plain `open(path, "a")` — no read-modify-write, no
regex substitution, and therefore no way to reproduce the prior art's `$&`
corruption bug. The bug class is unrepresentable rather than merely avoided.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .paths import MARKDOWN_SUFFIX, resolve_read, resolve_write, slug_to_filename

__all__ = [
    "SearchHit",
    "SavedNote",
    "search_notes",
    "read_note",
    "save_note",
    "append_note",
    "slugify",
]

# Directories that are never searched or listed: git internals and
# SilverBullet's derived index.
SKIP_DIRS = {".git", ".silverbullet", "node_modules"}

# Cap per-file read during search so one pathological file cannot stall a scan.
MAX_SEARCH_FILE_BYTES = 512 * 1024

MAX_SEARCH_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 20
EXCERPT_RADIUS = 120

# Collision suffixes to try before giving up (`-2` … `-50`).
MAX_COLLISION_ATTEMPTS = 50


@dataclass(frozen=True)
class SearchHit:
    path: str
    title: str
    excerpt: str


@dataclass(frozen=True)
class SavedNote:
    path: str
    bytes_written: int


def slugify(title: str, max_len: int = 60) -> str:
    """Lowercase, non-alphanumerics collapsed to `-`, trimmed and truncated."""
    normalised = unicodedata.normalize("NFKD", title)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    slug = slug[:max_len].strip("-")
    return slug or "note"


def _iter_pages(space_root: Path):
    """Yield every markdown page in the space, skipping internals."""
    for path in space_root.rglob(f"*{MARKDOWN_SUFFIX}"):
        if any(part in SKIP_DIRS for part in path.relative_to(space_root).parts):
            continue
        if path.is_symlink() or not path.is_file():
            # Symlinks are skipped rather than followed: a link could point
            # outside the space, and listing is not worth the escape risk.
            continue
        yield path


def _title_of(path: Path, text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem


def _excerpt(text: str, at: int) -> str:
    start = max(0, at - EXCERPT_RADIUS // 2)
    end = min(len(text), at + EXCERPT_RADIUS)
    snippet = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def search_notes(space_root: Path, query: str, limit: int | None = None) -> list[SearchHit]:
    """Literal, case-insensitive, all-tokens-must-match search across the space.

    No caller-supplied regex, ever. The prior art accepted one and had an
    unbounded-regex DoS on a single-threaded event loop; the simplest way not
    to have that bug is not to offer the feature.
    """
    if not query or not query.strip():
        raise ValueError("query must not be empty")

    effective_limit = DEFAULT_SEARCH_LIMIT if limit is None else int(limit)
    effective_limit = max(1, min(effective_limit, MAX_SEARCH_LIMIT))

    tokens = [t.lower() for t in query.split() if t]
    hits: list[SearchHit] = []

    for path in _iter_pages(space_root):
        try:
            if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        haystack = text.lower()
        rel = path.relative_to(space_root).as_posix()
        rel_lower = rel.lower()

        # A token matches if it appears in the body OR in the page path, so
        # searching for a page by name works without knowing its contents.
        if not all(t in haystack or t in rel_lower for t in tokens):
            continue

        first = haystack.find(tokens[0])
        excerpt = _excerpt(text, first) if first >= 0 else _excerpt(text, 0)
        hits.append(SearchHit(path=rel, title=_title_of(path, text), excerpt=excerpt))

        if len(hits) >= effective_limit:
            break

    return hits


def read_note(space_root: Path, rel: str) -> str:
    """Return the full text of one page. Space-wide reads are permitted."""
    path = resolve_read(space_root, rel)
    if not path.is_file():
        raise FileNotFoundError(f"no such page: {rel}")
    if path.suffix != MARKDOWN_SUFFIX:
        raise ValueError("only markdown pages can be read")
    return path.read_text(encoding="utf-8", errors="replace")


def render_note(
    title: str,
    body: str,
    tags: list[str] | None = None,
    source_url: str | None = None,
    now: datetime | None = None,
) -> str:
    """Render a capture in the space's own style.

    Deliberately NOT YAML frontmatter: real pages in this space carry none
    (CONVENTIONS.md reserves it for imported Keep pages), and SilverBullet
    renders a frontmatter block as a visible metadata header. The house style
    is a `# Title` heading followed by an italic provenance line — see
    `Notes/Asterisk PBX.md`.

    No hard wrapping: SilverBullet renders every newline as a real line break,
    so the body is passed through untouched.
    """
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    provenance = f"*Captured from a Claude chat {stamp}."
    if source_url:
        provenance += f" Source: {source_url}"
    provenance += "*"

    parts = [f"# {title.strip()}", "", provenance, "", body.rstrip()]
    if tags:
        cleaned = [f"#{t.lstrip('#').strip()}" for t in tags if t and t.strip()]
        if cleaned:
            parts += ["", " ".join(cleaned)]
    return "\n".join(parts).rstrip() + "\n"


def save_note(
    space_root: Path,
    inbox_dir: str,
    title: str,
    body: str,
    tags: list[str] | None = None,
    source_url: str | None = None,
    now: datetime | None = None,
) -> SavedNote:
    """Write a NEW note to the inbox. Never overwrites — there is no such option.

    The no-overwrite guarantee is enforced by the OS: the file is created with
    `O_EXCL`, so a collision raises rather than clobbering. A check-then-write
    would be a time-of-check/time-of-use race; this is not.
    """
    if not title or not title.strip():
        raise ValueError("title must not be empty")

    date_prefix = (now or datetime.now()).strftime("%Y-%m-%d")
    slug = slugify(title)
    content = render_note(title, body, tags=tags, source_url=source_url, now=now)
    encoded = content.encode("utf-8")

    inbox_path = space_root / inbox_dir
    inbox_path.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, MAX_COLLISION_ATTEMPTS + 1):
        filename = slug_to_filename(date_prefix, slug, attempt)
        target = resolve_write(space_root, inbox_dir, f"{inbox_dir}/{filename}")
        try:
            with open(target, "xb") as handle:  # O_CREAT | O_EXCL — atomic
                handle.write(encoded)
        except FileExistsError:
            continue
        return SavedNote(
            path=target.relative_to(space_root).as_posix(),
            bytes_written=len(encoded),
        )

    raise RuntimeError(
        f"could not find a free filename for {date_prefix}-{slug} "
        f"after {MAX_COLLISION_ATTEMPTS} attempts"
    )


def append_note(space_root: Path, inbox_dir: str, rel: str, body: str) -> SavedNote:
    """Append to an existing inbox note.

    Plain concatenation with a separator. No regex substitution anywhere in
    this path, so a body containing `$&`, `$1`, `` $` `` or `$$` round-trips
    byte for byte.
    """
    if not body or not body.strip():
        raise ValueError("body must not be empty")

    target = resolve_write(space_root, inbox_dir, rel)
    if not target.is_file():
        raise FileNotFoundError(f"no such note: {rel}")

    chunk = ("\n" + body.rstrip() + "\n").encode("utf-8")
    with open(target, "ab") as handle:
        handle.write(chunk)

    return SavedNote(
        path=target.relative_to(space_root).as_posix(),
        bytes_written=len(chunk),
    )
