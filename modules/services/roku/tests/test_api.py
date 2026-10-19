"""The JSON service: routing, the power gate, and what happens when the box
has moved. Served on a real socket against a fake device, so the handler under
test is the shipped one."""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roku import api
from roku.ecp import Roku, RokuError


class FakeDevice(api.Device):
    """A Device whose roku() hands back a stub, and which records whether it
    was told to forget an address."""

    def __init__(self, fail: str = ""):
        super().__init__(host="10.0.0.5")
        self.fail = fail
        self.calls: list[tuple] = []
        self.forgotten = 0

    def roku(self):
        dev = self

        class Stub(Roku):
            def __init__(self):
                super().__init__("10.0.0.5")

            def _req(self, method, path, *, raw=False):
                dev.calls.append((method, path))
                if dev.fail:
                    raise RokuError(dev.fail)
                if path == "/query/device-info":
                    return b"<device-info><model-name>M</model-name><power-mode>PowerOn</power-mode></device-info>"
                if path == "/query/active-app":
                    return b"<active-app><app id='1' type='home'>Home</app></active-app>"
                if path == "/query/apps":
                    return b"<apps><app id='12' type='appl' version='1'>Netflix</app></apps>"
                return b""
        return Stub()

    def forget(self):
        self.forgotten += 1


class ApiTests(unittest.TestCase):
    allow_power = False

    def setUp(self):
        self.device = FakeDevice()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                       api.make_handler(self.device, self.allow_power))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_status_and_apps(self):
        code, body = self.call("GET", "/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["model"], "M")
        self.assertTrue(body["awake"])
        code, body = self.call("GET", "/apps")
        self.assertEqual([a["name"] for a in body["apps"]], ["Netflix"])

    def test_a_key_is_pressed(self):
        code, body = self.call("POST", "/key/Home")
        self.assertEqual((code, body["ok"]), (200, True))
        self.assertIn(("POST", "/keypress/Home"), self.device.calls)

    def test_hold_is_an_action_on_the_same_endpoint(self):
        self.call("POST", "/key/Right?action=keydown")
        self.assertIn(("POST", "/keydown/Right"), self.device.calls)

    def test_an_unknown_key_is_a_400_and_never_reaches_the_device(self):
        """The caller's mistake, not the device's. Reporting it as 502 blamed
        the Roku for a request it never saw."""
        code, body = self.call("POST", "/key/Hmoe")
        self.assertEqual(code, 400)
        self.assertIn("not an ECP key", body["error"])
        self.assertEqual(self.device.calls, [], "a bad key still went to the device")
        self.assertEqual(self.device.forgotten, 0, "a typo made it forget a good address")

    def test_an_unknown_action_is_also_a_400(self):
        code, body = self.call("POST", "/key/Right?action=wiggle")
        self.assertEqual(code, 400)
        self.assertIn("not an ECP action", body["error"])

    def test_power_is_refused_by_default(self):
        """One request from a dark television while somebody is watching it."""
        code, body = self.call("POST", "/key/PowerOff")
        self.assertEqual(code, 403)
        self.assertIn("allowPower", body["error"])
        self.assertNotIn(("POST", "/keypress/PowerOff"), self.device.calls)

    def test_typing(self):
        code, body = self.call("POST", "/type", {"text": "hi"})
        self.assertEqual(body["sent"], 2)
        code, body = self.call("POST", "/type", {"text": ""})
        self.assertEqual(code, 400)

    def test_launch_and_search(self):
        self.call("POST", "/launch/12")
        self.assertIn(("POST", "/launch/12"), self.device.calls)
        code, _ = self.call("POST", "/search", {"keyword": "westerns"})
        self.assertEqual(code, 200)

    def test_an_unreachable_box_is_a_502_and_the_address_is_forgotten(self):
        """So the next call looks for it again rather than retrying an address
        that DHCP has moved."""
        self.device.fail = "timed out"
        code, body = self.call("GET", "/status")
        self.assertEqual(code, 502)
        self.assertIn("timed out", body["error"])
        self.assertEqual(self.device.forgotten, 1)

    def test_an_unknown_endpoint_is_a_404(self):
        code, _ = self.call("GET", "/nope")
        self.assertEqual(code, 404)


class PowerAllowed(ApiTests):
    """The same service with the gate opened, which is the only thing that
    should change."""
    allow_power = True

    def test_power_is_refused_by_default(self):
        code, body = self.call("POST", "/key/PowerOff")
        self.assertEqual((code, body["ok"]), (200, True))
        self.assertIn(("POST", "/keypress/PowerOff"), self.device.calls)


if __name__ == "__main__":
    unittest.main()
