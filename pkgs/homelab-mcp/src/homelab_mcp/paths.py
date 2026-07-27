"""Path scoping — the single most important module in this project.

EVERY caller-supplied path passes through here. Reads may address anything
inside the space; writes may address only flat `.md` files directly inside the
inbox folder. Nothing else is reachable, ever.

Design notes:

* We validate the **decoded, normalised** form. Percent-encoding the path before
  handing it to a filesystem call is not validation — `%2e%2e%2f` is `../` once
  something decodes it. Since a legitimate note path never contains
  percent-encoding, we decode once and reject outright if decoding changed the
  string, rather than trying to reason about how many times to decode.
* Containment is checked on the **resolved** path, so a symlink inside the space
  pointing at `/etc` cannot be used to escape: `Path.resolve()` follows it and
  the containment check then fails.
* `..` is rejected at the segment level *before* resolution, so we never depend
  on resolution alone to catch traversal.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import unquote

__all__ = ["PathRejected", "resolve_read", "resolve_write", "slug_to_filename"]

# Long enough for any real note name, short enough to stay clear of PATH_MAX
# once joined to the space root.
MAX_PATH_LEN = 400

MARKDOWN_SUFFIX = ".md"


class PathRejected(ValueError):
    """A caller-supplied path failed validation. Never partially applied."""


def _reject(reason: str) -> None:
    # Deliberately does not echo the offending path back: it is attacker
    # controlled and the message may end up in a log or a model context.
    raise PathRejected(f"path rejected: {reason}")


def _normalise(rel: str) -> PurePosixPath:
    """Validate and normalise a caller-supplied relative path.

    Returns a clean POSIX path with no traversal, or raises PathRejected.
    """
    if not isinstance(rel, str):
        _reject("not a string")
    if not rel or not rel.strip():
        _reject("empty")
    if len(rel) > MAX_PATH_LEN:
        _reject("too long")
    if "\x00" in rel:
        _reject("null byte")

    decoded = unquote(rel)
    if decoded != rel:
        # Legitimate note paths carry no percent-encoding. Anything that
        # changes under decoding is either an attack or a client bug; either
        # way we refuse rather than guess at the intended meaning.
        _reject("percent-encoding is not accepted")
    if "\x00" in decoded:
        _reject("null byte")
    if "\\" in decoded:
        # Backslash is a legal filename character on POSIX but is only ever
        # seen here as a Windows-style traversal attempt.
        _reject("backslash")
    if decoded.startswith("/") or decoded.startswith("~"):
        _reject("absolute path")

    pure = PurePosixPath(decoded)
    if pure.is_absolute():
        _reject("absolute path")

    for part in pure.parts:
        if part == "..":
            _reject("parent-directory segment")
        if part == ".":
            _reject("current-directory segment")
        if part.strip() != part:
            _reject("leading or trailing whitespace in a path segment")
        if part in ("", "/"):
            _reject("empty path segment")

    if not pure.parts:
        _reject("empty after normalisation")

    return pure


def _contained(root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and assert it stays inside `root`.

    `strict=False` so this works for files that do not exist yet (a new note).
    Symlink escapes are caught here, after resolution.
    """
    root_resolved = root.resolve()
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        _reject("resolves outside the permitted root")
    return resolved


def resolve_read(space_root: Path, rel: str) -> Path:
    """Resolve a read target. Anywhere inside the space is permitted."""
    pure = _normalise(rel)
    return _contained(space_root, space_root / pure)


def resolve_write(space_root: Path, inbox_dir: str, rel: str) -> Path:
    """Resolve a write target.

    Writes are permitted **only** for flat `.md` files directly inside the
    inbox folder. Not nested paths, not other folders, not SilverBullet config
    pages, not `Library/`.

    The `Inbox/` prefix is **required**, not optional. Accepting a bare
    `CONFIG.md` would mean silently reinterpreting "the space's config page"
    as "a new inbox note called CONFIG.md" — one string with two readings, and
    the safe reading chosen by luck rather than by contract. Requiring the
    prefix makes every write path mean exactly one thing.
    """
    pure = _normalise(rel)

    parts = pure.parts
    if parts[0] != inbox_dir:
        _reject(f"writes must be addressed as {inbox_dir}/<name>.md")
    parts = parts[1:]
    if len(parts) != 1:
        _reject(f"writes are limited to flat files directly inside {inbox_dir}/")

    name = parts[0]
    if not name.endswith(MARKDOWN_SUFFIX):
        _reject("writes must target a .md file")
    if name == MARKDOWN_SUFFIX:
        _reject("empty filename")
    if name.startswith("."):
        _reject("hidden files are not writable")

    inbox_root = space_root / inbox_dir
    resolved = _contained(inbox_root, inbox_root / name)

    # Belt and braces: the file must sit *directly* in the inbox, and the inbox
    # must itself be inside the space.
    if resolved.parent != inbox_root.resolve():
        _reject("resolves outside the inbox folder")
    _contained(space_root, resolved)
    return resolved


def slug_to_filename(date_prefix: str, slug: str, attempt: int = 1) -> str:
    """Build `YYYY-MM-DD-slug.md`, with `-2`, `-3`, … on collision."""
    suffix = "" if attempt <= 1 else f"-{attempt}"
    return f"{date_prefix}-{slug}{suffix}{MARKDOWN_SUFFIX}"
