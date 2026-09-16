import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from netradio import wake


def status(*sources):
    """Icecast's status-json shape, including its one-vs-many quirk."""
    if not sources:
        return {"icestats": {"dummy": None}}
    objs = [{"listenurl": f"http://127.0.0.1:8000/{m}.mp3", "listeners": n} for m, n in sources]
    return {"icestats": {"source": objs[0] if len(objs) == 1 else objs}}


class ParseListeners(unittest.TestCase):
    def test_none_one_many(self):
        self.assertEqual(wake.parse_listeners(status()), {})
        self.assertEqual(wake.parse_listeners(status(("rock", 1))), {"rock": 1})
        self.assertEqual(wake.parse_listeners(status(("rock", 0), ("folk", 2))), {"rock": 0, "folk": 2})

    def test_foreign_mounts_ignored(self):
        st = {"icestats": {"source": {"listenurl": "http://h/relay/live", "listeners": 3}}}
        self.assertEqual(wake.parse_listeners(st), {})


class FakeLiquidsoap:
    def __init__(self, icecast):
        self.icecast = icecast
        self.log = []

    def command(self, cmd):
        self.log.append(cmd)
        mount, op = cmd.split(".")
        if op == "start":
            self.icecast.up[mount] = 0
        elif op == "stop":
            self.icecast.up.pop(mount, None)
        return "OK"


class FakeIcecast:
    def __init__(self):
        self.up = {}

    def listeners(self):
        return dict(self.up)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pl = Path(self.tmp.name)
        (self.pl / "rock.m3u").write_text("#EXTM3U\n/m/a.mp3\n")
        (self.pl / "gospel.m3u").write_text("#EXTM3U\n")
        self.ic = FakeIcecast()
        self.ls = FakeLiquidsoap(self.ic)
        self.ctl = wake.Controller({"rock", "gospel"}, self.ls, self.ic, self.pl,
                                   idle_after=300, start_timeout=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_wake_starts_then_is_idempotent(self):
        self.assertEqual(self.ctl.wake("rock")[0], 200)
        self.assertEqual(self.ls.log, ["rock.start"])
        self.assertEqual(self.ctl.wake("rock")[0], 200)
        self.assertEqual(self.ls.log, ["rock.start"])  # already up: no second start

    def test_unknown_and_empty_stations_refuse(self):
        self.assertEqual(self.ctl.wake("jazz")[0], 404)
        self.assertEqual(self.ctl.wake("gospel")[0], 503)
        self.assertEqual(self.ls.log, [])

    def test_start_failure_is_503_not_hang(self):
        class Dead(FakeLiquidsoap):
            def command(self, cmd):
                self.log.append(cmd)
                return "no such output"
        self.ls = Dead(self.ic)
        self.ctl.ls = self.ls
        code, msg = self.ctl.wake("rock")
        self.assertEqual(code, 503)
        self.assertIn("no such output", msg)

    def test_idle_stop_after_grace_and_reset_by_listener(self):
        self.ctl.wake("rock")
        t = 1000.0
        self.assertEqual(self.ctl.tick(t), [])            # idle clock starts
        self.assertEqual(self.ctl.tick(t + 200), [])
        self.ic.up["rock"] = 1                             # someone tuned in
        self.assertEqual(self.ctl.tick(t + 250), [])
        self.ic.up["rock"] = 0                             # and left
        self.assertEqual(self.ctl.tick(t + 300), [])      # clock restarted at t+300
        self.assertEqual(self.ctl.tick(t + 599), [])
        self.assertEqual(self.ctl.tick(t + 600), ["rock"])
        self.assertEqual(self.ls.log[-1], "rock.stop")
        self.assertNotIn("rock", self.ic.up)

    def test_wake_resets_idle_clock(self):
        self.ctl.wake("rock")
        self.ctl.tick(1000.0)
        self.ctl.wake("rock")                              # re-tune before anyone attached
        self.assertEqual(self.ctl.tick(1299.0), [])       # clock restarts here, not at 1000
        self.assertEqual(self.ctl.tick(1300.0), [])
        self.assertEqual(self.ctl.tick(1598.0), [])
        self.assertEqual(self.ctl.tick(1599.0), ["rock"])

    def test_tick_leaves_foreign_mounts_alone(self):
        self.ic.up["live"] = 0
        self.assertEqual(self.ctl.tick(0.0), [])
        self.assertEqual(self.ctl.tick(10_000.0), [])
        self.assertEqual(self.ls.log, [])


class MountRegex(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(wake.MOUNT_RE.match("/radio/blues-jazz.mp3").group(1), "blues-jazz")
        self.assertIsNone(wake.MOUNT_RE.match("/radio/../etc.mp3"))
        self.assertIsNone(wake.MOUNT_RE.match("/radio/rock.mp3?x=1"))
        self.assertEqual(wake.MOUNT_RE.match(urlparse("/radio/rock.mp3?x=1").path).group(1), "rock")
        self.assertIsNone(wake.MOUNT_RE.match("/rock.mp3"))


if __name__ == "__main__":
    unittest.main()
