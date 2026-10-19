"""Tests for the Lab tab's project card.

As with the briefing card, what matters is what it refuses to do: report an
empty project as finished, invent a next step for something that is done, or
let a malformed file blank the card.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_state = os.environ.get("BRIEFING_STATE", "")
if not _state or _state.startswith("/var/lib"):
    raise SystemExit("refusing to run: set BRIEFING_STATE to a sandbox path")

from briefing import projects, render_projects  # noqa: E402

NOW = 1792446776.0
DAY = 86400.0
BOARD = "https://board.example.com/"


def mkdir():
    return Path(tempfile.mkdtemp(dir=_state))


def write(d: Path, slug: str, payload: dict, *, age_days: float = 0.0):
    p = d / f"{slug}.json"
    p.write_text(json.dumps(payload))
    t = NOW - age_days * DAY
    os.utime(p, (t, t))
    return p


def steps(done: int, todo: int):
    return ([{"name": f"d{i}", "state": "done"} for i in range(done)]
            + [{"name": f"t{i}", "state": "todo"} for i in range(todo)])


class Percent(unittest.TestCase):
    def test_percent_from_done_over_total(self):
        d = mkdir()
        write(d, "a", {"title": "A", "status": "active", "steps": steps(3, 5)})
        p = projects.load(d, now=NOW)[0]
        self.assertEqual((p["done"], p["total"], p["percent"]), (3, 8, 38))

    def test_all_done_is_100(self):
        d = mkdir()
        write(d, "a", {"title": "A", "status": "done", "steps": steps(7, 0)})
        self.assertEqual(projects.load(d, now=NOW)[0]["percent"], 100)

    def test_a_project_with_no_steps_is_zero_not_a_hundred(self):
        """⚠️ The empty-list average is the classic way to report everything
        as finished."""
        d = mkdir()
        write(d, "a", {"title": "A", "status": "active"})
        p = projects.load(d, now=NOW)[0]
        self.assertEqual(p["percent"], 0)
        self.assertEqual(p["total"], 0)

    def test_doing_does_not_count_as_done(self):
        d = mkdir()
        write(d, "a", {"title": "A", "status": "active", "steps": [
            {"name": "x", "state": "done"}, {"name": "y", "state": "doing"},
            {"name": "z", "state": "todo"}]})
        self.assertEqual(projects.load(d, now=NOW)[0]["percent"], 33)


class NextStep(unittest.TestCase):
    def test_prefers_the_cards_own_next_sentence(self):
        d = mkdir()
        write(d, "a", {"title": "A", "status": "blocked",
                       "next": "Chris decides the layout.", "steps": steps(1, 2)})
        self.assertEqual(projects.load(d, now=NOW)[0]["next"],
                         "Chris decides the layout.")

    def test_falls_back_to_the_first_unfinished_step(self):
        d = mkdir()
        write(d, "a", {"title": "A", "status": "active", "steps": [
            {"name": "already done", "state": "done"},
            {"name": "the real next thing", "state": "todo"}]})
        self.assertEqual(projects.load(d, now=NOW)[0]["next"],
                         "the real next thing")

    def test_a_done_project_gets_no_next_step(self):
        """⚠️ Inventing a next step for finished work is how a board starts
        lying. The real board has a `done` card carrying a stale `next`."""
        d = mkdir()
        write(d, "a", {"title": "A", "status": "done", "steps": steps(7, 0),
                       "next": "Done 2026-10-06. Left open because nothing reconciles."})
        self.assertEqual(projects.load(d, now=NOW)[0]["next"], "")


class Ordering(unittest.TestCase):
    def test_blocked_first_done_last(self):
        d = mkdir()
        for slug, status in (("w", "done"), ("x", "active"),
                             ("y", "blocked"), ("z", "paused")):
            write(d, slug, {"title": slug, "status": status, "steps": steps(1, 1)})
        self.assertEqual([p["status"] for p in projects.load(d, now=NOW)],
                         ["blocked", "active", "paused", "done"])

    def test_newest_touched_first_within_a_status(self):
        d = mkdir()
        write(d, "old", {"title": "old", "status": "active", "steps": steps(1, 1)}, age_days=9)
        write(d, "new", {"title": "new", "status": "active", "steps": steps(1, 1)}, age_days=1)
        self.assertEqual([p["slug"] for p in projects.load(d, now=NOW)], ["new", "old"])

    def test_quiet_days_comes_from_the_mtime(self):
        """⚠️ Same rule as the board: `updated` is the file's mtime, never a
        field. A timestamp the agent must remember to bump lies the first time
        it forgets."""
        d = mkdir()
        write(d, "a", {"title": "A", "status": "active"}, age_days=5)
        self.assertAlmostEqual(projects.load(d, now=NOW)[0]["quiet_days"], 5.0, places=1)


class Robustness(unittest.TestCase):
    def test_a_broken_file_is_skipped_not_faked(self):
        d = mkdir()
        (d / "broken.json").write_text("{not json")
        write(d, "good", {"title": "Good", "status": "active", "steps": steps(1, 1)})
        found = projects.load(d, now=NOW)
        self.assertEqual([p["slug"] for p in found], ["good"])

    def test_a_json_file_that_is_not_an_object_is_skipped(self):
        d = mkdir()
        (d / "list.json").write_text("[1,2,3]")
        self.assertEqual(projects.load(d, now=NOW), [])

    def test_missing_directory_is_empty(self):
        self.assertEqual(projects.load(mkdir() / "nope", now=NOW), [])

    def test_title_falls_back_to_the_slug(self):
        d = mkdir()
        write(d, "my-slug", {"status": "active"})
        self.assertEqual(projects.load(d, now=NOW)[0]["title"], "my-slug")


class Render(unittest.TestCase):
    def rows(self, n, status="active"):
        return [{"slug": f"s{i}", "title": f"Project {i}", "status": status,
                 "done": 1, "total": 2, "percent": 50,
                 "next": "do the thing", "mtime": NOW, "quiet_days": 0.0}
                for i in range(n)]

    def test_blocked_row_says_blocked_on(self):
        page = render_projects.render(self.rows(1, "blocked"), board_url=BOARD)
        self.assertIn("Blocked on:", page)
        self.assertNotIn("Next:", page)

    def test_active_row_says_next(self):
        page = render_projects.render(self.rows(1), board_url=BOARD)
        self.assertIn("Next:", page)

    def test_bar_width_is_the_percentage(self):
        page = render_projects.render(self.rows(1), board_url=BOARD)
        self.assertIn('style="width:50%"', page)

    def test_percent_is_clamped(self):
        rows = self.rows(1)
        rows[0]["percent"] = 140
        self.assertIn('style="width:100%"', render_projects.render(rows, board_url=BOARD))

    def test_overflowing_rows_are_summarised(self):
        page = render_projects.render(self.rows(7), board_url=BOARD)
        self.assertIn("and 3 more", page)
        self.assertEqual(page.count('class="p '), render_projects.MAX_ROWS)

    def test_empty_board_says_so(self):
        page = render_projects.render([], board_url=BOARD)
        self.assertIn("No projects on the board", page)

    def test_everything_is_escaped(self):
        rows = self.rows(1)
        rows[0]["title"] = '<script>alert("x")</script>'
        rows[0]["next"] = "<img src=x onerror=1>"
        page = render_projects.render(rows, board_url=BOARD)
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;script&gt;", page)

    def test_links_open_in_a_new_tab(self):
        page = render_projects.render(self.rows(3), board_url=BOARD)
        self.assertEqual(page.count("<a "), page.count('target="_blank"'))
        self.assertEqual(page.count("<a "), page.count('rel="noopener"'))

    def test_rows_cannot_be_shrunk(self):
        """⚠️ Guards the fault that made the briefing card unreadable: flex
        children in a column with overflow:hidden are squashed below their own
        text height unless they are told not to be."""
        for selector in (".p{flex:none", ".top{flex:none", ".next{flex:none",
                         ".bar{flex:none", ".head{flex:none"):
            self.assertIn(selector, render_projects.CSS,
                          selector + " missing — rows can be squashed again")

    def test_an_unknown_status_does_not_break_the_markup(self):
        rows = self.rows(1, "weird-status")
        page = render_projects.render(rows, board_url=BOARD)
        self.assertIn('class="p active"', page)


if __name__ == "__main__":
    unittest.main()
