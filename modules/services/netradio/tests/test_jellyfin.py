"""The heart, and the merge with Jellyfin's favourites."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import jellyfin, ratings
from netradio.config import Config

A = "/mnt/fusion/Music/Bill Monroe/Bluegrass 1950/03 Uncle Pen.mp3"
B = "/mnt/fusion/Music/Hank Williams/Moanin/01 Lovesick.mp3"


class FakeJF:
    """Jellyfin with the network taken out — the two calls that matter are
    looking a path up and setting the flag."""

    def __init__(self, favourites=(), known=None, refuse=False):
        self.favs = [{"id": f"id-{i}", "path": p, "title": Path(p).stem, "artist": ""}
                     for i, p in enumerate(favourites)]
        self.known = set(known if known is not None else list(favourites) + [A, B])
        self.refuse = refuse
        self.set_calls: list[tuple[str, bool]] = []

    def favourites(self):
        return list(self.favs)

    def item_for_path(self, path):
        return f"id-{path}" if path in self.known else None

    def set_favourite(self, item_id, on):
        self.set_calls.append((item_id, on))
        return not self.refuse


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "stations.json").write_text("[]")
        self.cfg = Config(self.root)
        self.addCleanup(self.tmp.cleanup)

    def entry(self, path):
        return (self.cfg.ratings().get("tracks") or {}).get(path) or {}


class ToggleWithoutJellyfin(Base):
    """The button has to work for somebody who does not run Jellyfin at all."""

    def test_it_stores_locally_and_says_so(self):
        r = jellyfin.toggle(self.cfg, None, A, True, "Bill Monroe", "Uncle Pen")
        self.assertTrue(r["ok"])
        self.assertTrue(r["heart"])
        self.assertFalse(r["mirrored"])
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A))

    def test_it_toggles_back_off(self):
        jellyfin.toggle(self.cfg, None, A, True)
        jellyfin.toggle(self.cfg, None, A, False)
        self.assertFalse(ratings.hearted(self.cfg.ratings(), A))

    def test_a_local_heart_is_not_marked_synced(self):
        """Which is what lets a later merge tell it apart from one REMOVED in
        Jellyfin — the distinction the whole design turns on."""
        jellyfin.toggle(self.cfg, None, A, True)
        self.assertFalse(self.entry(A).get("heart_synced"))


class ToggleWithJellyfin(Base):
    def test_it_mirrors_and_marks_synced(self):
        jf = FakeJF()
        r = jellyfin.toggle(self.cfg, jf, A, True)
        self.assertTrue(r["mirrored"])
        self.assertEqual(jf.set_calls, [(f"id-{A}", True)])
        self.assertTrue(self.entry(A).get("heart_synced"))

    def test_the_local_heart_survives_jellyfin_refusing(self):
        """A heart the listener pressed must not be lost because Jellyfin was
        unreachable; the next merge pushes it up."""
        jf = FakeJF(refuse=True)
        r = jellyfin.toggle(self.cfg, jf, A, True)
        self.assertTrue(r["heart"], "stored locally regardless")
        self.assertFalse(r["mirrored"])
        self.assertIn("next sync", r["message"])
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A))
        self.assertFalse(self.entry(A).get("heart_synced"))

    def test_a_track_jellyfin_does_not_know_is_still_hearted_locally(self):
        jf = FakeJF(known=[])
        r = jellyfin.toggle(self.cfg, jf, A, True)
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A))
        self.assertFalse(r["mirrored"])


class Merge(Base):
    def test_connecting_jellyfin_UNIONS_rather_than_either_side_erasing(self):
        """The case Chris asked about: hearts made locally while Jellyfin was
        not connected, and hearts already in Jellyfin. Both survive."""
        jellyfin.toggle(self.cfg, None, A, True)          # local only
        jf = FakeJF(favourites=[B])                        # Jellyfin has another
        counts = jellyfin.sync(self.cfg, jf)
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A), "the local heart stayed")
        self.assertTrue(ratings.hearted(self.cfg.ratings(), B), "Jellyfin's came down")
        self.assertEqual(counts["pulled"], 1)
        self.assertEqual(counts["pushed"], 1)
        self.assertEqual(counts["cleared"], 0, "nothing may be erased on a first merge")
        self.assertIn((f"id-{A}", True), jf.set_calls, "the local one went up")

    def test_a_heart_removed_in_jellyfin_is_cleared_here(self):
        """Only once the two are in step — which `heart_synced` records."""
        jf = FakeJF(favourites=[A])
        jellyfin.sync(self.cfg, jf)                        # now in step
        self.assertTrue(self.entry(A).get("heart_synced"))
        counts = jellyfin.sync(self.cfg, FakeJF(favourites=[]))   # removed there
        self.assertFalse(ratings.hearted(self.cfg.ratings(), A))
        self.assertEqual(counts["cleared"], 1)

    def test_clearing_a_heart_adds_no_skip(self):
        jf = FakeJF(favourites=[A])
        jellyfin.sync(self.cfg, jf)
        jellyfin.sync(self.cfg, FakeJF(favourites=[]))
        self.assertEqual(ratings.skips(self.cfg.ratings(), A), 0)

    def test_a_second_merge_with_no_changes_does_nothing(self):
        jf = FakeJF(favourites=[A])
        jellyfin.sync(self.cfg, jf)
        counts = jellyfin.sync(self.cfg, FakeJF(favourites=[A]))
        self.assertEqual((counts["pulled"], counts["pushed"], counts["cleared"]), (0, 0, 0))

    def test_a_dry_run_changes_nothing(self):
        jellyfin.toggle(self.cfg, None, A, True)
        before = self.cfg.ratings()
        jf = FakeJF(favourites=[B])
        counts = jellyfin.sync(self.cfg, jf, dry_run=True)
        self.assertGreater(counts["pulled"] + counts["pushed"], 0)
        self.assertEqual(self.cfg.ratings(), before)
        self.assertEqual(jf.set_calls, [], "and must not touch Jellyfin either")

    def test_an_unreachable_jellyfin_leaves_the_local_hearts_alone(self):
        jellyfin.toggle(self.cfg, None, A, True)
        class Down(FakeJF):
            def favourites(self): raise OSError("connection refused")
        counts = jellyfin.sync(self.cfg, Down())
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A))
        self.assertEqual(counts["remote"], 0)


class KeyReading(unittest.TestCase):
    def test_both_shapes_of_key_file_are_understood(self):
        with tempfile.TemporaryDirectory() as d:
            bare, env = Path(d) / "bare", Path(d) / "env"
            bare.write_text("abc123\n")
            env.write_text("# a comment\nJELLYFIN_API_KEY=abc123\n")
            self.assertEqual(jellyfin.read_key(key_file=bare), "abc123")
            self.assertEqual(jellyfin.read_key(key_file=env), "abc123")

    def test_a_missing_file_is_no_key_rather_than_a_crash(self):
        self.assertEqual(jellyfin.read_key(key_file=Path("/nonexistent")), "")
