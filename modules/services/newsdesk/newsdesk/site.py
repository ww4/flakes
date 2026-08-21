"""Publishing through Zola instead of hand-rolled HTML.

WHY THIS FILE REPLACED ~110 LINES OF TEMPLATING
-----------------------------------------------
Chris, watching the newsdesk grow an index page, prev/next navigation,
permalinks, a page template and a CSS blob:

    "I think you're reinventing a web blogging platform... we sort of snuck up
    on building our own framework. but we should at some point realize that
    we're recreating an existing platform and then start looking at off the
    shelf options."

No single step was wrong. "Write the edition to an HTML page" was right, and so
were "give it a dated permalink", "link to yesterday", "add an index". Six
reasonable increments later it was a static site generator — and Zola was
already running lock3.net and docs.broadlinc.com on this same stack.

So the backend is markdown on disk and Zola owns everything visual. What that
buys, all of it previously hand-written or missing: permalinks, previous/next,
the index, pagination, an Atom feed, a sitemap, and a client-side search index
over every past edition and every archived article.

HOW IT FITS TOGETHER
--------------------
  <state>/site/content/_index.md              the editions section
  <state>/site/content/<edition-id>.md        one edition
  <state>/site/content/archive/_index.md      the reading archive
  <state>/site/content/archive/<n>-<slug>.md  one saved article

Templates, config and CSS come from the Nix store and are copied in on every
render, so the look is declarative and the content is state. Zola builds into
the directory nginx already serves.

⚠️ Zola strips a leading date from a filename by convention, so
`2026-08-19-brief.md` and `2026-08-20-brief.md` both want to be `/brief/` and
the build dies with a path collision. Every page therefore sets an explicit
`slug`. Found by building a throwaway site before migrating anything.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .db import state_dir, write_atomic


def site_dir() -> Path:
    return state_dir() / "site"


def content_dir() -> Path:
    return site_dir() / "content"


def toml_str(s: str) -> str:
    """TOML basic string. Front matter is generated, so this must not be
    approximate — a stray quote in a headline would break the build."""
    out = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return f'"{out}"'


def slugify(text: str, fallback: str = "untitled") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or fallback)[:70]


SECTION_EDITIONS = """+++
title = "Newsdesk"
sort_by = "date"
template = "index.html"
page_template = "edition.html"
paginate_by = 30
+++
"""

SECTION_ARCHIVE = """+++
title = "Reading archive"
sort_by = "date"
template = "archive.html"
page_template = "read.html"
paginate_by = 40
+++
"""


def ensure_sections() -> None:
    write_atomic(content_dir() / "_index.md", SECTION_EDITIONS)
    write_atomic(content_dir() / "archive" / "_index.md", SECTION_ARCHIVE)


def write_edition(edition_id: str, kind_title: str, date: str, tldr: str,
                  body: str, *, n_published: int = 0, n_reads: int = 0,
                  had_event: bool = False) -> Path:
    """One edition as a markdown page. The body is markdown, links and all."""
    ensure_sections()
    front = [
        "+++",
        f"title = {toml_str(f'{kind_title} — {date}')}",
        f"date = {date}",
        f"slug = {toml_str(edition_id)}",
        "[extra]",
        f"kind_title = {toml_str(kind_title)}",
        f"tldr = {toml_str(tldr)}",
        f"n_published = {int(n_published)}",
        f"n_reads = {int(n_reads)}",
        f"had_event = {'true' if had_event else 'false'}",
        "+++",
        "",
    ]
    path = content_dir() / f"{edition_id}.md"
    write_atomic(path, "\n".join(front) + body.strip() + "\n")
    return path


def write_read(item_id: int, title: str, source: str, url: str,
               published: str | None, retrieved: str, text: str) -> str:
    """One archived article. Returns the site-relative path."""
    ensure_sections()
    slug = f"{item_id}-{slugify(title)}"
    date = (published or retrieved or "")[:10]
    # Built as a list of REAL lines only. An earlier version carried a
    # conditional "" for the missing-date case and filtered empties out at the
    # end — which also ate the blank line after the closing +++, gluing the
    # delimiter to the body. Zola then reported "couldn't find front matter"
    # on a file that visibly starts with +++.
    front = [
        "+++",
        f"title = {toml_str(title)}",
    ]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        front.append(f"date = {date}")
    front += [
        f"slug = {toml_str(slug)}",
        "[extra]",
        f"source = {toml_str(source)}",
        f"url = {toml_str(url)}",
        f"published = {toml_str((published or '')[:10])}",
        f"retrieved = {toml_str(retrieved[:10])}",
        f"words = {len(text.split())}",
        "+++",
        "",
    ]
    # The text is plain, not markdown: it came out of an HTML page. Escaping the
    # markdown-significant characters keeps a line starting with "#" or "-" from
    # silently becoming a heading or a list in someone else's prose.
    safe = re.sub(r"^([#>\-*+=]|\d+\.)", r"\\\1", text, flags=re.MULTILINE)
    write_atomic(content_dir() / "archive" / f"{slug}.md",
                 "\n".join(front) + safe.strip() + "\n")
    return f"archive/{slug}/"


def build(site_src: Path, out_dir: Path, *, zola: str = "zola") -> tuple[bool, str]:
    """Copy templates/config/static from the store, then build.

    Returns (ok, output). A failed build must never take the edition down with
    it — the caller keeps whatever was published last.
    """
    site = site_dir()
    content_dir().mkdir(parents=True, exist_ok=True)
    # ⚠️ Everything copied out of the Nix store arrives read-only, so the NEXT
    # run's rmtree dies with EACCES. The first build succeeds and the second
    # fails, which is the worst way to find out. Make the copies writable.
    site.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(site_src) / "config.toml", site / "config.toml")
    (site / "config.toml").chmod(0o644)
    for name in ("templates", "static"):
        src = Path(site_src) / name
        if not src.is_dir():
            continue
        dst = site / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        for path in dst.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        dst.chmod(0o755)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([zola, "build", "-o", str(out_dir), "--force"],
                           cwd=site, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"
    return r.returncode == 0, (r.stdout + r.stderr).strip()
