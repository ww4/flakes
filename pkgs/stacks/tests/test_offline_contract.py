"""The offline client and the catalog payload must agree on shape.

lookupOffline shipped broken for its entire life: ``catalog.build()`` emits
``isbns`` as ``{isbn13: work_id}`` — a bare integer — while app.js read
``hit.w``, so at a no-signal book sale every OWNED book answered "nothing
cached for this code" and only unknown books answered correctly. The Python
suite exercised ``build()``, loadcheck.js exercised the scripts' load order,
and the schema-version handshake guarded the version number; nothing anywhere
ran the real client function against the real payload. This does.

The rows are inserted here (and rolled back by conftest), so the test is
deterministic on any database, populated or empty.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from stacks.normalize import isbn13_check_digit

CONTRACT = Path(__file__).parent / "js" / "offline_contract.js"

needs_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs a database"
)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _isbn(body12: str) -> str:
    return body12 + isbn13_check_digit(body12)


@needs_db
@needs_node
class TestOfflineLookupContract:
    def test_lookup_offline_reads_a_real_payload(self, tmp_path):
        from stacks import catalog, db
        from stacks.models import Copy, CopyStatus, Edition, Provenance, Work

        from stacks.models import Series, WantKind, WantRule, WantSource

        owned = _isbn("979809999999")
        loaned = _isbn("979809999998")
        serieswant = _isbn("979809999997")

        with db.session_scope() as s:
            w = Work(title="Offline Contract Fixture", sort_title="offline contract fixture")
            s.add(w)
            s.flush()
            e = Edition(work_id=w.id, isbn13=owned, publisher="Testing Press",
                        publish_year=2026)
            s.add(e)
            s.flush()
            s.add(Copy(work_id=w.id, edition_id=e.id,
                       status=CopyStatus.present, provenance=Provenance.manual))

            # A book whose only copy is out on loan: still yours (SKIP_HAVE),
            # but schema 4 never shipped a loaned count, so offline it read
            # "No copies recorded" and invited buying a duplicate.
            wl = Work(title="Offline Loaned Fixture", sort_title="offline loaned fixture")
            s.add(wl)
            s.flush()
            el = Edition(work_id=wl.id, isbn13=loaned)
            s.add(el)
            s.flush()
            s.add(Copy(work_id=wl.id, edition_id=el.id,
                       status=CopyStatus.loaned, provenance=Provenance.manual))

            # An unowned volume of a series being collected: want_series was
            # in the payload from day one and the client never read it.
            series = Series(name="Offline Contract Series")
            s.add(series)
            s.flush()
            ws = Work(title="Offline Series Fixture", sort_title="offline series fixture",
                      series_id=series.id)
            s.add(ws)
            s.flush()
            s.add(Edition(work_id=ws.id, isbn13=serieswant))
            s.add(WantRule(kind=WantKind.series, source=WantSource.manual,
                           series_id=series.id, label=series.name,
                           match_key="offline contract series"))
            s.flush()
            payload = catalog.build(s)

        assert payload["isbns"][owned] == w.id, "build() no longer maps isbn -> work id"

        # A syntactically valid ISBN the catalog does not hold. If by
        # astronomical chance it exists, walk until one does not.
        miss = next(
            isbn for isbn in (_isbn(f"97980999{i:04d}") for i in range(1000))
            if isbn not in payload["isbns"]
        )

        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps(payload))
        checks_file = tmp_path / "checks.json"
        checks_file.write_text(json.dumps([
            {"isbn": owned, "expect": "hit", "title": "Offline Contract Fixture"},
            {"isbn": miss, "expect": "miss"},
            {"isbn": loaned, "expect": "verdict", "verdict": "SKIP_HAVE"},
            {"isbn": serieswant, "expect": "verdict", "verdict": "BUY_WANTED"},
        ]))

        r = subprocess.run(
            ["node", str(CONTRACT), str(payload_file), str(checks_file)],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, (
            f"offline contract broken:\n{r.stderr}{r.stdout}"
        )
