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
