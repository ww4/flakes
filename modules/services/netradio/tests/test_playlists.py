import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import playlists as pl


class GenreMatching(unittest.TestCase):
    def test_split_handles_separators_and_dashes(self):
        self.assertEqual(pl.split_genre("Folk/Rock"), ["folk", "rock"])
        self.assertEqual(pl.split_genre("Old-Time; Bluegrass"), ["old time", "bluegrass"])
        self.assertEqual(pl.split_genre("Classical - Romantic Era - Late"), ["classical romantic era late"])
        self.assertEqual(pl.split_genre(""), [])

    def test_word_match_is_whole_word(self):
        self.assertTrue(pl.word_in("rock", ["folk rock"]))
        self.assertTrue(pl.word_in("old time", ["old time"]))
        self.assertTrue(pl.word_in("classical", ["classical romantic era late"]))
        self.assertFalse(pl.word_in("rock", ["rockabilly"]))
        self.assertFalse(pl.word_in("soul", ["soulful house"]))
        self.assertTrue(pl.word_in("r&b", ["soul and r&b"]))


class Scan(unittest.TestCase):
    """A fake library: the tag reader is stubbed, so this covers the routing
    from tag to station, exclusions, and the cache — not mutagen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Music"
        self.files = {
            "A/1.mp3": "Bluegrass",
            "A/2.mp3": "Folk/Rock",
            "B/3.m4a": "",
            "B/4.mp3": "Instructional",
            "B/5.mp3": "Old-Time",
            "B/cover.jpg": "n/a",
        }
        for rel, _ in self.files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        self.stations = [
            pl.Station("all", "Everything"),
            pl.Station("bluegrass", "Bluegrass", ["bluegrass", "old time"]),
            pl.Station("rock", "Rock", ["rock"]),
            pl.Station("gospel", "Gospel", ["gospel"]),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def fake_genre(self, path):
        return self.files[str(Path(path).relative_to(self.root))]

    def test_routing_and_counts(self):
        cache = pl.TagCache(Path(self.tmp.name) / "cache.json")
        with mock.patch.object(pl, "read_genre", side_effect=self.fake_genre):
            counts = pl.scan([self.root], self.stations, cache)
        by = {s.mount: [Path(p).name for p in s.paths] for s in self.stations}
        self.assertEqual(by["all"], ["1.mp3", "2.mp3", "3.m4a", "5.mp3"])  # 4 excluded, jpg ignored
        self.assertEqual(by["bluegrass"], ["1.mp3", "5.mp3"])
        self.assertEqual(by["rock"], ["2.mp3"])
        self.assertEqual(by["gospel"], [])
        self.assertEqual(counts["files"], 5)
        self.assertEqual(counts["untagged"], 1)
        self.assertEqual(counts["excluded"], 1)

    def test_cache_skips_unchanged_files(self):
        cache_path = Path(self.tmp.name) / "cache.json"
        with mock.patch.object(pl, "read_genre", side_effect=self.fake_genre) as rg:
            cache = pl.TagCache(cache_path)
            pl.scan([self.root], self.stations, cache)
            cache.save()
            self.assertEqual(rg.call_count, 5)
            # second run: nothing changed -> no tag reads
            cache = pl.TagCache(cache_path)
            for s in self.stations:
                s.paths.clear()
            pl.scan([self.root], self.stations, cache)
            self.assertEqual(rg.call_count, 5)
            self.assertEqual((cache.hits, cache.misses), (5, 0))
            # touch one -> exactly one re-read
            p = self.root / "A/1.mp3"
            p.write_bytes(b"xy")
            cache = pl.TagCache(cache_path)
            pl.scan([self.root], self.stations, cache)
            self.assertEqual(rg.call_count, 6)

    def test_unreadable_dir_is_skipped_not_fatal(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        locked = self.root / "Locked"
        locked.mkdir()
        (locked / "x.mp3").write_bytes(b"x")
        locked.chmod(0o000)
        try:
            cache = pl.TagCache(Path(self.tmp.name) / "cache.json")
            with mock.patch.object(pl, "read_genre", side_effect=self.fake_genre):
                counts = pl.scan([self.root], self.stations, cache)
            self.assertEqual(counts["unreadable_dirs"], 1)
            self.assertEqual(counts["files"], 5)
        finally:
            locked.chmod(0o700)

    def test_playlists_written_atomically_with_header(self):
        out = Path(self.tmp.name) / "out"
        self.stations[0].paths = ["/m/a.mp3", "/m/b.mp3"]
        pl.write_playlists(self.stations, out)
        self.assertEqual((out / "all.m3u").read_text(), "#EXTM3U\n/m/a.mp3\n/m/b.mp3\n")
        self.assertEqual((out / "gospel.m3u").read_text(), "#EXTM3U\n")
        self.assertEqual([p.name for p in out.iterdir() if p.name.startswith(".")], [])

    def test_load_stations_from_module_json(self):
        p = Path(self.tmp.name) / "stations.json"
        p.write_text(json.dumps([
            {"mount": "all", "name": "Everything", "genres": None},
            {"mount": "folk", "name": "Folk", "genres": ["Folk", "Celtic"]},
        ]))
        st = pl.load_stations(p)
        self.assertIsNone(st[0].words)
        self.assertEqual(st[1].words, ["folk", "celtic"])


if __name__ == "__main__":
    unittest.main()
