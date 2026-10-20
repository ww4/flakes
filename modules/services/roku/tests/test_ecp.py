"""The ECP client, against a fake device.

Nothing here talks to hardware. The process boundary — `Roku._req` — is the
only thing stubbed; every method under test is the shipped one, which is the
lesson netradio's FakeMixer records: a fixture that reimplements the thing it
is testing passes while the real path is broken.
"""

import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roku.ecp import KEYS, POWER_KEYS, Roku, RokuError, RokuLimited

DEVICE_INFO = b"""<device-info>
  <udn>abc</udn>
  <serial-number>X01600JLN0C0</serial-number>
  <model-name>Roku Express 4K+</model-name>
  <model-number>3941X</model-number>
  <friendly-device-name>Roku Express 4K+</friendly-device-name>
  <user-device-name>Living room box</user-device-name>
  <software-version>14.5.4</software-version>
  <network-type>wifi</network-type>
  <power-mode>Suspend</power-mode>
  <supports-find-remote>true</supports-find-remote>
</device-info>"""

APPS = b"""<apps>
  <app id="12" type="appl" version="5.1">Netflix</app>
  <app id="837" type="appl" version="2.0">YouTube</app>
  <app id="tvinput.hdmi1" type="tvin" version="1.0">HDMI 1</app>
</apps>"""

ACTIVE = b"""<active-app><app id="562859" type="home" version="12.5.1">Home</app></active-app>"""


class FakeRoku(Roku):
    """The real client with only the wire replaced."""

    def __init__(self, fail: str = ""):
        super().__init__("10.0.0.5")
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def _req(self, method, path, *, raw=False):
        self.sent.append((method, path))
        if self.fail:
            raise RokuError(self.fail)
        if path == "/query/device-info":
            return DEVICE_INFO
        if path == "/query/apps":
            return APPS
        if path == "/query/active-app":
            return ACTIVE
        if path.startswith("/query/icon/"):
            return (b"\xff\xd8jpeg", "image/jpeg") if raw else b""
        return b""


class Reading(unittest.TestCase):
    def test_status_names_things_for_people(self):
        s = FakeRoku().status()
        self.assertEqual(s["name"], "Living room box")      # user name beats the friendly one
        self.assertEqual(s["model"], "Roku Express 4K+")
        self.assertEqual(s["power"], "Suspend")
        self.assertFalse(s["awake"])
        self.assertTrue(s["supports_find_remote"])
        self.assertEqual(s["app"]["name"], "Home")

    def test_device_info_passes_everything_through(self):
        """Roku add fields between firmware versions. A chosen subset here
        would mean a new capability is invisible until someone edits this."""
        d = FakeRoku().device_info()
        self.assertEqual(d["model-number"], "3941X")
        self.assertIn("udn", d)

    def test_apps_and_icons(self):
        r = FakeRoku()
        apps = r.apps()
        self.assertEqual([a["name"] for a in apps], ["Netflix", "YouTube", "HDMI 1"])
        self.assertEqual(apps[0]["id"], "12")
        data, ctype = r.icon("12")
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(data[:2], b"\xff\xd8")

    def test_an_unreachable_box_raises_rather_than_crashes(self):
        with self.assertRaises(RokuError):
            FakeRoku(fail="asleep").status()


class Pressing(unittest.TestCase):
    def test_a_real_key_is_sent(self):
        r = FakeRoku()
        r.press("Home")
        self.assertEqual(r.sent, [("POST", "/keypress/Home")])

    def test_a_typo_is_refused_here_because_the_device_will_not_refuse_it(self):
        """ECP answers 200 to `/keypress/Hmoe` and does nothing. A button that
        silently does not work is the worst thing to debug from a sofa."""
        r = FakeRoku()
        with self.assertRaises(RokuError):
            r.press("Hmoe")
        self.assertEqual(r.sent, [], "a bad key still went to the device")

    def test_hold_and_release(self):
        r = FakeRoku()
        r.press("Right", "keydown")
        r.press("Right", "keyup")
        self.assertEqual([p for _, p in r.sent], ["/keydown/Right", "/keyup/Right"])
        with self.assertRaises(RokuError):
            r.press("Right", "wiggle")

    def test_power_keys_are_known_keys(self):
        """The gate is the module's, not the protocol's — but the client has to
        recognise them for the gate to have anything to check."""
        self.assertTrue(POWER_KEYS <= KEYS)

    def test_typing_sends_one_keypress_per_character_and_escapes(self):
        r = FakeRoku()
        self.assertEqual(r.literal("a b&c"), 5)
        self.assertEqual([p for _, p in r.sent],
                         ["/keypress/Lit_a", "/keypress/Lit_%20", "/keypress/Lit_b",
                          "/keypress/Lit_%26", "/keypress/Lit_c"])


class Launching(unittest.TestCase):
    def test_launch_and_search(self):
        r = FakeRoku()
        r.launch("12")
        r.search(keyword="the thin man", type="movie")
        self.assertEqual(r.sent[0], ("POST", "/launch/12"))
        self.assertIn("/search/browse?keyword=the+thin+man", r.sent[1][1])

    def test_search_without_a_keyword_is_refused(self):
        with self.assertRaises(RokuError):
            FakeRoku().search(type="movie")

    def test_an_app_id_with_odd_characters_is_quoted(self):
        r = FakeRoku()
        r.launch("tvinput.hdmi1")
        self.assertEqual(r.sent[0][1], "/launch/tvinput.hdmi1")


class LimitedMode(unittest.TestCase):
    """Roku's "Control by mobile apps" has a Limited position, and in it the box
    answers 403 to everything that would DO something while still answering
    /query/device-info and /query/active-app.

    So it looks reachable and healthy and ignores every button. That is exactly
    how it arrived: "none of the buttons are working in the remote", against a
    service whose log was five lines of HTTP 403 (Chris, 2026-10-19).
    """

    class Box(Roku):
        def __init__(self, limited_paths=()):
            super().__init__("10.0.0.5")
            self.blocked = set(limited_paths)
            self.sent = []

        def _req(self, method, path, *, raw=False):
            self.sent.append(path)
            if any(path.startswith(p) for p in self.blocked):
                raise RokuLimited(f"{method} {path}")
            if path == "/query/device-info":
                return b"<device-info><model-name>M</model-name><power-mode>PowerOn</power-mode></device-info>"
            if path == "/query/active-app":
                return b"<active-app><app id='1' type='home'>Home</app></active-app>"
            if path == "/query/apps":
                return b"<apps></apps>"
            return b""

    def test_the_message_says_where_the_setting_is(self):
        """A bare "HTTP 403" in a toast tells a person nothing they can act on.
        Nothing on our side can clear this — it is a setting on the television."""
        e = RokuLimited("POST /keypress/Home")
        self.assertIn("Limited mode", str(e))
        self.assertIn("Control by mobile apps", str(e))
        self.assertIsInstance(e, RokuError)      # still catchable as one

    def test_a_403_becomes_limited_rather_than_a_number(self):
        import urllib.error
        box = Roku("10.0.0.5")
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RokuLimited):
                box.press("Home")

    def test_a_404_is_still_an_ordinary_error(self):
        import urllib.error
        box = Roku("10.0.0.5")
        err = urllib.error.HTTPError("u", 404, "Nope", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RokuError) as cm:
                box.press("Home")
        self.assertNotIsInstance(cm.exception, RokuLimited)

    def test_status_answers_will_my_buttons_work_without_pressing_one(self):
        """Pressing a key to find out would turn a diagnostic into an action on
        somebody's television. /query/apps is blocked by the same setting and
        changes nothing."""
        blocked = self.Box(limited_paths=("/query/apps", "/keypress"))
        self.assertTrue(blocked.status()["limited"])
        self.assertNotIn("/keypress/Home", blocked.sent, "it pressed a key to find out")

        ok = self.Box()
        self.assertFalse(ok.status()["limited"])

    def test_an_unreachable_box_is_not_reported_as_limited(self):
        box = self.Box()
        with mock.patch.object(self.Box, "_req", side_effect=RokuError("timed out")):
            self.assertFalse(box.limited())


if __name__ == "__main__":
    unittest.main()
