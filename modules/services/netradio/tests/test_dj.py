import logging
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import dj

logging.disable(logging.CRITICAL)


def T(title, artist, path=None):
    return dj.Track(path or f"/m/{artist}/{title}.mp3", title, artist)


class Compose(unittest.TestCase):
    def test_mentions_previous_and_next(self):
        prev = [T("Sweet Dixie", "Art Stamper"), T("Whoa Mule", "Adam Tanner"), T("Sugar Hill", "Art Stamper")]
        nxt = T("Double Banjo Blues", "Reno and Smiley")
        for seed in range(20):
            text = dj.compose(prev, nxt, "Bluegrass and Old-Time", random.Random(seed))
            for needle in ("Sugar Hill", "Whoa Mule", "Sweet Dixie", "Double Banjo Blues", "Reno and Smiley"):
                self.assertIn(needle, text, text)
            self.assertNotIn("{", text)
            self.assertNotIn("  ", text)

    def test_varies_wording(self):
        prev = [T("A", "X"), T("B", "Y"), T("C", "Z")]
        texts = {dj.compose(prev, T("D", "W"), "Rock", random.Random(s)) for s in range(40)}
        self.assertGreater(len(texts), 10)

    def test_first_break_with_one_or_no_previous(self):
        text = dj.compose([], T("D", "W"), "Rock", random.Random(3))
        self.assertIn("D", text)
        self.assertIn("W", text)
        text = dj.compose([T("A", "X")], T("D", "W"), "Rock", random.Random(3))
        self.assertIn("A", text)
        self.assertIn("D", text)

    def test_at_most_three_earlier_tracks_named(self):
        prev = [T(f"T{i}", f"A{i}") for i in range(8)]
        text = dj.compose(prev, T("N", "B"), "Folk", random.Random(1))
        self.assertIn("T7", text)                      # the one that just finished
        self.assertNotIn("T0", text)
        self.assertNotIn("T3", text)
        self.assertIn("T4", text)


class Cleaning(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(dj.clean("Whoa Mule (Live) [Remastered]"), "Whoa Mule")
        self.assertEqual(dj.clean('  "Sugar Hill" -- '), "Sugar Hill")
        self.assertEqual(dj.clean("()"), "untitled")

    def test_read_tags_falls_back_to_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Art Stamper" / "Goodbye Girls" / "03 Hickory Jack.mp3"
            p.parent.mkdir(parents=True)
            p.write_bytes(b"not audio")
            t = dj.read_tags(str(p))
            self.assertEqual((t.title, t.artist), ("Hickory Jack", "Art Stamper"))

    def test_annotate_escapes(self):
        uri = dj.annotate({"title": 'Say "hi", now', "dj": "true"}, "/x/a b.wav")
        self.assertEqual(uri, 'annotate:title="Say \\"hi\\", now",dj="true":/x/a b.wav')


class FakeLS:
    """Liquidsoap's queue commands, minimally: push returns a RID, queue lists
    pending RIDs; `play()` consumes one, as a track ending would."""

    def __init__(self):
        self.pending = []
        self.pushed = []
        self.rid = 0

    def command(self, cmd):
        if cmd.startswith("q_x.push "):
            self.rid += 1
            uri = cmd[len("q_x.push "):]
            self.pending.append(self.rid)
            self.pushed.append(uri)
            return str(self.rid)
        if cmd == "q_x.queue":
            return " ".join(str(r) for r in self.pending)
        if cmd.startswith("q_x.ignore "):
            # Liquidsoap 2.4 has no such command; the real server answers this
            # way and the DJ must never depend on it again (2026-09-25)
            return "ERROR: unknown command, type \"help\" to get a list of commands."
        if cmd == "src_x.skip":
            self.skipped = getattr(self, "skipped", 0) + 1
            return "Done"
        raise AssertionError(cmd)

    def play(self):
        self.pending.pop(0)


class FakeTTS:
    def __init__(self, fail=False):
        self.texts = []
        self.fail = fail

    def render(self, text):
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("kokoro down")
        return b"RIFF" + text.encode()


class StationDJTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pl = root / "x.m3u"
        lines = ["#EXTM3U"] + [f"/m/Artist{i}/Album/0{i} Song{i}.mp3" for i in range(12)]
        self.pl.write_text("\n".join(lines) + "\n")
        self.ls = FakeLS()
        self.tts = FakeTTS()
        self.dj = dj.StationDJ("x", "Test Station", self.pl, root / "dj" / "x", self.ls, self.tts,
                               rng=random.Random(7), breaks_every=(3, 3))
        self.read_tags = mock.patch.object(dj, "read_tags", side_effect=lambda p: dj.Track(p, Path(p).stem[3:], Path(p).parent.parent.name))
        self.read_tags.start()

    def tearDown(self):
        self.read_tags.stop()
        self.tmp.cleanup()

    def test_keeps_two_ahead_and_breaks_every_three(self):
        self.dj.fill()
        self.assertEqual(len(self.ls.pending), 2)
        # play items one by one; a break rides in front of its track, so the
        # queue holds two tracks plus at most one break
        for _ in range(12):
            self.ls.play()
            self.dj.fill()
            self.assertIn(len(self.ls.pending), (2, 3))
        kinds = ["break" if u.startswith("annotate:") else "track" for u in self.ls.pushed]
        # first three are tracks, then a break, then three tracks, ...
        self.assertEqual(kinds[:8], ["track", "track", "track", "break", "track", "track", "track", "break"])
        self.assertEqual(kinds.count("break"), 3)

    def test_break_names_the_three_previous_and_the_next(self):
        for _ in range(6):
            self.dj.fill()
            self.ls.play()
        text = self.tts.texts[0]
        pushed_tracks = [u for u in self.ls.pushed if not u.startswith("annotate:")]
        names = [Path(u).stem[3:] for u in pushed_tracks]
        for n in names[:3]:
            self.assertIn(n, text, text)
        self.assertIn(names[3], text, text)          # the one queued right after the break
        self.assertNotIn(names[4], text, text)
        break_uri = [u for u in self.ls.pushed if u.startswith("annotate:")][0]
        self.assertIn('liq_amplify="1.8"', break_uri)
        self.assertIn('title="Station break"', break_uri)
        self.assertTrue(break_uri.endswith("break-000001.wav"))
        self.assertTrue((self.dj.out_dir / "break-000001.wav").exists())

    def test_tts_failure_skips_break_and_keeps_music_going(self):
        self.tts.fail = True
        for _ in range(8):
            self.dj.fill()
            self.ls.play()
        self.assertFalse(any(u.startswith("annotate:") for u in self.ls.pushed))
        self.assertGreaterEqual(len(self.ls.pushed), 8)
        self.assertGreaterEqual(len(self.tts.texts), 2)  # it kept trying at each break point

    def test_no_repeat_until_pool_exhausted(self):
        for _ in range(12):
            self.dj.fill()
            self.ls.play()
        tracks = [u for u in self.ls.pushed if not u.startswith("annotate:")]
        self.assertEqual(len(tracks), len(set(tracks)))

    def test_empty_playlist_queues_nothing(self):
        self.pl.write_text("#EXTM3U\n")
        self.dj.fill()
        self.assertEqual(self.ls.pushed, [])

    def test_next_json_for_the_page(self):
        import json
        now = Path(self.tmp.name) / "now"
        self.dj.now_dir = now
        self.dj.fill()                                   # two tracks queued
        d = json.loads((now / "x-next.json").read_text())
        self.assertEqual(len(d["next"]), 2)
        self.assertEqual(d["next"][0]["kind"], "track")
        self.assertIn("artist", d["planned"])
        self.dj.until_break = 0
        self.ls.play(); self.dj.fill()                   # a break rides in front of the next track
        d = json.loads((now / "x-next.json").read_text())
        self.assertEqual([e["kind"] for e in d["next"]], ["track", "break", "track"])
        self.assertTrue(d["last_break"])

    def test_old_breaks_are_pruned(self):
        self.dj.breaks_every = (1, 1)
        self.dj.until_break = 0
        for _ in range(10):
            self.dj.fill()
            self.ls.play()
        self.assertLessEqual(len(list(self.dj.out_dir.glob("break-*.wav"))), dj.KEEP_BREAKS)


if __name__ == "__main__":
    unittest.main()


class Feedback(unittest.TestCase):
    """The admin's inbox files: skip, and a request that jumps the queue."""
    setUp = StationDJTests.setUp
    tearDown = StationDJTests.tearDown

    def test_skip_and_request(self):
        import json
        self.dj.fill()
        pending_before = list(self.ls.pending)
        self.assertEqual(len(pending_before), 2)
        self.tracks = ["/m/Artist3/Album/03 Song3.mp3"]
        inbox = Path(self.tmp.name) / "dj" / "inbox"        # where the admin API writes: <dj>/inbox, not <dj>/<mount>/inbox
        self.assertEqual(self.dj.inbox, inbox)
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "x-1.json").write_text(json.dumps({"action": "skip"}))
        (inbox / "x-2.json").write_text(json.dumps({"action": "request", "path": self.tracks[0], "who": "Chris"}))
        self.dj.fill()
        self.assertEqual(self.ls.skipped, 1)
        self.assertFalse(list(inbox.glob("x-*.json")))                     # consumed
        # NOTHING already queued is dropped: Liquidsoap 2.4 cannot remove a
        # queued item, so a request joins the back of a shallow queue
        for rid in pending_before:
            self.assertIn(rid, self.ls.pending)
        self.assertIn("Chris", " ".join(self.tts.texts))                   # announced on air, by name
        self.assertTrue(any(self.tracks[0] in u for u in self.ls.pushed[-4:]))
        self.assertTrue(any(e.get("request") for e in self.dj.pushed))

    def test_several_requests_share_one_announcement_and_keep_their_order(self):
        import json
        self.dj.fill()
        inbox = Path(self.tmp.name) / "dj" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        for i, (who, song) in enumerate((("Chris", "First"), ("Mary", "Second"), ("", "Third"))):
            (inbox / f"x-{1000000000000 + i}-0000.json").write_text(
                json.dumps({"action": "request", "path": f"/m/AAA/Album/0{i} {song}.mp3", "who": who}))
        before = len(self.tts.texts)
        self.dj.handle_inbox()
        # ONE break for the batch, naming all three and both askers
        breaks = self.tts.texts[before:]
        self.assertEqual(len(breaks), 1, breaks)
        for name in ("First", "Second", "Third", "Chris", "Mary"):
            self.assertIn(name, breaks[0])
        # and the three tracks queued behind it, in the order they were asked
        reqs = [e for e in self.dj.pushed if e.get("request")]
        self.assertEqual([r["title"] for r in reqs], ["First", "Second", "Third"])

    def test_the_dj_never_calls_a_command_liquidsoap_lacks(self):
        import json
        self.dj.fill()
        inbox = Path(self.tmp.name) / "dj" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "x-9.json").write_text(json.dumps({"action": "request", "path": "/m/AAA/Album/01 X.mp3"}))
        seen = []
        real = self.ls.command
        self.ls.command = lambda c: seen.append(c) or real(c)
        self.dj.handle_inbox()
        self.assertFalse([c for c in seen if ".ignore" in c],
                         "q.ignore does not exist in Liquidsoap 2.4 — see play_requests")

    def test_skip_waits_while_liquidsoap_is_down(self):
        """A press while Liquidsoap's socket is gone (a deploy) is kept for
        the next pass — unless it has gone stale, when it is dropped rather
        than fired minutes later at whatever is playing then."""
        import json
        import os
        import time
        inbox = Path(self.tmp.name) / "dj" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        real = self.ls.command
        self.ls.command = lambda cmd: (_ for _ in ()).throw(FileNotFoundError("no socket"))
        fresh = inbox / "x-1.json"
        fresh.write_text(json.dumps({"action": "skip"}))
        self.dj.handle_inbox()
        self.assertTrue(fresh.exists())                                    # kept for retry
        stale = inbox / "x-0.json"
        stale.write_text(json.dumps({"action": "skip"}))
        old = time.time() - 600
        os.utime(stale, (old, old))
        self.dj.handle_inbox()
        self.assertFalse(stale.exists())                                   # too old: dropped
        self.assertTrue(fresh.exists())
        self.ls.command = real
        self.dj.handle_inbox()
        self.assertEqual(self.ls.skipped, 1)                               # fired once the socket is back
        self.assertFalse(fresh.exists())

    def test_skip_refused_is_logged_not_swallowed(self):
        import json
        inbox = Path(self.tmp.name) / "dj" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        self.ls.command = lambda cmd: 'ERROR: unknown command, type "help" to get a list of commands.'
        (inbox / "x-1.json").write_text(json.dumps({"action": "skip"}))
        logging.disable(logging.NOTSET)          # the module silences logging; this test reads it
        try:
            with self.assertLogs("netradio.dj", level="ERROR") as cm:
                self.dj.handle_inbox()
        finally:
            logging.disable(logging.CRITICAL)
        self.assertTrue(any("skip refused" in line for line in cm.output))
        self.assertFalse(list(inbox.glob("x-*.json")))


class Excursion(unittest.TestCase):
    """The fringe list beside the playlist: a small share of base picks, none
    while a segment is on, none when the station sets excursion 0."""
    setUp = StationDJTests.setUp
    tearDown = StationDJTests.tearDown

    def test_fringe_share(self):
        (Path(self.tmp.name) / "x-fringe.m3u").write_text("#EXTM3U\n/m/Fringe/Album/01 Edge.mp3\n")
        self.dj.rng = random.Random(3)
        picks = [self.dj.choose().path for _ in range(300)]
        fringe = sum("Fringe" in p for p in picks)
        self.assertEqual(len(self.dj.fringe), 1)
        self.assertTrue(15 <= fringe <= 50, fringe)            # ~10% of 300
        self.dj.excursion = 0.0
        self.assertFalse(any("Fringe" in self.dj.choose().path for _ in range(100)))

    def test_no_fringe_file_means_no_excursion(self):
        self.dj.load_playlist()
        self.assertEqual(self.dj.fringe, [])
        self.assertFalse(any("Fringe" in self.dj.choose().path for _ in range(50)))
