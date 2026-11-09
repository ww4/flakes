"""Tests for the project-board renderer, run at build time.

The renderer is all branching -- status ordering, a progress bar, a quiet
marker, an unreadable file, escaping -- and every branch is a thing Chris would
otherwise have to notice was wrong by reading the page. PROJECT_BOARD_PY points
at the script (set by the derivation).
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest

SCRIPT = os.environ["PROJECT_BOARD_PY"]


def load_module(projects_dir, web_dir, stale_hours=48):
    os.environ["PROJECT_BOARD_PROJECTS"] = projects_dir
    os.environ["PROJECT_BOARD_WEB"] = web_dir
    os.environ["PROJECT_BOARD_STALE_HOURS"] = str(stale_hours)
    spec = importlib.util.spec_from_file_location("project_board_%d" % time.time_ns(), SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = os.path.join(self.tmp.name, "projects")
        self.web = os.path.join(self.tmp.name, "web")
        os.makedirs(self.projects)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data, age_hours=0):
        path = os.path.join(self.projects, name)
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(data, str):
                fh.write(data)
            else:
                json.dump(data, fh)
        if age_hours:
            old = time.time() - age_hours * 3600
            os.utime(path, (old, old))
        return path

    def page(self, stale_hours=48):
        return load_module(self.projects, self.web, stale_hours).render()

    # ── the empty state ─────────────────────────────────────────────────────
    def test_empty_directory_says_so(self):
        # A blank page cannot be distinguished from a renderer that died.
        self.assertIn("No projects are being tracked", self.page())

    def test_missing_directory_is_reported_not_silent(self):
        mod = load_module(os.path.join(self.tmp.name, "nope"), self.web)
        _, broken = mod.load()
        self.assertEqual(len(broken), 1)
        self.assertIn("cannot list", broken[0][1])

    # ── a normal project ────────────────────────────────────────────────────
    def test_renders_title_status_next_and_progress(self):
        self.write("guide.json", {
            "title": "Imperfect Homelab guide",
            "status": "active",
            "goal": "A stranger installs it from the ISO unaided",
            "next": "Chris's real-hardware run",
            "steps": [
                {"name": "ISO published", "state": "done"},
                {"name": "QEMU rehearsal", "state": "done"},
                {"name": "Hardware run", "state": "doing"},
                {"name": "LUP submission", "state": "todo"},
            ],
        })
        html = self.page()
        self.assertIn("Imperfect Homelab guide", html)
        self.assertIn('class="pill active"', html)
        self.assertIn("Chris&#x27;s real-hardware run", html)
        self.assertIn("2 of 4 steps", html)
        self.assertIn("width:50%", html)

    def test_one_step_is_not_pluralised(self):
        self.write("one.json", {"title": "T", "steps": [{"name": "only", "state": "done"}]})
        self.assertIn("1 of 1 step<", self.page())

    def test_steps_may_be_bare_strings(self):
        self.write("s.json", {"title": "T", "steps": ["one", "two"]})
        html = self.page()
        self.assertIn("0 of 2 steps", html)
        self.assertIn('class="todo"', html)

    def test_log_is_collapsed_newest_first(self):
        self.write("l.json", {"title": "T", "log": [
            {"when": "2026-10-01", "what": "started"},
            {"when": "2026-10-04", "what": "shipped"},
        ]})
        html = self.page()
        self.assertIn("Log (2)", html)
        self.assertLess(html.index("shipped"), html.index("started"))

    # ── the quiet marker ────────────────────────────────────────────────────
    def test_active_and_untouched_is_flagged_quiet(self):
        self.write("q.json", {"title": "Stalled", "status": "active"}, age_hours=72)
        self.assertIn("quiet 3d", self.page())

    def test_fresh_active_is_not_flagged(self):
        self.write("f.json", {"title": "Moving", "status": "active"})
        # class=, not the bare word: the stylesheet defines .quiet on every page.
        self.assertNotIn('class="quiet"', self.page())

    def test_paused_and_done_are_quiet_on_purpose(self):
        # Flagging these would train him to ignore the marker, which is how a
        # warning that fires on normal states stops being read at all.
        self.write("p.json", {"title": "Parked", "status": "paused"}, age_hours=500)
        self.write("d.json", {"title": "Finished", "status": "done"}, age_hours=500)
        html = self.page()
        self.assertNotIn('class="quiet"', html)

    def test_stale_threshold_is_configurable(self):
        self.write("q.json", {"title": "T", "status": "active"}, age_hours=5)
        self.assertNotIn('class="quiet"', self.page(stale_hours=48))
        self.assertIn('class="quiet"', self.page(stale_hours=4))

    # ── blocked ─────────────────────────────────────────────────────────────
    def test_blocked_shows_what_it_needs_instead_of_next(self):
        self.write("b.json", {
            "title": "Needs a token", "status": "blocked",
            "blocked_on": "a Cloudflare token only Chris can mint",
            "next": "should not be shown",
        })
        html = self.page()
        self.assertIn("Blocked on:", html)
        self.assertIn("only Chris can mint", html)
        self.assertNotIn("should not be shown", html)

    def test_blocked_without_a_reason_says_so(self):
        self.write("b.json", {"title": "T", "status": "blocked"})
        self.assertIn("not recorded", self.page())

    # ── ordering ────────────────────────────────────────────────────────────
    def test_order_is_blocked_active_paused_done(self):
        self.write("a.json", {"title": "AAA", "status": "active"})
        self.write("b.json", {"title": "BBB", "status": "blocked"})
        self.write("c.json", {"title": "CCC", "status": "done"})
        self.write("d.json", {"title": "DDD", "status": "paused"})
        html = self.page()
        self.assertLess(html.index("BBB"), html.index("AAA"))
        self.assertLess(html.index("AAA"), html.index("DDD"))
        self.assertLess(html.index("DDD"), html.index("CCC"))

    def test_same_status_orders_most_recently_touched_first(self):
        self.write("old.json", {"title": "OLDER", "status": "active"}, age_hours=10)
        self.write("new.json", {"title": "NEWER", "status": "active"})
        html = self.page()
        self.assertLess(html.index("NEWER"), html.index("OLDER"))

    # ── unreadable files are shown, never dropped ───────────────────────────
    def test_malformed_json_becomes_a_visible_card(self):
        self.write("broken.json", "{not json at all")
        html = self.page()
        self.assertIn("broken.json", html)
        self.assertIn("UNREADABLE", html)
        self.assertIn("NOT on this board", html)

    def test_missing_title_is_unreadable(self):
        self.write("t.json", {"status": "active"})
        self.assertIn("UNREADABLE", self.page())

    def test_unknown_status_is_unreadable_not_coerced(self):
        # Coercing to "active" would quietly hide a typo'd "blocekd" forever.
        self.write("s.json", {"title": "T", "status": "blocekd"})
        html = self.page()
        self.assertIn("UNREADABLE", html)
        self.assertIn("blocekd", html)

    def test_top_level_list_is_unreadable(self):
        self.write("l.json", [{"title": "T"}])
        self.assertIn("UNREADABLE", self.page())

    def test_one_bad_file_does_not_hide_the_good_ones(self):
        self.write("good.json", {"title": "GOOD ONE", "status": "active"})
        self.write("bad.json", "}")
        html = self.page()
        self.assertIn("GOOD ONE", html)
        self.assertIn("UNREADABLE", html)

    def test_check_fails_on_a_bad_file_and_render_does_not(self):
        self.write("bad.json", "nope")
        mod = load_module(self.projects, self.web)
        self.assertEqual(self._run(mod, "check"), 1)
        self.assertEqual(self._run(mod, "render"), 0)
        # …and the page it wrote exists and names the file.
        with open(os.path.join(self.web, "index.html"), encoding="utf-8") as fh:
            self.assertIn("bad.json", fh.read())

    def _run(self, mod, cmd):
        import sys
        argv = sys.argv
        sys.argv = ["project-board", cmd]
        try:
            return mod.main()
        finally:
            sys.argv = argv

    # ── escaping ────────────────────────────────────────────────────────────
    def test_html_in_a_field_is_escaped(self):
        self.write("x.json", {"title": "<script>alert(1)</script>", "next": "a & b"})
        html = self.page()
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("a &amp; b", html)

    def test_non_http_links_are_dropped(self):
        self.write("x.json", {"title": "T", "links": [
            {"label": "ok", "url": "https://example.com/a"},
            {"label": "bad", "url": "javascript:alert(1)"},
        ]})
        html = self.page()
        self.assertIn("https://example.com/a", html)
        self.assertNotIn("javascript:", html)

    # ── the written file ────────────────────────────────────────────────────
    def test_render_writes_atomically_and_leaves_no_temp_file(self):
        self.write("a.json", {"title": "T"})
        mod = load_module(self.projects, self.web)
        self._run(mod, "render")
        self.assertTrue(os.path.exists(os.path.join(self.web, "index.html")))
        self.assertFalse(os.path.exists(os.path.join(self.web, ".index.html.tmp")))
        self.assertEqual(oct(os.stat(os.path.join(self.web, "index.html")).st_mode)[-3:], "644")


if __name__ == "__main__":
    unittest.main(verbosity=2)
