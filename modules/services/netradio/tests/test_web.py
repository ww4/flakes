"""The web pages are plain files served as-is, so nothing type-checks them.
These are the cheap structural checks that catch the mistakes that actually
happened.
"""

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
PAGES = ["index.html", "desktop.html"]
SCRIPTS = ["app.js", "desktop.js"]


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
    def test_the_desktop_page_knows_about_the_local_speaker(self):
        """The phone page grew a third target (this box's own sound card) and
        the desktop page never did — so a wide screen, which index.html
        redirects to desktop.html automatically, has no way to reach it.
        """
        js = (WEB / "desktop.js").read_text()
        self.assertIn("speaker", js,
                      "desktop.js has no speaker support: a wide screen gets redirected here "
                      "and loses the local-speaker target entirely")


if __name__ == "__main__":
    unittest.main()
