"""The CLI and API must at least import.

Renaming a Verdict member broke `stacks serve` at module import while the whole
suite stayed green, because nothing imported the CLI. A crash on startup is the
cheapest possible bug to catch and the most embarrassing one to ship.
"""


def test_cli_module_imports():
    import stacks.cli  # noqa: F401


def test_api_module_imports():
    import stacks.api  # noqa: F401


def test_every_verdict_has_a_display_style():
    from stacks.cli import _VERDICT_STYLE
    from stacks.match import Verdict

    missing = set(Verdict) - set(_VERDICT_STYLE)
    assert not missing, f"verdicts with no CLI style: {missing}"


def test_web_assets_exist():
    """The API mounts these; a rename would 404 the whole app silently."""
    from stacks.api import WEB_DIR

    for name in ("index.html", "app.js", "sw.js", "manifest.webmanifest"):
        assert (WEB_DIR / name).is_file(), f"missing web asset: {name}"
