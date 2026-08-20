"""Tests for the newsdesk pipeline.

⚠️ These NEVER touch the live database. Every test gets a fresh temp state dir,
and the guard below refuses to run at all if NEWSDESK_STATE points anywhere
near /var/lib — a suite pointed at real data corrupts it silently, which has
happened here before.

The bias of this suite is towards the properties that are easy to break and
expensive to notice: lane fairness, cap enforcement, dry-run purity, one bad
source not taking down a run, and — most of all — that an ungraded item is
never read as disapproval.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from newsdesk import collect as collect_mod  # noqa: E402
from newsdesk import edition as edition_mod  # noqa: E402
from newsdesk import feedback, feeds, gradeserver  # noqa: E402
from newsdesk.db import connect, seed_sources  # noqa: E402
from newsdesk.score import score_text  # noqa: E402

PROFILE = {
    "interests": {"nixos": 6, "wood gas": 8, "bitcoin core": 5, "backup": 3},
    "penalties": {"price target": 8, "celebrity": 8},
}


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class Guard(unittest.TestCase):
    def test_not_pointed_at_live_data(self):
        live = os.environ.get("NEWSDESK_STATE", "")
        self.assertFalse(live.startswith("/var/lib"),
                         "refusing to run: NEWSDESK_STATE points at live data")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ["NEWSDESK_STATE"] = str(self.dir)
        (self.dir / "interests.json").write_text(json.dumps(PROFILE))
        self.con = connect(self.dir / "test.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def add_source(self, name, lane, *, tier="core", cap=2, weight=1.0, url=None,
                   dormant=0):
        self.con.execute(
            "INSERT INTO sources (name, lane, url, tier, cap, weight, dormant)"
            " VALUES (?,?,?,?,?,?,?)",
            (name, lane, url or f"https://example.invalid/{name}", tier, cap,
             weight, dormant))
        self.con.commit()

    # `published` uses a sentinel so an explicit None means SQL NULL (an item
    # whose feed carried no date) rather than "use the default".
    UNSET = object()

    def add_item(self, source, lane, title, *, score=1.0, words=500, state="new",
                 guid=None, body="", published=UNSET, first_seen=None):
        cur = self.con.execute(
            "INSERT INTO items (source, lane, guid, url, title, summary, body,"
            " published, first_seen, words, score, signals, state)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,'[]',?)",
            (source, lane, guid or title, f"https://example.invalid/{title}",
             title, "", body,
             _iso(1) if published is self.UNSET else published,
             first_seen or _iso(1), words, score, state))
        self.con.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

class TestScore(Base):
    def test_density_not_volume(self):
        """A tight piece must beat a rambling one with the same term count."""
        tight, _ = score_text("x", ("nixos " + "filler " * 300), PROFILE)
        long, _ = score_text("x", ("nixos " + "filler " * 3000), PROFILE)
        self.assertGreater(tight, long)

    def test_short_teaser_does_not_dominate(self):
        """The RSS-specific fix: a 20-word teaser must not out-score an article.

        Without the floor, dividing by 0.02 thousand words makes one keyword
        worth fifty times what it is worth in a real article.
        """
        teaser, _ = score_text("news", "nixos " + "word " * 20, PROFILE)
        article, _ = score_text("news", "nixos " + "word " * 400, PROFILE)
        self.assertLess(teaser, article * 3,
                        "short items are being wildly over-scored")

    def test_penalties_suppress(self):
        good, _ = score_text("a", "nixos " * 5 + "word " * 300, PROFILE)
        bad, _ = score_text("a", "nixos " * 5 + "price target " * 5 + "word " * 300,
                            PROFILE)
        self.assertLess(bad, good)

    def test_title_match_counts_double(self):
        in_title, _ = score_text("wood gas rebuild", "word " * 400, PROFILE)
        in_body, _ = score_text("a rebuild", "wood gas " + "word " * 400, PROFILE)
        self.assertGreater(in_title, in_body)

    def test_title_penalty_applies(self):
        """A penalty term in the title must hurt, symmetrically with interests."""
        clean, _ = score_text("nixos release", "word " * 400, PROFILE)
        dirty, _ = score_text("nixos release celebrity", "word " * 400, PROFILE)
        self.assertLess(dirty, clean)


# ---------------------------------------------------------------------------
# feed parsing
# ---------------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Test</title>
<item><title>First &amp; foremost</title><link>https://e.invalid/1</link>
 <guid>g1</guid><pubDate>Tue, 18 Aug 2026 10:00:00 +0000</pubDate>
 <description>&lt;p&gt;Teaser&lt;/p&gt;</description>
 <content:encoded>&lt;p&gt;Full body about nixos&lt;/p&gt;</content:encoded></item>
<item><title>Second</title><link>https://e.invalid/2</link><guid>g2</guid>
 <pubDate>Mon, 17 Aug 2026 10:00:00 +0000</pubDate>
 <description>Plain</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>T</title>
<entry><title>Atom one</title><id>a1</id>
 <link rel="edit" href="https://e.invalid/edit"/>
 <link rel="alternate" href="https://e.invalid/a1"/>
 <updated>2026-08-18T10:00:00Z</updated>
 <content type="html">&lt;p&gt;body&lt;/p&gt;</content></entry></feed>"""


class TestFeeds(unittest.TestCase):
    def test_rss(self):
        e = feeds.parse_feed(RSS)
        self.assertEqual(len(e), 2)
        self.assertEqual(e[0].title, "First & foremost")
        self.assertEqual(e[0].guid, "g1")
        self.assertIn("nixos", e[0].body)
        self.assertEqual(e[0].published.year, 2026)

    def test_atom_prefers_alternate_link(self):
        e = feeds.parse_feed(ATOM)
        self.assertEqual(e[0].url, "https://e.invalid/a1")
        self.assertEqual(e[0].title, "Atom one")

    def test_not_a_feed_raises(self):
        with self.assertRaises(Exception):
            feeds.parse_feed(b"<!doctype html><html><body>hi</body></html>")

    def test_date_formats(self):
        for s in ("Tue, 18 Aug 2026 10:00:00 +0000", "2026-08-18T10:00:00Z",
                  "2026-08-18T10:00:00+00:00", "2026-08-18"):
            self.assertIsNotNone(feeds.parse_date(s), s)
        self.assertIsNone(feeds.parse_date("not a date"))

    def test_strip_html_survives_garbage(self):
        self.assertEqual(feeds.strip_html("<p>a</p><script>x=1</script>"), "a")
        self.assertIn("text", feeds.strip_html("<p>text<<<"))


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------

class FakeFeeds:
    """Stands in for the network. Tests must never make a real request."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, url, *, etag=None, last_modified=None, insecure=False,
                 timeout=30):
        self.calls.append(url)
        outcome = self.mapping[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestCollect(Base):
    def setUp(self):
        super().setUp()
        self._real = feeds.fetch_feed

    def tearDown(self):
        feeds.fetch_feed = self._real
        super().tearDown()

    def _result(self, entries):
        return feeds.FeedResult(entries=entries, etag="e1", last_modified="lm")

    def test_inserts_and_dedups(self):
        self.add_source("A", "linux", url="https://a.invalid")
        entries = [feeds.Entry(guid="g1", url="https://a.invalid/1", title="nixos post",
                               body="nixos " * 50, published=datetime.now(timezone.utc))]
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": self._result(entries)})
        first = collect_mod.collect(self.con, PROFILE)
        second = collect_mod.collect(self.con, PROFILE)
        self.assertEqual(first["new_items"], 1)
        self.assertEqual(second["new_items"], 0, "same guid inserted twice")

    def test_one_bad_source_does_not_stop_the_run(self):
        self.add_source("A", "linux", url="https://a.invalid")
        self.add_source("B", "macro", url="https://b.invalid")
        entries = [feeds.Entry(guid="g", url="https://b.invalid/1", title="t",
                               body="backup " * 50, published=datetime.now(timezone.utc))]
        feeds.fetch_feed = FakeFeeds({
            "https://a.invalid": RuntimeError("boom"),
            "https://b.invalid": self._result(entries),
        })
        stats = collect_mod.collect(self.con, PROFILE)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["new_items"], 1)
        row = self.con.execute("SELECT fail_streak, last_error FROM sources WHERE name='A'").fetchone()
        self.assertEqual(row["fail_streak"], 1)
        self.assertIn("boom", row["last_error"])

    def test_304_is_success_not_failure(self):
        self.add_source("A", "linux", url="https://a.invalid")
        self.con.execute("UPDATE sources SET fail_streak=2 WHERE name='A'")
        self.con.commit()
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": feeds.NotModified()})
        stats = collect_mod.collect(self.con, PROFILE)
        self.assertEqual(stats["not_modified"], 1)
        row = self.con.execute("SELECT fail_streak, last_success FROM sources WHERE name='A'").fetchone()
        self.assertEqual(row["fail_streak"], 0)
        self.assertIsNotNone(row["last_success"])

    def test_empty_roster_fails_closed(self):
        """Zero sources must be an error, not a quiet success."""
        with self.assertRaises(SystemExit):
            collect_mod.collect(self.con, PROFILE)

    def test_published_is_normalised_to_utc(self):
        """Mixed offsets would silently mis-order the recency filter."""
        self.add_source("A", "linux", url="https://a.invalid")
        est = timezone(timedelta(hours=-4))
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": self._result(
            [feeds.Entry(guid="g1", url="u1", title="t", body="x " * 50,
                         published=datetime(2026, 8, 18, 10, 0, tzinfo=est))])})
        collect_mod.collect(self.con, PROFILE)
        pub = self.con.execute("SELECT published FROM items").fetchone()[0]
        self.assertTrue(pub.endswith("+00:00"), pub)
        self.assertIn("T14:00", pub)

    def test_last_item_at_only_moves_forward(self):
        """A feed that drops its history must not look newly stale."""
        self.add_source("A", "linux", url="https://a.invalid")
        recent = datetime.now(timezone.utc)
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": self._result(
            [feeds.Entry(guid="g1", url="u1", title="t", body="x " * 50,
                         published=recent)])})
        collect_mod.collect(self.con, PROFILE)
        after_first = self.con.execute(
            "SELECT last_item_at FROM sources WHERE name='A'").fetchone()[0]
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": self._result(
            [feeds.Entry(guid="g2", url="u2", title="t2", body="x " * 50,
                         published=recent - timedelta(days=400))])})
        collect_mod.collect(self.con, PROFILE)
        after_second = self.con.execute(
            "SELECT last_item_at FROM sources WHERE name='A'").fetchone()[0]
        self.assertEqual(after_first, after_second)


class TestStale(Base):
    def test_failing_source_is_reported(self):
        self.add_source("A", "linux")
        self.con.execute("UPDATE sources SET fail_streak=5, last_error='HTTP 500'"
                         " WHERE name='A'")
        self.con.commit()
        stale = collect_mod.stale_sources(self.con)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["kind"], "failing")

    def test_silent_source_is_reported(self):
        """The Calculated Risk case: polls fine, has simply stopped publishing."""
        self.add_source("A", "macro")
        self.con.execute(
            "UPDATE sources SET last_item_at=?, median_gap_h=24 WHERE name='A'",
            (_iso(200),))
        self.con.commit()
        stale = collect_mod.stale_sources(self.con)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["kind"], "silent")

    def test_rare_source_is_not_called_stale(self):
        """Lyn Alden publishes roughly monthly; that is not a fault."""
        self.add_source("A", "macro")
        self.con.execute(
            "UPDATE sources SET last_item_at=?, median_gap_h=720 WHERE name='A'",
            (_iso(20),))
        self.con.commit()
        self.assertEqual(collect_mod.stale_sources(self.con), [])

    def test_release_feeds_are_never_called_silent(self):
        """Bursty by nature: ten tags in a week then nothing for two months.
        'Bitcoin Core has not released in 41 days' is noise, not signal."""
        self.add_source("Bitcoin Core releases", "release-radar")
        self.con.execute(
            "UPDATE sources SET last_item_at=?, median_gap_h=8"
            " WHERE name='Bitcoin Core releases'", (_iso(60),))
        self.con.commit()
        self.assertEqual(collect_mod.stale_sources(self.con), [])

    def test_a_failing_release_feed_is_still_reported(self):
        """Silence is meaningless for them; a broken poll is not."""
        self.add_source("R", "release-radar")
        self.con.execute("UPDATE sources SET fail_streak=4, last_error='404'"
                         " WHERE name='R'")
        self.con.commit()
        self.assertEqual(len(collect_mod.stale_sources(self.con)), 1)

    def test_dormant_source_is_not_reported_as_stale(self):
        """Ten feeds are kept BECAUSE they are dormant. Warning about them
        every single morning is how the warning stops being read."""
        self.add_source("A", "energy", dormant=1)
        self.con.execute(
            "UPDATE sources SET last_item_at=?, median_gap_h=24 WHERE name='A'",
            (_iso(400),))
        self.con.commit()
        self.assertEqual(collect_mod.stale_sources(self.con), [])

    def test_source_with_no_history_is_not_stale(self):
        self.add_source("A", "macro")
        self.assertEqual(collect_mod.stale_sources(self.con), [])


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------

class TestAwakening(Base):
    def setUp(self):
        super().setUp()
        self._real = feeds.fetch_feed

    def tearDown(self):
        feeds.fetch_feed = self._real
        super().tearDown()

    def _publishes(self, url):
        return FakeFeeds({url: feeds.FeedResult(entries=[
            feeds.Entry(guid="new", url="u", title="It lives",
                        body="wood gas " * 50,
                        published=datetime.now(timezone.utc))], etag=None,
            last_modified=None)})

    def test_dormant_source_that_publishes_is_announced_once(self):
        """Rick's blog restarting is the entire reason it is in the catalogue."""
        self.add_source("Dry Creek", "agrarian", url="https://d.invalid", dormant=1)
        feeds.fetch_feed = self._publishes("https://d.invalid")
        collect_mod.collect(self.con, PROFILE)
        row = self.con.execute(
            "SELECT dormant, awakened_at FROM sources WHERE name='Dry Creek'").fetchone()
        self.assertEqual(row["dormant"], 0)
        self.assertIsNotNone(row["awakened_at"])
        self.assertEqual([a["name"] for a in collect_mod.awakened_sources(self.con)],
                         ["Dry Creek"])

    def test_first_sight_of_a_back_catalogue_is_not_an_awakening(self):
        """Found by running this for real: on a fresh database every item is
        new, so nine of the ten dormant sources 'woke up' on first contact."""
        self.add_source("A", "energy", url="https://a.invalid", dormant=1)
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": feeds.FeedResult(
            entries=[feeds.Entry(guid=f"old{i}", url=f"u{i}", title=f"t{i}",
                                 body="x " * 50,
                                 published=datetime.now(timezone.utc)
                                 - timedelta(days=400 + i))
                     for i in range(20)], etag=None, last_modified=None)})
        stats = collect_mod.collect(self.con, PROFILE)
        self.assertEqual(stats["new_items"], 20)
        self.assertEqual(
            self.con.execute("SELECT dormant FROM sources WHERE name='A'").fetchone()[0],
            1, "a back-catalogue was mistaken for a resurrection")
        self.assertEqual(collect_mod.awakened_sources(self.con), [])

    def test_long_silence_decays_into_dormancy(self):
        """The stale list must not grow without bound: past 120 days the
        warning has been made and the interesting event becomes its RETURN."""
        self.add_source("A", "macro", url="https://a.invalid")
        self.con.execute(
            "UPDATE sources SET last_item_at=?, median_gap_h=24 WHERE name='A'",
            (_iso(200),))
        self.con.commit()
        self.assertEqual(len(collect_mod.stale_sources(self.con)), 1)
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": feeds.FeedResult(
            entries=[], etag=None, last_modified=None)})
        collect_mod.collect(self.con, PROFILE)
        self.assertEqual(
            self.con.execute("SELECT dormant FROM sources WHERE name='A'").fetchone()[0], 1)
        self.assertEqual(collect_mod.stale_sources(self.con), [])

    def test_a_permanently_blocked_source_stops_warning(self):
        """Telecompetitor 403s this box outright. Warning about it every
        morning forever is the same noise problem in another costume."""
        self.add_source("A", "network", url="https://a.invalid")
        self.con.execute("UPDATE sources SET fail_streak=30, last_error='403'"
                         " WHERE name='A'")
        self.con.commit()
        self.assertEqual(len(collect_mod.stale_sources(self.con)), 1)
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": RuntimeError("403")})
        collect_mod.collect(self.con, PROFILE)
        self.assertEqual(
            self.con.execute("SELECT dormant FROM sources WHERE name='A'").fetchone()[0], 1)
        self.assertEqual(collect_mod.stale_sources(self.con), [])

    def test_a_briefly_failing_source_still_warns(self):
        self.add_source("A", "network", url="https://a.invalid")
        self.con.execute("UPDATE sources SET fail_streak=4, last_error='500'"
                         " WHERE name='A'")
        self.con.commit()
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": RuntimeError("500")})
        collect_mod.collect(self.con, PROFILE)
        self.assertEqual(len(collect_mod.stale_sources(self.con)), 1)

    def test_undated_items_never_trigger_an_awakening(self):
        self.add_source("A", "energy", url="https://a.invalid", dormant=1)
        feeds.fetch_feed = FakeFeeds({"https://a.invalid": feeds.FeedResult(
            entries=[feeds.Entry(guid="x", url="u", title="t", body="x " * 50,
                                 published=None)], etag=None, last_modified=None)})
        collect_mod.collect(self.con, PROFILE)
        self.assertEqual(
            self.con.execute("SELECT dormant FROM sources WHERE name='A'").fetchone()[0], 1)

    def test_awakening_is_not_reported_forever(self):
        self.add_source("A", "energy", url="https://a.invalid", dormant=1)
        feeds.fetch_feed = self._publishes("https://a.invalid")
        collect_mod.collect(self.con, PROFILE)
        self.con.execute("UPDATE sources SET awakened_at=? WHERE name='A'", (_iso(30),))
        self.con.commit()
        self.assertEqual(collect_mod.awakened_sources(self.con), [])

    def test_a_deploy_does_not_put_an_awakened_source_back_to_sleep(self):
        cat = self.dir / "cat.json"
        cat.write_text(json.dumps({"sources": [
            {"name": "A", "lane": "energy", "url": "https://a.invalid",
             "tier": "core", "cap": 1, "dormant": True}]}))
        seed_sources(self.con, cat)
        self.con.execute("UPDATE sources SET dormant=0, awakened_at=? WHERE name='A'",
                         (_iso(1),))
        self.con.commit()
        seed_sources(self.con, cat)
        self.assertEqual(
            self.con.execute("SELECT dormant FROM sources WHERE name='A'").fetchone()[0], 0)


class TestRank(Base):
    def test_lane_round_robin_beats_a_firehose(self):
        """23 loud linux sources must not crowd out the quiet energy lane."""
        self.add_source("loud", "linux", cap=50)
        self.add_source("quiet", "energy", cap=50)
        for i in range(60):
            self.add_item("loud", "linux", f"linux {i}", score=100 + i)
        for i in range(5):
            self.add_item("quiet", "energy", f"energy {i}", score=1 + i)
        res = edition_mod.rank(self.con, "brief", fetch_articles=False)
        lanes = [c["lane"] for c in res["candidates"]]
        self.assertEqual(lanes.count("energy"), 5,
                         "the small lane lost its slots to the big one")
        self.assertGreater(lanes.count("linux"), 0)

    def test_per_source_cap_enforced(self):
        self.add_source("A", "linux", cap=2)
        for i in range(10):
            self.add_item("A", "linux", f"t{i}", score=10 - i)
        res = edition_mod.rank(self.con, "brief", fetch_articles=False)
        self.assertEqual(len(res["candidates"]), 2)

    def test_source_weight_reorders(self):
        self.add_source("fav", "linux", cap=1, weight=2.0)
        self.add_source("meh", "linux", cap=1, weight=0.5)
        self.add_item("meh", "linux", "meh item", score=10)
        self.add_item("fav", "linux", "fav item", score=8)
        res = edition_mod.rank(self.con, "brief", fetch_articles=False)
        self.assertEqual(res["candidates"][0]["source"], "fav")

    def test_release_radar_excluded_from_a_plain_brief(self):
        self.add_source("R", "release-radar", cap=5)
        self.add_item("R", "release-radar", "v1.2.3", score=50)
        self.assertEqual(edition_mod.rank(self.con, "brief", fetch_articles=False)["shortlisted"], 0)
        self.assertEqual(edition_mod.rank(self.con, "brief-monday", fetch_articles=False)["shortlisted"], 1)

    def test_longread_requires_length(self):
        self.add_source("A", "ideas", cap=5)
        self.add_item("A", "ideas", "short", score=50, words=100)
        self.add_item("A", "ideas", "long", score=10, words=3000)
        res = edition_mod.rank(self.con, "longread", fetch_articles=False)
        self.assertEqual([c["title"] for c in res["candidates"]], ["long"])

    def test_old_items_are_not_news(self):
        """The first collect pulls years of history; a brief must ignore it."""
        self.add_source("A", "linux", cap=5)
        self.add_item("A", "linux", "ancient", score=500, published=_iso(400))
        self.add_item("A", "linux", "today", score=1, published=_iso(0.5))
        res = edition_mod.rank(self.con, "brief", fetch_articles=False)
        self.assertEqual([c["title"] for c in res["candidates"]], ["today"])

    def test_undated_item_falls_back_to_first_seen(self):
        self.add_source("A", "linux", cap=5)
        self.add_item("A", "linux", "fresh-undated", score=5,
                      published=None, first_seen=_iso(1))
        self.add_item("A", "linux", "stale-undated", score=500,
                      published=None, first_seen=_iso(90))
        res = edition_mod.rank(self.con, "brief", fetch_articles=False)
        self.assertEqual([c["title"] for c in res["candidates"]], ["fresh-undated"])

    def test_longread_reaches_further_back(self):
        """A substantial essay is still worth reading three weeks late."""
        self.add_source("A", "ideas", cap=5)
        self.add_item("A", "ideas", "essay", score=5, words=3000, published=_iso(20))
        self.assertEqual(
            edition_mod.rank(self.con, "brief", fetch_articles=False)["shortlisted"], 0)
        self.assertEqual(
            edition_mod.rank(self.con, "longread", fetch_articles=False)["shortlisted"], 1)

    def test_source_editorial_note_reaches_the_reader(self):
        """A source's standing policy is data. It is useless if the reader
        never sees it — which is exactly how a photo-only build thread got
        published on 2026-08-20."""
        self.add_source("Forum", "energy", cap=2)
        self.con.execute("UPDATE sources SET note=? WHERE name='Forum'",
                         ("EDITORIAL: discussion only, never build logs.",))
        self.con.commit()
        self.add_item("Forum", "energy", "build pictures", score=5)
        res = edition_mod.rank(self.con, "brief", fetch_articles=False)
        self.assertIn("discussion only", res["candidates"][0]["source_note"])

    def test_dry_run_changes_nothing(self):
        self.add_source("A", "linux", cap=5)
        self.add_item("A", "linux", "t", score=5)
        edition_mod.rank(self.con, "brief", fetch_articles=False, dry_run=True)
        state = self.con.execute("SELECT state FROM items").fetchone()[0]
        self.assertEqual(state, "new", "dry run mutated item state")


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------

class TestPublish(Base):
    def setUp(self):
        super().setUp()
        self.add_source("A", "linux", cap=5)
        self.kept = self.add_item("A", "linux", "kept", state="shortlisted")
        self.dropped = self.add_item("A", "linux", "dropped", state="shortlisted")
        self.web = self.dir / "web"
        self.space = self.dir / "space" / "Newsdesk"
        self.space.parent.mkdir(parents=True, exist_ok=True)

    def _publish(self, md):
        return edition_mod.publish(self.con, "brief", md, web_dir=self.web,
                                   space_dir=self.space, cmark="/nonexistent")

    def test_mentioned_published_unmentioned_passed_over(self):
        res = self._publish(f"- **kept** [nd:{self.kept}] — worth it\n\nTLDR: one thing.")
        self.assertEqual(res["published"], 1)
        states = dict(self.con.execute("SELECT id, state FROM items").fetchall())
        self.assertEqual(states[self.kept], "published")
        self.assertEqual(states[self.dropped], "passed_over")

    def test_tldr_extracted(self):
        res = self._publish(f"[nd:{self.kept}]\n\nTLDR: the summary line.")
        self.assertEqual(res["tldr"], "the summary line.")

    def test_empty_judge_output_falls_back_loudly(self):
        """A blank edition must never be indistinguishable from 'no news'."""
        res = self._publish("")
        self.assertTrue(res["fell_back"])
        page = (self.web / "index.html").read_text()
        self.assertIn("reader did not complete", page.lower())
        self.assertEqual(res["published"], 2, "fallback should list the shortlist")

    def test_grading_links_rendered(self):
        self._publish(f"- **kept** [nd:{self.kept}]\n")
        page = (self.web / "index.html").read_text()
        self.assertIn(f"/news/g?e=", page)
        self.assertIn(f"i={self.kept}", page)
        self.assertIn("v=up", page)
        self.assertIn("v=down", page)

    def test_unknown_token_does_not_break_the_page(self):
        res = self._publish("- **ghost** [nd:999999]\n")
        self.assertEqual(res["published"], 0)
        self.assertTrue((self.web / "index.html").exists())

    def test_space_page_written_without_grading_urls(self):
        self._publish(f"- **kept** [nd:{self.kept}]\n")
        pages = list(self.space.glob("*.md"))
        self.assertEqual(len(pages), 1)
        text = pages[0].read_text()
        self.assertIn(f"nd:{self.kept}", text)
        self.assertNotIn("/news/g?", text)

    def test_awakened_sources_appear_in_the_edition(self):
        self.con.execute("UPDATE sources SET awakened_at=? WHERE name='A'", (_iso(1),))
        self.con.commit()
        self._publish(f"[nd:{self.kept}]")
        self.assertIn("Back from the dead", (self.web / "index.html").read_text())

    def test_item_dates_are_rendered_not_left_to_the_reader(self):
        self.con.execute("UPDATE items SET published=? WHERE id=?",
                         ("2026-08-18T10:00:00+00:00", self.kept))
        self.con.commit()
        self._publish(f"- **kept** [nd:{self.kept}]\n")
        self.assertIn("18 Aug", (self.web / "index.html").read_text())

    def test_raw_scores_are_not_shown_to_the_reader(self):
        """They are meaningless to him, and this edition proved they are not
        even correlated with what is worth publishing."""
        self._publish(f"- **kept** [nd:{self.kept}]\n")
        page = (self.web / "index.html").read_text()
        self.assertIn("Not selected", page)
        self.assertNotIn("score", page.lower())

    def test_stale_sources_appear_in_the_edition(self):
        self.con.execute("UPDATE sources SET fail_streak=9, last_error='dead'"
                         " WHERE name='A'")
        self.con.commit()
        self._publish(f"[nd:{self.kept}]")
        page = (self.web / "index.html").read_text()
        self.assertIn("gone quiet", page)


# ---------------------------------------------------------------------------
# grading and tuning
# ---------------------------------------------------------------------------

class TestFeedback(Base):
    """Grades now arrive through the real loopback service.

    These drive an actual HTTP server on an ephemeral port rather than a stub,
    because the previous design was killed by things a stub cannot model — the
    endpoint's behaviour at the edges IS the feature now.
    """

    def setUp(self):
        super().setUp()
        self.add_source("A", "linux", cap=2)
        self.ids = [self.add_item("A", "linux", f"t{i}", state="published")
                    for i in range(6)]
        gradeserver.GradeHandler.db_path = self.dir / "test.db"
        self.httpd = gradeserver.ThreadingHTTPServer(
            ("127.0.0.1", 0), gradeserver.GradeHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _get(self, query: str) -> int:
        url = f"http://127.0.0.1:{self.port}/news/g?{query}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def _grade(self, item_id, v="up") -> int:
        return self._get(f"e=ed-1&i={item_id}&v={v}")

    def test_a_click_records_a_grade(self):
        self.assertEqual(self._grade(self.ids[0], "up"), 204)
        row = self.con.execute("SELECT via, value FROM grades WHERE item_id=?",
                               (self.ids[0],)).fetchone()
        self.assertEqual((row["via"], row["value"]), ("web", 1))

    def test_clicking_twice_does_not_duplicate(self):
        self._grade(self.ids[0], "up")
        self._grade(self.ids[0], "up")
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM grades").fetchone()[0], 1)

    def test_regrading_overwrites(self):
        self._grade(self.ids[0], "up")
        self._grade(self.ids[0], "down")
        self.assertEqual(
            self.con.execute("SELECT value FROM grades WHERE item_id=?",
                             (self.ids[0],)).fetchone()[0], -1)

    def test_malformed_requests_are_rejected(self):
        for q in ("", "i=abc&v=up", f"i={self.ids[0]}", f"i={self.ids[0]}&v=sideways",
                  "i=&v=up"):
            self.assertEqual(self._get(q), 400, q)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM grades").fetchone()[0], 0)

    def test_grade_for_unknown_item_is_404(self):
        self.assertEqual(self._grade(987654), 404)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM grades").fetchone()[0], 0)

    def test_unknown_path_is_404(self):
        url = f"http://127.0.0.1:{self.port}/etc/passwd"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 404)

    def test_space_tags_ingested(self):
        page = self.dir / "space2"
        page.mkdir()
        (page / "e.md").write_text(
            f"- [ ] **thing** `nd:{self.ids[0]}` #good\n"
            f"- [ ] **other** `nd:{self.ids[1]}` #meh\n"
            f"- [ ] **plain** `nd:{self.ids[2]}`\n")
        self.assertEqual(feedback.ingest_space(self.con, page), 2)

    def test_silence_is_not_a_negative_signal(self):
        """THE rule. Six published items, none graded, nothing may change."""
        before = self.con.execute("SELECT cap, weight FROM sources WHERE name='A'").fetchone()
        report = feedback.tune(self.con, PROFILE)
        after = self.con.execute("SELECT cap, weight FROM sources WHERE name='A'").fetchone()
        self.assertEqual((before["cap"], before["weight"]), (after["cap"], after["weight"]))
        self.assertIn("optional", report)

    def test_downvotes_demote_within_bounds(self):
        for i in range(5):
            self._grade(self.ids[i], "down")
        feedback.tune(self.con, PROFILE)
        row = self.con.execute("SELECT cap, weight, enabled FROM sources WHERE name='A'").fetchone()
        self.assertEqual(row["cap"], 1)
        self.assertLess(row["weight"], 1.0)
        self.assertEqual(row["enabled"], 1, "the tuner must never switch a source off")

    def test_cap_never_falls_below_one(self):
        self.con.execute("UPDATE sources SET cap=1 WHERE name='A'")
        self.con.commit()
        for i in range(5):
            self._grade(self.ids[i], "down")
        feedback.tune(self.con, PROFILE)
        self.assertEqual(
            self.con.execute("SELECT cap FROM sources WHERE name='A'").fetchone()[0], 1)

    def test_upvotes_promote(self):
        for i in range(4):
            self._grade(self.ids[i], "up")
        feedback.tune(self.con, PROFILE)
        row = self.con.execute("SELECT cap, weight FROM sources WHERE name='A'").fetchone()
        self.assertEqual(row["cap"], 3)
        self.assertGreater(row["weight"], 1.0)

    def test_mixed_feedback_does_nothing(self):
        self._grade(self.ids[0], "up")
        for i in range(1, 5):
            self._grade(self.ids[i], "down")
        feedback.tune(self.con, PROFILE)
        row = self.con.execute("SELECT cap, weight FROM sources WHERE name='A'").fetchone()
        self.assertEqual((row["cap"], row["weight"]), (2, 1.0))

    def test_term_changes_are_proposed_not_applied(self):
        for i in range(4):
            self.con.execute("UPDATE items SET body='wood gas everywhere' WHERE id=?",
                             (self.ids[i],))
        self.con.commit()
        for i in range(4):
            self._grade(self.ids[i], "down")
        report = feedback.tune(self.con, PROFILE)
        self.assertIn("NOT applied", report)
        self.assertIn("wood gas", report)
        self.assertEqual(PROFILE["interests"]["wood gas"], 8, "profile was mutated")
        kinds = [r[0] for r in self.con.execute("SELECT kind FROM tuning_log")]
        self.assertIn("proposed", kinds)

    def test_both_surfaces_count_once(self):
        page = self.dir / "space3"
        page.mkdir()
        (page / "e.md").write_text(f"- [ ] x `nd:{self.ids[0]}` #good\n")
        self._grade(self.ids[0], "up")
        feedback.ingest_space(self.con, page)
        totals = feedback._grade_totals(self.con)
        self.assertEqual(totals[self.ids[0]], 1)
        self.assertEqual(len(totals), 1)


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------

class TestSeed(Base):
    def _catalogue(self, cap=2):
        p = self.dir / "cat.json"
        p.write_text(json.dumps({"sources": [
            {"name": "A", "lane": "linux", "url": "https://a.invalid",
             "tier": "core", "cap": cap, "note": "n"}]}))
        return p

    def test_seed_inserts_then_refreshes(self):
        added, updated = seed_sources(self.con, self._catalogue())
        self.assertEqual((added, updated), (1, 0))
        added, updated = seed_sources(self.con, self._catalogue())
        self.assertEqual((added, updated), (0, 1))

    def test_seed_never_clobbers_tuned_columns(self):
        """A deploy must not undo a week of his feedback."""
        seed_sources(self.con, self._catalogue(cap=2))
        self.con.execute("UPDATE sources SET cap=5, weight=1.6 WHERE name='A'")
        self.con.commit()
        seed_sources(self.con, self._catalogue(cap=2))
        row = self.con.execute("SELECT cap, weight FROM sources WHERE name='A'").fetchone()
        self.assertEqual((row["cap"], row["weight"]), (5, 1.6))


if __name__ == "__main__":
    unittest.main(verbosity=2)
