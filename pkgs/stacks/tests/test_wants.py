"""Publisher want rules must fire on books already in the catalog.

They never did: wants_for_work's rule query matches only work/author/series
anchors, and publisher rules have none — so "DK books" fired only via
wants_for_metadata, which runs only for books with NO catalog record. The
exact book the sale-day instruction is FOR — an unverified pre-flood DK book
scanned at a sale — showed CAUTION with the standing instruction silently
absent (2026-08 audit M4).

The matching is deliberately scoped to this household's printings (owned
copies' editions + the printing in hand), because enrichment attaches
hundreds of third-party editions and one stray DK printing on a
Scholastic-owned work must not fire "DK books".
"""

from __future__ import annotations

import os

import pytest

from stacks.normalize import isbn13_check_digit


def _isbn(body12: str) -> str:
    return body12 + isbn13_check_digit(body12)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs a database"
)


@pytest.fixture
def session():
    from stacks.db import session_scope

    with session_scope() as s:
        yield s
        s.rollback()


@pytest.fixture
def dk_rule(session):
    from stacks.models import WantKind, WantRule, WantSource

    r = WantRule(kind=WantKind.publisher, source=WantSource.manual,
                 label="Wantpub DK", match_key="wantpub dk")
    session.add(r)
    session.flush()
    return r


def _work_with_edition(session, title, publisher, *, owned):
    from stacks.models import Copy, CopyStatus, Edition, Provenance, Work

    w = Work(title=title, sort_title=title.lower(), ol_work_keys=[])
    session.add(w)
    session.flush()
    e = Edition(work_id=w.id, publisher=publisher)
    session.add(e)
    session.flush()
    if owned:
        session.add(Copy(work_id=w.id, edition_id=e.id,
                         status=CopyStatus.unverified,
                         provenance=Provenance.libib_import))
        session.flush()
    return w, e


class TestPublisherWants:
    def test_fires_on_an_owned_printing(self, session, dk_rule):
        from stacks.match import wants_for_work

        w, _ = _work_with_edition(session, "Wantpub Owned",
                                  "Wantpub DK Publishing", owned=True)
        hits = wants_for_work(session, w)
        assert any("Wantpub DK" in h for h in hits), hits

    def test_fires_on_the_printing_in_hand(self, session, dk_rule):
        from stacks.match import wants_for_work
        from stacks.models import Edition

        w, e = _work_with_edition(session, "Wantpub Inhand",
                                  "Wantpub DK Publishing", owned=False)
        isbn = _isbn("979809999899")
        e.isbn13 = isbn
        session.flush()
        assert not wants_for_work(session, w), "no copies, no scan — no fire"
        hits = wants_for_work(session, w, scanned_isbn=isbn)
        assert any("Wantpub DK" in h for h in hits), hits

    def test_a_stray_third_party_edition_does_not_fire(self, session, dk_rule):
        """The false-positive guard: owned Scholastic copy, unowned DK edition."""
        from stacks.match import wants_for_work
        from stacks.models import Edition

        w, _ = _work_with_edition(session, "Wantpub Stray",
                                  "Wantpub Scholastic", owned=True)
        session.add(Edition(work_id=w.id, publisher="Wantpub DK Publishing"))
        session.flush()
        hits = wants_for_work(session, w)
        assert not any("Wantpub DK" in h for h in hits), hits

    def test_scan_verdict_carries_the_want(self, session, dk_rule):
        """End to end: the audit's scenario — unverified DK book, scanned."""
        from stacks.match import evaluate_scan
        from stacks.models import Edition

        w, e = _work_with_edition(session, "Wantpub Scanned",
                                  "Wantpub DK Publishing", owned=True)
        isbn = _isbn("979809999898")
        e.isbn13 = isbn
        session.flush()
        result = evaluate_scan(session, isbn)
        assert any("Wantpub DK" in x for x in result.wants), result.wants
