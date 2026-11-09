"""Store behaviour, with the real shapes found in the space.

The fixtures below are trimmed copies of actual pages: a Keep-import note with
hand-written frontmatter, a sectioned checklist with no frontmatter and no h1,
and a plain bare-lines note (which is what most of the 556 Keep pages are).
"""

from pathlib import Path

import pytest

from shoebox import paths, store

KEEP_NOTE = """---
date: 2017-03-04
category: logistics
source: Keep.json
tags: keep-import
---

# Bring from Kentucky

*Google Keep, 2017-03-04 · archived*

Exercise mat
Monitor
Spices
"""

GROCERY = """## Walmart
- [ ] Bread

## Kroger

## Frankfort house
*(pantry/storage at dad's — grab on the next visit)*
- [ ] Diapers
"""


@pytest.fixture
def space(tmp_path):
    (tmp_path / "Keep").mkdir()
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Inbox" / "Log").mkdir(parents=True)
    (tmp_path / "Journal").mkdir()
    (tmp_path / "Keep" / "2017-03-04 Bring from Kentucky.md").write_text(KEEP_NOTE)
    (tmp_path / "Grocery List.md").write_text(GROCERY)
    (tmp_path / "Inbox" / "Log" / "2026-11.md").write_text("# log\nnot a card\n")
    (tmp_path / "Journal" / "2026-11-08.md").write_text("# day\n")
    return tmp_path


def test_reads_frontmatter_title_and_date(space):
    n = store.read(space, "Keep/2017-03-04 Bring from Kentucky.md")
    assert n.title == "Bring from Kentucky"
    assert n.date == "2017-03-04"
    assert n.cat == "logistics"
    assert n.pinned is False and n.archived is False
    assert n.body.splitlines()[-3:] == ["Exercise mat", "Monitor", "Spices"]
    assert not n.body.startswith("# ")


def test_title_falls_back_to_filename_without_the_date(space):
    (space / "Keep" / "2015-10-14 website file notes.md").write_text("a\nb\n")
    n = store.read(space, "Keep/2015-10-14 website file notes.md")
    assert n.title == "website file notes"
    assert n.had_h1 is False


def test_no_frontmatter_no_h1_survives_a_write(space):
    """Grocery List has neither; a pin must not grow an h1 or reorder it."""
    before = (space / "Grocery List.md").read_text()
    store.write(space, "Grocery List.md", pinned=True)
    after = (space / "Grocery List.md").read_text()
    assert after.startswith("---\npinned: true\n---\n")
    assert "## Walmart" in after and "## Frankfort house" in after
    assert "# Grocery List" not in after          # no invented heading
    assert before.strip() in after                # body untouched


def test_unknown_frontmatter_keys_are_preserved_verbatim(space):
    rel = "Keep/2017-03-04 Bring from Kentucky.md"
    store.write(space, rel, pinned=True)
    text = (space / rel).read_text()
    for line in ("date: 2017-03-04", "category: logistics",
                 "source: Keep.json", "tags: keep-import"):
        assert line in text, line
    assert "pinned: true" in text


def test_unpinning_drops_the_key_rather_than_writing_false(space):
    rel = "Keep/2017-03-04 Bring from Kentucky.md"
    store.write(space, rel, pinned=True)
    store.write(space, rel, pinned=False)
    text = (space / rel).read_text()
    assert "pinned" not in text
    assert "category: logistics" in text


def test_body_round_trips_without_drift(space):
    rel = "Keep/2017-03-04 Bring from Kentucky.md"
    first = store.read(space, rel)
    store.write(space, rel, body=first.body)
    second = store.read(space, rel)
    assert second.body == first.body
    assert second.title == first.title


def test_checkbox_edits_land_in_the_file(space):
    store.write(space, "Grocery List.md",
                body=GROCERY.replace("- [ ] Bread", "- [x] Bread"))
    assert "- [x] Bread" in (space / "Grocery List.md").read_text()


def test_retitling_a_note_without_an_h1_adds_one(space):
    rel = "Keep/2017-03-04 Bring from Kentucky.md"
    store.write(space, rel, title="Bring from KY")
    assert (space / rel).read_text().count("# Bring from KY") == 1


def test_create_lands_in_inbox_and_is_unique(space):
    a = store.create(space, "paint list", "blue pearl")
    b = store.create(space, "paint list", "white")
    assert a.id.startswith("Inbox/") and a.id.endswith(".md")
    assert a.id != b.id
    assert store.read(space, a.id).body == "blue pearl"


def test_create_slug_strips_path_characters(space):
    n = store.create(space, "../../etc/passwd", "x")
    assert paths.in_scope(n.id)
    assert ".." not in n.id


def test_trash_moves_and_restore_returns(space):
    rel = "Keep/2017-03-04 Bring from Kentucky.md"
    trashed = store.trash(space, rel)
    assert not (space / rel).exists()
    assert (space / trashed).exists()
    restored = store.restore(space, trashed)
    assert restored == rel
    assert (space / rel).exists()


def test_restore_refuses_to_clobber(space):
    rel = "Keep/2017-03-04 Bring from Kentucky.md"
    trashed = store.trash(space, rel)
    (space / rel).write_text("something else\n")
    with pytest.raises(paths.PathError):
        store.restore(space, trashed)


def test_listing_excludes_the_agents_pages(space):
    ids = {n.id for n in store.listing(space)}
    assert "Keep/2017-03-04 Bring from Kentucky.md" in ids
    assert "Grocery List.md" in ids
    assert not any(i.startswith("Journal/") for i in ids)
    assert not any(i.startswith("Inbox/Log") for i in ids)


def test_atomic_write_leaves_no_temp_files(space):
    store.write(space, "Grocery List.md", pinned=True)
    assert not list(space.glob(".shoebox-*"))
