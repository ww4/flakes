"""Write-path behaviour: no-overwrite, collision suffixing, byte-exact appends."""

from __future__ import annotations

from datetime import datetime

import pytest

from homelab_mcp.paths import PathRejected
from homelab_mcp.space import (
    append_note,
    read_note,
    render_note,
    save_note,
    search_notes,
    slugify,
)

INBOX = "Inbox"
NOW = datetime(2026, 7, 27, 10, 30)


@pytest.fixture()
def space(tmp_path):
    root = tmp_path / "space"
    (root / INBOX).mkdir(parents=True)
    (root / "Areas").mkdir()
    (root / "CONFIG.md").write_text("# CONFIG\nplainPages:\n")
    (root / "Areas" / "Homelab.md").write_text(
        "# Homelab\n\nThe gromit box runs mergerfs and SnapRAID. No ZFS.\n"
    )
    (root / ".git").mkdir()
    (root / ".git" / "COMMIT_EDITMSG.md").write_text("# should never be searched\n")
    return root


# --------------------------------------------------------------------------
# The $& corruption bug from the prior art — must round-trip byte for byte.
# --------------------------------------------------------------------------

DOLLAR_CORPUS = [
    "$&",
    "$1",
    "$`",
    "$'",
    "$$",
    "cost: $1 and $2 makes $$3",
    "regex replacement uses $& to mean the whole match",
    r"a backslash \1 and a dollar $1 walk into a bar",
    "$&$1$`$'$$",
]


@pytest.mark.parametrize("payload", DOLLAR_CORPUS)
def test_save_note_preserves_dollar_sequences(space, payload):
    saved = save_note(space, INBOX, "Dollar test", payload, now=NOW)
    text = read_note(space, saved.path)
    assert payload in text


@pytest.mark.parametrize("payload", DOLLAR_CORPUS)
def test_append_note_preserves_dollar_sequences(space, payload):
    saved = save_note(space, INBOX, "Append target", "original body", now=NOW)
    before = read_note(space, saved.path)
    append_note(space, INBOX, saved.path, payload)
    after = read_note(space, saved.path)

    # The original content is untouched and the payload survives verbatim.
    assert after.startswith(before.rstrip("\n"))
    assert payload in after
    assert "original body" in after


def test_append_is_byte_exact(space):
    """Concatenation only — the appended bytes appear unmodified."""
    saved = save_note(space, INBOX, "Exact", "first", now=NOW)
    payload = "second $& line\nthird $1 line"
    append_note(space, INBOX, saved.path, payload)
    raw = (space / saved.path).read_bytes()
    assert payload.encode("utf-8") in raw


# --------------------------------------------------------------------------
# Never overwrite.
# --------------------------------------------------------------------------

def test_save_note_never_overwrites_and_suffixes(space):
    first = save_note(space, INBOX, "Same title", "body one", now=NOW)
    second = save_note(space, INBOX, "Same title", "body two", now=NOW)
    third = save_note(space, INBOX, "Same title", "body three", now=NOW)

    assert first.path == "Inbox/2026-07-27-same-title.md"
    assert second.path == "Inbox/2026-07-27-same-title-2.md"
    assert third.path == "Inbox/2026-07-27-same-title-3.md"

    # The first note still holds its original body — nothing clobbered it.
    assert "body one" in read_note(space, first.path)
    assert "body two" in read_note(space, second.path)


def test_save_note_does_not_clobber_a_preexisting_file(space):
    (space / INBOX / "2026-07-27-taken.md").write_text("PRE-EXISTING\n")
    saved = save_note(space, INBOX, "Taken", "new body", now=NOW)
    assert saved.path == "Inbox/2026-07-27-taken-2.md"
    assert (space / INBOX / "2026-07-27-taken.md").read_text() == "PRE-EXISTING\n"


def test_append_refuses_outside_inbox(space):
    with pytest.raises(PathRejected):
        append_note(space, INBOX, "Areas/Homelab.md", "sneaky")
    assert "sneaky" not in (space / "Areas" / "Homelab.md").read_text()


def test_append_refuses_missing_note(space):
    with pytest.raises(FileNotFoundError):
        append_note(space, INBOX, "Inbox/nope.md", "body")


# --------------------------------------------------------------------------
# Rendering matches the space's house style.
# --------------------------------------------------------------------------

def test_render_has_no_yaml_frontmatter(space):
    out = render_note("A title", "some body", tags=["inbox"], now=NOW)
    assert not out.startswith("---")
    assert out.startswith("# A title")
    assert "*Captured from a Claude chat 2026-07-27." in out
    assert "#inbox" in out


def test_render_does_not_rewrap_body():
    body = "one long line that should not be wrapped " * 5
    out = render_note("T", body, now=NOW)
    assert body.rstrip() in out


def test_render_includes_source_url():
    out = render_note("T", "b", source_url="https://example.com/x", now=NOW)
    assert "https://example.com/x" in out


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Hello, World!", "hello-world"),
        ("  spaced  out  ", "spaced-out"),
        ("MikroTik RouterOS 7.x", "mikrotik-routeros-7-x"),
        ("!!!", "note"),
        ("café", "cafe"),
    ],
)
def test_slugify(title, expected):
    assert slugify(title) == expected


# --------------------------------------------------------------------------
# Search.
# --------------------------------------------------------------------------

def test_search_finds_content(space):
    hits = search_notes(space, "mergerfs")
    assert len(hits) == 1
    assert hits[0].path == "Areas/Homelab.md"
    assert hits[0].title == "Homelab"
    assert "mergerfs" in hits[0].excerpt


def test_search_requires_all_tokens(space):
    assert search_notes(space, "mergerfs snapraid")
    assert not search_notes(space, "mergerfs kubernetes")


def test_search_skips_git_internals(space):
    assert not search_notes(space, "should never be searched")


def test_search_matches_on_path(space):
    hits = search_notes(space, "Homelab")
    assert any(h.path == "Areas/Homelab.md" for h in hits)


def test_search_caps_limit(space):
    for i in range(10):
        save_note(space, INBOX, f"Note {i}", "shared-token body", now=NOW)
    assert len(search_notes(space, "shared-token", limit=3)) == 3
    assert len(search_notes(space, "shared-token", limit=9999)) <= 50


def test_search_rejects_empty_query(space):
    with pytest.raises(ValueError):
        search_notes(space, "   ")
