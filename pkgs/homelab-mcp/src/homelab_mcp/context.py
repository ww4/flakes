"""`get_context()` — the briefing that makes a phone conversation informed.

This is the half the original task doc missed. Capture-out is easy; the reason
an app conversation is useless about the homelab is that it has no idea what
the homelab *is*. This composes a compact orientation from durable sources.

Everything here is read-only.
"""

from __future__ import annotations

import re
from pathlib import Path

from .paths import PathRejected, resolve_read
from .space import SKIP_DIRS, MARKDOWN_SUFFIX

MAX_OPEN_TASKS = 40
MAX_PAGE_CHARS = 4000
MAX_SUMMARY_PAGES = 40

TASK_RE = re.compile(r"^\s*[-*]\s+\[ \]\s+(.*)$")


def _read(path: Path, limit: int = MAX_PAGE_CHARS) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > limit:
        text = text[:limit] + "\n…(truncated)…\n"
    return text


def _first_prose_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        return stripped[:200]
    return ""


def _folder_summary(space_root: Path, folder: str) -> list[str]:
    base = space_root / folder
    if not base.is_dir():
        return []
    out: list[str] = []
    for path in sorted(base.rglob(f"*{MARKDOWN_SUFFIX}"))[:MAX_SUMMARY_PAGES]:
        text = _read(path, limit=1200)
        if text is None:
            continue
        rel = path.relative_to(space_root).as_posix()
        out.append(f"- `{rel}` — {_first_prose_line(text)}")
    return out


def _open_tasks(space_root: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(space_root.rglob(f"*{MARKDOWN_SUFFIX}")):
        rel_parts = path.relative_to(space_root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        # Journal pages restate tasks that live elsewhere; skip to avoid dupes.
        if rel_parts and rel_parts[0] in {"Journal", "Keep"}:
            continue
        text = _read(path, limit=20000)
        if text is None:
            continue
        rel = path.relative_to(space_root).as_posix()
        for line in text.splitlines():
            match = TASK_RE.match(line)
            if match:
                out.append(f"- [ ] {match.group(1).strip()}  _({rel})_")
                if len(out) >= MAX_OPEN_TASKS:
                    return out
    return out


def _service_inventory(flake_root: Path) -> list[str]:
    """Service names and vhosts, derived from the flake. Names only, no config."""
    modules = flake_root / "modules" / "services"
    if not modules.is_dir():
        return []
    services = sorted(p.stem for p in modules.glob("*.nix"))
    vhosts: set[str] = set()
    vhost_re = re.compile(r'virtualHosts\."([^"]+)"')
    for path in modules.glob("*.nix"):
        try:
            for match in vhost_re.finditer(path.read_text(encoding="utf-8", errors="replace")):
                vhosts.add(match.group(1).replace("${domain}", "rosemaryacres.com"))
        except OSError:
            continue
    lines = [f"Service modules ({len(services)}): " + ", ".join(services)]
    if vhosts:
        lines.append(f"Web vhosts ({len(vhosts)}): " + ", ".join(sorted(vhosts)))
    return lines


def build_context(
    space_root: Path,
    *,
    flake_root: Path | None = None,
    include_service_inventory: bool = True,
    context_page: str | None = None,
) -> str:
    """Assemble the briefing as one markdown document."""
    sections: list[str] = ["# Homelab & knowledgebase context"]

    conventions = _read(space_root / "CONVENTIONS.md")
    if conventions:
        sections += ["", "## How this space works (the shared contract)", "", conventions]

    index = _read(space_root / "index.md", limit=2000)
    if index:
        sections += ["", "## Space landing page", "", index]

    projects = _folder_summary(space_root, "Projects")
    if projects:
        sections += ["", "## Active projects", ""] + projects

    areas = _folder_summary(space_root, "Areas")
    if areas:
        sections += ["", "## Areas of responsibility", ""] + areas

    tasks = _open_tasks(space_root)
    if tasks:
        sections += ["", f"## Open tasks (first {len(tasks)})", ""] + tasks

    if include_service_inventory and flake_root is not None:
        inventory = _service_inventory(flake_root)
        if inventory:
            sections += ["", "## Deployed services (from the NixOS flake)", ""] + inventory

    if context_page:
        # Scoped through resolve_read, not just joined. This value is operator
        # configuration rather than caller input, so it is not an attack path —
        # but a typo like "../secret.md" would otherwise read a file outside
        # the space, and a config typo should not be able to leak anything.
        # (Caught by test_never_escapes_the_space, which a plain join failed.)
        try:
            curated_path = resolve_read(space_root, context_page)
        except PathRejected:
            curated_path = None
        if curated_path is not None:
            curated = _read(curated_path, limit=20000)
            if curated:
                sections += ["", "## Current focus (curated)", "", curated]

    return "\n".join(sections).rstrip() + "\n"
