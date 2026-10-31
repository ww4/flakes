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
                   local="Storms reach Kentucky Thursday afternoon.",
                   hazards=("hail", "wind"), confidence="moderate"):
    con.execute(
        "INSERT INTO extraction (video_id, created_at, fence_score, window_end, "
        "summary, local, hazards, confidence) VALUES (?,?,?,?,?,?,?,?)",
        (video_id, created.isoformat(), fence,
         window_end.isoformat() if window_end else None,
         summary, local, json.dumps(list(hazards)), confidence))
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
        """The card now prints the KENTUCKY line rather than the whole summary,
        so the fixture has to carry a local term — otherwise the text is
        dropped for being about somewhere else and the escaping is never
        exercised. A test that passes because nothing was rendered is not a
        test of escaping."""
        con = mkcon()
        # local="" forces the summary FALLBACK path, which is the one under
        # test here; the dedicated test below covers the `local` field itself.
        add_extraction(con, "v1", created=NOW, window_end=NOW + timedelta(days=1),
                       local="",
                       summary='Kentucky rain <script>alert("x")</script>')
        page = strip.render(sample(), strip.latest_read(con, now=NOW), now=NOW)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_escaping_also_covers_the_local_field(self):
        con = mkcon()
        add_extraction(con, "v1", created=NOW, window_end=NOW + timedelta(days=1),
                       summary="harmless", local='<img src=x onerror="alert(1)">')
        page = strip.render(sample(), strip.latest_read(con, now=NOW), now=NOW)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;img", page)

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


# ------------------------------------------- the 2026-10-30 overflow incident

# The summary that was LIVE on the card when Chris reported the fault, copied
# off https://rosemaryacres.com/wx/strip.html. 1,496 characters on a tile with
# room for two lines. Kept verbatim because the whole point is its length and
# the ratio of Colorado to Kentucky in it.
REAL_LONG_SUMMARY = (
    "Ryan says an omega block has the West stormy and the central and eastern "
    "US stuck under a ridge with record warmth and drought, and that the block "
    "holds for another four or five days. The immediate hazard is in "
    "southwestern Colorado, where tropical moisture from the remnants of "
    "Pacific Hurricane Trudy is driving 4 to 5 inches of rain in the mountains "
    "through Friday on top of existing snowpack, with the flood risk at level "
    "3 of 4 and close to 4; he calls out flash flooding in canyons. He recaps "
    "Wednesday's severe weather out west (Arizona's first tornado watch since "
    "2010, 12 tornado warnings, golf ball hail and 72 mph gusts near Flagstaff, "
    "record rain at Oxnard and Palmdale) as events that already happened. "
    "Looking ahead, a Pacific storm merging with a dropping Canadian jet early "
    "next week should finally shove the block east and break the ridge down, "
    "bringing rain to Texas, Oklahoma and Kansas around Halloween, spreading "
    "Sunday into the Mid-South and Ohio Valley including Louisville, and "
    "reaching the East Coast Monday. He flags likely strong thunderstorms in "
    "Texas and Oklahoma as the next severe weather event but says it is too "
    "early to judge their strength, and notes models disagree on timing with "
    "the European slower and the American faster — he leans faster. Behind it, "
    "highs in the 70s and 80s drop to the 50s and 60s in the first few days of "
    "November, with a wetter pattern for the Midwest and Southeast in the first "
    "week of November."
)


def row_with(**over):
    """A sqlite3.Row-alike. dict is enough — strip.py only subscripts."""
    base = {"summary": "", "local": "", "hazards": "[]", "regions": "[]",
            "confidence": "hedged", "window_start": None, "window_end": None,
            "video_id": "v1"}
    base.update(over)
    return base


class LocalLine(unittest.TestCase):
    def test_the_real_summary_is_far_too_long_for_the_card(self):
        """The bug, stated as a measurement so it cannot quietly return."""
        self.assertGreater(len(REAL_LONG_SUMMARY), 1400)

    def test_local_field_is_preferred_over_the_summary(self):
        line = strip.local_line(row_with(
            summary=REAL_LONG_SUMMARY,
            local="Rain arrives Sunday across the Ohio Valley, then highs drop "
                  "into the 50s and 60s early next week."))
        self.assertTrue(line.startswith("Rain arrives Sunday"))
        self.assertNotIn("Colorado", line)
        self.assertNotIn("Flagstaff", line)

    def test_fallback_keeps_only_the_kentucky_sentences(self):
        """Rows written before `local` existed still must not print Arizona."""
        line = strip.local_line(row_with(summary=REAL_LONG_SUMMARY))
        self.assertIn("Ohio Valley", line)
        for elsewhere in ("Colorado", "Flagstaff", "Oxnard", "Trudy",
                          "Arizona", "tornado watch"):
            self.assertNotIn(elsewhere, line)

    def test_every_output_is_bounded(self):
        for row in (row_with(summary=REAL_LONG_SUMMARY),
                    row_with(local=REAL_LONG_SUMMARY),
                    row_with(local="x" * 900)):
            self.assertLessEqual(len(strip.local_line(row)), strip.LOCAL_MAX + 1)

    def test_truncation_breaks_on_a_word_and_marks_itself(self):
        line = strip.local_line(row_with(local="word " * 200))
        self.assertTrue(line.endswith("…"))
        self.assertNotIn("wo…", line)

    def test_a_summary_about_nowhere_near_here_yields_nothing(self):
        """⚠️ Returning "" is the point: the caller renders "nothing for this
        area", which is true. Printing the first sentence regardless would put
        Colorado's flood risk on the card as though it were his forecast."""
        self.assertEqual(strip.local_line(row_with(
            summary="A Pacific storm is soaking the Oregon coast. "
                    "Flood risk is level 3 of 4 near Medford.")), "")

    def test_missing_column_does_not_raise(self):
        """A database that predates the migration has no `local` key at all."""
        row = {"summary": REAL_LONG_SUMMARY}
        self.assertIn("Ohio Valley", strip.local_line(row))


class NotLocal(unittest.TestCase):
    def test_the_sentinel_is_recognised_loosely(self):
        for text in ("nothing for northern Kentucky",
                     "Nothing for northern Kentucky.",
                     "  nothing for northern kentucky  "):
            self.assertTrue(strip.is_not_local(text), text)

    def test_a_real_forecast_is_not_the_sentinel(self):
        self.assertFalse(strip.is_not_local(
            "Rain reaches northern Kentucky Sunday."))

    def test_nothing_local_drops_the_hazard_chips(self):
        """⚠️ `hazards` is what the VIDEO claimed. On a national video those
        belong to Colorado, and "flash flood" beside "nothing for this area"
        is a contradiction on one line."""
        page = strip.render(sample(), {
            "summary": REAL_LONG_SUMMARY, "local": "nothing for northern Kentucky",
            "hazards": ["flash flood", "heat"], "confidence": "hedged",
            "window_start": None, "window_end": None, "video_id": "v"},
            now=NOW)
        self.assertIn("nothing for this area", page)
        self.assertNotIn("flash flood", page)

    def test_a_local_read_keeps_its_chips(self):
        page = strip.render(sample(), {
            "summary": "", "local": "Rain Sunday across the Ohio Valley.",
            "hazards": ["rain"], "confidence": "hedged",
            "window_start": None, "window_end": None, "video_id": "v"},
            now=NOW)
        self.assertIn("rain", page)
        self.assertIn("Ohio Valley", page)


class LocalHazards(unittest.TestCase):
    def test_a_hazard_the_local_line_does_not_mention_is_dropped(self):
        """⚠️ The live case: the video's hazards were cold/flash flood/heat —
        Colorado's flood and the West's heat — printed beside an Ohio Valley
        line as if they were forecast for this house."""
        kept = strip.local_hazards(
            "spreading Sunday into the Mid-South and Ohio Valley including Louisville",
            ["cold", "flash flood", "heat"])
        self.assertEqual(kept, [])

    def test_a_hazard_the_local_line_names_is_kept(self):
        self.assertEqual(
            strip.local_hazards("Tornado risk reaches Kentucky Thursday.",
                                ["tornado", "flash flood"]),
            ["tornado"])

    def test_capped(self):
        line = "rain hail wind tornado cold in Kentucky"
        self.assertEqual(
            len(strip.local_hazards(line, ["rain", "hail", "wind", "tornado", "cold"])),
            3)

    def test_the_card_drops_them_end_to_end(self):
        page = strip.render(sample(), {
            "summary": "", "local": "Rain spreads into the Ohio Valley Sunday.",
            "hazards": ["flash flood", "heat"], "confidence": "hedged",
            "window_start": None, "window_end": None, "video_id": "v"}, now=NOW)
        self.assertNotIn("flash flood", page)
        self.assertNotIn(">heat<", page)
        self.assertIn("Ohio Valley", page)


class ReadBlockIsClamped(unittest.TestCase):
    def test_the_card_still_contains_the_ten_day_row(self):
        """The structural guarantee: whatever the read block holds, the rest of
        the card is still THERE. Height is CSS and unittest cannot lay it out,
        so this asserts the markup and the clamp declarations; the measured
        proof is in the PR."""
        page = strip.render(sample(), {
            "summary": REAL_LONG_SUMMARY, "local": REAL_LONG_SUMMARY,
            "hazards": [], "confidence": "", "window_start": None,
            "window_end": None, "video_id": "v"}, now=NOW)
        self.assertIn('<div class="ten">', page)
        self.assertEqual(page.count('<div class="d">'), 10)

    def test_clamp_declarations_are_present(self):
        for rule in ("max-height:2.9rem", "-webkit-line-clamp:2",
                     ".now,.say,.ten,.stamp{flex:none}"):
            self.assertIn(rule, strip.CSS if hasattr(strip, "CSS") else strip._CSS,
                          rule + " missing — the forecast can be evicted again")

