"""Verdict logic, tested without a database.

``_decide`` is pure: a work plus a holding breakdown in, a decision out. That is
deliberate — this is the logic that costs money when wrong, so it is testable in
isolation from Postgres, Open Library, and the scanner.
"""

import pytest

from stacks.match import BUYS, Holding, Verdict, _decide
from stacks.models import Work


def work(desired: int = 1) -> Work:
    return Work(title="Test", sort_title="test", desired_copies=desired, ol_work_keys=[])


class TestVerdicts:
    def test_never_owned_is_NOT_a_buy(self):
        """Unknown books are information, not advice.

        Most books at a sale are ones this family has never owned. Recommending
        all of them drowns the three signals that matter — wanted, destroyed,
        want-more — so an unrecognised book gets a neutral verdict.
        """
        v, _, _ = _decide(work(), Holding())
        assert v is Verdict.NOT_IN_CATALOG
        assert v not in BUYS

    def test_only_positive_reasons_are_buys(self):
        assert BUYS == {Verdict.BUY_WANTED, Verdict.BUY_REPLACE, Verdict.BUY_MORE}
        assert Verdict.NOT_IN_CATALOG not in BUYS
        assert Verdict.UNKNOWN not in BUYS

    def test_verified_holding_is_a_skip(self):
        v, rec, _ = _decide(work(), Holding(present=1))
        assert v is Verdict.SKIP_HAVE
        assert "confirmed" in rec

    def test_flood_loss_is_a_replace(self):
        v, rec, detail = _decide(work(), Holding(lost_flood=1))
        assert v is Verdict.BUY_REPLACE
        assert v in BUYS
        assert "flood" in rec
        # The recommendation already said it; the fact must not be repeated.
        assert not any("lost in the flood" in d for d in detail)

    def test_facts_are_not_repeated_in_the_recommendation(self):
        """Saying the same worried thing twice reads as the system fretting."""
        _, rec, detail = _decide(work(), Holding(unverified=3))
        assert "not seen since the flood" in rec
        assert not any("unconfirmed" in d for d in detail)

    def test_unrelated_facts_still_surface(self):
        """Dedup must not swallow counts the recommendation did not mention."""
        _, rec, detail = _decide(work(), Holding(lost_flood=2, unverified=1))
        assert "flood" in rec
        assert any("unconfirmed" in d for d in detail), detail

    def test_unverified_only_is_never_a_skip(self):
        """The core safety property.

        Libib says we own it, but nobody has confirmed it since the flood.
        Telling someone to skip on that basis is how they go home without a book
        they no longer own.
        """
        v, _, _ = _decide(work(), Holding(unverified=3))
        assert v is Verdict.CAUTION_UNVERIFIED
        assert v is not Verdict.SKIP_HAVE

    def test_one_confirmed_copy_beats_unverified_noise(self):
        v, _, _ = _decide(work(desired=1), Holding(present=1, unverified=2))
        assert v is Verdict.SKIP_HAVE

    def test_wanting_more_than_we_hold(self):
        v, headline, _ = _decide(work(desired=3), Holding(present=1))
        assert v is Verdict.BUY_MORE
        assert "1 of 3" in headline

    def test_loaned_copies_still_exist(self):
        # A book at a friend's house is one we own; don't buy another.
        v, _, _ = _decide(work(desired=1), Holding(loaned=1))
        assert v is Verdict.SKIP_HAVE

    def test_replaced_after_flood_is_a_skip(self):
        # The rebuy case: lost one, bought it again, sweep confirmed it.
        v, _, detail = _decide(work(), Holding(present=1, lost_flood=1))
        assert v is Verdict.SKIP_HAVE
        assert any("flood" in d for d in detail)

    @pytest.mark.parametrize(
        "holding",
        [Holding(), Holding(lost_flood=2), Holding(present=1, unverified=1)],
    )
    def test_desired_zero_never_recommends_buying(self, holding):
        """desired_copies=0 means 'we deliberately do not want this'."""
        v, _, _ = _decide(work(desired=0), holding)
        assert v is not Verdict.BUY_MORE


class TestReplacementSupersedesLoss:
    """The book-replacement document records real acquisitions.

    All Libib data is pre-flood (2023), so a flood record always supersedes it.
    But a replacement bought afterwards supersedes the loss in turn — and the
    loss branch used to be checked first, so a book already re-bought still
    announced "not replaced yet" and would send someone to buy a second one.
    """

    def test_replacement_beats_the_loss(self):
        v, rec, _ = _decide(work(), Holding(lost_flood=1, unverified=1, re_acquired=1))
        assert v is Verdict.CAUTION_UNVERIFIED
        assert v not in BUYS
        assert "Replaced after the flood" in rec

    def test_loss_without_replacement_still_says_buy(self):
        v, rec, _ = _decide(work(), Holding(lost_flood=1))
        assert v is Verdict.BUY_REPLACE
        assert "not replaced yet" in rec

    def test_replacement_reports_as_replaced_not_unconfirmed(self):
        from stacks.match import status_for

        h = Holding(lost_flood=1, unverified=1, re_acquired=1)
        v, _, _ = _decide(work(), h)
        assert status_for(v, h) == "REPLACED"

    def test_plain_unverified_is_still_unconfirmed(self):
        from stacks.match import status_for

        h = Holding(unverified=2)
        v, _, _ = _decide(work(), h)
        assert status_for(v, h) == "UNCONFIRMED"

    def test_a_confirmed_copy_still_wins(self):
        """Actually seeing the book beats any paperwork."""
        v, rec, _ = _decide(work(), Holding(present=1, lost_flood=1, re_acquired=1))
        assert v is Verdict.SKIP_HAVE
        assert "confirmed" in rec

    def test_mixed_counts_report_the_plain_unverified_remainder(self):
        _, _, detail = _decide(work(), Holding(unverified=3, re_acquired=1, present=1))
        joined = " ".join(detail)
        assert "1 bought again after the flood" in joined
        assert "2 unconfirmed since the flood" in joined
