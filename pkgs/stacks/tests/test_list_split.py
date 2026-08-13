"""Splitting loss-document lines that name several books.

These cover the two decisions that were wrong on the first attempt and are
invisible in the output when they go wrong again: how a fragment is matched,
and what a newly created record is called.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from stacks.importers.list_split import _match_key, _new_title

SPEC = Path(__file__).resolve().parent.parent / "data" / "list-records.toml"


class TestMatchKey:
    def test_initialisms_close_up(self):
        """The document writes U.F.O.; the catalog writes UFO.

        Punctuation stripping turns the first into "u f o", which shares no
        useful trigrams and no substring with "ufo", so the lost copy and the
        replacement landed on two different records.
        """
        assert _match_key("Mystery of the U.F.O.") == _match_key("Mystery of the UFO")

    def test_a_lone_pronoun_is_not_an_initialism(self):
        """Only runs of two or more single letters collapse."""
        assert _match_key("I have to go") == "i have to go"

    def test_a_fragment_is_contained_in_its_full_title(self):
        """The property the whole matcher rests on."""
        assert _match_key("zoo note") in _match_key(
            "Young Cam Jansen and the Zoo Note Mystery"
        )
        assert _match_key("corn popper") in _match_key(
            "Cam Jansen and the Mystery of the Stolen Corn Popper"
        )


class TestNewTitle:
    def test_a_series_entry_is_always_prefixed(self):
        """Not cosmetic — it is what makes the next fragment findable.

        The document lists the same book twice in different words, once lost
        and once replaced. The second only finds the first if the first is
        filed under the series, because that is what the scoped search looks
        for.
        """
        title = _new_title("Mystery of the Stolen Corn Popper", "Cam Jansen", False)
        assert title.startswith("Cam Jansen")
        assert _match_key("corn popper") in _match_key(title)

    def test_a_series_name_is_not_repeated(self):
        assert _new_title("Cam Jansen and the Zoo Note", "Cam Jansen", False) == (
            "Cam Jansen and the Zoo Note"
        )

    def test_an_author_keeps_a_complete_title(self):
        """An author is not a series — "Robert Munsch: The Paper Bag Princess"
        would misname the book."""
        assert _new_title("The Paper Bag Princess", "Robert Munsch", True) == (
            "The Paper Bag Princess"
        )

    def test_an_author_fragment_is_still_qualified(self):
        assert _new_title("pigs", "Robert Munsch", True) == "Robert Munsch: pigs"


class TestSpec:
    """The judgement file itself — a typo here silently drops books."""

    def test_every_record_is_actionable(self):
        spec = tomllib.loads(SPEC.read_text())
        assert spec["record"], "no records"
        for rec in spec["record"]:
            assert "work_id" in rec
            if rec.get("delete"):
                assert rec.get("why"), "a deletion must say why"
                continue
            assert rec.get("series"), f"{rec['work_id']} has no series or author"
            assert (
                rec.get("fragments") or rec.get("volumes") or rec.get("special_volumes")
            ), f"{rec['work_id']} names no books, so splitting it would lose it"

    def test_no_record_is_listed_twice(self):
        spec = tomllib.loads(SPEC.read_text())
        ids = [r["work_id"] for r in spec["record"]]
        assert len(ids) == len(set(ids))
