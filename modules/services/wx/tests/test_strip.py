"""Tests for the dashboard weather strip.

Same contract as the rest of the suite: these run in the derivation's
checkPhase, so a broken strip fails the BUILD rather than quietly rendering a
wrong card on a dashboard nobody is studying.

What is worth testing here is not "does it produce HTML" — that is obvious on
sight — but the three things that are only observable at the wrong moment:

  - a stale Ryan Hall read must not be presented as current,
  - the home coordinates must not reach the page,
  - an upstream outage must render as an outage, not as yesterday's forecast.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_state = os.environ.get("WX_STATE", "")
if not _state or _state.startswith("/var/lib"):
    raise SystemExit("refusing to run: set WX_STATE to a sandbox path")

from wx import db, strip  # noqa: E402

# Deliberately NOT Chris's coordinates — a test fixture that happens to be the
# real location would make the leak test pass for the wrong reason forever.
LAT, LON = 41.8781, -87.6298
NOW = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)


def mkcon():
    tmp = tempfile.mkdtemp(dir=_state) if Path(_state).exists() else tempfile.mkdtemp()
    return db.connect(Path(tmp) / "t.db")


def add_extraction(con, video_id, *, created, fence=2, window_end=None,
                   summary="A line of storms Thursday afternoon.",
                   hazards=("hail", "wind"), confidence="moderate"):
    con.execute(
        "INSERT INTO extraction (video_id, created_at, fence_score, window_end, "
        "summary, hazards, confidence) VALUES (?,?,?,?,?,?,?)",
        (video_id, created.isoformat(), fence,
         window_end.isoformat() if window_end else None,
         summary, json.dumps(list(hazards)), confidence))
    con.commit()


def sample(days=10, *, codes=None, pops=None):
    codes = codes if codes is not None else [3] * days
    pops = pops if pops is not None else [10] * days
    return {
        "current": {"temperature_2m": 54.1, "apparent_temperature": 51.0,
                    "weather_code": 3, "wind_speed_10m": 8.2,
                    "wind_direction_10m": 315},
        "daily": {
            "time": [f"2026-10-{19 + i:02d}" for i in range(days)],
            "weather_code": codes,
            "temperature_2m_max": [61 + i for i in range(days)],
            "temperature_2m_min": [44 + i for i in range(days)],
            "precipitation_probability_max": pops,
        },
    }


# ------------------------------------------------------------- the Ryan read

class LatestRead(unittest.TestCase):
    def test_in_window_is_shown(self):
        con = mkcon()
        add_extraction(con, "v1", created=NOW - timedelta(days=9),
                       window_end=NOW + timedelta(days=1))
        self.assertIsNotNone(strip.latest_read(con, now=NOW))

    def test_expired_window_is_not_shown(self):
        """The whole point. A read whose window closed is history, and a
        dashboard that renders history in the present tense is lying."""
        con = mkcon()
        add_extraction(con, "v1", created=NOW - timedelta(days=9),
                       window_end=NOW - timedelta(hours=1))
        self.assertIsNone(strip.latest_read(con, now=NOW))

    def test_windowless_read_expires_on_the_fallback_ttl(self):
        con = mkcon()
        add_extraction(con, "fresh", created=NOW - timedelta(days=1))
        self.assertIsNotNone(strip.latest_read(con, now=NOW))

        con2 = mkcon()
        add_extraction(con2, "old", created=NOW - strip.READ_FALLBACK_TTL
                       - timedelta(hours=1))
        self.assertIsNone(strip.latest_read(con2, now=NOW))

    def test_read_outside_the_geofence_is_ignored(self):
        """fence_score below the threshold means he talked about somewhere
        else. Showing it would be a forecast for the wrong state."""
        con = mkcon()
        add_extraction(con, "v1", created=NOW, fence=1,
                       window_end=NOW + timedelta(days=1))
        self.assertIsNone(strip.latest_read(con, now=NOW))

    def test_newest_live_read_wins(self):
        con = mkcon()
        add_extraction(con, "older", created=NOW - timedelta(days=2),
                       window_end=NOW + timedelta(days=1), summary="older")
        add_extraction(con, "newer", created=NOW - timedelta(hours=2),
                       window_end=NOW + timedelta(days=1), summary="newer")
        self.assertEqual(strip.latest_read(con, now=NOW)["summary"], "newer")


# ---------------------------------------------------------------- the render

class Render(unittest.TestCase):
    def test_no_coordinates_in_the_output(self):
        """⚠️ PII guard. ww4/flakes is public and the strip is served to the
        whole tailnet; the coordinates exist only to build the API query."""
        con = mkcon()
        add_extraction(con, "v1", created=NOW, window_end=NOW + timedelta(days=1))
        page = strip.render(sample(), strip.latest_read(con, now=NOW), now=NOW)
        for fragment in (str(LAT), str(LON), f"{LAT:.4f}", f"{LON:.4f}",
                         str(abs(LON)), f"{abs(LON):.4f}"):
            self.assertNotIn(fragment, page)

    def test_quiet_week_says_so_rather_than_leaving_a_hole(self):
        page = strip.render(sample(), None, now=NOW)
        self.assertIn("no current read", page)
        self.assertIn("Ryan Hall", page)

    def test_read_fields_are_html_escaped(self):
        con = mkcon()
        add_extraction(con, "v1", created=NOW, window_end=NOW + timedelta(days=1),
                       summary='<script>alert("x")</script>')
        page = strip.render(sample(), strip.latest_read(con, now=NOW), now=NOW)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_ten_columns_are_rendered(self):
        page = strip.render(sample(10), None, now=NOW)
        self.assertEqual(page.count('<div class="d">'), 10)

    def test_unknown_weather_code_does_not_blank_the_card(self):
        page = strip.render(sample(10, codes=[999] * 10), None, now=NOW)
        self.assertIn('<div class="d">', page)

    def test_short_forecast_does_not_raise(self):
        """zip() over time/weather_code is only as long as the shorter list;
        a truncated upstream response must degrade, not explode."""
        data = sample(10)
        data["daily"]["weather_code"] = data["daily"]["weather_code"][:3]
        page = strip.render(data, None, now=NOW)
        self.assertEqual(page.count('<div class="d">'), 3)

    def test_missing_current_block_still_renders(self):
        page = strip.render({"daily": sample()["daily"]}, None, now=NOW)
        self.assertIn("&deg;", page)


# --------------------------------------------------------------- the summary

class Summary(unittest.TestCase):
    def test_storms_are_named_by_day(self):
        daily = sample(10, codes=[3, 3, 95] + [3] * 7)["daily"]
        self.assertIn("Storms", strip.summary_line(daily))

    def test_storm_today_reads_as_today(self):
        daily = sample(10, codes=[95] + [3] * 9)["daily"]
        self.assertIn("today", strip.summary_line(daily))

    def test_storm_tomorrow_reads_as_tomorrow(self):
        daily = sample(10, codes=[3, 96] + [3] * 8)["daily"]
        self.assertIn("tomorrow", strip.summary_line(daily))

    def test_high_rain_chance_without_storms(self):
        daily = sample(10, pops=[10, 70] + [10] * 8)["daily"]
        line = strip.summary_line(daily)
        self.assertIn("Wet", line)
        self.assertIn("70%", line)

    def test_storms_outrank_a_wet_day(self):
        daily = sample(10, codes=[3, 3, 95] + [3] * 7,
                       pops=[80] + [10] * 9)["daily"]
        self.assertIn("Storms", strip.summary_line(daily))

    def test_settled_week(self):
        self.assertIn("Dry", strip.summary_line(sample(10, pops=[5] * 10)["daily"]))

    def test_middling_week_reports_the_wettest_day(self):
        line = strip.summary_line(sample(10, pops=[10, 45] + [10] * 8)["daily"])
        self.assertIn("45%", line)


# ---------------------------------------------------------------- the outage

class Outage(unittest.TestCase):
    def test_build_renders_the_failure_instead_of_raising(self):
        con = mkcon()
        with mock.patch.object(strip, "fetch",
                               side_effect=urllib.error.URLError("no route")):
            page = strip.build(con, LAT, LON, user_agent="test", now=NOW)
        self.assertIn("could not be fetched", page)

    def test_outage_page_does_not_pretend_to_have_a_forecast(self):
        """⚠️ The anti-keep-last-good assertion. If this ever starts failing
        because someone added a cached fallback, the card has become capable
        of showing a stale forecast as if it were current."""
        page = strip.render_error("no route", now=NOW)
        self.assertNotIn('<div class="ten">', page)
        self.assertIn("Nothing stale", page)

    def test_outage_message_is_escaped(self):
        page = strip.render_error('<img src=x onerror="alert(1)">', now=NOW)
        self.assertNotIn("<img", page)


class Compass(unittest.TestCase):
    def test_cardinals(self):
        self.assertEqual(strip._compass(0), "N")
        self.assertEqual(strip._compass(90), "E")
        self.assertEqual(strip._compass(180), "S")
        self.assertEqual(strip._compass(270), "W")
        self.assertEqual(strip._compass(315), "NW")

    def test_wraps_and_tolerates_nonsense(self):
        self.assertEqual(strip._compass(360), "N")
        self.assertEqual(strip._compass(None), "")


class Fetch(unittest.TestCase):
    def test_query_carries_the_coordinates_and_local_timezone(self):
        captured = {}

        class FakeResponse:
            def read(self):
                return json.dumps(sample()).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            strip.fetch(LAT, LON, user_agent="test")

        self.assertIn("latitude=41.8781", captured["url"])
        # timezone=auto is what makes a "day" a LOCAL day; without it the
        # daily highs are bucketed by UTC and land a column late.
        self.assertIn("timezone=auto", captured["url"])
        self.assertIn("forecast_days=10", captured["url"])


if __name__ == "__main__":
    unittest.main()
