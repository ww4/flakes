"""The web pages are plain files served as-is, so nothing type-checks them.
These are the cheap structural checks that catch the mistakes that actually
happened.
"""

import os
import re
import unittest
from pathlib import Path

# web/ is deliberately NOT part of the Python package's source (editing a page
# must not rehash the package and restart every unit), so inside that build
# there is nothing here to check and these tests skip. They are run for real by
# the `netradio-web` derivation, which has the pages and sets NETRADIO_WEB —
# and that derivation is nginx's document root, so it gates every deploy.
WEB = Path(os.environ.get("NETRADIO_WEB") or (Path(__file__).resolve().parent.parent / "web"))
PAGES = ["index.html", "desktop.html"]
SCRIPTS = ["app.js", "desktop.js"]


def setUpModule():
    if not (WEB / "index.html").exists():
        raise unittest.SkipTest(f"no web/ at {WEB} — these run in the netradio-web build")


def module_functions(js: str) -> set[str]:
    """Functions declared at module scope — visible inside the script, and
    NOT inside a Vue template expression."""
    return set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", js, re.M))


def template_calls(html: str) -> set[str]:
    """Identifiers invoked inside Vue binding attributes and {{ }}."""
    exprs = re.findall(r'(?:@[\w.:-]+|v-if|v-else-if|v-model[\w.]*|:[\w-]+)\s*=\s*"([^"]*)"', html)
    exprs += re.findall(r"\{\{(.*?)\}\}", html, re.S)
    names: set[str] = set()
    for e in exprs:
        # Only BARE identifiers: `foo(` resolves on the component, while
        # `x.some(` is a method on some other object and says nothing about
        # the component's own surface. Missing this made `Array.some` look
        # like an undefined method.
        names |= set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", e))
    return names


class TemplateScope(unittest.TestCase):
    def test_no_template_calls_a_module_scope_function(self):
        """Vue evaluates a template expression against the component, so a
        module-scope helper is simply not there — and the failure is a runtime
        "call is not a function" in the browser, which no build step catches.

        This happened: the local-speaker volume slider was written as
            @change="speakerAction('', () => call('POST', 'speaker/volume', …))"
        and `call` is a module function in app.js. Mute worked (a real method),
        volume silently did nothing but throw in the console, and it read as
        "the volume doesn't work" (Chris, 2026-09-26).
        """
        for page, script in zip(PAGES, SCRIPTS):
            html = (WEB / page).read_text()
            js = (WEB / script).read_text()
            bad = template_calls(html) & module_functions(js)
            self.assertEqual(bad, set(),
                             f"{page} calls module-scope {sorted(bad)} from {script} — "
                             f"move it onto the component as a method")

    def test_every_method_a_template_calls_actually_exists(self):
        """The mirror of the above: a template naming a method that was renamed
        away fails the same silent way."""
        for page, script in zip(PAGES, SCRIPTS):
            html = (WEB / page).read_text()
            js = (WEB / script).read_text()
            # method shorthand `name(...)  {`, `name: function`, `name: (…) =>`
            defined = set(re.findall(r"^\s{4}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(", js, re.M))
            defined |= set(re.findall(r"^\s{4}([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:function|\()", js, re.M))
            defined |= set(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", js))   # anything called in the script too
            # JS builtins and Vue helpers a template may legitimately use
            allowed = {"Math", "Number", "String", "Boolean", "Object", "Array", "JSON", "Date",
                       "parseInt", "parseFloat", "isNaN", "encodeURIComponent", "$event"}
            missing = template_calls(html) - defined - allowed
            self.assertEqual(missing, set(),
                             f"{page} calls {sorted(missing)}, which {script} does not define")


class SpeakerTargetParity(unittest.TestCase):
    """The two pages drift, and always the same way: something is built on the
    phone and the desktop never gets it. index.html redirects a wide screen to
    desktop.html automatically, so whatever is missing there is missing for
    anyone on a computer — silently, because both pages still work.

    This began as one check for one omission (the local speaker) and caught
    nothing after it: by 2026-09-28 the desktop had also never gained the
    heart, station artwork, the receiver remote or resume. One check per
    capability, named for what a listener loses without it.
    """

    def script(self, name: str) -> str:
        return (WEB / name).read_text()

    def test_the_desktop_page_knows_about_the_local_speaker(self):
        self.assertIn("speaker", self.script("desktop.js"),
                      "desktop.js has no speaker support: a wide screen gets redirected here "
                      "and loses the local-speaker target entirely")

    def test_both_pages_offer_the_heart(self):
        """A toggle, mirrored to Jellyfin. It reached the phone on 2026-09-27;
        the desktop kept the "Never" / "Less of them" pair it replaced."""
        for page, script in zip(PAGES, SCRIPTS):
            js = self.script(script)
            self.assertIn("toggleHeart", js, f"{script} cannot set the heart")
            self.assertIn("admin/api/heart", js, f"{script} never reads the heart back")
            self.assertIn("hearted", (WEB / page).read_text(),
                          f"{page} never shows whether the track is hearted")

    def test_both_pages_show_station_artwork(self):
        """now/tiles.json and admin/api/art are served for both pages; the
        desktop ignored them and drew a list of names."""
        for script in SCRIPTS:
            js = self.script(script)
            self.assertIn("now/tiles.json", js, f"{script} never loads the cover manifest")
            self.assertIn("admin/api/art", js, f"{script} never asks for a cover")

    def test_both_pages_tell_the_OS_what_is_playing(self):
        """Without MediaSession the phone's notification is a bare pause button
        over the page title — no track, no artist, no cover (Chris, 2026-09-28).
        Skip is wired to `nexttrack`; there is no heart action to wire, and no
        amount of wanting one changes MediaSessionAction's closed list."""
        for script in SCRIPTS:
            js = self.script(script)
            self.assertIn("MediaMetadata", js, f"{script} never tells the OS what is playing")
            self.assertIn('setActionHandler', js, f"{script} sets no media control handlers")
            self.assertIn('"nexttrack"', js, f"{script} does not offer skip on the notification")

    def test_the_desktop_page_can_drive_and_recover_the_receiver(self):
        """Without resume, a receiver left stopped by a deploy stays stopped
        until somebody notices it went quiet (2026-09-27)."""
        js = self.script("desktop.js")
        self.assertIn("receiver/menu/cursor", js, "desktop.js cannot drive the receiver's menus")
        self.assertIn("admin/api/resume", js, "desktop.js cannot put the receiver back")


class Packaging(unittest.TestCase):
    """Every asset a page asks the browser for must be one the derivation
    copies. Nothing else catches this: an uncopied stylesheet is a 404 at
    runtime, and the page then renders unstyled but otherwise working — which
    reads as a CSS bug rather than a missing file.
    """

    def test_every_local_asset_a_page_references_is_installed(self):
        nix_path = os.environ.get("NETRADIO_NIX")
        if not nix_path or not Path(nix_path).exists():
            self.skipTest("no NETRADIO_NIX — this runs for real in the netradio-web build")
        nix = Path(nix_path).read_text()
        for page in PAGES:
            html = (WEB / page).read_text()
            for ref in re.findall(r'(?:href|src)\s*=\s*"([^"]+)"', html):
                if ref.startswith(("http://", "https://", "#", "data:", "/")):
                    continue
                name = ref.split("?")[0].lstrip("./")
                if not (WEB / name).exists():
                    continue          # written at build time (site.json) or by the scanner (now/…)
                self.assertIn(name, nix,
                              f"{page} loads {name}, which default.nix never copies into $out — "
                              f"it 404s on the deployed page")


if __name__ == "__main__":
    unittest.main()
