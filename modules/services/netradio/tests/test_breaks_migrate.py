import json
import logging
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import config, dj, migrate
from test_dj import FakeLS, FakeTTS

logging.disable(logging.CRITICAL)


class BreaksSpec(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(dj.breaks_spec(4), (4, 4))
        self.assertEqual(dj.breaks_spec("4"), (4, 4))
        self.assertEqual(dj.breaks_spec("3-5"), (3, 5))
        self.assertEqual(dj.breaks_spec(0), (0, 0))
        self.assertEqual(dj.breaks_spec(None), (3, 4))
        self.assertEqual(dj.breaks_spec("lots"), (3, 4))

    def test_zero_means_no_breaks_and_live_reload(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = config.Config(root / "config")
            station = {"mount": "x", "name": "X", "kind": "curated", "family": ["any"], "breaks_every": 0}
            cfg.seed({}, [station], [])
            (root / "x.m3u").write_text("#EXTM3U\n" + "".join(f"/m/A{i}/B/0{i} t{i}.mp3\n" for i in range(8)))
            ls, tts = FakeLS(), FakeTTS()
            prog = dj.Programme(station, cfg, root / "pools")
            with mock.patch.object(dj, "read_tags", side_effect=lambda p: dj.Track(p, Path(p).stem[3:], "A")):
                s = dj.StationDJ("x", "X", root / "x.m3u", root / "b", ls, tts, rng=random.Random(1),
                                 breaks_every=dj.breaks_spec(station["breaks_every"]), programme=prog)
                for _ in range(8):
                    s.fill(); ls.play()
                self.assertEqual(tts.texts, [])                       # never a break
                # Chris sets every 2 songs on the admin page: picked up without a restart
                import os, time
                cfg.save_stations([{**station, "breaks_every": 2}])
                os.utime(cfg.root / "stations.json", (time.time() + 5, time.time() + 5))
                for _ in range(8):
                    s.fill(); ls.play()
                self.assertGreaterEqual(len(tts.texts), 2)


class Migration(unittest.TestCase):
    def test_split_is_idempotent_and_moves_slots(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.Config(Path(d))
            cfg.seed({}, [{"mount": "country", "name": "C", "kind": "curated", "family": ["country"], "base": {}},
                         {"mount": "blues-jazz", "name": "Blues, Jazz & Soul", "kind": "curated", "family": ["blues-jazz"],
                          "base": {"genres": ["blues", "jazz"], "era": {"exclude": ["shellac"]}}, "breaks_every": 5},
                         {"mount": "gospel", "name": "G", "kind": "curated", "family": ["gospel"], "base": {}}],
                     [{"id": "s1", "station": "blues-jazz", "kind": "auto", "like": "artist", "start": "20:00"}])
            out = migrate.run(cfg)
            self.assertEqual(len(out), 1)
            mounts = [s["mount"] for s in cfg.stations()]
            self.assertEqual(mounts, ["country", "blues", "jazz", "soul", "gospel"])     # in place, order kept
            jazz = next(s for s in cfg.stations() if s["mount"] == "jazz")
            self.assertEqual(jazz["base"]["era"], {"exclude": ["shellac"]})
            self.assertEqual(jazz["breaks_every"], 5)
            self.assertEqual(cfg.schedule()[0]["station"], "blues")
            self.assertTrue(list((Path(d) / "requests").glob("apply-*.json")))               # and an apply follows
            self.assertEqual(migrate.run(cfg), [])                                          # recorded: not again
            self.assertIn("split-blues-jazz-soul", cfg._read("migrations.json", {}))

    def test_nothing_to_do_on_a_fresh_seed(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.Config(Path(d))
            cfg.seed({}, [{"mount": "blues", "name": "Blues", "kind": "curated"}], [])
            self.assertEqual(migrate.run(cfg), ["split-blues-jazz-soul: nothing to do"])


if __name__ == "__main__":
    unittest.main()
