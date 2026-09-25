import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import config, playlists as pl

logging.disable(logging.CRITICAL)


class GenreMatching(unittest.TestCase):
    def test_split_handles_separators_and_dashes(self):
        self.assertEqual(pl.split_genre("Folk/Rock"), ["folk", "rock"])
        self.assertEqual(pl.split_genre("Old-Time; Bluegrass"), ["old time", "bluegrass"])
        self.assertEqual(pl.split_genre(""), [])

    def test_singer_songwriter_is_one_genre(self):
        self.assertEqual(pl.split_genre("Singer/Songwriter; Folk"), ["singer songwriter", "folk"])

    def test_word_match_is_whole_word(self):
        self.assertTrue(pl.word_in("rock", ["folk rock"]))
        self.assertFalse(pl.word_in("rock", ["rockabilly"]))
        self.assertTrue(pl.word_in("r&b", ["soul and r&b"]))

    def test_holiday_by_name_catches_songs_not_places(self):
        for title in ["The Reindeer Boogie", "Santa's Big Parade", "Santa Claus Is Comin' To Town", "Sleigh Ride",
                      "Jingle Bell Rock", "Blue Christmas", "Frosty The Snowman", "Rudolph The Red-Nosed Reindeer"]:
            self.assertTrue(pl.HOLIDAY_NAME.search(title), title)
        for title in ["'Longside The Santa Fe Trail", "Santa Rosa Serenade", "Santa Ana's Retreat", "Cold Frosty Morn",
                      "Santa Câfé", "Billie Holiday", "Snow Bird", "Carlos Santana"]:
            self.assertFalse(pl.HOLIDAY_NAME.search(title), title)

    def test_feed_rule_shellac_default(self):
        self.assertEqual(pl.feed_rule({"rule": {"artists": ["A"]}})["era"], {"exclude": ["shellac"]})
        self.assertNotIn("era", pl.feed_rule({"rule": {"artists": ["A"]}, "shellac": True}))
        self.assertEqual(pl.feed_rule({"rule": {"artists": ["A"], "era": {"only": ["shellac"]}}})["era"], {"only": ["shellac"]})

    def test_families(self):
        self.assertEqual(pl.families_of(["bluegrass"]), ["bluegrass"])
        self.assertEqual(sorted(pl.families_of(["folk rock"])), ["folk", "rock"])
        self.assertEqual(pl.families_of(["other"]), [])


class Build(unittest.TestCase):
    """A fake library on disk with stubbed tags; exercises walk → build end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.root = d / "Music"
        self.files = {   # relative path: (genre, artist)
            "The Louvin Brothers/A/01 a.mp3": ("Country", "The Louvin Brothers"),
            "The Louvin Brothers/A/02 b.mp3": ("Other", "The Louvin Brothers"),
            "Bob Wills/B/01 c.mp3": ("Western Swing", "Bob Wills"),
            "Boston/C/01 d.mp3": ("Rock", "Boston"),
            "Hal Leonard/D/01 e.mp3": ("Instructional", "Hal Leonard"),
            "Talker/E/01 f.mp3": ("Country", "Talker"),
            "Byrds/F/01 g.mp3": ("Folk Rock; Country", "Byrds"),          # country by a later tag only: fringe
            "Dan Gellert/G/01 h.mp3": ("Country", "Dan Gellert"),        # tagged Country, but the old-time feed holds him
            "Dan Gellert/G/02 i.mp3": ("Country", "Dan Gellert"),
            "X/cover.jpg": ("", ""),
        }
        for rel in self.files:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        self.cfg = config.Config(d / "config")
        self.cfg.seed(
            {"brother-duets": {"title": "Brother Duets", "status": "ready", "family": ["bluegrass", "country"],
                               "shellac": True, "rule": {"artists": ["Louvin Brothers"]}},
             "western-swing": {"title": "Western Swing", "status": "ready", "family": ["country"],
                               "rule": {"genres": ["western swing"]}},
             "old-time": {"title": "Old-Time", "status": "ready", "family": ["bluegrass"],
                          "rule": {"artists": ["Dan Gellert"]}},
             "draft": {"title": "Draft", "status": "pending"}},
            [{"mount": "all", "name": "Everything", "kind": "curated", "family": ["any"], "base": {"all": True}},
             {"mount": "country", "name": "Classic Country", "kind": "curated", "family": ["country"],
              "base": {"genres": ["country", "western swing"], "era": {"exclude": ["shellac"]},
                       "exclude_genres": ["bluegrass", "old time"], "fringe_genres": ["rock"]}},
             {"mount": "brother-duets", "name": "Brother Duets", "kind": "specialty", "feed": "brother-duets"}],
            [])
        self.out = d / "playlists"
        self.pools = d / "pools"

    def tearDown(self):
        self.tmp.cleanup()

    def fake_tags(self, path):
        return self.files[str(Path(path).relative_to(self.root))]

    def test_walk_and_build(self):
        cache = pl.TagCache(Path(self.tmp.name) / "cache.json")
        with mock.patch.object(pl, "read_tags", side_effect=self.fake_tags):
            tracks, counts = pl.walk([self.root], cache)
        self.assertEqual(counts["files"], 9)
        self.assertEqual(counts["excluded"], 1)                       # the lesson
        self.assertEqual(len(tracks), 8)
        talk = {str(self.root / "Talker/E/01 f.mp3")}
        n = pl.build(tracks, self.cfg, self.out, self.pools, talk=talk,
                     summary=Path(self.tmp.name) / "summary.json", ycast=Path(self.tmp.name) / "stations.yml",
                     public_base="http://h/radio", quick_picks=[{"name": "NPR", "url": "http://npr"}],
                     web_base="https://r/radio")
        # country's core: the "Other"-tagged Louvin track is not country by tag; the
        # Byrds are country by a later tag only (fringe); Dan Gellert says Country
        # but the old-time feed vouches for him (out — the exclusion's intent)
        self.assertEqual(n, {"all": 7, "country": 2, "brother-duets": 2})
        self.assertEqual((self.out / "country-fringe.m3u").read_text().count(".mp3"), 1)
        self.assertIn("Byrds", (self.out / "country-fringe.m3u").read_text())
        self.assertNotIn("Gellert", (self.out / "country.m3u").read_text() + (self.out / "country-fringe.m3u").read_text())
        self.assertEqual((self.out / "all-fringe.m3u").read_text().count(".mp3"), 0)
        self.assertEqual((self.pools / "feeds" / "western-swing.m3u").read_text().count(".mp3"), 1)
        self.assertFalse((self.pools / "feeds" / "draft.m3u").exists())
        feeds = self.cfg.feeds()
        self.assertEqual((feeds["brother-duets"]["count"], feeds["western-swing"]["count"], feeds["draft"]["count"]), (2, 1, 0))
        artists = self.cfg.artists()
        self.assertEqual(artists["The Louvin Brothers"]["tracks"], 2)
        self.assertEqual(artists["The Louvin Brothers"]["families"], ["bluegrass", "country"])   # tag + inherited from the feed
        self.assertEqual(artists["Boston"]["families"], ["rock"])
        self.assertNotIn("Talker", artists)                           # talk tracks are not an artist pool
        self.assertTrue((self.pools / "artists" / "bob-wills.m3u").exists())
        yml = (Path(self.tmp.name) / "stations.yml").read_text()
        self.assertIn('Curated:\n  "Everything": "http://h/radio/all.mp3"', yml)
        self.assertIn('Specialty:\n  "Brother Duets": "http://h/radio/brother-duets.mp3"', yml)
        self.assertIn('"NPR": "http://npr"', yml)
        self.assertEqual(json.loads((Path(self.tmp.name) / "summary.json").read_text())["country"], {"tracks": 2, "fringe": 1})
        cat = json.loads((Path(self.tmp.name) / "catalogue.json").read_text())
        self.assertEqual([c["mount"] for c in cat], ["all", "country", "brother-duets"])
        self.assertIn("https://r/radio/brother-duets-lo.mp3", (Path(self.tmp.name) / "stations-lo.m3u").read_text())
        self.assertIn("NumberOfEntries=3", (Path(self.tmp.name) / "stations.pls").read_text())

    def test_cache_skips_unchanged_files_and_ignores_old_format(self):
        cache_path = Path(self.tmp.name) / "cache.json"
        cache_path.write_text(json.dumps({"/old/style.mp3": [1, 2, "genre"]}))   # v1 cache: ignored
        with mock.patch.object(pl, "read_tags", side_effect=self.fake_tags) as rt:
            cache = pl.TagCache(cache_path)
            pl.walk([self.root], cache)
            cache.save()
            self.assertEqual(rt.call_count, 9)
            cache = pl.TagCache(cache_path)
            pl.walk([self.root], cache)
            self.assertEqual(rt.call_count, 9)
            self.assertEqual((cache.hits, cache.misses), (9, 0))

    def test_unreadable_dir_is_skipped_not_fatal(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        locked = self.root / "Locked"
        locked.mkdir()
        (locked / "x.mp3").write_bytes(b"x")
        locked.chmod(0o000)
        try:
            with mock.patch.object(pl, "read_tags", side_effect=self.fake_tags):
                _, counts = pl.walk([self.root], pl.TagCache(Path(self.tmp.name) / "c.json"))
            self.assertEqual(counts["unreadable_dirs"], 1)
        finally:
            locked.chmod(0o700)


if __name__ == "__main__":
    unittest.main()


class Tiles(unittest.TestCase):
    def test_covers_are_unique_across_stations_and_all_gets_a_glyph(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            def track(artist, album, n):
                p = root / artist / album / f"{n:02d} x.mp3"; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"\x00"); return str(p)
            # five artists with one album each, all with a cover file; A shared by two stations
            paths = {a: track(a, "Album", 1) for a in "ABCDE"}
            for a in "ABCDE":
                (root / a / "Album" / "cover.jpg").write_bytes(b"jpg")
            artist_of = {p: a for a, p in paths.items()}
            stations = [{"mount": "all", "kind": "curated", "base": {"all": True}},
                        {"mount": "x", "kind": "curated", "base": {"genres": ["x"]}},
                        {"mount": "y", "kind": "curated", "base": {"genres": ["y"]}}]
            pools = {"all": list(paths.values()), "x": [paths[a] for a in "ABCD"], "y": [paths[a] for a in "AE"]}
            from netradio import playlists
            with mock.patch.object(playlists, "has_art", return_value=True):
                tiles = playlists.station_tiles(stations, pools, {}, artist_of)
            self.assertEqual(tiles["all"], {"icon": "radio", "covers": []})
            self.assertEqual(len(tiles["y"]["covers"]), 2)                     # the smaller station picks first: A and E
            self.assertEqual(len(tiles["x"]["covers"]), 3)                     # so x gets B, C, D — a hero, not a mosaic — rather than reuse A
            used = [Path(p).parent for t in tiles.values() for p in t["covers"]]
            self.assertEqual(len(used), len(set(used)))                        # no album folder on two tiles


class ArtistScope(unittest.TestCase):
    """`Artist :: album words` — an artist admitted for part of their work."""

    def hit(self, rule, artist, path):
        from netradio import feeds
        return feeds.artist_hit(rule, artist, path)

    def test_only_the_named_albums_count(self):
        rule = ["Bear McCreary :: Outlander"]
        self.assertTrue(self.hit(rule, "Bear McCreary", "/mnt/fusion/Music/Bear McCreary/Outlander Vol 1/01 x.mp3"))
        self.assertFalse(self.hit(rule, "Bear McCreary", "/mnt/fusion/Music/Bear McCreary/The Singularity/01 y.mp3"))
        self.assertFalse(self.hit(rule, "Bear McCreary", "/mnt/fusion/Music/Bear McCreary/Ekleipsis/01 z.mp3"))

    def test_an_unscoped_entry_still_takes_the_whole_artist(self):
        rule = ["Clannad"]
        for album in ("Magical Ring", "Anam"):
            self.assertTrue(self.hit(rule, "Clannad", f"/mnt/fusion/Music/Clannad/{album}/01 a.mp3"))

    def test_a_scoped_entry_does_not_match_a_different_artist(self):
        self.assertFalse(self.hit(["Bear McCreary :: Outlander"], "Raya Yarbrough",
                                  "/mnt/fusion/Music/Raya Yarbrough/Outlander Songs/01 q.mp3"))
