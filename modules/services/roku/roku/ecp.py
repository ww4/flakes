"""Roku's External Control Protocol, as a small client.

ECP is plain HTTP on port 8060 with no authentication, no pairing and no cloud
account — it is what Roku's own phone app speaks, which is why that app keeps
working when the internet does not. Roku document it publicly.

Two things this adds over talking to the box directly with curl:

  * XML in, dicts out. Every ECP response is XML; everything upstream of here
    wants JSON, and parsing it in one place means the page never has to.
  * A key is checked against the protocol's own list before it is sent.
    `/keypress/Wat` returns 200 and does nothing, so a typo in a button is
    silent — exactly the class of failure that is worst to debug from a sofa.

What it deliberately does NOT do is decide policy. Whether PowerOff may be
pressed at all is the module's business (`services.roku.allowPower`), not this
file's.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("roku.ecp")

PORT = 8060

# The protocol's keys. Sending one that is not here is a typo, and ECP answers
# 200 to a typo — so it is caught at this end or not at all.
KEYS = frozenset("""
    Home Rev Fwd Play Select Left Right Down Up Back InstantReplay Info
    Backspace Search Enter FindRemote
    VolumeDown VolumeMute VolumeUp
    PowerOff PowerOn Power
    ChannelUp ChannelDown InputTuner InputHDMI1 InputHDMI2 InputHDMI3
    InputHDMI4 InputAV1
""".split())

# The three ways a key can be sent: a tap, or a hold as a down/up pair.
ACTIONS = frozenset({"keypress", "keydown", "keyup"})

# Keys that change what the room is doing rather than where the cursor is.
# The module gates these; see `allowPower`.
POWER_KEYS = frozenset({"PowerOff", "PowerOn", "Power"})


class RokuError(RuntimeError):
    """The device refused, or could not be reached."""


class Roku:
    def __init__(self, host: str, port: int = PORT, timeout: float = 5.0):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    # -- the wire ----------------------------------------------------------
    def _req(self, method: str, path: str, *, raw: bool = False):
        url = self.base + path
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read(2_000_000)
                return (body, r.headers.get("Content-Type", "")) if raw else body
        except urllib.error.HTTPError as e:
            raise RokuError(f"{method} {path}: HTTP {e.code}") from e
        except Exception as e:
            # Asleep, unplugged, or off the wifi. The caller decides whether
            # that is an error worth showing; it is never a crash.
            raise RokuError(f"{method} {path}: {e}") from e

    @staticmethod
    def _xml(body: bytes) -> ET.Element:
        try:
            return ET.fromstring(body)
        except ET.ParseError as e:
            raise RokuError(f"not XML: {e}") from e

    # -- what it is --------------------------------------------------------
    def device_info(self) -> dict:
        """The whole of /query/device-info, flattened. Roku add fields between
        firmware versions, so everything is passed through rather than a chosen
        subset — a caller that wants five keys should take five keys."""
        root = self._xml(self._req("GET", "/query/device-info"))
        return {child.tag: (child.text or "") for child in root}

    def status(self) -> dict:
        """The handful a remote actually needs, named for people rather than
        for the protocol."""
        d = self.device_info()
        app = self.active_app()
        return {
            "name": d.get("user-device-name") or d.get("friendly-device-name") or "Roku",
            "model": d.get("model-name", ""),
            "serial": d.get("serial-number", ""),
            "software": d.get("software-version", ""),
            "network": d.get("network-type", ""),
            # "PowerOn" when awake, "Suspend"/"Ready" when not. ECP answers in
            # all of them, which is why a remote can wake the box.
            "power": d.get("power-mode", ""),
            "awake": d.get("power-mode", "") == "PowerOn",
            "app": app,
            "supports_find_remote": d.get("supports-find-remote") == "true",
        }

    def apps(self) -> list[dict]:
        root = self._xml(self._req("GET", "/query/apps"))
        return [{"id": a.get("id", ""), "name": (a.text or "").strip(),
                 "type": a.get("type", ""), "version": a.get("version", "")}
                for a in root.findall("app")]

    def active_app(self) -> dict:
        """What is on screen. An idle box reports its Home screen as an app,
        which is the honest answer and worth passing on."""
        root = self._xml(self._req("GET", "/query/active-app"))
        a = root.find("app")
        if a is None:
            return {}
        return {"id": a.get("id", ""), "name": (a.text or "").strip(), "type": a.get("type", "")}

    def icon(self, app_id: str) -> tuple[bytes, str]:
        data, ctype = self._req("GET", f"/query/icon/{urllib.parse.quote(app_id)}", raw=True)
        return data, (ctype or "image/jpeg")

    # -- what it does ------------------------------------------------------
    def press(self, key: str, action: str = "keypress") -> None:
        """One button. `action` is keypress, keydown or keyup — the last two
        are how you hold a direction to scrub rather than step."""
        if key not in KEYS:
            raise RokuError(f"not an ECP key: {key!r}")
        if action not in ACTIONS:
            raise RokuError(f"not an ECP action: {action!r}")
        self._req("POST", f"/{action}/{key}")

    def literal(self, text: str) -> int:
        """Type into whatever has focus, a character at a time.

        ECP has no "send a string": each character is its own keypress of
        `Lit_<the character, percent-encoded>`. Returns how many were sent, so
        a caller can report a partial send rather than claim the whole word
        arrived.
        """
        sent = 0
        for ch in text:
            self._req("POST", "/keypress/Lit_" + urllib.parse.quote(ch, safe=""))
            sent += 1
        return sent

    def launch(self, app_id: str, params: dict | None = None) -> None:
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        self._req("POST", f"/launch/{urllib.parse.quote(app_id)}{q}")

    def search(self, **params) -> None:
        """Roku's own search, across whatever providers the box knows about.
        `keyword` is the one that matters; the rest (type, provider-id, launch)
        are passed through as given."""
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if not clean.get("keyword"):
            raise RokuError("search needs a keyword")
        self._req("POST", "/search/browse?" + urllib.parse.urlencode(clean))
