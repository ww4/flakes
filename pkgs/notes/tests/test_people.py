"""Identity -> person -> display name, and the markdown table it lives in."""

from __future__ import annotations

import pytest

from notes import people
from notes.people import People, Row


TABLE = """# People

Some prose that must survive a rewrite.

| Identity | Person | Display name | First seen |
| --- | --- | --- | --- |
| sam | sam | the owner | 2026-07-09 |
| ana | ana | Ana Parker | 2026-11-09 |
| github:aparker | ana |  | 2026-11-10 |
| google:a.parker@gmail.com | ana |  | 2026-11-10 |
| someone-new |  |  | 2026-11-10 |

A trailing note.
"""


# ------------------------------------------------------------- normalisation

@pytest.mark.parametrize("raw,want", [
    ("ana", "ana"),
    ("Ana", "ana"),
    ("ana@example.com", "ana@example.com"),       # the domain distinguishes sources
    ("github:aparker", "github:aparker"),
    ("CORP\\mjones", "corp-mjones"),
    ("  spaced  out ", "spaced-out"),
    ("", ""),
    (None, ""),
    ("x" * 300, "x" * 128),
])
def test_ident_keeps_what_tells_sources_apart(raw, want):
    assert people.ident(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("ana", "ana"),
    ("ana@example.com", "ana"),                   # a slug drops the domain
    ("Ana.Jones@corp.example", "ana-jones"),
    ("123", None),
    ("", None),
    ("@example.com", None),
])
def test_slug_is_narrow_because_it_lands_in_frontmatter(raw, want):
    assert people.slug(raw) == want


# -------------------------------------------------------------------- lookup

def test_aliases_resolve_to_one_person():
    p = people.parse(TABLE)
    for login in ("ana", "github:aparker", "google:a.parker@gmail.com", "GitHub:AParker"):
        assert p.canonical(login) == "ana", login


def test_an_unclaimed_identity_is_not_someone_else():
    p = people.parse(TABLE)
    assert p.canonical("someone-new") == "someone-new"


def test_an_unknown_identity_still_works_with_no_table():
    """The app must behave exactly as before when nobody has set this up."""
    p = People()
    assert p.canonical("ana@example.com") == "ana"
    assert p.canonical("") is None


def test_display_names():
    p = people.parse(TABLE)
    assert p.display_for("ana") == "Ana Parker"
    assert p.display_for("sam") == "the owner"
    assert p.display_for("nobody") == "nobody", "falls back to the slug, never blank"
    assert p.display_for("") == ""


def test_display_name_can_live_on_an_alias_row():
    p = People([Row("ana", "ana", "", "2026-11-09"),
                Row("github:m", "ana", "Ana S", "2026-11-09")])
    assert p.display_for("ana") == "Ana S"


def test_people_lists_each_person_once():
    assert people.parse(TABLE).people() == ["sam", "ana"]


# -------------------------------------------------------------------- change

def test_observe_records_something_new_once():
    p = People()
    assert p.observe("github:newcomer", "2026-11-10") is True
    assert p.observe("github:newcomer", "2026-11-10") is False
    assert p.rows[0].identity == "github:newcomer"
    assert p.rows[0].person == "", "a new identity is unclaimed until someone says otherwise"
    assert p.rows[0].first_seen == "2026-11-10"


@pytest.mark.parametrize("login,person", [
    ("ana", "ana"),                          # a name we already know
    ("ana@example.com", "ana"),              # ...and its email form
    ("ANA@CORP.EXAMPLE", "ana"),
])
def test_a_login_we_have_evidence_for_is_claimed_silently(login, person):
    """No question worth asking: the app would resolve it there anyway."""
    p = People()
    p.observe(login, "2026-11-10", known={"sam", "ana"})
    assert p.rows[0].person == person
    assert p.canonical(login) == person


@pytest.mark.parametrize("login", [
    "github:aparker",     # a provider id could be anyone
    "bob",                  # a name nobody has used here
    "newperson@example.com",
])
def test_a_login_we_cannot_place_waits_to_be_asked_about(login):
    p = People()
    p.observe(login, "2026-11-10", known={"sam", "ana"})
    assert p.rows[0].person == "", "the front end asks; it does not guess"


def test_with_no_evidence_at_all_nothing_is_claimed():
    p = People()
    p.observe("ana", "2026-11-10")
    assert p.rows[0].person == ""


def test_an_unclaimed_login_still_works_on_its_own():
    """Being unclaimed must never break anything — it only prompts a question."""
    p = People()
    p.observe("github:aparker", "2026-11-10", known={"sam"})
    assert p.canonical("github:aparker") == "github-aparker"
    p2 = People()
    p2.observe("bob", "2026-11-10", known={"sam"})
    assert p2.canonical("bob") == "bob"


def test_has_reports_whether_an_identity_is_on_file():
    p = People()
    assert p.has("ana") is False
    p.observe("ana", "2026-11-10", known={"ana"})
    assert p.has("ANA") is True, "the same identity in a different case"
    assert p.has("") is False


def test_unclaimed_rows_are_capped():
    p = People()
    for n in range(people.MAX_UNCLAIMED + 50):
        p.observe("github:user%d" % n, "2026-11-10")
    assert sum(1 for r in p.rows if not r.person) == people.MAX_UNCLAIMED


def test_assign_links_an_identity_and_sets_a_name():
    p = people.parse(TABLE)
    p.assign("someone-new", "ana")
    assert p.canonical("someone-new") == "ana"
    p.assign("ana", None, "Ana Alice Parker")
    assert p.display_for("ana") == "Ana Alice Parker"


def test_assign_can_unlink():
    p = people.parse(TABLE)
    p.assign("github:aparker", "")
    assert p.canonical("github:aparker") == "github-aparker"


def test_assign_creates_a_row_for_an_identity_never_seen():
    p = People()
    p.assign("google:x@y.com", "ana", "Ana")
    assert p.canonical("google:x@y.com") == "ana"


def test_a_display_name_is_bounded_and_single_line():
    p = People()
    p.assign("ana", "ana", "  Ana\n\nParker  " + "x" * 200)
    assert "\n" not in p.rows[0].display
    assert len(p.rows[0].display) <= 60


# ---------------------------------------------------------------- the file

def test_round_trip_keeps_the_prose():
    p = people.parse(TABLE)
    out = people.render(p)
    assert "Some prose that must survive a rewrite." in out
    assert "A trailing note." in out
    again = people.parse(out)
    # render() orders rows deterministically, so compare content not position,
    # then prove the ordering itself is stable.
    assert sorted((r.identity, r.person, r.display) for r in again.rows) == \
           sorted((r.identity, r.person, r.display) for r in p.rows)
    assert people.render(again) == out, "render -> parse -> render must be a fixed point"


def test_a_file_with_no_table_gets_one_and_keeps_its_text():
    p = people.parse("# My own heading\n\nnotes to self\n")
    p.observe("ana", "2026-11-10")
    out = people.render(p)
    assert "# My own heading" in out
    assert "notes to self" in out
    assert "| Identity | Person | Display name | First seen |" in out
    assert people.parse(out).canonical("ana") == "ana"


def test_an_empty_file_gets_the_explanatory_default():
    out = people.render(people.parse(""))
    assert "Who is who" in out
    assert "| Identity |" in out


def test_parsing_tolerates_a_ragged_hand_edited_table():
    ragged = """| Identity | Person | Display name | First seen |
|---|---|---|---|
|ana|ana|Ana|
| github:m | ana |
|   |  |  |  |
| trailing | ana | Ana | 2026-01-01 | extra | cells |
"""
    p = people.parse(ragged)
    assert p.canonical("ana") == "ana"
    assert p.canonical("github:m") == "ana"
    assert p.canonical("trailing") == "ana"
    assert all(r.identity for r in p.rows), "a blank row is not an identity"


def test_a_pipe_in_a_display_name_cannot_break_the_table():
    p = People()
    p.assign("ana", "ana", "Ana | Parker")
    again = people.parse(people.render(p))
    assert again.canonical("ana") == "ana"
    assert again.display_for("ana") == "Ana / Parker", "the whole name survives"


def test_the_fullest_proxy_name_wins_when_several_offer_one():
    """Two directories, two spellings: "Ana Parker" beats "Ana S"."""
    p = People([Row("github:m", "ana", "Ana S", "2026-11-09"),
                Row("ana@example.com", "ana", "Ana Parker", "2026-11-09")])
    assert p.display_for("ana") == "Ana Parker"


def test_a_name_set_on_the_person_itself_beats_any_proxy_guess():
    p = People([Row("ana", "ana", "Mop", "2026-11-09"),
                Row("ana@example.com", "ana", "Ana Parker", "2026-11-09")])
    assert p.display_for("ana") == "Mop", "what someone typed wins outright"
