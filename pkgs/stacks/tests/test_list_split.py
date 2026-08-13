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


class TestSeriesVolumeSpec:
    """The volume-number mapping — a wrong row here names the wrong book.

    These records are flood losses, so they drive the replace list at a sale.
    Getting one wrong sends someone home with a book they did not lose and
    without the one they did.
    """

    SPEC = Path(__file__).resolve().parent.parent / "data" / "series-volumes.toml"

    def _volumes(self):
        return tomllib.loads(self.SPEC.read_text())["volume"]

    def test_every_volume_is_complete(self):
        for v in self._volumes():
            assert v.get("series") and v.get("number") and v.get("title")
            assert not v["title"].startswith("#"), v

    def test_no_number_is_claimed_twice(self):
        seen = set()
        for v in self._volumes():
            key = (v["series"], str(v["number"]))
            assert key not in seen, f"{key} appears more than once"
            seen.add(key)

    def test_the_renumbering_is_resolved_by_evidence_not_preference(self):
        """Magic Tree House above #28 could name two different books.

        The Merlin Missions were originally 29-55 and were later split into
        their own 1-27. 1-28 is identical in both schemes. The document
        settles the rest itself: it lists 40 and 48, and the modern main
        series stops at 39, so those numbers only exist under the original
        continuous numbering.

        This pins the reasoning, not just the answer — if the mapping is ever
        regenerated against the modern scheme, #29 becomes A Big Day for
        Baseball and this fails.
        """
        got = {str(v["number"]): v["title"] for v in self._volumes()
               if v["series"] == "Magic Tree House"}
        assert got.get("40") and got.get("48"), (
            "40 and 48 are the evidence that fixes the numbering scheme; "
            "dropping them removes the justification for 29-48"
        )
        assert got.get("29") == "Christmas in Camelot", (
            f"#29 is {got.get('29')!r} — 'A Big Day for Baseball' means the "
            f"modern scheme was used, which contradicts 40 and 48 existing"
        )

    def test_known_anchors_are_right(self):
        """Spot-checks against the actual series.

        A first pass at parsing read the wrong table on the page and returned
        the Benny-and-Watch early readers as the novels — every row plausible,
        every row wrong.
        """
        got = {(v["series"], str(v["number"])): v["title"] for v in self._volumes()}
        for key, expect in {
            ("Magic Tree House", "1"): "Dinosaurs Before Dark",
            ("Magic Tree House", "2"): "The Knight at Dawn",
            ("Magic Tree House", "17"): "Tonight on the Titanic",
            ("Boxcar Children", "1"): "The Boxcar Children",
            ("Boxcar Children", "2"): "Surprise Island",
            ("Boxcar Children", "4"): "Mystery Ranch",
        }.items():
            assert got.get(key) == expect, f"{key} is {got.get(key)!r}, expected {expect!r}"
