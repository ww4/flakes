"""Tests for wx. These run in the derivation's checkPhase, so a broken pipeline
fails the BUILD and a bad merge cannot deploy.

What is tested here is the part that is otherwise only observable during an
actual storm: the dedup that decides he is not told twice, the four clauses of
the Layer 3 threshold, and the quiet-hours contract. Those are exactly the
behaviours nobody can check by watching it run on a calm Tuesday.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ⚠️ Same guard as the newsdesk suite: a test run that can reach live state
# will eventually corrupt it. Refuse rather than trust the environment.
_state = os.environ.get("WX_STATE", "")
if not _state or _state.startswith("/var/lib"):
    raise SystemExit("refusing to run: set WX_STATE to a sandbox path")

from wx import db, extract, geo, notify, render, rules, ryan  # noqa: E402
from wx import nws  # noqa: E402


def mkcon():
    tmp = tempfile.mkdtemp(dir=_state) if Path(_state).exists() else tempfile.mkdtemp()
    return db.connect(Path(tmp) / "t.db")


# --------------------------------------------------------------------------- geo

SQUARE = {"type": "Polygon", "coordinates": [
    [[-85.0, 38.0], [-84.0, 38.0], [-84.0, 39.0], [-85.0, 39.0], [-85.0, 38.0]]]}

# A square with a square hole punched out of the middle. SPC draws these.
HOLED = {"type": "Polygon", "coordinates": [
    [[-85.0, 38.0], [-84.0, 38.0], [-84.0, 39.0], [-85.0, 39.0], [-85.0, 38.0]],
    [[-84.7, 38.3], [-84.3, 38.3], [-84.3, 38.7], [-84.7, 38.7], [-84.7, 38.3]]]}


class TestGeo(unittest.TestCase):
    def test_point_inside_and_outside(self):
        self.assertTrue(geo.point_in_geometry(SQUARE, -84.5, 38.5))
        self.assertFalse(geo.point_in_geometry(SQUARE, -83.5, 38.5))
        self.assertFalse(geo.point_in_geometry(SQUARE, -84.5, 39.5))

    def test_hole_is_not_inside(self):
        self.assertFalse(geo.point_in_geometry(HOLED, -84.5, 38.5))   # in the hole
        self.assertTrue(geo.point_in_geometry(HOLED, -84.9, 38.1))    # in the ring

    def test_multipolygon(self):
        multi = {"type": "MultiPolygon",
                 "coordinates": [SQUARE["coordinates"], HOLED["coordinates"]]}
        self.assertTrue(geo.point_in_geometry(multi, -84.5, 38.5))

    def test_missing_geometry_never_matches(self):
        # An alert with no polygon must not silently match his house.
        self.assertFalse(geo.point_in_geometry(None, -84.5, 38.5))
        self.assertFalse(geo.point_in_geometry({"type": "Point"}, -84.5, 38.5))

    def test_fence_strong_term_alone_passes(self):
        self.assertTrue(geo.in_fence(["Northern Kentucky"]))
        self.assertTrue(geo.in_fence(["the Ohio Valley"]))

    def test_fence_one_weak_term_is_not_enough(self):
        self.assertFalse(geo.in_fence(["the Midwest"]))
        self.assertFalse(geo.in_fence([]))

    def test_fence_two_weak_terms_pass(self):
        self.assertTrue(geo.in_fence(["the Midwest", "Tennessee Valley"]))

    def test_fence_scores_terms_not_mentions(self):
        # Saying Kentucky four times is one signal, not four.
        self.assertEqual(geo.fence_score(["Kentucky", "Kentucky", "Kentucky"]),
                         geo.fence_score(["Kentucky"]))


# --------------------------------------------------------------------------- nws

def alert(event, aid, **kw):
    props = {"id": aid, "event": event, "severity": "Severe",
             "headline": kw.pop("headline", f"{event} issued"), "description": ""}
    props.update(kw)
    return props


class TestNwsClassify(unittest.TestCase):
    def test_tornado_warning_is_critical(self):
        self.assertEqual(nws.classify(alert("Tornado Warning", "a")), "critical")

    def test_severe_thunderstorm_warning_is_not_critical(self):
        # Chris was explicit about this one on 2026-09-07.
        self.assertEqual(nws.classify(alert("Severe Thunderstorm Warning", "a")),
                         "warning")

    def test_flash_flood_emergency_detected_in_the_text(self):
        # It arrives as a Flash Flood Warning; the event name alone misses it.
        props = alert("Flash Flood Warning", "a",
                      description="...FLASH FLOOD EMERGENCY FOR OWEN COUNTY...")
        self.assertEqual(nws.classify(props), "critical")

    def test_extreme_severity_is_critical_whatever_the_event(self):
        self.assertEqual(nws.classify(alert("Odd Product", "a", severity="Extreme")),
                         "critical")

    def test_advisories_are_info(self):
        self.assertEqual(nws.classify(alert("Heat Advisory", "a")), "info")
        self.assertEqual(nws.classify(alert("Special Weather Statement", "a")), "info")

    def test_family(self):
        self.assertEqual(nws.family("Tornado Warning"), "Tornado")
        self.assertEqual(nws.family("Tornado Watch"), "Tornado")


class TestNwsProcess(unittest.TestCase):
    def setUp(self):
        self.con = mkcon()

    def test_new_warning_is_reported_once(self):
        a = [alert("Tornado Warning", "urn:1")]
        self.assertEqual(len(nws.process(self.con, a)), 1)
        # Same id again on the next poll: silence.
        self.assertEqual(len(nws.process(self.con, a)), 0)

    def test_reissue_with_a_new_id_is_suppressed(self):
        t0 = datetime(2026, 9, 7, 18, 0, tzinfo=timezone.utc)
        nws.process(self.con, [alert("Tornado Warning", "urn:1")], when=t0)
        # NWS reissues the same warning minutes later under a new id.
        out = nws.process(self.con, [alert("Tornado Warning", "urn:2")],
                          when=t0 + timedelta(minutes=12))
        self.assertEqual(out, [], "a reissue must not push a second time")

    def test_escalation_gets_through_the_cooldown(self):
        t0 = datetime(2026, 9, 7, 18, 0, tzinfo=timezone.utc)
        nws.process(self.con, [alert("Tornado Watch", "urn:1")], when=t0)
        out = nws.process(self.con, [alert("Tornado Warning", "urn:2")],
                          when=t0 + timedelta(minutes=20))
        self.assertEqual(len(out), 1, "watch -> warning is an upgrade, not a reissue")
        self.assertEqual(out[0]["class"], "critical")
        self.assertTrue(out[0]["escalated"])

    def test_after_the_cooldown_a_fresh_episode_reports_again(self):
        t0 = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        nws.process(self.con, [alert("Severe Thunderstorm Warning", "urn:1")], when=t0)
        out = nws.process(self.con, [alert("Severe Thunderstorm Warning", "urn:9")],
                          when=t0 + timedelta(hours=5))
        self.assertEqual(len(out), 1)

    def test_info_alerts_never_report(self):
        self.assertEqual(nws.process(self.con, [alert("Heat Advisory", "urn:1")]), [])

    def test_unrelated_families_do_not_suppress_each_other(self):
        t0 = datetime(2026, 9, 7, 18, 0, tzinfo=timezone.utc)
        nws.process(self.con, [alert("Flash Flood Warning", "urn:1")], when=t0)
        out = nws.process(self.con, [alert("Tornado Warning", "urn:2")],
                          when=t0 + timedelta(minutes=5))
        self.assertEqual(len(out), 1)


# ------------------------------------------------------------------------- rules

BASE = {
    "system_id": "2026-09-10-ohv",
    "hazards": ["tornado"],
    "regions": ["northern Kentucky"],
    "window_start": "2026-09-10",
    "window_end": "2026-09-10",
    "confidence": "confident",
    "escalation": "flat",
    "fence_score": 2,
}
TODAY = date(2026, 9, 7)


def fields(**kw):
    out = dict(BASE)
    out.update(kw)
    return out


class TestWindowOffsets(unittest.TestCase):
    def test_forward_window(self):
        self.assertEqual(rules.window_offsets("2026-09-09", "2026-09-10", TODAY), [2, 3])

    def test_past_window_is_empty(self):
        self.assertEqual(rules.window_offsets("2026-09-01", "2026-09-02", TODAY), [])

    def test_missing_dates(self):
        self.assertEqual(rules.window_offsets(None, None, TODAY), [])


class TestSpcAhead(unittest.TestCase):
    def test_beyond_the_horizon_is_always_ahead(self):
        self.assertTrue(rules.spc_is_ahead([5], drawn=set(), known={1, 2, 3}))

    def test_looked_and_blank_is_ahead(self):
        self.assertTrue(rules.spc_is_ahead([2], drawn=set(), known={1, 2, 3}))

    def test_drawn_is_not_ahead(self):
        self.assertFalse(rules.spc_is_ahead([2], drawn={2}, known={1, 2, 3}))

    def test_could_not_look_is_treated_as_covered(self):
        # The inverse would turn every SPC outage into a push storm.
        self.assertFalse(rules.spc_is_ahead([2], drawn=set(), known=set()))


class TestThreshold(unittest.TestCase):
    def setUp(self):
        self.con = mkcon()

    def ev(self, f, drawn=frozenset(), known=frozenset({1, 2, 3})):
        return rules.evaluate(f, drawn=set(drawn), known=set(known), today=TODAY)

    def test_pushes_when_ahead_of_spc(self):
        d = self.ev(fields())
        self.assertTrue(d.push, d.reason)
        self.assertIn("ahead of SPC", d.reason)

    def test_quiet_when_spc_already_has_it(self):
        d = self.ev(fields(), drawn={3})
        self.assertFalse(d.push)
        self.assertIn("SPC already has it", d.reason)

    def test_escalation_pushes_even_when_spc_has_it(self):
        d = self.ev(fields(escalation="up"), drawn={3})
        self.assertTrue(d.push, d.reason)
        self.assertIn("escalating", d.reason)

    def test_non_severe_hazard_never_pushes(self):
        self.assertFalse(self.ev(fields(hazards=["heat"])).push)
        self.assertFalse(self.ev(fields(hazards=["rain"])).push)

    def test_outside_the_fence_never_pushes(self):
        self.assertFalse(self.ev(fields(regions=["west Texas"], fence_score=0)).push)

    def test_beyond_seven_days_never_pushes(self):
        far = fields(window_start="2026-09-30", window_end="2026-09-30")
        self.assertFalse(self.ev(far).push)

    def test_speculative_without_escalation_is_quiet(self):
        self.assertFalse(self.ev(fields(confidence="speculative")).push)

    def test_speculative_with_escalation_pushes(self):
        self.assertTrue(self.ev(fields(confidence="speculative", escalation="up")).push)

    def test_recap_of_past_weather_never_pushes(self):
        past = fields(window_start="2026-09-01", window_end="2026-09-02")
        self.assertFalse(self.ev(past).push)

    def test_per_system_cooldown(self):
        f = fields()
        now_ = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        recent = rules.evaluate(f, drawn=set(), known={1, 2, 3}, today=TODAY,
                                previous_push=now_ - timedelta(hours=3), when=now_)
        self.assertFalse(recent.push)
        later = rules.evaluate(f, drawn=set(), known={1, 2, 3}, today=TODAY,
                               previous_push=now_ - timedelta(hours=30), when=now_)
        self.assertTrue(later.push)

    def test_decide_reads_the_cooldown_from_the_push_log(self):
        db.record_push(self.con, layer=3, key="ryan:2026-09-10-ohv",
                       priority="default", title="t", body="b")
        d = rules.decide(self.con, fields(), drawn=set(), known={1, 2, 3}, today=TODAY)
        self.assertFalse(d.push)


# ------------------------------------------------------------------------- ryan

JSON3 = json.dumps({"events": [
    {"tStartMs": 0, "segs": [{"utf8": "A big "}, {"utf8": "severe"}]},
    {"tStartMs": 900, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 1800, "segs": [{"utf8": " threat for Kentucky."}]},
]})


class TestRyan(unittest.TestCase):
    def setUp(self):
        self.con = mkcon()

    def test_parse_json3_flattens_to_prose(self):
        self.assertEqual(ryan.parse_json3(JSON3), "A big severe threat for Kentucky.")

    def test_sync_listing_is_idempotent(self):
        vids = [{"id": "a", "title": "T", "duration": 700}]
        self.assertEqual(ryan.sync_listing(self.con, vids), 1)
        self.assertEqual(ryan.sync_listing(self.con, vids), 0)

    def test_throttling_does_not_burn_the_attempt_budget(self):
        ryan.sync_listing(self.con, [{"id": "a", "title": "T", "duration": 700}])
        for _ in range(4):
            state = ryan.record_failure(self.con, "a", "HTTP Error 429: Too Many Requests")
            self.assertEqual(state, "deferred")
        row = self.con.execute("SELECT attempts FROM video WHERE id='a'").fetchone()
        self.assertLess(row["attempts"], ryan.MAX_ATTEMPTS)

    def test_real_failures_eventually_give_up(self):
        ryan.sync_listing(self.con, [{"id": "b", "title": "T", "duration": 700}])
        state = "deferred"
        for _ in range(ryan.MAX_ATTEMPTS):
            state = ryan.record_failure(self.con, "b", "video unavailable")
        self.assertEqual(state, "failed")

    def test_backoff_holds_a_deferred_video_back(self):
        ryan.sync_listing(self.con, [{"id": "c", "title": "T", "duration": 700}])
        ryan.record_failure(self.con, "c", "HTTP Error 429")
        soon = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.assertEqual(ryan.pending_videos(self.con, when=soon), [])
        later = datetime.now(timezone.utc) + timedelta(hours=4)
        self.assertEqual(len(ryan.pending_videos(self.con, when=later)), 1)

    def test_per_run_limit_protects_against_the_429(self):
        ryan.sync_listing(self.con, [
            {"id": f"v{i}", "title": "T", "duration": 700} for i in range(8)])
        self.assertEqual(len(ryan.pending_videos(self.con)), ryan.PER_RUN_LIMIT)


# ---------------------------------------------------------------------- extract

class TestExtractParsing(unittest.TestCase):
    def test_bare_json(self):
        self.assertEqual(extract.parse_response('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(extract.parse_response('```json\n{"a": 1}\n```'), {"a": 1})

    def test_prefaced_json(self):
        self.assertEqual(
            extract.parse_response('Here you go:\n{"a": 1}\nhope that helps'),
            {"a": 1})

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            extract.parse_response("I could not read that transcript.")

    def test_normalise_fills_defaults_and_scores_the_fence(self):
        out = extract.normalise({"hazards": ["Tornadoes", "flooding"],
                                 "regions": ["Ohio Valley"],
                                 "escalation": "sideways"})
        self.assertEqual(out["hazards"], ["flash flood", "tornado"])
        self.assertEqual(out["escalation"], "new")     # unknown value rejected
        self.assertEqual(out["confidence"], "hedged")  # default
        self.assertGreaterEqual(out["fence_score"], 2)

    def test_normalise_tolerates_a_junk_window(self):
        out = extract.normalise({"window": "next week"})
        self.assertIsNone(out["window_start"])


# ----------------------------------------------------------------------- render

class TestRender(unittest.TestCase):
    def test_a_terse_post_still_clears_the_newsdesk_floor(self):
        # The failure this guards is silent: newsdesk drops short corpus posts
        # and reports success, so the item simply never appears.
        transcript = ("We are watching a system for Thursday. " * 60)
        body = render.render_post(
            video={"id": "abc", "title": "A Severe Threat Is Coming",
                   "published": "2026-09-07T18:00:00+00:00"},
            fields=extract.normalise({"summary": "He is watching Thursday.",
                                      "hazards": ["tornado"],
                                      "regions": ["northern Kentucky"],
                                      "quotes": ["Thursday looks active."]}),
            transcript=transcript, spc_note="SPC has nothing drawn")
        self.assertGreaterEqual(render.word_count(body), render.NEWSDESK_MIN_WORDS)

    def test_front_matter_is_what_the_newsdesk_expects(self):
        body = render.render_post(
            video={"id": "abc", "title": "T", "published": "2026-09-07T00:00:00+00:00"},
            fields=extract.normalise({}), transcript="x " * 400)
        self.assertTrue(body.startswith("---\n"))
        self.assertIn("date: 2026-09-07", body)
        self.assertIn("url: https://www.youtube.com/watch?v=abc", body)

    def test_excerpt_centres_on_the_region_mention(self):
        transcript = ("filler " * 200) + "big trouble for northern Kentucky " + ("tail " * 200)
        out = render._excerpt(transcript, ["northern Kentucky"])
        self.assertIn("northern Kentucky", out)


# ----------------------------------------------------------------------- notify

EVENING = datetime(2026, 9, 7, 23, 30, tzinfo=timezone.utc)   # 7:30 p.m. Eastern
LATE = datetime(2026, 9, 8, 4, 30, tzinfo=timezone.utc)       # 12:30 a.m. Eastern


class TestQuietHours(unittest.TestCase):
    def setUp(self):
        self.con = mkcon()

    def test_quiet_window(self):
        self.assertFalse(notify.in_quiet_hours(EVENING))
        self.assertTrue(notify.in_quiet_hours(LATE))

    def test_warning_at_night_is_held(self):
        with mock.patch.object(notify, "send") as send:
            state = notify.deliver(self.con, layer=1, key="k", title="t", body="b",
                                   alert_class="warning", when=LATE)
        self.assertEqual(state, "held")
        send.assert_not_called()
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) c FROM pending").fetchone()["c"], 1)

    def test_critical_at_night_goes_straight_out_as_urgent(self):
        with mock.patch.object(notify, "send") as send:
            state = notify.deliver(self.con, layer=1, key="k", title="Tornado Warning",
                                   body="b", alert_class="critical", when=LATE)
        self.assertEqual(state, "sent")
        self.assertEqual(send.call_args.kwargs["priority"], "urgent")

    def test_warning_in_the_evening_goes_out_normally(self):
        with mock.patch.object(notify, "send") as send:
            state = notify.deliver(self.con, layer=1, key="k", title="t", body="b",
                                   alert_class="warning", when=EVENING)
        self.assertEqual(state, "sent")
        self.assertEqual(send.call_args.kwargs["priority"], "default")

    def test_flush_sends_one_message_not_n(self):
        for i in range(4):
            db.queue_pending(self.con, layer=1, key=f"k{i}", title=f"t{i}", body="b")
        with mock.patch.object(notify, "send") as send:
            n = notify.flush_pending(self.con)
        self.assertEqual(n, 4)
        self.assertEqual(send.call_count, 1, "one consolidated summary, never N pings")
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) c FROM pending").fetchone()["c"], 0)

    def test_flush_of_an_empty_queue_sends_nothing(self):
        with mock.patch.object(notify, "send") as send:
            self.assertEqual(notify.flush_pending(self.con), 0)
        send.assert_not_called()


# --------------------------------------------------------------------------- db

class TestLocation(unittest.TestCase):
    def _write(self, text):
        p = Path(tempfile.mkdtemp()) / "loc"
        p.write_text(text, encoding="utf-8")
        return p

    def test_json_form(self):
        lat, lon = db.load_location(
            self._write('{"latitude": "38.4", "longitude": "-84.8"}'))
        self.assertAlmostEqual(lat, 38.4)
        self.assertAlmostEqual(lon, -84.8)

    def test_key_value_form(self):
        # Whichever way sops-nix materialises the secret, this must work.
        lat, lon = db.load_location(
            self._write("# comment\nlatitude: 38.4\nlongitude: '-84.8'\n"))
        self.assertAlmostEqual(lat, 38.4)
        self.assertAlmostEqual(lon, -84.8)

    def test_missing_keys_raise_without_echoing_the_contents(self):
        with self.assertRaises(ValueError) as ctx:
            db.load_location(self._write("nothing: here\n"))
        self.assertNotIn("here", str(ctx.exception))

    def test_layer1_ready_discriminates(self):
        # Since 2026-09-09 the unit is SKIPPED rather than failed when the
        # secret is missing, so this function is the ONLY thing that reports
        # "layer 1 never ran". A constant here would make the state invisible.
        from wx import cli
        good = self._write('{"latitude": "38.4", "longitude": "-84.8"}')
        with mock.patch.dict(os.environ, {"WX_LOCATION_FILE": str(good)}):
            self.assertTrue(cli.layer1_ready())
        with mock.patch.dict(os.environ,
                             {"WX_LOCATION_FILE": "/nonexistent/wx-location"}):
            self.assertFalse(cli.layer1_ready())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
