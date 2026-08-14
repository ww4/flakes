"""Places and tags — the tree, the rollup, and the one rule that differs.

The database-backed tests skip cleanly without DATABASE_URL, like the API
tests; the path helpers are pure and run anywhere.
"""

from __future__ import annotations

import os

import pytest

from stacks import labels


class TestPaths:
    def test_a_path_splits_on_slashes(self):
        assert labels.split_path("Frankfort / science shelf") == [
            "Frankfort", "science shelf"
        ]

    def test_spacing_is_forgiving(self):
        """Typed on a phone, in a hurry, standing in front of a bookcase."""
        for raw in ("Frankfort/science shelf", "Frankfort /science shelf",
                    "  Frankfort  /  science shelf  "):
            assert labels.split_path(raw) == ["Frankfort", "science shelf"]

    def test_empty_levels_are_dropped(self):
        assert labels.split_path("Frankfort // science shelf") == [
            "Frankfort", "science shelf"
        ]

    def test_round_trip(self):
        parts = ["Sonlight", "Core B"]
        assert labels.split_path(labels.join_path(parts)) == parts


db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs a database"
)


@pytest.fixture
def s():
    from stacks.db import session_scope

    with session_scope() as session:
        yield session
        session.rollback()


@db
class TestTree:
    def test_missing_levels_are_created(self, s):
        node = labels.find_or_create(s, "place", "Testhouse / test shelf")
        assert node.name == "test shelf"
        assert labels.path_of(s, "place", node.id) == "Testhouse / test shelf"

    def test_the_same_path_twice_is_the_same_node(self, s):
        a = labels.find_or_create(s, "place", "Testhouse / test shelf")
        b = labels.find_or_create(s, "place", "Testhouse / test shelf")
        assert a.id == b.id

    def test_a_leaf_name_may_repeat_under_different_parents(self, s):
        """"science shelf" exists at Frankfort and at Youngstown."""
        a = labels.find_or_create(s, "place", "TestA / science shelf")
        b = labels.find_or_create(s, "place", "TestB / science shelf")
        assert a.id != b.id

    def test_counts_roll_up(self, s):
        from sqlalchemy import select

        from stacks.models import Work

        shelf = labels.find_or_create(s, "place", "Testhouse / test shelf")
        works = list(s.scalars(select(Work.id).limit(3)).all())
        labels.place_works(s, works, shelf.id)
        s.flush()

        by_path = {n.path: n for n in labels.flatten(labels.tree(s, "place"))}
        assert by_path["Testhouse / test shelf"].own_count == 3
        # The number Chris asked for: the parent counts what is underneath it.
        assert by_path["Testhouse"].own_count == 0
        assert by_path["Testhouse"].total_count == 3

    def test_a_prefix_of_another_name_does_not_bleed(self, s):
        """"Frankfort" must not pick up "Frankfort Annex".

        This is why the tree is real rather than a path-prefix match on the
        name — the first design would have counted these together.
        """
        from sqlalchemy import select

        from stacks.models import Work

        a = labels.find_or_create(s, "place", "Testfort")
        labels.find_or_create(s, "place", "Testfort Annex")
        works = list(s.scalars(select(Work.id).limit(2)).all())
        labels.place_works(s, works, a.id)
        s.flush()

        by_path = {n.path: n for n in labels.flatten(labels.tree(s, "place"))}
        assert by_path["Testfort"].total_count == 2
        assert by_path["Testfort Annex"].total_count == 0


@db
class TestTheOneRuleThatDiffers:
    def test_a_place_replaces_the_previous_place(self, s):
        from sqlalchemy import select

        from stacks.models import Copy, Work

        first = labels.find_or_create(s, "place", "TestFirst")
        second = labels.find_or_create(s, "place", "TestSecond")
        works = list(s.scalars(select(Work.id).limit(2)).all())

        labels.place_works(s, works, first.id)
        labels.place_works(s, works, second.id)
        s.flush()

        places = set(s.scalars(
            select(Copy.location_id).where(Copy.work_id.in_(works))
        ).all())
        assert places == {second.id}, "a copy must be in exactly one place"

    def test_tags_accumulate(self, s):
        """Sonlight reuses titles across cores — Core B must not evict Core D."""
        from sqlalchemy import select

        from stacks.models import Work, WorkTag

        b = labels.find_or_create(s, "tag", "TestSonlight / Core B")
        d = labels.find_or_create(s, "tag", "TestSonlight / Core D")
        works = list(s.scalars(select(Work.id).limit(2)).all())

        labels.tag_works(s, works, b.id, add=True)
        labels.tag_works(s, works, d.id, add=True)
        s.flush()

        for w in works:
            got = set(s.scalars(
                select(WorkTag.tag_id).where(WorkTag.work_id == w)
            ).all())
            assert {b.id, d.id} <= got

    def test_tagging_twice_does_not_duplicate(self, s):
        from sqlalchemy import select

        from stacks.models import Work

        t = labels.find_or_create(s, "tag", "TestIdempotent")
        works = list(s.scalars(select(Work.id).limit(3)).all())
        assert labels.tag_works(s, works, t.id, add=True) == 3
        assert labels.tag_works(s, works, t.id, add=True) == 0

    def test_a_tag_can_be_removed(self, s):
        from sqlalchemy import select

        from stacks.models import Work

        t = labels.find_or_create(s, "tag", "TestRemovable")
        works = list(s.scalars(select(Work.id).limit(2)).all())
        labels.tag_works(s, works, t.id, add=True)
        s.flush()
        assert labels.tag_works(s, works, t.id, add=False) == 2

    def test_unplacing(self, s):
        from sqlalchemy import select

        from stacks.models import Copy, Work

        p = labels.find_or_create(s, "place", "TestTemp")
        works = list(s.scalars(select(Work.id).limit(2)).all())
        labels.place_works(s, works, p.id)
        s.flush()
        labels.place_works(s, works, None)
        s.flush()
        rows = s.scalars(
            select(Copy.location_id).where(Copy.work_id.in_(works))
        ).all()
        assert set(rows) == {None}


@db
class TestRollupIsDistinct:
    def test_a_parent_does_not_double_count_a_book_in_two_children(self, s):
        """Sonlight reuses titles across cores.

        Summing children reports six books where three exist. The rollup is a
        distinct count over the subtree, not a sum — places are immune because
        a copy is in one place, but tags are not.
        """
        from sqlalchemy import select

        from stacks.models import Work

        one = labels.find_or_create(s, "tag", "TestDouble / One")
        two = labels.find_or_create(s, "tag", "TestDouble / Two")
        works = list(s.scalars(select(Work.id).limit(3)).all())
        labels.tag_works(s, works, one.id, add=True)
        labels.tag_works(s, works, two.id, add=True)
        s.flush()

        by_path = {n.path: n for n in labels.flatten(labels.tree(s, "tag"))}
        assert by_path["TestDouble / One"].own_count == 3
        assert by_path["TestDouble / Two"].own_count == 3
        assert by_path["TestDouble"].total_count == 3, "same three books, counted once"
