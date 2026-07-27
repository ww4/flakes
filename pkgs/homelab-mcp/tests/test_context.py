"""get_context composition — and the guarantee that it reads nothing outside the space."""

from __future__ import annotations

import pytest

from homelab_mcp.context import build_context

CURATED = """# Agent Context

What's in flight right now: the connector build, phases B and C.
Blocked on: nothing.
"""


@pytest.fixture()
def space(tmp_path):
    root = tmp_path / "space"
    (root / "Areas").mkdir(parents=True)
    (root / "Projects").mkdir()
    (root / ".git").mkdir()
    (root / "CONVENTIONS.md").write_text("# Conventions\n\nNo hard-wrapping.\n")
    (root / "index.md").write_text("# Daybook\n\nThe shared space.\n")
    (root / "Areas" / "Agent Context.md").write_text(CURATED)
    (root / "Areas" / "Homelab.md").write_text(
        "# Homelab\n\nmergerfs, no ZFS.\n\n- [ ] buy a parity drive\n"
    )
    (root / "Projects" / "Lock3.md").write_text("# Lock3\n\nZola rebuild.\n")
    (root / ".git" / "notes.md").write_text("- [ ] should never appear\n")

    # A file OUTSIDE the space, to prove context never reaches it.
    (tmp_path / "secret.md").write_text("# Secret\n\nADMIN_TOKEN is plaintext\n")
    return root


def test_includes_the_curated_page(space):
    out = build_context(space, context_page="Areas/Agent Context.md")
    assert "Current focus (curated)" in out
    assert "the connector build" in out


def test_omits_curated_section_when_page_absent(space):
    out = build_context(space, context_page="Areas/Nonexistent.md")
    assert "Current focus (curated)" not in out
    # The rest of the briefing still builds.
    assert "Conventions" in out


def test_includes_conventions_projects_areas_and_tasks(space):
    out = build_context(space, context_page=None)
    assert "No hard-wrapping" in out
    assert "Areas/Homelab.md" in out
    assert "Projects/Lock3.md" in out
    assert "buy a parity drive" in out


def test_skips_git_internals(space):
    out = build_context(space, context_page=None)
    assert "should never appear" not in out


def test_never_escapes_the_space(space):
    """A context_page pointing outside the space must not leak it.

    `_read` joins against the space root; a traversal attempt lands on a
    non-existent path rather than the sibling file.
    """
    out = build_context(space, context_page="../secret.md")
    assert "ADMIN_TOKEN" not in out
    assert "Current focus (curated)" not in out


def test_service_inventory_is_opt_in(space, tmp_path):
    flake = tmp_path / "flake" / "modules" / "services"
    flake.mkdir(parents=True)
    (flake / "jellyfin.nix").write_text('services.nginx.virtualHosts."media.${domain}" = {};\n')

    off = build_context(space, flake_root=tmp_path / "flake", include_service_inventory=False)
    assert "Deployed services" not in off

    on = build_context(space, flake_root=tmp_path / "flake", include_service_inventory=True)
    assert "Deployed services" in on
    assert "jellyfin" in on
    assert "media.rosemaryacres.com" in on


def test_no_code_path_to_the_agent_board_remains():
    """There must be no functional route to the open-loops board.

    Removed rather than left behind a disabled flag: section filtering could
    not make it safe (the board's *open* sections are the hazard, not just the
    archive), and a dormant flag invites someone to switch it back on believing
    it was safe. Prose explaining *why* it was removed is fine and expected —
    this asserts on the interface, not on the comments.
    """
    import inspect

    import homelab_mcp.context as ctx
    from homelab_mcp.config import Settings

    # No board-related settings remain.
    assert "include_agent_board" not in Settings.model_fields
    assert "agent_board_full" not in Settings.model_fields

    # No board parameter on the context builder, and no filter helper.
    params = inspect.signature(ctx.build_context).parameters
    assert "agent_board" not in params
    assert "agent_board_full" not in params
    assert not hasattr(ctx, "filter_agent_board")
    assert not hasattr(ctx, "BOARD_ARCHIVE_MARKERS")

    # Nothing anywhere points at the agent's memory directory.
    for module_file in (ctx.__file__, Settings.__module__ and __import__(
        "homelab_mcp.server", fromlist=["x"]
    ).__file__):
        assert "/.claude/projects/" not in open(module_file).read()
