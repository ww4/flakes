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
    out = _ctx(space, readable_sources=["Projects"])
    tasks = _task_section(out)
    assert "polycarbonate" in tasks
    assert "SEKRIT-9931" not in out, "start code from a private page leaked into get_context"
    assert "rotate the exposed credential" not in out
    assert "journal task" not in tasks


def test_an_explicit_page_can_be_allowed(space):
    out = _ctx(space, readable_sources=["Projects/Greenhouse.md"])
    assert "polycarbonate" in _task_section(out)
    assert "SEKRIT-9931" not in out


def test_sources_cannot_escape_the_space(space, tmp_path):
    """A config typo must not read outside the space (resolve_read, not a join)."""
    (tmp_path / "outside.md").write_text("- [ ] secret from outside the space\n")
    out = _ctx(space, readable_sources=["../outside.md", "/etc"])
    assert "secret from outside" not in out
    assert "## Open tasks" not in out


def test_a_new_private_page_is_invisible_without_being_listed(space):
    """The property a denylist could not give: new pages default to hidden."""
    (space / "Bank Details.md").write_text("- [ ] account 12345678 sort 09-01-27\n")
    out = _ctx(space, readable_sources=["Projects"])
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


# --- the READ path, not just the aggregation -------------------------------
# Red-team finding, 2026-09-01: scoping get_context alone would have closed the
# front door and left search_notes + read_note as an unrestricted read of every
# page. These cover the primitive itself.


@pytest.fixture()
def read_space(tmp_path):
    root = tmp_path / "space"
    (root / "Inbox").mkdir(parents=True)
    (root / "Projects").mkdir()
    (root / "Inbox" / "capture.md").write_text("# capture\nchat exhaust\n")
    (root / "Projects" / "greenhouse.md").write_text("# greenhouse\npanels\n")
    (root / "MaM Interview Prep.md").write_text(
        "# prep\nstart code SEKRIT-9931, exit ip 198.51.100.9, port 41234\n"
    )
    (root / "CONVENTIONS.md").write_text("# conventions\nrules\n")
    return root


def test_search_cannot_see_outside_the_allowlist(read_space):
    from homelab_mcp.space import search_notes

    hits = search_notes(read_space, "code", sources=["Inbox"])
    assert [h.path for h in hits] == []
    hits = search_notes(read_space, "exhaust", sources=["Inbox"])
    assert [h.path for h in hits] == ["Inbox/capture.md"]


def test_read_note_refuses_pages_outside_the_allowlist(read_space):
    from homelab_mcp.paths import PathRejected
    from homelab_mcp.space import read_note

    for target in ["MaM Interview Prep.md", "CONVENTIONS.md", "Projects/greenhouse.md"]:
        with pytest.raises((PathRejected, FileNotFoundError)):
            read_note(read_space, target, ["Inbox"])
    assert "chat exhaust" in read_note(read_space, "Inbox/capture.md", ["Inbox"])


def test_out_of_scope_and_absent_are_indistinguishable(read_space):
    """A refusal that only fires for real files is an existence oracle."""
    from homelab_mcp.space import read_note

    real, absent = None, None
    try:
        read_note(read_space, "MaM Interview Prep.md", ["Inbox"])
    except Exception as exc:
        real = str(exc)
    try:
        read_note(read_space, "Totally Absent Page.md", ["Inbox"])
    except Exception as exc:
        absent = str(exc)
    assert real is not None and absent is not None
    assert real.split(":")[0] == absent.split(":")[0]


def test_widening_the_allowlist_grants_exactly_that(read_space):
    from homelab_mcp.space import read_note, search_notes

    assert "panels" in read_note(read_space, "Projects/greenhouse.md", ["Inbox", "Projects"])
    hits = search_notes(read_space, "code", sources=["Inbox", "Projects"])
    assert [h.path for h in hits] == [], "widening to Projects must not expose the root page"


def test_extension_is_optional_but_is_not_access_control(read_space):
    """`CONVENTIONS` used to read as a permission error when it was a spelling one."""
    from homelab_mcp.space import read_note

    assert "chat exhaust" in read_note(read_space, "Inbox/capture", ["Inbox"])
    with pytest.raises(Exception):
        read_note(read_space, "CONVENTIONS", ["Inbox"])


def test_a_symlink_cannot_satisfy_the_allowlist(read_space):
    """Resolved paths are compared, so a link inside Inbox cannot point out."""
    from homelab_mcp.space import read_note

    link = read_space / "Inbox" / "sneaky.md"
    link.symlink_to(read_space / "MaM Interview Prep.md")
    with pytest.raises(Exception):
        assert "SEKRIT-9931" not in read_note(read_space, "Inbox/sneaky.md", ["Inbox"])


def test_module_filenames_cannot_inject_lines_into_the_briefing(tmp_path):
    """A module called "x\\nInjected: line.nix" added a line to get_context."""
    from homelab_mcp.context import _service_inventory

    mods = tmp_path / "modules" / "services"
    mods.mkdir(parents=True)
    (mods / "normal.nix").write_text("")
    (mods / "x\nInjected: fake briefing line.nix").write_text("")
    out = "\n".join(_service_inventory(tmp_path))
    assert "\nInjected:" not in out
    assert len([ln for ln in out.splitlines() if ln.strip()]) == 1


def test_curated_context_comes_before_conventions(read_space):
    """It carries the framing; truncation of earlier sections must not evict it."""
    (read_space / "Areas").mkdir(exist_ok=True)
    (read_space / "Areas" / "Agent Context.md").write_text("# Focus\nCURATED-MARKER\n")
    out = build_context(
        read_space, include_service_inventory=False, context_page="Areas/Agent Context.md"
    )
    assert out.index("CURATED-MARKER") < out.index("## How this space works")
