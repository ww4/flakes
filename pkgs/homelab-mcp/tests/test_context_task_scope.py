"""Open tasks come from an allowlist, never from a walk of the whole space.

Regression cover for a live finding on 2026-09-01: the connector's get_context()
returned tasks from `MaM Interview Prep.md`, a root-level personal page carrying
a start code, plus a set of security review items. The old implementation walked
the entire space and skipped a hardcoded denylist (`Journal`, `Keep`), so any
page nobody had thought about was exposed by default — including every page
added afterwards.

The tests below are written so that *adding a new page to the space cannot make
them pass*. That is the property that failed before.
"""

from __future__ import annotations

import pytest

from homelab_mcp.context import build_context


@pytest.fixture()
def space(tmp_path):
    root = tmp_path / "space"
    (root / "Projects").mkdir(parents=True)
    (root / "Journal" / "Day").mkdir(parents=True)
    (root / "Areas").mkdir()

    (root / "CONVENTIONS.md").write_text("# Conventions\n\nBe consistent.\n")
    (root / "index.md").write_text("# Landing\n\nThe space.\n")

    # The shape of the leak: a private page at the space root.
    (root / "MaM Interview Prep.md").write_text(
        "# Interview prep\n\n- [ ] use start code SEKRIT-9931 at the gate\n"
    )
    (root / "Keep Import Review.md").write_text(
        "# Review\n\n- [ ] rotate the exposed credential on the public host\n"
    )
    # A page that is fine to surface, in a folder someone opted into.
    (root / "Projects" / "Greenhouse.md").write_text(
        "# Greenhouse\n\n- [ ] order the polycarbonate panels\n"
    )
    (root / "Journal" / "Day" / "2026-08-11.md").write_text("- [ ] journal task\n")
    return root


def _ctx(space, **kw) -> str:
    return build_context(space, include_service_inventory=False, **kw)


def _task_section(out: str) -> str:
    """Just the Open-tasks block.

    Scoped deliberately: `_folder_summary` previews the first prose line of every
    page in Projects/ and Areas/, so a bare substring search over the whole
    document conflates two different exposure paths. This asserts about the task
    list only — the folder preview is a separate surface, tested separately.
    """
    if "## Open tasks" not in out:
        return ""
    tail = out.split("## Open tasks", 1)[1]
    return tail.split("\n## ", 1)[0]


def test_no_sources_means_no_tasks_at_all(space):
    """The default must be silent, not 'everything except what we remembered'."""
    out = _ctx(space)
    assert "## Open tasks" not in out
    assert "SEKRIT-9931" not in out


def test_private_root_pages_never_leak_when_a_folder_is_allowed(space):
    """The exact 2026-09-01 finding: allow Projects, and nothing else appears."""
    out = _ctx(space, task_sources=["Projects"])
    tasks = _task_section(out)
    assert "polycarbonate" in tasks
    assert "SEKRIT-9931" not in out, "start code from a private page leaked into get_context"
    assert "rotate the exposed credential" not in out
    assert "journal task" not in tasks


def test_an_explicit_page_can_be_allowed(space):
    out = _ctx(space, task_sources=["Projects/Greenhouse.md"])
    assert "polycarbonate" in _task_section(out)
    assert "SEKRIT-9931" not in out


def test_sources_cannot_escape_the_space(space, tmp_path):
    """A config typo must not read outside the space (resolve_read, not a join)."""
    (tmp_path / "outside.md").write_text("- [ ] secret from outside the space\n")
    out = _ctx(space, task_sources=["../outside.md", "/etc"])
    assert "secret from outside" not in out
    assert "## Open tasks" not in out


def test_a_new_private_page_is_invisible_without_being_listed(space):
    """The property a denylist could not give: new pages default to hidden."""
    (space / "Bank Details.md").write_text("- [ ] account 12345678 sort 09-01-27\n")
    out = _ctx(space, task_sources=["Projects"])
    assert "12345678" not in out


def test_conventions_is_not_truncated_at_the_generic_page_cap(space):
    """CONVENTIONS.md was 4437 bytes against a 4000-char cap and lost its tail."""
    body = "# Conventions\n\n" + ("Rule about formatting. " * 400) + "\nFINAL-RULE-MARKER\n"
    assert len(body) > 4000
    (space / "CONVENTIONS.md").write_text(body)
    out = _ctx(space)
    assert "FINAL-RULE-MARKER" in out


def test_unresolved_nix_interpolation_is_not_reported_as_a_hostname(tmp_path):
    """`${authHost}.rosemaryacres.com` is not a host; it is a regex miss.

    _service_inventory greps Nix source rather than evaluating it, so anything
    but the one substitution it knows about stays literal. Reporting those as
    real vhosts is worse than omitting them: a chat repeats them as fact.
    """
    from homelab_mcp.context import _service_inventory

    modules = tmp_path / "modules" / "services"
    modules.mkdir(parents=True)
    (modules / "thing.nix").write_text(
        'services.nginx.virtualHosts."real.example.com" = {};\n'
        'services.nginx.virtualHosts."${domain}" = {};\n'
        'services.nginx.virtualHosts."${authHost}.rosemaryacres.com" = {};\n'
    )
    lines = "\n".join(_service_inventory(tmp_path))
    assert "real.example.com" in lines
    assert "rosemaryacres.com" in lines          # ${domain} IS substituted
    assert "${" not in lines, "an unresolved interpolation was reported as a vhost"
    assert "authHost" not in lines
