"""Tests for the briefing card. Run in checkPhase, so a break fails the build.

The interesting behaviour is all in what the card refuses to do: claim an
open issue that has cleared, promote FYI lines out of the daybook as if they
were today's work, or invite Chris to read an edition that fell back.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same guard as the wx suite: a test run that can reach live state will
# eventually corrupt it.
_state = os.environ.get("BRIEFING_STATE", "")
if not _state or _state.startswith("/var/lib"):
    raise SystemExit("refusing to run: set BRIEFING_STATE to a sandbox path")

from briefing import render, sources  # noqa: E402

NOW = 1792436310.0  # 2026-10-19 14:58 EDT, the day this was written
DAY = 86400.0


def tmpdir():
    return Path(tempfile.mkdtemp(dir=_state))


def write_state(checks):
    d = tmpdir()
    (d / "state.json").write_text(json.dumps({"checks": checks}))
    (d / "incidents").mkdir()
    return d


# --------------------------------------------------------------- sentinel

class Sentinel(unittest.TestCase):
    def test_only_active_checks_are_reported(self):
        d = write_state({
            "disk-space": {"active": True, "last_escalated": NOW - 5 * DAY},
            "oom": {"active": False, "last_escalated": NOW - DAY},
        })
        rows = sources.sentinel(d / "state.json", d / "incidents", now=NOW)
        self.assertEqual([r["id"] for r in rows], ["disk-space"])

    def test_a_cleared_check_disappears_even_with_a_huge_counter(self):
        """`consecutive` is deliberately ignored. A check that stopped firing
        has active=False, and that is the only thing that means "open"."""
        d = write_state({"swap-headroom": {"active": False, "consecutive": 9098,
                                           "last_escalated": NOW - DAY}})
        self.assertEqual(sources.sentinel(d / "state.json", d / "incidents", now=NOW), [])

    def test_freshest_escalation_first(self):
        d = write_state({
            "old": {"active": True, "last_escalated": NOW - 20 * DAY},
            "new": {"active": True, "last_escalated": NOW - 1 * DAY},
            "mid": {"active": True, "last_escalated": NOW - 5 * DAY},
        })
        rows = sources.sentinel(d / "state.json", d / "incidents", now=NOW)
        self.assertEqual([r["id"] for r in rows], ["new", "mid", "old"])

    def test_age_is_carried_so_a_chronic_latch_reads_as_chronic(self):
        d = write_state({"claude-login": {"active": True,
                                          "last_escalated": NOW - 14 * DAY}})
        row = sources.sentinel(d / "state.json", d / "incidents", now=NOW)[0]
        self.assertAlmostEqual(row["age_days"], 14.0, places=1)
        self.assertEqual(render._age(row["age_days"]), "2 weeks")

    def test_detail_comes_from_the_newest_evidence_file(self):
        d = write_state({"disk-space": {"active": True, "last_escalated": NOW}})
        (d / "incidents" / "disk-space-100.txt").write_text("old line\nmore\n")
        (d / "incidents" / "disk-space-200.txt").write_text("newest line\nmore\n")
        row = sources.sentinel(d / "state.json", d / "incidents", now=NOW)[0]
        self.assertEqual(row["detail"], "newest line")

    def test_another_checks_evidence_is_not_borrowed(self):
        """`startswith(cid + "-")` matters: `disk` must not pick up
        `disk-space-123.txt`."""
        d = write_state({"disk": {"active": True, "last_escalated": NOW}})
        (d / "incidents" / "disk-space-200.txt").write_text("not mine\n")
        self.assertEqual(sources.sentinel(d / "state.json", d / "incidents",
                                          now=NOW)[0]["detail"], "")

    def test_pruned_evidence_is_not_an_error(self):
        d = write_state({"claude-login": {"active": True, "last_escalated": NOW}})
        row = sources.sentinel(d / "state.json", d / "incidents", now=NOW)[0]
        self.assertEqual(row["detail"], "")
        self.assertEqual(row["label"], "Claude login")

    def test_missing_or_corrupt_state_is_empty_not_fatal(self):
        d = tmpdir()
        self.assertEqual(sources.sentinel(d / "nope.json", d, now=NOW), [])
        (d / "bad.json").write_text("{not json")
        self.assertEqual(sources.sentinel(d / "bad.json", d, now=NOW), [])


# ---------------------------------------------------------------- daybook

PAGE = """# 2026-10-19 (Monday)

## Plan

1. 📥 **flakes PRs — merge in order: [#392](https://x/392) → [#393](https://x/393)**. Re-verified. [[Areas/Homelab]]
2. 💾 **[[Projects/Gromit storage reclaim|Gromit storage]] — three decisions still blocking**: the layout call.
3. ⌨️ Plain item with no bold lead and a `code` span
4. 🔐 **Fourth item**
5. 🔑 **Fifth item, beyond the limit**

### Calendar
Synced. Nothing today.

### Homelab — overnight, FYI only (not yours to act on)
- 🔴 Something broke overnight

### Carryover
- Nothing came due today.
"""


class Daybook(unittest.TestCase):
    def setUp(self):
        self.space = tmpdir()
        day = self.space / "Journal" / "Day"
        day.mkdir(parents=True)
        (day / "2026-10-19.md").write_text(PAGE)

    def plan(self, **kw):
        return sources.daybook(self.space, today="2026-10-19", **kw)

    def test_bold_lead_becomes_the_headline(self):
        self.assertTrue(self.plan()[0].startswith("flakes PRs"))

    def test_markdown_links_are_flattened_to_their_text(self):
        first = self.plan()[0]
        self.assertNotIn("](", first)
        self.assertNotIn("https://", first)
        self.assertIn("#392", first)

    def test_wikilinks_use_their_display_text(self):
        second = self.plan()[1]
        self.assertIn("Gromit storage", second)
        self.assertNotIn("[[", second)
        self.assertNotIn("Projects/", second)

    def test_unpiped_wikilink_loses_its_folder(self):
        self.space  # noqa: B018 - setUp built the page
        day = self.space / "Journal" / "Day"
        (day / "2026-10-21.md").write_text(
            "## Plan\n\n1. **[[Projects/Lock3 Website]] — six calls open**\n")
        item = sources.daybook(self.space, today="2026-10-21")[0]
        self.assertTrue(item.startswith("Lock3 Website"))
        self.assertNotIn("Projects/", item)

    def test_item_without_a_bold_lead_still_renders(self):
        third = self.plan()[2]
        self.assertIn("Plain item", third)
        self.assertNotIn("`", third)

    def test_the_fyi_section_is_never_promoted_into_the_plan(self):
        """⚠️ The daybook marks that block "not yours to act on". Putting it on
        a card headed "Today" would invert its meaning."""
        for item in self.plan(limit=99):
            self.assertNotIn("broke overnight", item)
            self.assertNotIn("came due today", item)
            self.assertNotIn("Synced", item)

    def test_limit_is_honoured(self):
        self.assertEqual(len(self.plan(limit=4)), 4)

    def test_missing_page_is_empty(self):
        self.assertEqual(sources.daybook(self.space, today="2026-01-01"), [])

    def test_page_without_a_plan_section_is_empty(self):
        day = self.space / "Journal" / "Day"
        (day / "2026-10-20.md").write_text("# 2026-10-20\n\n### Calendar\nNothing.\n")
        self.assertEqual(sources.daybook(self.space, today="2026-10-20"), [])


# ---------------------------------------------------------------- edition

class Edition(unittest.TestCase):
    def write(self, payload):
        d = tmpdir()
        p = d / "last-publish.json"
        p.write_text(json.dumps(payload))
        return p

    def test_good_edition_builds_an_absolute_url(self):
        p = self.write({"tldr": "A thing happened.", "url": "2026-10-19-brief/",
                        "published": 10, "fell_back": False})
        ed = sources.edition(p, base_url="https://digest.example.com/news")
        self.assertEqual(ed["url"], "https://digest.example.com/news/2026-10-19-brief/")
        self.assertEqual(ed["published"], 10)

    def test_a_fallback_run_is_not_offered(self):
        """⚠️ fell_back means the newsdesk could not assemble a real edition."""
        p = self.write({"tldr": "x", "url": "u/", "published": 3, "fell_back": True})
        self.assertIsNone(sources.edition(p, base_url="https://e/news"))

    def test_an_empty_edition_is_not_offered(self):
        p = self.write({"tldr": "x", "url": "u/", "published": 0})
        self.assertIsNone(sources.edition(p, base_url="https://e/news"))

    def test_missing_tldr_is_not_offered(self):
        p = self.write({"url": "u/", "published": 5})
        self.assertIsNone(sources.edition(p, base_url="https://e/news"))

    def test_missing_file_is_not_fatal(self):
        self.assertIsNone(sources.edition(tmpdir() / "nope.json",
                                          base_url="https://e/news"))


# ----------------------------------------------------------------- render

ED = {"tldr": "Two networks peer with no IPv4 address at all.",
      "url": "https://digest.example.com/news/x/", "published": 10, "edition": "x"}
URLS = {"notes_url": "https://notes.example.com",
        "sentinel_url": "https://digest.example.com/sentinel/"}


class Render(unittest.TestCase):
    def test_all_clear_says_so(self):
        page = render.render([], ["a plan item"], ED, **URLS)
        self.assertIn("Nothing needs attention", page)
        self.assertIn("dot ok", page)

    def test_issue_count_is_pluralised(self):
        one = render.render([{"id": "a", "label": "A", "detail": "", "age_days": 1,
                              "escalated": 1}], [], ED, **URLS)
        self.assertIn("1 thing wants attention", one)
        two = render.render([{"id": "a", "label": "A", "detail": "", "age_days": 1,
                              "escalated": 1},
                             {"id": "b", "label": "B", "detail": "", "age_days": 1,
                              "escalated": 1}], [], ED, **URLS)
        self.assertIn("2 things want attention", two)

    def test_overflowing_issues_are_summarised_not_dumped(self):
        issues = [{"id": str(i), "label": f"C{i}", "detail": "", "age_days": 1,
                   "escalated": i} for i in range(7)]
        page = render.render(issues, [], ED, **URLS)
        self.assertIn("and 4 more", page)

    def test_read_panel_goes_full_width_when_there_is_no_plan(self):
        """Chris's 'if nothing there, give me something to read' case."""
        page = render.render([], [], ED, **URLS)
        self.assertIn("read solo", page)
        self.assertNotIn('class="kicker">Today<', page)

    def test_read_panel_shares_the_row_when_there_is_a_plan(self):
        page = render.render([], ["item"], ED, **URLS)
        self.assertIn('class="kicker">Today<', page)
        self.assertNotIn("read solo", page)

    def test_no_edition_says_so_rather_than_leaving_a_hole(self):
        page = render.render([], ["item"], None, **URLS)
        self.assertIn("No edition today", page)

    def test_links_open_in_a_new_tab(self):
        """⚠️ Inside an iframe a bare link navigates the FRAME, leaving a news
        site embedded in a dashboard card."""
        page = render.render([{"id": "a", "label": "A", "detail": "d",
                               "age_days": 1, "escalated": 1}], ["item"], ED, **URLS)
        self.assertEqual(page.count("<a "), 3)
        # Every anchor on the card must carry it, not just three of them.
        self.assertEqual(page.count("<a "), page.count('target="_blank"'))
        self.assertEqual(page.count("<a "), page.count('rel="noopener"'))

    def test_everything_from_outside_is_escaped(self):
        nasty = '<script>alert("x")</script>'
        page = render.render(
            [{"id": "a", "label": nasty, "detail": nasty, "age_days": 1, "escalated": 1}],
            [nasty],
            {"tldr": nasty, "url": "https://e/", "published": 1, "edition": ""},
            **URLS)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_machine_evidence_is_suppressed_not_shown(self):
        """⚠️ The sentinel's threshold checks write their id twice and then a
        bare expression. On the face of a card that is noise."""
        self.assertEqual(
            render.useful_detail("[swap-headroom] swap-headroom: 127.0.0.1:9100=7.9621"), "")
        self.assertEqual(render.useful_detail("[disk-space] disk-space: /=90.8903"), "")
        self.assertEqual(render.useful_detail("[claude-login] claude-login"), "")
        self.assertEqual(render.useful_detail(""), "")
        self.assertEqual(render.useful_detail(None), "")

    def test_prose_evidence_survives_with_its_id_prefixes_stripped(self):
        detail = render.useful_detail(
            "[vhost-health] vhost-health: nginx is 'failed' (expected active) "
            "— EVERY vhost on the box is unreachable")
        self.assertTrue(detail.startswith("nginx is 'failed'"))
        self.assertNotIn("vhost-health", detail)

    def test_evidence_goes_in_a_tooltip_not_on_the_pill(self):
        page = render.render(
            [{"id": "vhost-health", "label": "Vhost health",
              "detail": "nginx is down and every vhost is unreachable",
              "age_days": 1, "escalated": 1}], [], ED, **URLS)
        self.assertIn('title="nginx is down and every vhost is unreachable"', page)
        self.assertNotIn('>nginx is down', page)

    def test_tooltip_text_is_escaped(self):
        page = render.render(
            [{"id": "a", "label": "A", "detail": 'he said "x" and <b>y</b> ok',
              "age_days": 1, "escalated": 1}], [], ED, **URLS)
        self.assertNotIn('<b>', page)
        self.assertIn("&quot;", page)

    def test_plan_rows_and_teaser_cannot_be_shrunk(self):
        """⚠️ Guards the fault that made the card unreadable on a phone.

        The plan rows are flex items in a column with overflow:hidden. With
        the default flex-shrink:1 they are squashed BELOW their own text
        height when the panel runs short, and every row clips through the
        middle of its letters. `flex:none` is what forbids that.

        This asserts the declaration, not the rendering — a stylesheet is not
        something unittest can lay out. It is here so the rule cannot be
        deleted silently; the rendered proof is in the PR.
        """
        for selector in (".plan li{flex:none", ".read .tldr{flex:none",
                         ".kicker{flex:none", ".read .go{flex:none"):
            self.assertIn(selector, render.CSS,
                          selector + " missing — rows can be squashed again")

    def test_plan_rows_are_single_line(self):
        """Two-line rows made the panel height unpredictable, which is what
        made it possible to pick a frame height that did not fit."""
        self.assertIn("white-space:nowrap", render.CSS)
        self.assertNotIn("-webkit-line-clamp:2;-webkit-box-orient:vertical}\n.plan",
                         render.CSS)

    def test_age_wording(self):
        self.assertEqual(render._age(0.2), "today")
        self.assertEqual(render._age(1.4), "1 day")
        self.assertEqual(render._age(5.6), "5 days")
        self.assertEqual(render._age(21.0), "3 weeks")
        self.assertEqual(render._age(None), "")


if __name__ == "__main__":
    unittest.main()
