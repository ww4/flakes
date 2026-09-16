import json
import logging
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from netradio import admin, compile as compile_, config, liq

logging.disable(logging.CRITICAL)


class AdminApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.Config(Path(self.tmp.name) / "config")
        self.cfg.seed(
            {"western-swing": {"title": "Western Swing", "description": "Bob Wills", "status": "ready", "count": 30,
                               "family": ["country"], "rule": {"artists": ["Bob Wills"]}}},
            [{"mount": "country", "name": "Classic Country", "kind": "curated", "family": ["country"], "base": {"genres": ["country"]}},
             {"mount": "rock", "name": "Rock", "kind": "curated", "family": ["rock"], "base": {"genres": ["rock"]}},
             {"mount": "western-swing", "name": "Western Swing", "kind": "specialty", "feed": "western-swing", "family": ["country"]}],
            [])
        self.cfg._write("artists.json", {"George Jones": {"tracks": 40, "families": ["country"], "slug": "george-jones"},
                                          "Boston": {"tracks": 30, "families": ["rock"], "slug": "boston"},
                                          "Tiny": {"tracks": 2, "families": ["country"], "slug": "tiny"}})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), admin.make_handler(admin.Admin(self.cfg)))
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def requests(self, kind):
        return sorted(p.name for p in (self.cfg.root / "requests").glob(f"{kind}-*.json"))

    def test_state_and_options(self):
        code, st = self.call("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertTrue(st["feeds"]["western-swing"]["listenable"])
        self.assertEqual([s["mount"] for s in st["stations"]], ["country", "rock", "western-swing"])
        code, opt = self.call("GET", "/api/options?station=country")
        self.assertEqual([f["id"] for f in opt["feeds"]], ["western-swing"])
        self.assertEqual([a["name"] for a in opt["artists"]], ["George Jones"])     # not Boston, not Tiny
        code, opt = self.call("GET", "/api/options?station=rock")
        self.assertEqual(opt["feeds"], [])
        self.assertEqual([a["name"] for a in opt["artists"]], ["Boston"])

    def test_add_feed_requests_compile_and_makes_a_station(self):
        code, r = self.call("POST", "/api/feeds", {"title": "Brother Duets", "description": "Louvins, Delmores…"})
        self.assertEqual(code, 201)
        self.assertEqual(r["id"], "brother-duets")
        self.assertEqual(self.cfg.feeds()["brother-duets"]["status"], "pending")
        self.assertEqual(len(self.requests("compile")), 1)
        self.assertIn("brother-duets", [s["mount"] for s in self.cfg.stations()])
        code, _ = self.call("POST", "/api/feeds", {"title": ""})
        self.assertEqual(code, 400)

    def test_edit_description_recompiles_edit_rule_applies(self):
        code, r = self.call("PUT", "/api/feeds/western-swing", {"description": "more swing"})
        self.assertTrue(r["recompile"])
        self.assertEqual(self.cfg.feeds()["western-swing"]["status"], "pending")
        code, r = self.call("PUT", "/api/feeds/western-swing", {"rule": {"artists": ["Bob Wills", "Asleep at the Wheel"]}})
        self.assertEqual(code, 200)
        self.assertEqual(self.cfg.feeds()["western-swing"]["status"], "ready")
        self.assertEqual(len(self.requests("apply")), 1)
        code, r = self.call("PUT", "/api/feeds/western-swing", {"rule": {}})
        self.assertEqual(code, 400)
        code, r = self.call("PUT", "/api/feeds/western-swing", {"listenable": False})
        self.assertNotIn("western-swing", [s["mount"] for s in self.cfg.stations()])

    def test_schedule_validation(self):
        good = [{"station": "country", "name": "Swing Hour", "kind": "feed", "feed": "western-swing", "days": ["sat"], "start": "10:00", "minutes": 60},
                {"station": "country", "kind": "auto", "like": "artist", "days": "daily", "start": "20:00"}]
        code, r = self.call("PUT", "/api/schedule", good)
        self.assertEqual((code, r["count"]), (200, 2))
        ids = [s["id"] for s in self.cfg.schedule()]
        self.assertEqual(len(set(ids)), 2)
        for bad in ([{"station": "nope", "kind": "feed", "feed": "western-swing", "start": "10:00"}],
                    [{"station": "country", "kind": "feed", "feed": "missing", "start": "10:00"}],
                    [{"station": "country", "kind": "artist", "start": "10:00"}],
                    [{"station": "country", "kind": "feed", "feed": "western-swing", "start": "ten"}],
                    [{"station": "country", "kind": "feed", "feed": "western-swing", "start": "10:00", "minutes": 5}],
                    [{"station": "western-swing", "kind": "auto", "like": "feed", "start": "10:00"}]):
            code, r = self.call("PUT", "/api/schedule", bad)
            self.assertEqual(code, 400, r)

    def test_state_shows_todays_resolved_schedule(self):
        self.call("PUT", "/api/schedule", [{"station": "country", "kind": "auto", "like": "artist", "days": "daily", "start": "20:00"}])
        _, st = self.call("GET", "/api/state")
        self.assertEqual(st["today"]["country"][0]["value"], "George Jones")
        self.assertEqual(st["today"]["country"][0]["start"], "20:00")

    def test_delete_feed_cleans_up(self):
        self.call("PUT", "/api/schedule", [{"station": "country", "kind": "feed", "feed": "western-swing", "start": "10:00"}])
        code, _ = self.call("DELETE", "/api/feeds/western-swing")
        self.assertEqual(code, 200)
        self.assertEqual(self.cfg.feeds(), {})
        self.assertEqual(self.cfg.schedule(), [])
        self.assertNotIn("western-swing", [s["mount"] for s in self.cfg.stations()])


class Compile(unittest.TestCase):
    def test_prompt_and_apply(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.Config(Path(d))
            cfg.seed({"ws": {"title": "Western Swing", "description": "Bob Wills style", "status": "pending"}}, [], [])
            cfg._write("artists.json", {"Bob Wills": {"tracks": 5, "families": ["country"], "slug": "bob-wills"}})
            prompt = compile_.build_prompt(cfg, "ws", ["country", "western swing"])
            self.assertIn("Bob Wills (5 tracks; country)", prompt)
            self.assertIn("Bob Wills style", prompt)
            answer = 'Sure, here you go:\n{"rule": {"artists": ["Bob Wills"], "genres": ["western swing"]}, "family": ["country"], "note": "thin"}\n'
            self.assertEqual(compile_.apply_result(cfg, "ws", compile_.parse_result(answer)), [])
            f = cfg.feeds()["ws"]
            self.assertEqual((f["status"], f["family"], f["rule"]["genres"]), ("ready", ["country"], ["western swing"]))
            self.assertTrue(list((Path(d) / "requests").glob("apply-*.json")))
            errs = compile_.apply_result(cfg, "ws", {"rule": {}, "family": ["country"]})
            self.assertTrue(errs)
            self.assertEqual(cfg.feeds()["ws"]["status"], "failed")
            with self.assertRaises(ValueError):
                compile_.parse_result("no json here")
            # prose with braces before the object, a code fence, a bare rule
            r = compile_.parse_result('Notes {thin} first.\n```json\n{"rule": {"artists": ["A"]}, "family": ["country"]}\n```')
            self.assertEqual(r["rule"]["artists"], ["A"])
            r = compile_.parse_result('{"artists": ["A"], "genres": ["x"]}')
            self.assertEqual(r["rule"]["genres"], ["x"])
            r = compile_.parse_result('{"note": "x"} then {"rule": {"all": true}}')
            self.assertTrue(r["rule"]["all"])


class Liq(unittest.TestCase):
    def test_render(self):
        text = liq.render([{"mount": "country", "name": "Classic Country"}, {"mount": "ws", "name": "Western Swing"}],
                          socket="/run/x.sock", playlists="/p", now_dir="/n", port=8020)
        self.assertIn('station("country", "Classic Country")', text)
        self.assertIn('station("ws", "Western Swing")', text)
        self.assertIn('port=8020', text)
        self.assertIn('playlists = "/p"', text)
        self.assertEqual(text.count("{{"), 0)   # the format braces were resolved


if __name__ == "__main__":
    unittest.main()
