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
from newsdesk import corpus as corpus_mod  # noqa: E402
from newsdesk import events  # noqa: E402
from newsdesk import longform  # noqa: E402
from newsdesk import sources as sources_mod  # noqa: E402
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


# ---------------------------------------------------------------------------
# the human-editable source list
# ---------------------------------------------------------------------------

class TestSourcesPage(Base):
    """He edits the control block; the table is regenerated and never parsed
    back. Everything here guards the promise that an instruction is either
    applied or returned WITH A REASON — never silently dropped."""

    def setUp(self):
        super().setUp()
        self.add_source("Existing", "linux", cap=2)
        self.page = self.dir / "space" / "Sources.md"
        self.page.parent.mkdir(parents=True, exist_ok=True)
        self._real = feeds.fetch_feed

    def tearDown(self):
        feeds.fetch_feed = self._real
        super().tearDown()

    def _good_feed(self):
        return lambda url, **kw: feeds.FeedResult(
            entries=[feeds.Entry(guid="a", url="u", title="t", body="x",
                                 published=datetime.now(timezone.utc))],
            etag=None, last_modified=None)

    def _write(self, control):
        self.page.write_text("# Newsdesk sources\n\n```control\n" + control + "\n```\n")

    def _sync(self):
        return sources_mod.sync(self.con, self.page)

    def test_add_by_url_validates_the_feed_first(self):
        feeds.fetch_feed = self._good_feed()
        self._write('+ https://new.invalid/feed lane=energy tier=core cap=3 name="New Thing"')
        self._sync()
        row = self.con.execute("SELECT * FROM sources WHERE name='New Thing'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row["lane"], row["tier"], row["cap"]), ("energy", "core", 3))
        self.assertIn("added", self.page.read_text())

    def test_a_url_that_is_not_a_feed_is_refused_with_a_reason(self):
        feeds.fetch_feed = lambda url, **kw: (_ for _ in ()).throw(ValueError("not a feed"))
        self._write("+ https://bad.invalid/page lane=energy")
        self._sync()
        self.assertIsNone(
            self.con.execute("SELECT 1 FROM sources WHERE url LIKE '%bad.invalid%'").fetchone())
        text = self.page.read_text()
        self.assertIn("not added", text)
        self.assertIn("bad.invalid", text, "the failed line must come BACK, not vanish")

    def test_quoted_names_with_spaces_survive(self):
        """Source names have spaces. `name=Some Thing` used to capture only
        'Some' and glue 'Thing' onto the URL."""
        feeds.fetch_feed = self._good_feed()
        self._write('+ https://sp.invalid/feed lane=macro name="Bank of Somewhere"')
        self._sync()
        row = self.con.execute(
            "SELECT url FROM sources WHERE name='Bank of Somewhere'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["url"], "https://sp.invalid/feed")

    def test_add_without_a_lane_is_returned(self):
        feeds.fetch_feed = self._good_feed()
        self._write("+ https://new.invalid/feed")
        self._sync()
        self.assertIn("needs lane=", self.page.read_text())

    def test_disable_keeps_the_row_and_its_grades(self):
        self._write("- Existing")
        self._sync()
        row = self.con.execute("SELECT enabled FROM sources WHERE name='Existing'").fetchone()
        self.assertEqual(row["enabled"], 0)
        self.assertIsNotNone(
            self.con.execute("SELECT 1 FROM sources WHERE name='Existing'").fetchone(),
            "disabling must never delete — history and grades hang off this row")

    def test_reenable_by_name(self):
        self.con.execute("UPDATE sources SET enabled=0, fail_streak=9 WHERE name='Existing'")
        self.con.commit()
        self._write("+ Existing")
        self._sync()
        row = self.con.execute("SELECT enabled, fail_streak FROM sources WHERE name='Existing'").fetchone()
        self.assertEqual((row["enabled"], row["fail_streak"]), (1, 0))

    def test_change_settings(self):
        self._write("= Existing cap=4 tier=firehose")
        self._sync()
        row = self.con.execute("SELECT cap, tier FROM sources WHERE name='Existing'").fetchone()
        self.assertEqual((row["cap"], row["tier"]), (4, "firehose"))

    def test_lane_change_moves_existing_items_too(self):
        self.add_item("Existing", "linux", "t")
        self._write("= Existing lane=ideas")
        self._sync()
        self.assertEqual(
            self.con.execute("SELECT lane FROM items WHERE source='Existing'").fetchone()[0],
            "ideas")

    def test_unknown_name_is_returned_not_ignored(self):
        self._write("- Nonexistent Source")
        self._sync()
        self.assertIn("no source called", self.page.read_text())

    def test_bad_setting_is_returned(self):
        self._write("= Existing frobnicate=7")
        self._sync()
        self.assertIn("unknown setting", self.page.read_text())

    def test_probe_reports_without_adding(self):
        feeds.fetch_feed = self._good_feed()
        self._write("? https://maybe.invalid/feed")
        self._sync()
        self.assertIsNone(
            self.con.execute("SELECT 1 FROM sources WHERE url LIKE '%maybe.invalid%'").fetchone())
        self.assertIn("looks like a feed", self.page.read_text())

    def test_successful_instructions_are_cleared_from_the_block(self):
        self._write("- Existing")
        self._sync()
        block = sources_mod.CONTROL_RE.search(self.page.read_text()).group(1)
        self.assertEqual(block.strip(), "", "an applied instruction must not run twice")

    def test_applying_twice_is_not_double_applied(self):
        feeds.fetch_feed = self._good_feed()
        self._write("+ https://new.invalid/feed lane=energy name=Twice")
        self._sync()
        self._sync()
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM sources WHERE name='Twice'").fetchone()[0], 1)

    def test_duplicate_add_is_refused(self):
        feeds.fetch_feed = self._good_feed()
        self._write("+ https://example.invalid/Existing lane=linux name=Existing")
        self._sync()
        self.assertIn("already in the list", self.page.read_text())

    def test_table_regenerates_and_editing_it_does_nothing(self):
        self._sync()
        text = self.page.read_text()
        self.assertIn("Existing", text)
        self.assertIn("| Source | Tier | Cap |", text)
        # Mangle the table; the next sync must rebuild it and change no state.
        self.page.write_text(text.replace("| Existing", "| DELETED-BY-HAND"))
        self._sync()
        self.assertIsNotNone(
            self.con.execute("SELECT 1 FROM sources WHERE name='Existing'").fetchone())
        self.assertIn("Existing", self.page.read_text())

    def test_missing_page_is_created(self):
        self.assertFalse(self.page.exists())
        self._sync()
        self.assertTrue(self.page.exists())
        self.assertIn("```control", self.page.read_text())


# ---------------------------------------------------------------------------
# good reads: rotation, retirement, archive
# ---------------------------------------------------------------------------

class TestLongform(Base):
    """The rules Chris specified: an unread piece comes back at a lower weight,
    a click retires it, and only picks get archived."""

    def setUp(self):
        super().setUp()
        self.add_source("Essays", "ideas", cap=2)
        self.add_source("Other", "energy", cap=2)

    def _long(self, source, lane, title, **kw):
        return self.add_item(source, lane, title, words=2500, **kw)

    def test_no_recency_filter(self):
        """THE point of the section: a 2019 essay is as good as today's."""
        old = self._long("Essays", "ideas", "ancient essay", published=_iso(1500))
        ids = [c["id"] for c in longform.shortlist(self.con)]
        self.assertIn(old, ids)

    def test_short_items_are_not_good_reads(self):
        self.add_item("Essays", "ideas", "a note", words=200)
        self.assertEqual(longform.shortlist(self.con), [])

    def test_a_click_retires_it_for_good(self):
        i = self._long("Essays", "ideas", "essay")
        self.assertTrue(longform.record_click(self.con, i))
        self.assertEqual(longform.shortlist(self.con), [])

    def test_being_shown_does_not_retire_it(self):
        """Not clicking is not a no. It comes back around."""
        i = self._long("Essays", "ideas", "essay")
        longform.mark_shown(self.con, [i])
        # inside the cooldown it rests...
        self.assertEqual(longform.shortlist(self.con), [])
        # ...and afterwards it returns.
        self.con.execute("UPDATE items SET last_shown_at=? WHERE id=?", (_iso(30), i))
        self.con.commit()
        self.assertEqual([c["id"] for c in longform.shortlist(self.con)], [i])

    def test_it_returns_at_a_lower_weight_but_never_zero(self):
        i = self._long("Essays", "ideas", "essay")
        row = self.con.execute("SELECT * FROM items WHERE id=?", (i,)).fetchone()
        self.assertEqual(longform.weight(row), 1.0)
        self.con.execute("UPDATE items SET shown_count=3 WHERE id=?", (i,))
        self.con.commit()
        row = self.con.execute("SELECT * FROM items WHERE id=?", (i,)).fetchone()
        self.assertLess(longform.weight(row), 1.0)
        self.con.execute("UPDATE items SET shown_count=50 WHERE id=?", (i,))
        self.con.commit()
        row = self.con.execute("SELECT * FROM items WHERE id=?", (i,)).fetchone()
        self.assertGreaterEqual(longform.weight(row), longform.MIN_MULTIPLIER,
                                "a good essay must never decay out of reach")

    def test_thumbs_down_retires_it(self):
        i = self._long("Essays", "ideas", "essay")
        self.con.execute("INSERT INTO grades (item_id, via, value, at) VALUES (?,'web',-1,?)",
                         (i, _iso(0)))
        self.con.commit()
        self.assertEqual(longform.shortlist(self.con), [])

    def test_already_published_as_news_is_excluded(self):
        self._long("Essays", "ideas", "essay", state="published")
        self.assertEqual(longform.shortlist(self.con), [])

    def test_passed_over_by_the_news_judge_is_still_eligible(self):
        """Losing a news slot is not a verdict on it as an essay."""
        i = self._long("Essays", "ideas", "essay", state="passed_over")
        self.assertIn(i, [c["id"] for c in longform.shortlist(self.con)])

    def test_one_per_source_and_one_per_lane(self):
        """The backlog is 30% one author. Without this it is his blog."""
        for n in range(5):
            self._long("Essays", "ideas", f"essay {n}", score=100 - n)
        self._long("Other", "energy", "other essay", score=1)
        picks = longform.shortlist(self.con)
        self.assertEqual(len(picks), 2)
        self.assertEqual({p["source"] for p in picks}, {"Essays", "Other"})
        self.assertEqual({p["lane"] for p in picks}, {"ideas", "energy"})

    def test_a_crowded_lane_does_not_squeeze_out_a_thin_one(self):
        """The backlog is 40% linux and 2% network. Ordering by score alone
        gives three systems essays every morning; the lane rule is what puts
        the thin lanes in front of him. Breaking it left the previous test
        green, which is why this one exists."""
        for n in range(10):
            self.add_source(f"Big{n}", "ideas")
            self._long(f"Big{n}", "ideas", f"ideas essay {n}", score=500 + n)
        self.add_source("Thin", "network")
        self._long("Thin", "network", "the one network essay", score=1)
        picks = longform.shortlist(self.con, limit=3)
        self.assertEqual(len(picks), 3)
        self.assertIn("network", {p["lane"] for p in picks},
                      "the thin lane was crowded out by higher scores")

    def test_selection_does_not_use_the_news_keyword_score(self):
        """The profile ranks NEWS about what he runs. Ordering essays by it
        buries anything not about his stack — which is the opposite of what
        this section is for."""
        self.add_source("Essayist", "ideas")
        self.add_source("Techie", "linux")
        self._long("Techie", "linux", "nixos nixos nixos", score=900)
        self._long("Essayist", "ideas", "a beautiful essay about nothing", score=0)
        # Across many day-seeds the zero-scoring essay must lead sometimes;
        # under score-ordering it would never lead.
        led = sum(1 for d in range(40)
                  if longform.shortlist(self.con, limit=1, seed=f"day-{d}")[0]["source"]
                  == "Essayist")
        self.assertGreater(led, 5, "the low-scoring essay never gets a look")

    def test_a_much_shown_piece_still_comes_around(self):
        self.add_source("A2", "ideas")
        fresh = self._long("Essays", "ideas", "never shown")
        old = self._long("A2", "ideas", "shown five times")
        self.con.execute("UPDATE items SET shown_count=5, last_shown_at=? WHERE id=?",
                         (_iso(60), old))
        self.con.commit()
        led = sum(1 for d in range(60)
                  if longform.shortlist(self.con, limit=1, seed=f"d{d}")[0]["id"] == old)
        self.assertGreater(led, 3, "a repeatedly-skipped piece never resurfaces")
        self.assertLess(led, 40, "a repeatedly-skipped piece is not being demoted at all")

    def test_a_release_note_is_never_a_good_read(self):
        """Found by running against the live backlog: a 1,296-word Bitcoin
        Knots release note took a shortlist slot."""
        self.add_source("Releases", "release-radar")
        self._long("Releases", "release-radar", "v29.3 release notes", score=999)
        self.assertEqual(longform.shortlist(self.con), [])

    def test_a_source_can_opt_out_of_being_a_good_read(self):
        """Newsletters and aggregators are long but are not essays."""
        self._long("Essays", "ideas", "essay")
        self.con.execute("UPDATE sources SET longform=0 WHERE name='Essays'")
        self.con.commit()
        self.assertEqual(longform.shortlist(self.con), [])

    def test_disabled_source_drops_out(self):
        i = self._long("Essays", "ideas", "essay")
        self.con.execute("UPDATE sources SET enabled=0 WHERE name='Essays'")
        self.con.commit()
        self.assertEqual(longform.shortlist(self.con), [])

    def test_archive_writes_a_page_and_records_it(self):
        i = self._long("Essays", "ideas", "essay", body="word " * 900)
        rel = longform.archive(self.con, i, web_dir=self.dir / "web")
        self.assertIsNotNone(rel)
        page = (self.dir / "web" / rel)
        self.assertTrue(page.exists())
        text = page.read_text()
        self.assertIn("original", text)
        self.assertIn("noindex", text, "the personal archive must not be indexable")
        self.assertIn("rights remain with the", text)
        self.assertEqual(
            self.con.execute("SELECT archived_path FROM items WHERE id=?", (i,)).fetchone()[0],
            rel)

    def test_archive_is_idempotent(self):
        i = self._long("Essays", "ideas", "essay", body="word " * 900)
        a = longform.archive(self.con, i, web_dir=self.dir / "web")
        b = longform.archive(self.con, i, web_dir=self.dir / "web")
        self.assertEqual(a, b)

    def test_archive_skips_when_there_is_no_real_text(self):
        i = self._long("Essays", "ideas", "essay", body="too short")
        self.assertIsNone(longform.archive(self.con, i, web_dir=self.dir / "web"))

    def test_archive_escapes_html_in_the_text(self):
        i = self._long("Essays", "ideas", "essay",
                       body="<script>alert(1)</script> " + "word " * 900)
        rel = longform.archive(self.con, i, web_dir=self.dir / "web")
        page = (self.dir / "web" / rel).read_text()
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)


class TestCorpusAndEvergreen(Base):
    def setUp(self):
        super().setUp()
        self.corpus = self.dir / "corpus"
        self.corpus.mkdir()
        self.con.execute(
            "INSERT INTO sources (name, lane, url, tier, cap, kind, evergreen, longform)"
            " VALUES ('Archive','agrarian',?, 'core',1,'corpus',1,1)", (str(self.corpus),))
        self.con.commit()

    def _post(self, name, title, words, date="2004-06-01", junk=False):
        body = "Server error: there was an error. " if junk else ""
        body += "word " * words
        (self.corpus / name).write_text(
            f"---\ndate: {date}\ntitle: '{title}'\nurl: http://e.invalid/{name}\n---\n\n"
            f"# {title}\n*{date}*\n\n{body}")

    def test_ingests_local_markdown(self):
        self._post("a.md", "An essay", 500)
        stats = corpus_mod.ingest(self.con, PROFILE)
        self.assertEqual(stats["added"], 1)
        row = self.con.execute("SELECT title, url, published, words FROM items").fetchone()
        self.assertEqual(row["title"], "An essay")
        self.assertEqual(row["url"], "http://e.invalid/a.md")
        self.assertTrue(row["published"].startswith("2004-06-01"))

    def test_ingest_is_idempotent(self):
        self._post("a.md", "An essay", 500)
        corpus_mod.ingest(self.con, PROFILE)
        corpus_mod.ingest(self.con, PROFILE)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

    def test_scrape_wreckage_is_dropped(self):
        """The real archive contains pages like 'UserLand Frontier Server Error'."""
        self._post("err.md", "UserLand Frontier Server Error", 400, junk=True)
        self._post("ok.md", "A real post", 400)
        stats = corpus_mod.ingest(self.con, PROFILE)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["skipped"], 1)

    def test_stubs_are_dropped(self):
        self._post("stub.md", "Two lines", 20)
        self.assertEqual(corpus_mod.ingest(self.con, PROFILE)["added"], 0)

    def test_a_missing_corpus_directory_is_not_a_quiet_success(self):
        self.con.execute("UPDATE sources SET url='/nonexistent/path' WHERE name='Archive'")
        self.con.commit()
        stats = corpus_mod.ingest(self.con, PROFILE)
        self.assertEqual(stats["missing"], 1)
        self.assertIn("not found",
                      self.con.execute("SELECT last_error FROM sources").fetchone()[0])

    def test_collect_never_tries_to_fetch_a_corpus(self):
        """Polling a directory would fail every run and mark a healthy archive dead."""
        real = feeds.fetch_feed
        feeds.fetch_feed = lambda url, **kw: (_ for _ in ()).throw(
            AssertionError(f"corpus source was fetched over HTTP: {url}"))
        try:
            self.add_source("Feed", "linux", url="https://f.invalid")
            feeds.fetch_feed = FakeFeeds({"https://f.invalid": feeds.FeedResult(
                entries=[], etag=None, last_modified=None)})
            stats = collect_mod.collect(self.con, PROFILE)
            self.assertEqual(stats["sources"], 1, "the corpus source was polled")
        finally:
            feeds.fetch_feed = real

    def test_evergreen_gets_exactly_one_reserved_slot(self):
        """479 Dry Creek posts against a ~500 pool would otherwise appear daily."""
        for n in range(30):
            self._post(f"e{n}.md", f"old essay {n}", 1200)
        corpus_mod.ingest(self.con, PROFILE)
        self.add_source("Fresh", "ideas")
        self.add_item("Fresh", "ideas", "a new essay", words=2000)
        picks = longform.shortlist(self.con)
        ever = [p for p in picks if p["evergreen"]]
        self.assertEqual(len(ever), 1, "evergreen must take exactly one slot")
        self.assertIn("Fresh", {p["source"] for p in picks})

    def test_evergreen_is_flagged_for_the_reader(self):
        self._post("a.md", "An old essay", 1200)
        corpus_mod.ingest(self.con, PROFILE)
        self.assertTrue(longform.shortlist(self.con)[0]["evergreen"])


class TestMissedClicks(Base):
    def test_a_click_on_a_rejected_item_promotes_its_source(self):
        """Chris: a massive indicator it should have been valued higher."""
        self.add_source("Under", "linux", cap=2, weight=1.0)
        i = self.add_item("Under", "linux", "the one he went and read",
                          state="passed_over")
        longform.record_click(self.con, i)
        self.assertEqual([m["id"] for m in feedback.missed_clicks(self.con)], [i])
        report = feedback.tune(self.con, PROFILE)
        row = self.con.execute("SELECT cap, weight FROM sources WHERE name='Under'").fetchone()
        self.assertEqual(row["cap"], 3)
        self.assertGreater(row["weight"], 1.0)
        self.assertIn("not-selected", report)

    def test_one_missed_click_outweighs_the_demotion_threshold(self):
        """A demotion needs four thumbs-down; a missed click needs one."""
        self.add_source("Under", "linux", cap=2)
        i = self.add_item("Under", "linux", "read anyway", state="passed_over")
        longform.record_click(self.con, i)
        feedback.tune(self.con, PROFILE)
        self.assertEqual(
            self.con.execute("SELECT cap FROM sources WHERE name='Under'").fetchone()[0], 3)

    def test_a_click_on_a_published_item_is_not_a_missed_click(self):
        self.add_source("Fine", "linux")
        i = self.add_item("Fine", "linux", "was published", state="published")
        longform.record_click(self.con, i)
        self.assertEqual(feedback.missed_clicks(self.con), [])


class TestEventDetection(Base):
    """Corroboration as its own signal: many independent sources saying the
    same thing at once, where no single item would pass the quality bar."""

    def _src(self, name, role="signal"):
        self.con.execute(
            "INSERT INTO sources (name, lane, url, tier, cap, role)"
            " VALUES (?,'bitcoin',?, 'firehose',1,?)",
            (name, f"https://{name}.invalid", role))
        self.con.commit()

    def _head(self, source, title, days_ago=0.2):
        self.add_item(source, "bitcoin", title, words=120, published=_iso(days_ago))

    def _story(self, n=6, days_ago=0.2):
        heads = [
            "Treasury doubles debt buybacks as bitcoin surges",
            "Bitcoin blasts past highs after Treasury buybacks announcement",
            "Treasury buybacks send bitcoin higher, shorts liquidated",
            "Bitcoin rallies on Treasury debt buybacks plan",
            "Treasury doubles buybacks; bitcoin extends gains",
            "Bitcoin jumps as Treasury confirms doubled buybacks",
        ]
        for i in range(n):
            self._src(f"wire{i}")
            self._head(f"wire{i}", heads[i % len(heads)], days_ago)

    def test_many_sources_on_one_story_is_detected(self):
        self._story()
        clusters = events.detect(self.con)
        self.assertTrue(clusters)
        self.assertGreaterEqual(clusters[0]["n_sources"], 4)
        self.assertIn("treasury", [clusters[0]["term"]] + clusters[0]["also"])

    def test_one_source_repeating_itself_is_not_corroboration(self):
        """Otherwise the highest-volume aggregator wins every single day.

        A/B: the SAME twelve headlines, from one source and then from six.
        Asserting only the negative half passed even with both source gates
        removed, because each gate covered for the other."""
        self._src("loud")
        for i in range(12):
            self._head("loud", f"Treasury doubles debt buybacks as bitcoin surges {i}")
        self.assertEqual(events.detect(self.con), [],
                         "one outlet talking to itself was read as corroboration")

        for n in range(6):
            self._src(f"other{n}")
            self._head(f"other{n}", f"Treasury doubles debt buybacks as bitcoin surges {n}")
        self.assertTrue(events.detect(self.con),
                        "positive control: six sources on the same story must fire")

    def test_an_everyday_topic_is_not_an_event(self):
        """'release' fired on the real corpus before the baseline check.

        Asserts NO cluster at all. Checking only the cluster's headline term
        was blind: the greedy pick could land on 'project' or 'version' and
        the assertion would pass while the cluster still formed."""
        for i in range(6):
            self._src(f"s{i}")
            for d in range(3, 18):                      # long, boring history
                self._head(f"s{i}", f"Project release version {d}", days_ago=d)
            self._head(f"s{i}", "Project release version today")
        self.assertEqual(events.detect(self.con), [],
                         "a topic these sources publish every week is not an event")

    def test_numeric_tokens_are_never_terms(self):
        """'$72,000' tokenises to '000', which clustered a bitcoin rally with
        '$10,000 teaching aide bonuses' and '70,000 doses of Ervebo'.

        Tested on the tokeniser directly. The end-to-end version passed
        vacuously — the numeric cluster was discarded for having no shared
        vocabulary, so the loop it asserted over was empty."""
        terms = events._terms("Bitcoin tops $72,000 today after 70,000 trades")
        numeric = [t for t in terms if not any(c.isalpha() for c in t)]
        self.assertEqual(numeric, [], f"numeric tokens survived: {numeric}")
        self.assertIn("bitcoin", terms)

    def test_a_signal_source_can_never_be_published(self):
        self._src("wire", role="signal")
        self.add_item("wire", "bitcoin", "a wire story", score=999)
        self.assertEqual(
            edition_mod.rank(self.con, "brief", fetch_articles=False)["shortlisted"], 0)

    def test_a_signal_source_can_never_be_a_good_read(self):
        self._src("wire", role="signal")
        self.add_item("wire", "bitcoin", "a long wire piece", words=3000)
        self.assertEqual(longform.shortlist(self.con), [])

    def test_read_sources_still_corroborate(self):
        """A cluster should span both roles — a normal source reporting the
        same thing is corroboration too."""
        self._story(n=5)
        self._src("Wolf Street", role="read")
        self._head("Wolf Street", "Treasury doubles buybacks, swapping old debt")
        cl = events.detect(self.con)
        self.assertTrue(cl)
        self.assertIn("Wolf Street", cl[0]["sources"])
