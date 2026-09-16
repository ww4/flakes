import datetime as dt
import logging
import random
import tempfile
import unittest
from pathlib import Path

from netradio import config, feeds, schedule

logging.disable(logging.CRITICAL)

T = dict(path="/mnt/fusion/Music/The Louvin Brothers/Album/01 x.mp3", artist="The Louvin Brothers",
         genre="Country", yamnet={"banjo": 0.4, "singing": 0.02}, era="vintage")


class Rules(unittest.TestCase):
    def test_artist_match_normalised_and_substring(self):
        self.assertTrue(feeds.matches({"artists": ["Louvin Brothers"]}, **T))
        self.assertTrue(feeds.matches({"artists": ["louvin brothers"]}, **{**T, "artist": "Ralph Stanley & The Louvin Brothers"}))
        self.assertTrue(feeds.matches({"artists": ["Louvin Brothers"]}, **{**T, "artist": ""}))   # folder name still hits
        self.assertFalse(feeds.matches({"artists": ["Delmore Brothers"]}, **T))

    def test_genre_and_instruments(self):
        self.assertTrue(feeds.matches({"genres": ["country"]}, **T))
        self.assertFalse(feeds.matches({"genres": ["western swing"]}, **T))
        self.assertTrue(feeds.matches({"instruments": {"banjo": [0.3, None], "singing": [None, 0.05]}}, **T))
        self.assertFalse(feeds.matches({"instruments": {"banjo": [0.5]}}, **T))
        self.assertFalse(feeds.matches({"instruments": {"banjo": [0.3]}}, **{**T, "yamnet": None}))

    def test_clauses_combine_with_and(self):
        # old-time fiddle: the genre AND the instrument
        rule = {"genres": ["country"], "instruments": {"banjo": [0.3]}}
        self.assertTrue(feeds.matches(rule, **T))
        self.assertFalse(feeds.matches(rule, **{**T, "genre": "Rock"}))
        self.assertFalse(feeds.matches(rule, **{**T, "yamnet": {"banjo": 0.1}}))
        # artists OR genres inside the who/what clause
        self.assertTrue(feeds.matches({"artists": ["Nobody"], "genres": ["country"]}, **T))
        self.assertTrue(feeds.matches({"artists": ["Louvin Brothers"], "genres": ["polka"]}, **T))
        self.assertFalse(feeds.matches({"artists": ["Nobody"], "genres": ["polka"]}, **T))

    def test_era(self):
        self.assertFalse(feeds.matches({"all": True, "era": {"only": ["shellac"]}}, **T))
        self.assertTrue(feeds.matches({"all": True, "era": {"only": ["shellac"]}}, **{**T, "era": "shellac"}))
        self.assertFalse(feeds.matches({"all": True, "era": {"only": ["shellac"]}}, **{**T, "era": ""}))   # unmeasured
        self.assertFalse(feeds.matches({"all": True, "era": {"exclude": ["shellac"]}}, **{**T, "era": "shellac"}))
        self.assertTrue(feeds.matches({"all": True, "era": {"exclude": ["shellac"]}}, **{**T, "era": ""}))

    def test_validate(self):
        self.assertEqual(feeds.validate({"artists": ["a"]}), [])
        self.assertTrue(feeds.validate({}))
        self.assertTrue(feeds.validate({"instruments": {"banjo": "high"}}))


class Families(unittest.TestCase):
    def test_compatible(self):
        self.assertTrue(config.compatible(["country"], ["country", "folk"]))
        self.assertFalse(config.compatible(["rock"], ["bluegrass"]))
        self.assertTrue(config.compatible(["rock"], ["any"]))
        self.assertTrue(config.compatible(None, ["rock"]))

    def test_ids(self):
        self.assertEqual(config.new_id("Western Swing!", set()), "western-swing")
        self.assertEqual(config.new_id("Western Swing", {"western-swing"}), "western-swing-2")


class Slots(unittest.TestCase):
    def setUp(self):
        self.slots = [
            {"id": "sat-swing", "station": "country", "name": "Western Swing Hour", "kind": "feed", "feed": "western-swing",
             "days": ["sat"], "start": "10:00", "minutes": 60},
            {"id": "ht", "station": "country", "name": "Honky Tonk Happy Hour", "kind": "feed", "feed": "honky-tonk",
             "days": "weekdays", "start": "17:00", "minutes": 60},
            {"id": "spot", "station": "country", "kind": "auto", "like": "artist", "days": "daily", "start": "20:00", "minutes": 60},
            {"id": "folk", "station": "folk", "name": "Old-Time Hour", "kind": "feed", "feed": "old-time", "days": "daily", "start": "09:00"},
        ]
        self.sat = dt.datetime(2026, 9, 19, 10, 30)   # a Saturday
        self.wed = dt.datetime(2026, 9, 16, 17, 15)

    def test_active_and_upcoming(self):
        self.assertEqual(schedule.active_slot("country", self.slots, self.sat)["id"], "sat-swing")
        self.assertIsNone(schedule.active_slot("country", self.slots, self.sat.replace(hour=12)))
        self.assertEqual(schedule.active_slot("country", self.slots, self.wed)["id"], "ht")
        up = schedule.upcoming("country", self.slots, self.wed.replace(hour=12))
        self.assertEqual([s["id"] for _, _, s in up], ["ht", "spot"])
        self.assertEqual([s["id"] for _, _, s in schedule.upcoming("country", self.slots, self.sat.replace(hour=9))], ["sat-swing", "spot"])

    def test_auto_resolves_once_per_day_and_avoids_recent(self):
        artists = {f"Artist {i}": {"tracks": 20, "families": ["country"]} for i in range(10)}
        artists["Rocker"] = {"tracks": 50, "families": ["rock"]}
        artists["Tiny"] = {"tracks": 2, "families": ["country"]}
        station = {"mount": "country", "family": ["country"]}
        picks = {}
        slot = self.slots[2]
        r1, changed = schedule.resolve(slot, self.wed.date(), picks, station=station, artists=artists, feeds={})
        self.assertTrue(changed)
        self.assertEqual(r1["kind"], "artist")
        self.assertIn(r1["artist"], artists)
        self.assertNotIn(r1["artist"], ("Rocker", "Tiny"))
        self.assertEqual(r1["name"], f"{r1['artist']} spotlight")
        r2, changed = schedule.resolve(slot, self.wed.date(), picks, station=station, artists=artists, feeds={})
        self.assertFalse(changed)
        self.assertEqual(r2["artist"], r1["artist"])                     # same day, same answer
        seen = {r1["artist"]}
        for d in range(1, 6):
            r, _ = schedule.resolve(slot, self.wed.date() + dt.timedelta(days=d), picks, station=station, artists=artists, feeds={})
            self.assertNotIn(r["artist"], seen)                          # a different one each day
            seen.add(r["artist"])

    def test_auto_feed_pick_needs_ready_feeds(self):
        fds = {"western-swing": {"status": "ready", "count": 40, "family": ["country"]},
               "banjo": {"status": "ready", "count": 800, "family": ["bluegrass"]},
               "pending": {"status": "pending", "count": 0, "family": ["country"]}}
        slot = {"id": "x", "station": "country", "kind": "auto", "like": "feed", "days": "daily", "start": "13:00"}
        r, _ = schedule.resolve(slot, self.wed.date(), {}, station={"family": ["country"]}, artists={}, feeds=fds)
        self.assertEqual(r["feed"], "western-swing")
        self.assertEqual(r["name"], "western-swing")

    def test_promos_and_intros(self):
        rng = random.Random(1)
        up = schedule.upcoming("country", self.slots, self.wed.replace(hour=12))
        text = schedule.promo("Classic Country", up, rng)
        self.assertIn("5 PM", text)
        self.assertIn("Honky Tonk Happy Hour", text)
        spot = {"kind": "artist", "artist": "George Jones", "name": "George Jones spotlight"}
        t = schedule.promo("Classic Country", [(self.wed.replace(hour=20, minute=0), self.wed.replace(hour=21, minute=0), spot)], rng)
        self.assertIn("George Jones", t)
        self.assertIn("8 PM", t)
        self.assertIn("George Jones", schedule.intro("Classic Country", spot, rng))
        self.assertIn("Classic Country", schedule.intro("Classic Country", self.slots[0], rng))
        self.assertEqual(schedule.say_time(dt.datetime(2026, 1, 1, 0, 30)), "12:30 AM")
        self.assertEqual(schedule.say_time(dt.datetime(2026, 1, 1, 12, 0)), "12 PM")


class Store(unittest.TestCase):
    def test_seed_once_and_requests(self):
        with tempfile.TemporaryDirectory() as d:
            c = config.Config(Path(d))
            self.assertEqual(c.seed({"a": {}}, [{"mount": "x"}], []), ["feeds.json", "stations.json", "schedule.json"])
            c.save_feeds({"b": {}})
            self.assertEqual(c.seed({"a": {}}, None, None), [])          # never overwrites
            self.assertEqual(c.feeds(), {"b": {}})
            p = c.request("apply", {"why": "test"})
            self.assertTrue(p.exists())
            self.assertTrue(p.name.startswith("apply-"))


if __name__ == "__main__":
    unittest.main()
