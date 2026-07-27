"""Traversal corpus for the path-scoping function.

This is the security boundary of the whole server. If these tests pass, a
caller cannot read outside the space or write outside the inbox — including via
percent-encoding, normalisation tricks, or symlinks.
"""

from __future__ import annotations

import pytest

from homelab_mcp.paths import PathRejected, resolve_read, resolve_write

INBOX = "Inbox"


@pytest.fixture()
def space(tmp_path):
    """A miniature space: an inbox, a config page, a Library, and an outside secret."""
    (tmp_path / "space" / INBOX).mkdir(parents=True)
    (tmp_path / "space" / "Library").mkdir()
    (tmp_path / "space" / "Journal" / "Day").mkdir(parents=True)
    (tmp_path / "space" / "CONFIG.md").write_text("# config\n")
    (tmp_path / "space" / "Library" / "std.md").write_text("# lib\n")
    (tmp_path / "space" / "Journal" / "Day" / "2026-07-27.md").write_text("# day\n")
    (tmp_path / "space" / INBOX / "existing.md").write_text("# note\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.md").write_text("SECRET\n")
    return tmp_path / "space"


# --------------------------------------------------------------------------
# Writes: must land in the inbox, nothing else.
# --------------------------------------------------------------------------

TRAVERSAL_CORPUS = [
    "../outside/secret.md",
    "../../etc/passwd",
    "Inbox/../CONFIG.md",
    "Inbox/../../outside/secret.md",
    "Inbox/./../CONFIG.md",
    "./../CONFIG.md",
    "/etc/passwd",
    "/var/lib/silverbullet/CONFIG.md",
    "~/notes.md",
    "..%2Fsecret.md",
    "%2e%2e%2fsecret.md",
    "%2e%2e/secret.md",
    "Inbox%2f..%2fCONFIG.md",
    "%2E%2E%2Fsecret.md",
    "..\\secret.md",
    "Inbox\\..\\CONFIG.md",
    "..\\..\\Windows\\System32\\config",
    "Inbox/sub/nested.md",          # nesting is not allowed
    "Inbox/../Inbox/ok.md",         # traversal even though it lands back inside
    "note.md\x00.txt",
    "\x00note.md",
    "",
    "   ",
    ".",
    "..",
    "./",
    "../",
]


@pytest.mark.parametrize("candidate", TRAVERSAL_CORPUS)
def test_write_rejects_traversal(space, candidate):
    with pytest.raises(PathRejected):
        resolve_write(space, INBOX, candidate)


# The specific targets §6 of the task doc calls out by name.
@pytest.mark.parametrize(
    "candidate",
    [
        "CONFIG.md",
        "Library/std.md",
        "Library/../CONFIG.md",
        "Journal/Day/2026-07-27.md",
        "index.md",
        "CONVENTIONS.md",
        "SETTINGS.md",
    ],
)
def test_write_rejects_pages_outside_inbox(space, candidate):
    """A path that is a perfectly valid .md page is still refused if it is not in the inbox."""
    with pytest.raises(PathRejected):
        resolve_write(space, INBOX, candidate)


@pytest.mark.parametrize(
    "candidate",
    ["Inbox/notes.txt", "Inbox/notes", "Inbox/notes.md.txt", "Inbox/.md", "Inbox/.hidden.md"],
)
def test_write_rejects_non_markdown_and_hidden(space, candidate):
    with pytest.raises(PathRejected):
        resolve_write(space, INBOX, candidate)


@pytest.mark.parametrize("candidate", ["ok.md", "2026-07-27-an-idea.md", "CONFIG.md"])
def test_write_requires_explicit_inbox_prefix(space, candidate):
    """A bare filename is ambiguous — it could mean a space page or an inbox note.

    We refuse rather than pick a reading. `CONFIG.md` in particular must never
    resolve to `Inbox/CONFIG.md` by accident.
    """
    with pytest.raises(PathRejected):
        resolve_write(space, INBOX, candidate)


@pytest.mark.parametrize("candidate", ["Inbox/ok.md", "Inbox/2026-07-27-an-idea.md"])
def test_write_accepts_inbox_files(space, candidate):
    resolved = resolve_write(space, INBOX, candidate)
    assert resolved.parent == (space / INBOX).resolve()
    assert resolved.suffix == ".md"


def test_write_rejects_symlink_escape(space):
    """A symlink inside the inbox pointing outside must not be writable through."""
    (space / INBOX / "escape.md").symlink_to(space.parent / "outside" / "secret.md")
    with pytest.raises(PathRejected):
        resolve_write(space, INBOX, "escape.md")


def test_write_rejects_symlinked_inbox_subdir(space):
    """A symlinked directory inside the inbox cannot be used as a bridge."""
    (space / INBOX / "bridge").symlink_to(space.parent / "outside", target_is_directory=True)
    with pytest.raises(PathRejected):
        resolve_write(space, INBOX, "bridge/secret.md")


def test_write_rejects_long_path(space):
    with pytest.raises(PathRejected):
        resolve_write(space, INBOX, "a" * 500 + ".md")


# --------------------------------------------------------------------------
# Reads: space-wide, but still confined to the space.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "candidate",
    [
        "../outside/secret.md",
        "../../etc/passwd",
        "/etc/passwd",
        "%2e%2e%2foutside%2fsecret.md",
        "..\\outside\\secret.md",
        "Journal/../../outside/secret.md",
        "\x00",
        "",
    ],
)
def test_read_rejects_escape(space, candidate):
    with pytest.raises(PathRejected):
        resolve_read(space, candidate)


@pytest.mark.parametrize(
    "candidate",
    ["CONFIG.md", "Library/std.md", "Journal/Day/2026-07-27.md", "Inbox/existing.md"],
)
def test_read_accepts_anything_inside_the_space(space, candidate):
    resolved = resolve_read(space, candidate)
    assert resolved.exists()


def test_read_rejects_symlink_escape(space):
    (space / "leak.md").symlink_to(space.parent / "outside" / "secret.md")
    with pytest.raises(PathRejected):
        resolve_read(space, "leak.md")


def test_rejection_message_does_not_echo_the_path(space):
    """Rejection messages must not reflect attacker-controlled input back into logs/context."""
    nasty = "../../etc/passwd"
    with pytest.raises(PathRejected) as excinfo:
        resolve_read(space, nasty)
    assert nasty not in str(excinfo.value)
