"""Skips and thumbs, and what they do to the odds."""

import random
import unittest
from collections import Counter

from netradio import ratings

P = "/mnt/fusion/Music/Bill Monroe/Bluegrass 1950/03 Uncle Pen.mp3"
Q = "/mnt/fusion/Music/Bill Monroe/Bluegrass 1950/04 Blue Moon.mp3"
OTHER = "/mnt/fusion/Music/Hank Williams/Moanin/01 Lovesick.mp3"


def artist_of(path):
    parts = [p for p in path.split("/") if p]
    return parts[-3].strip().lower() if len(parts) >= 3 else ""


class Scoring(unittest.TestCase):
    def test_a_skip_costs_ground_and_a_thumb_buys_it_back(self):
        st = {}
        ratings.record(st, kind="skip", path=P, artist="Bill Monroe", title="Uncle Pen")
        self.assertEqual(ratings.track_score(st, P), -1)
        ratings.record(st, kind="thumb", path=P, artist="Bill Monroe", title="Uncle Pen")
        self.assertEqual(ratings.track_score(st, P), 0)

    def test_four_skips_take_it_out_of_rotation_with_no_verdict(self):
        """The escalation Chris asked for: nothing has to be declared "never"
        for repeated skipping to stop a track coming back."""
        st = {}
        for n in range(1, 5):
            ratings.record(st, kind="skip", path=P, artist="Bill Monroe")
            w = ratings.weight(st, P, "Bill Monroe")
            if n < 4:
                self.assertGreater(w, 0, f"skip {n} must not banish it outright")
            else:
                self.assertEqual(w, 0.0, "the fourth skip retires it")

    def test_one_skip_only_halves_it(self):
        """A skip because you were not in the mood is not a life sentence."""
        st = {}
        ratings.record(st, kind="skip", path=P)
        self.assertAlmostEqual(ratings.weight(st, P), 0.5)

    def test_thumbs_are_capped_so_a_favourite_cannot_take_over(self):
        st = {}
        for _ in range(10):
            ratings.record(st, kind="thumb", path=P, artist="Bill Monroe")
        self.assertLessEqual(ratings.weight(st, P, "Bill Monroe"), ratings.MAX_WEIGHT)

    def test_the_artist_moves_at_half_strength_and_cannot_banish_anything(self):
        st = {}
        for _ in range(9):      # nine skips of OTHER tracks by the same artist
            ratings.record(st, kind="skip", path="", artist="Bill Monroe")
        self.assertEqual(ratings.artist_score(st, "Bill Monroe"), -9)
        # an unrated track by that artist is quieter, but still offered
        self.assertGreater(ratings.weight(st, Q, "Bill Monroe"), 0.0)

    def test_the_artist_key_matches_however_it_is_capitalised(self):
        st = {}
        ratings.record(st, kind="thumb", path=P, artist="Bill Monroe")
        self.assertEqual(ratings.artist_score(st, "  bill monroe "), 1)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            ratings.record({}, kind="shrug", path=P)


class Picking(unittest.TestCase):
    def test_a_thumbed_track_really_does_come_round_oftener(self):
        st = {}
        for _ in range(2):
            ratings.record(st, kind="thumb", path=P, artist="Bill Monroe")
        rng = random.Random(7)
        pool = [P, Q, OTHER]
        got = Counter(ratings.pick(st, pool, artist_of, rng) for _ in range(3000))
        self.assertGreater(got[P], got[Q] * 2, got)

    def test_a_retired_track_is_never_offered_while_others_remain(self):
        st = {}
        for _ in range(4):
            ratings.record(st, kind="skip", path=P, artist="Bill Monroe")
        rng = random.Random(11)
        got = Counter(ratings.pick(st, [P, OTHER], artist_of, rng) for _ in range(500))
        self.assertEqual(got[P], 0, got)
        self.assertEqual(got[OTHER], 500)

    def test_a_station_skipped_to_nothing_plays_anyway_rather_than_going_silent(self):
        st = {}
        for p in (P, Q):
            for _ in range(5):
                ratings.record(st, kind="skip", path=p, artist=artist_of(p))
        rng = random.Random(3)
        picked = ratings.pick(st, [P, Q], artist_of, rng)
        self.assertIn(picked, (P, Q), "silence is worse than a song you once skipped")

    def test_an_empty_pool_picks_nothing(self):
        self.assertIsNone(ratings.pick({}, [], artist_of, random.Random(1)))

    def test_with_no_opinions_every_track_is_equally_likely(self):
        rng = random.Random(5)
        got = Counter(ratings.pick({}, [P, Q, OTHER], artist_of, rng) for _ in range(3000))
        for n in got.values():
            self.assertGreater(n, 800, got)


class Stars(unittest.TestCase):
    """The translation Chris asked about — thumbs into library stars."""

    def test_no_opinion_is_not_zero_stars_but_no_rating_at_all(self):
        self.assertIsNone(ratings.stars({}, P))

    def test_the_mapping(self):
        for n, want in ((1, 4), (2, 5), (5, 5)):
            st = {}
            for _ in range(n):
                ratings.record(st, kind="thumb", path=P)
            self.assertEqual(ratings.stars(st, P), want, f"{n} thumbs")
        for n, want in ((1, 2), (2, 2), (3, 1), (6, 1)):
            st = {}
            for _ in range(n):
                ratings.record(st, kind="skip", path=P)
            self.assertEqual(ratings.stars(st, P), want, f"{n} skips")


if __name__ == "__main__":
    unittest.main()
