import json
import logging
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from netradio import ambient, speaker

logging.disable(logging.CRITICAL)


class FakeMixer(speaker.Mixer):
    def __init__(self):
        self.card, self.amixer, self.control = "0", "amixer", "Master"
        self.level, self.muted = 35, False

    def state(self): return self.level, self.muted
    def set(self, level): self.level = max(0, min(100, int(level))); self.muted = False
    def mute(self, on): self.muted = bool(on)


class MixerParsing(unittest.TestCase):
    def test_picks_master_and_reads_level(self):
        m = speaker.Mixer.__new__(speaker.Mixer)
        m.card, m.amixer = "0", "amixer"
        with mock.patch.object(speaker.Mixer, "_run", return_value="Simple mixer control 'PCM',0\nSimple mixer control 'Master',0\n"):
            self.assertEqual(m._find(), "Master")          # Master wins over PCM
        m.control = "Master"
        with mock.patch.object(speaker.Mixer, "_run", return_value="  Front Left: Playback 87 [68%] [-12.00dB] [on]\n"):
            self.assertEqual(m.state(), (68, False))
        with mock.patch.object(speaker.Mixer, "_run", return_value="  Front Left: Playback 0 [0%] [off]\n"):
            self.assertEqual(m.state(), (0, True))

    def test_a_missing_amixer_is_not_fatal(self):
        m = speaker.Mixer("99", amixer="/nonexistent/amixer")
        self.assertEqual(m.control, "")                    # no control found, no exception
        self.assertEqual(m.state(), (None, False))
        m.set(50); m.mute(True)                            # and these do nothing rather than raise


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # a stand-in player that just stays alive: no shell, no PATH lookup —
        # the build sandbox has neither (2026-09-23, this test broke the deploy)
        fake = Path(self.tmp.name) / "player"
        fake.write_text(f"#!{sys.executable}\nimport time; time.sleep(30)\n")
        fake.chmod(0o755)
        self.player = speaker.Player("http://127.0.0.1:8020", str(fake))
        self.mixer = FakeMixer()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), speaker.make_handler(self.player, self.mixer))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.player.stop(); self.srv.shutdown(); self.tmp.cleanup()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_play_stop_volume_and_a_bad_mount(self):
        code, st = self.call("GET", "/state")
        self.assertEqual((code, st["playing"], st["volume"]), (200, False, 35))
        code, st = self.call("POST", "/play", {"mount": "rain"})
        self.assertEqual((code, st["playing"], st["mount"]), (200, True, "rain"))
        code, st = self.call("POST", "/play", {"mount": "bluegrass"})   # switching kills the old player
        self.assertEqual(st["mount"], "bluegrass")
        self.assertEqual(self.call("POST", "/volume", {"level": 60})[1]["volume"], 60)
        self.assertEqual(self.call("POST", "/volume", {"step": -10})[1]["volume"], 50)
        self.assertTrue(self.call("POST", "/mute", {"on": True})[1]["muted"])
        for bad in ("../evil", "rain; rm -rf /", "", "Rain"):
            code, body = self.call("POST", "/play", {"mount": bad})
            self.assertEqual(code, 400, bad)
        self.assertEqual(self.call("POST", "/stop")[1]["playing"], False)

    def test_a_dead_player_is_restarted_while_a_mount_is_selected(self):
        self.player.play("rain")
        self.player.proc.kill(); self.player.proc.wait()
        self.assertFalse(self.player.alive())
        stop = threading.Event()
        t = threading.Thread(target=self.player.watch, args=(stop, 0.2), daemon=True)
        t.start()
        for _ in range(40):
            if self.player.alive(): break
            threading.Event().wait(0.2)
        stop.set()
        self.assertTrue(self.player.alive())
        self.assertEqual(self.player.mount, "rain")


class Ambient(unittest.TestCase):
    def test_playlist_lists_whatever_is_in_the_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root, pl = Path(d) / "ambient", Path(d) / "rain.m3u"
            (root / "rain").mkdir(parents=True)
            for name in ("a.flac", "b.mp3", "notes.txt", "cover.jpg"):
                (root / "rain" / name).write_bytes(b"x")
            with mock.patch.object(ambient, "fetch", return_value=True) as f:
                n = ambient.build("rain", root, pl)
            self.assertTrue(f.called)                       # the fetch was attempted
            self.assertEqual(n, 2)                          # only the audio is listed
            lines = [l for l in pl.read_text().splitlines() if not l.startswith("#")]
            self.assertTrue(all(l.endswith((".flac", ".mp3")) for l in lines))
            self.assertIn("aporee", (root / "rain" / "LICENCES.txt").read_text())   # provenance is written beside the audio


class BedCheck(unittest.TestCase):
    """The guard that keeps a slated sound-effects cut off the station."""

    def run_check(self, stderr):
        with mock.patch.object(ambient.subprocess, "run",
                               return_value=mock.Mock(stderr=stderr, stdout="")):
            return ambient.slate_or_gap(Path("/x/bed.flac"))

    def test_a_slate_is_rejected(self):
        # the real shape of GOLD TAPE G46-04: announcer to 1.5 s, gap, then rain
        why = self.run_check("lavfi.silence_start=1.506\nlavfi.silence_end=2.865\n")
        self.assertIn("slated", why)

    def test_a_long_internal_silence_is_rejected(self):
        why = self.run_check("lavfi.silence_start=6.0\nlavfi.silence_end=11.9\n")
        self.assertIn("silent", why)

    def test_continuous_rain_passes(self):
        self.assertEqual(self.run_check(""), "")
        self.assertEqual(self.run_check("lavfi.silence_start=25.0\nlavfi.silence_end=25.4\n"), "")

    def test_the_check_failing_does_not_drop_the_file(self):
        with mock.patch.object(ambient.subprocess, "run", side_effect=OSError("no ffmpeg")):
            self.assertEqual(ambient.slate_or_gap(Path("/x/bed.flac")), "")

    def test_a_bed_no_longer_wanted_is_removed_but_a_dropped_in_file_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ambient"; (root / "rain").mkdir(parents=True)
            (root / "rain" / "old.mp3").write_bytes(b"x")       # fetched last time
            (root / "rain" / "mine.mp3").write_bytes(b"x")      # Chris's own
            (root / "rain" / ".fetched.json").write_text('["old.mp3"]')
            with mock.patch.object(ambient, "SETS", {"rain": [("i", "new.mp3", "CC0")]}), \
                 mock.patch.object(ambient, "fetch", side_effect=lambda i, n, dest: dest.write_bytes(b"x") or True), \
                 mock.patch.object(ambient, "slate_or_gap", return_value=""):
                ambient.build("rain", root, Path(d) / "rain.m3u")
            self.assertFalse((root / "rain" / "old.mp3").exists())
            self.assertTrue((root / "rain" / "mine.mp3").exists())
            self.assertTrue((root / "rain" / "new.mp3").exists())
