"""The browser scripts must load together, not merely parse apart.

A duplicate top-level ``esc`` — ``const`` in card.js, ``function`` in edit.js —
was a redeclaration SyntaxError that killed edit.js at load. Every file passed
``node --check`` individually, and nothing in the Python suite could see it.
The visible symptom was a book tile that depressed on tap and did nothing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "stacks" / "web"
LOADCHECK = Path(__file__).parent / "js" / "loadcheck.js"

_TOP_LEVEL_DECL = re.compile(r"^(?:const|let|var|function|async function)\s+([A-Za-z_$][\w$]*)")


def _page_scripts(page: str) -> list[str]:
    html = (WEB / page).read_text()
    return re.findall(r'<script src="([^"]+)"></script>', html)


def _top_level_names(js: Path) -> set[str]:
    names = set()
    for line in js.read_text().splitlines():
        m = _TOP_LEVEL_DECL.match(line)
        if m:
            names.add(m.group(1))
    return names


class TestNoGlobalCollisions:
    """Pure-Python guard, so it runs even where node is unavailable."""

    @pytest.mark.parametrize("page", ["index.html", "browse.html"])
    def test_no_duplicate_top_level_declarations(self, page):
        seen: dict[str, str] = {}
        clashes = []
        for name in _page_scripts(page):
            f = WEB / name
            if not f.exists():
                continue
            for decl in _top_level_names(f):
                if decl in seen and seen[decl] != name:
                    clashes.append(f"{decl} in both {seen[decl]} and {name}")
                seen[decl] = name
        assert not clashes, (
            f"{page} loads scripts that redeclare the same global — in a browser "
            f"this is a SyntaxError that silently kills the later file: {clashes}"
        )

    def test_every_referenced_script_exists(self):
        for page in ("index.html", "browse.html"):
            for name in _page_scripts(page):
                assert (WEB / name).is_file(), f"{page} references missing {name}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestScriptsLoadTogether:
    def test_shared_global_scope(self):
        r = subprocess.run(
            ["node", str(LOADCHECK)], capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 0, f"page scripts failed to load:\n{r.stderr}{r.stdout}"


class TestPagesDeclareWhatScriptsUse:
    """Scripts must not wire up elements their page does not have.

    "I have this" shipped invisible: the markup was dropped in a card rewrite,
    app.js kept calling el('btn-have').addEventListener(...) at top level, and
    every test stayed green because the harness handed back an element for any
    id it was asked for. It now answers only for ids the page really declares.
    """

    ELEMENT_IDS = re.compile(r'id="([^"]+)"')
    # el('x') / document.getElementById('x').
    #
    # The lookahead matters: without it `el(` also matched the tail of
    # `askLabel('place')`, so the check invented two ids that no script ever
    # asked for. A check that reports things nobody wrote is a check people
    # learn to skim.
    LOOKUPS = re.compile(
        r"""(?<![\w$])(?:el|\$)\(\s*['"]([\w-]+)['"]\s*\)"""
        r"""|getElementById\(\s*['"]([\w-]+)['"]\s*\)"""
    )
    # Ids a script builds for itself — `x.id = 'sel'` or markup it writes.
    SELF_MADE = re.compile(
        r"""\.id\s*=\s*['"]([\w-]+)['"]"""
        r'''|id="([\w-]+)"'''
    )

    @pytest.mark.parametrize("page", ["index.html", "browse.html", "shelf.html",
                                      "cleanup.html", "logs.html", "labels.html"])
    def test_every_looked_up_id_exists_on_the_page(self, page):
        html = (WEB / page).read_text()
        declared = set(self.ELEMENT_IDS.findall(html))
        missing: dict[str, set[str]] = {}

        for name in _page_scripts(page):
            f = WEB / name
            if not f.exists():
                continue
            src = f.read_text()
            # A script that constructs its own elements may of course look them
            # up again; the rule is about reaching for markup that was supposed
            # to be on the page and is not.
            self_made = {a or b for a, b in self.SELF_MADE.findall(src)}
            for a, b in self.LOOKUPS.findall(src):
                wanted = a or b
                # Ids created at runtime by the editor's own markup are fair.
                if wanted.startswith("e-") or wanted in self_made:
                    continue
                if wanted not in declared:
                    missing.setdefault(name, set()).add(wanted)

        assert not missing, (
            f"{page} scripts reference ids the page does not declare: "
            f"{ {k: sorted(v) for k, v in missing.items()} }"
        )
