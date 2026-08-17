"""One identity rule for tags, enforced at every layer.

The audit found two: the API add_tag endpoint uppercased names and matched
globally, while labels.find_or_create matched case-insensitively per tree
level and preserved case. Result: "sell" and "SELL" were two tags, and a
same-named tag nested under an unrelated parent could be attached by
accident. On top of that, UNIQUE(parent_id, name) is inert for NULL parents
on PG14, so nothing at the database stopped root-level twins either.
"""

from __future__ import annotations

import os

import pytest

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
def work_id(session):
    from stacks.models import Work

    w = Work(title="Tag Identity Fixture", sort_title="tag identity fixture",
             ol_work_keys=[])
    session.add(w)
    session.commit()
    return w.id


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from stacks.api import app

    with TestClient(app) as c:
        yield c


class TestOneTagIdentity:
    def test_case_twins_are_one_tag(self, client, work_id):
        r1 = client.post(f"/api/work/{work_id}/tags", json={"name": "identcheck"})
        assert r1.status_code == 200, r1.text
        r2 = client.post(f"/api/work/{work_id}/tags", json={"name": "IDENTCHECK"})
        assert r2.status_code == 200, r2.text
        tags = [t for t in r2.json()["tags"] if t.lower() == "identcheck"]
        assert len(tags) == 1, f"case twins created two tags: {r2.json()['tags']}"

    def test_names_keep_their_case(self, client, work_id):
        r = client.post(f"/api/work/{work_id}/tags", json={"name": "Sonlight Core B"})
        assert r.status_code == 200, r.text
        assert "Sonlight Core B" in r.json()["tags"]

    def test_remove_is_case_insensitive(self, client, work_id):
        client.post(f"/api/work/{work_id}/tags", json={"name": "Removable"})
        r = client.delete(f"/api/work/{work_id}/tags/REMOVABLE")
        assert r.status_code == 200
        assert "Removable" not in r.json()["tags"]

    def test_derived_badges_still_rejected_any_case(self, client, work_id):
        r = client.post(f"/api/work/{work_id}/tags", json={"name": "have"})
        assert r.status_code == 400

    def test_database_refuses_root_case_twins(self, session):
        """The partial unique index — PG14's answer to NULLS NOT DISTINCT."""
        from sqlalchemy.exc import IntegrityError

        from stacks.models import Tag

        session.add(Tag(name="RootTwinCheck"))
        session.commit()
        session.add(Tag(name="roottwincheck"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


class TestSeriesMatching:
    def test_percent_in_a_series_name_is_not_a_wildcard(self, client, session, work_id):
        from stacks.models import Series

        session.add(Series(name="Wildcard Fixture Classics"))
        session.commit()

        # Under ilike, "Wildcard%" matched the fixture series above and
        # silently bound the work to it. It must create its own series.
        r = client.patch(f"/api/work/{work_id}", json={"series": "Wildcard%"})
        assert r.status_code == 200, r.text
        assert r.json()["series"] == "Wildcard%"
