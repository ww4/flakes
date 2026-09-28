"""Stars in the library files, both directions."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from netradio import ratings, stars
from netradio.config import Config

try:
    import mutagen                      # noqa: F401
    HAVE_MUTAGEN = True
except ImportError:                     # the package build has it; a bare shell may not
    HAVE_MUTAGEN = False

P = "/mnt/fusion/Music/Bill Monroe/Bluegrass 1950/03 Uncle Pen.mp3"


class Mapping(unittest.TestCase):
    def test_the_two_maps_are_inverses_where_it_matters(self):
        """score -> stars -> score must not drift a track's standing."""
        for n_thumbs, n_skips in ((1, 0), (2, 0), (5, 0), (0, 1), (0, 2), (0, 3), (0, 5)):
            st = {}
            for _ in range(n_thumbs):
                ratings.record(st, kind="thumb", path=P)
            for _ in range(n_skips):
                ratings.record(st, kind="skip", path=P)
            s = ratings.stars(st, P)
            self.assertIsNotNone(s)
            back = stars.score_for_stars(s)
            # the same SIDE of neutral, which is the property that matters
            self.assertEqual(back > 0, ratings.track_score(st, P) > 0, f"{n_thumbs}up/{n_skips}down -> {s}*")
            self.assertEqual(back < 0, ratings.track_score(st, P) < 0, f"{n_thumbs}up/{n_skips}down -> {s}*")

    def test_three_stars_is_no_opinion(self):
        self.assertEqual(stars.score_for_stars(3), 0)

    def test_popm_bytes_read_back_as_the_star_they_were_written_as(self):
        for star, byte in stars.POPM.items():
            self.assertEqual(stars.popm_to_stars(byte), star, f"{byte} should be {star} stars")

    def test_a_rating_on_any_of_the_scales_in_circulation_is_understood(self):
        for raw, want in (("1.0", 5), ("0.8", 4), ("0.2", 1),       # FMPS 0-1
                          ("5", 5), ("3", 3),                       # plain stars
                          ("100", 5), ("80", 4), ("20", 1),         # RATING 0-100
                          ("255", 5), ("196", 4), ("1", 1)):        # POPM-ish
            self.assertEqual(stars._scale_to_stars(raw), want, f"{raw!r}")

    def test_junk_and_zero_are_no_rating_rather_than_one_star(self):
        for raw in ("", "0", "not a number", "-3"):
            self.assertEqual(stars._scale_to_stars(raw), 0, f"{raw!r}")


@unittest.skipUnless(HAVE_MUTAGEN, "mutagen not available in this interpreter")
class RoundTripThroughRealFiles(unittest.TestCase):
    """Written with mutagen, read back with mutagen, on REAL audio.

    A synthetic MP3 is not good enough: `mutagen.File()` declines to claim a
    hand-built frame sequence at all, so the first version of this test failed
    against working code (2026-09-27). ffmpeg makes a second of silence in each
    format instead — it is already in the package's closure, so the check just
    needs it on PATH.
    """

    FFMPEG = shutil.which("ffmpeg")

    def setUp(self):
        if not self.FFMPEG:
            self.skipTest("no ffmpeg on PATH to make a fixture with")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, ext: str) -> str:
        out = Path(self.tmp.name) / f"silence.{ext}"
        subprocess.run([self.FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                        "-i", "anullsrc=r=44100:cl=mono", "-t", "1", str(out)],
                       check=True, capture_output=True)
        return str(out)

    def test_every_format_keeps_the_star_it_was_given(self):
        """mp3 through POPM, flac through RATING/FMPS_RATING, m4a through the
        iTunes atom — three different conventions, one behaviour."""
        for ext in ("mp3", "flac", "m4a"):
            with self.subTest(ext):
                path = self.make(ext)
                self.assertIsNone(stars.read_stars(path), "a fresh file has no rating")
                for star in (1, 3, 5):
                    self.assertTrue(stars.write_stars(path, star), f"{star} stars into {ext}")
                    self.assertEqual(stars.read_stars(path), star, f"{star} stars back out of {ext}")

    def test_a_rewrite_replaces_rather_than_accumulates(self):
        path = self.make("mp3")
        stars.write_stars(path, 5)
        stars.write_stars(path, 2)
        self.assertEqual(stars.read_stars(path), 2, "the old POPM frame must not win")

    def test_a_file_that_is_not_audio_is_declined_not_mangled(self):
        junk = Path(self.tmp.name) / "notes.txt"
        junk.write_text("this is not a song")
        self.assertFalse(stars.write_stars(str(junk), 4))
        self.assertIsNone(stars.read_stars(str(junk)))


class NoFeedbackLoop(unittest.TestCase):
    """The failure this design exists to prevent: writing a star, reading it
    back, mistaking it for the listener, and drifting."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "stations.json").write_text("[]")
        self.cfg = Config(self.root)
        self.addCleanup(self.tmp.cleanup)

    def rate(self, kind, n=1):
        st = self.cfg.ratings()
        for _ in range(n):
            ratings.record(st, kind=kind, path=P, artist="Bill Monroe", title="Uncle Pen")
        self.cfg.save_ratings(st)

    def test_our_own_write_is_not_read_back_as_an_opinion(self):
        self.rate("thumb", 2)                       # score +2 -> 5 stars
        # pretend the export happened and recorded what it wrote
        st = self.cfg.ratings()
        st["tracks"][P]["stars_seen"] = 5
        self.cfg.save_ratings(st)
        before = ratings.track_score(self.cfg.ratings(), P)
        # a sync that reads 5 stars back must change nothing
        counts = stars.sync(self.cfg, write=False, read=True)
        self.assertEqual(ratings.track_score(self.cfg.ratings(), P), before)
        self.assertEqual(counts["imported"], 0)

    def test_a_star_CHANGED_outside_does_move_the_score(self):
        self.rate("skip", 3)                        # score -3
        st = self.cfg.ratings()
        st["tracks"][P]["stars_seen"] = 1           # what we last wrote
        self.cfg.save_ratings(st)
        # somebody sets it to 5 in a tagger; simulate the file reporting that
        import unittest.mock as mock
        with mock.patch.object(stars, "read_stars", return_value=5), \
             mock.patch.object(Path, "exists", return_value=True):
            stars.sync(self.cfg, write=False, read=True)
        got = self.cfg.ratings()["tracks"][P]
        self.assertEqual(got["stars_seen"], 5)
        self.assertEqual(got["stars_from"], "file")
        self.assertGreater(ratings.track_score(self.cfg.ratings(), P), 0,
                           "a deliberate 5 stars should outrank three skips")

    def test_the_thumb_and_skip_history_survives_an_imported_star(self):
        self.rate("skip", 3)
        st = self.cfg.ratings()
        st["tracks"][P]["stars_seen"] = 1
        self.cfg.save_ratings(st)
        import unittest.mock as mock
        with mock.patch.object(stars, "read_stars", return_value=4), \
             mock.patch.object(Path, "exists", return_value=True):
            stars.sync(self.cfg, write=False, read=True)
        got = self.cfg.ratings()["tracks"][P]
        self.assertEqual(got["skips"], 3, "the skips are still on the record")
        self.assertGreater(got["thumbs"], 0, "and the star was added on top")

    def test_a_dry_run_writes_neither_the_file_nor_the_store(self):
        self.rate("thumb", 2)
        import unittest.mock as mock
        with mock.patch.object(stars, "write_stars", side_effect=AssertionError("must not write")), \
             mock.patch.object(Path, "exists", return_value=True):
            counts = stars.sync(self.cfg, write=True, read=False, dry_run=True)
        self.assertEqual(counts["exported"], 1)
        self.assertNotIn("stars_seen", self.cfg.ratings()["tracks"][P])

    def test_a_track_with_no_opinion_is_never_written(self):
        st = self.cfg.ratings()
        st.setdefault("tracks", {})[P] = {"skips": 1, "thumbs": 1}   # score 0
        self.cfg.save_ratings(st)
        import unittest.mock as mock
        with mock.patch.object(stars, "write_stars", side_effect=AssertionError("must not write")), \
             mock.patch.object(Path, "exists", return_value=True):
            counts = stars.sync(self.cfg, write=True, read=False)
        self.assertEqual(counts["exported"], 0)

    def test_a_missing_file_is_skipped_quietly(self):
        self.rate("thumb", 2)
        counts = stars.sync(self.cfg, write=True, read=True)   # P does not exist here
        self.assertEqual(counts["exported"], 0)
        self.assertEqual(counts["failed"], 0)


if __name__ == "__main__":
    unittest.main()
