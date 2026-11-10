"""Identity -> person -> display name, and the markdown table it lives in."""

from __future__ import annotations

import pytest

from notes import people
from notes.people import People, Row


TABLE = """# People

Some prose that must survive a rewrite.

| Identity | Person | Display name | First seen |
| --- | --- | --- | --- |
| chris | chris | Chris | 2026-07-09 |
| mary | mary | Mary Saenz | 2026-11-09 |
| github:marysaenz | mary |  | 2026-11-10 |
| google:mary.saenz@gmail.com | mary |  | 2026-11-10 |
| someone-new |  |  | 2026-11-10 |

A trailing note.
"""


# ------------------------------------------------------------- normalisation

@pytest.mark.parametrize("raw,want", [
    ("mary", "mary"),
    ("Mary", "mary"),
    ("mary@example.com", "mary@example.com"),       # the domain distinguishes sources
    ("github:marysaenz", "github:marysaenz"),
    ("CORP\\mjones", "corp-mjones"),
    ("  spaced  out ", "spaced-out"),
    ("", ""),
    (None, ""),
    ("x" * 300, "x" * 128),
])
def test_ident_keeps_what_tells_sources_apart(raw, want):
    assert people.ident(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("mary", "mary"),
    ("mary@example.com", "mary"),                   # a slug drops the domain
    ("Mary.Jones@corp.example", "mary-jones"),
    ("123", None),
    ("", None),
    ("@example.com", None),
])
def test_slug_is_narrow_because_it_lands_in_frontmatter(raw, want):
    assert people.slug(raw) == want


# -------------------------------------------------------------------- lookup

def test_aliases_resolve_to_one_person():
    p = people.parse(TABLE)
    for login in ("mary", "github:marysaenz", "google:mary.saenz@gmail.com", "GitHub:MarySaenz"):
        assert p.canonical(login) == "mary", login


def test_an_unclaimed_identity_is_not_someone_else():
    p = people.parse(TABLE)
    assert p.canonical("someone-new") == "someone-new"


def test_an_unknown_identity_still_works_with_no_table():
    """The app must behave exactly as before when nobody has set this up."""
    p = People()
    assert p.canonical("mary@example.com") == "mary"
    assert p.canonical("") is None


def test_display_names():
    p = people.parse(TABLE)
    assert p.display_for("mary") == "Mary Saenz"
    assert p.display_for("chris") == "Chris"
    assert p.display_for("nobody") == "nobody", "falls back to the slug, never blank"
    assert p.display_for("") == ""


def test_display_name_can_live_on_an_alias_row():
    p = People([Row("mary", "mary", "", "2026-11-09"),
                Row("github:m", "mary", "Mary S", "2026-11-09")])
    assert p.display_for("mary") == "Mary S"


def test_people_lists_each_person_once():
    assert people.parse(TABLE).people() == ["chris", "mary"]


# -------------------------------------------------------------------- change

def test_observe_records_something_new_once():
    p = People()
    assert p.observe("github:newcomer", "2026-11-10") is True
    assert p.observe("github:newcomer", "2026-11-10") is False
    assert p.rows[0].identity == "github:newcomer"
    assert p.rows[0].person == "", "a new identity is unclaimed until someone says otherwise"
    assert p.rows[0].first_seen == "2026-11-10"


@pytest.mark.parametrize("login,person", [
    ("mary", "mary"),                        # a plain name is itself
    ("mary@example.com", "mary"),            # an email already resolves to its local part
    ("Mary.Jones@corp.example", "mary-jones"),
])
def test_an_unprefixed_login_claims_itself(login, person):
    """The table should state what the app already does, not ask about it."""
    p = People()
    p.observe(login, "2026-11-10")
    assert p.rows[0].person == person
    assert p.canonical(login) == person


@pytest.mark.parametrize("login", ["github:marysaenz", "google:mary@gmail.com", "oidc:1234"])
def test_a_provider_prefixed_login_waits_to_be_claimed(login):
    """It could be anyone, so it goes to the Users page instead of guessing."""
    p = People()
    p.observe(login, "2026-11-10")
    assert p.rows[0].person == ""


def test_an_unclaimed_login_still_works_on_its_own():
    p = People()
    p.observe("github:marysaenz", "2026-11-10")
    assert p.canonical("github:marysaenz") == "github-marysaenz"


def test_unclaimed_rows_are_capped():
    p = People()
    for n in range(people.MAX_UNCLAIMED + 50):
        p.observe("github:user%d" % n, "2026-11-10")
    assert sum(1 for r in p.rows if not r.person) == people.MAX_UNCLAIMED


def test_assign_links_an_identity_and_sets_a_name():
    p = people.parse(TABLE)
    p.assign("someone-new", "mary")
    assert p.canonical("someone-new") == "mary"
    p.assign("mary", None, "Mary Alice Saenz")
    assert p.display_for("mary") == "Mary Alice Saenz"


def test_assign_can_unlink():
    p = people.parse(TABLE)
    p.assign("github:marysaenz", "")
    assert p.canonical("github:marysaenz") == "github-marysaenz"


def test_assign_creates_a_row_for_an_identity_never_seen():
    p = People()
    p.assign("google:x@y.com", "mary", "Mary")
    assert p.canonical("google:x@y.com") == "mary"


def test_a_display_name_is_bounded_and_single_line():
    p = People()
    p.assign("mary", "mary", "  Mary\n\nSaenz  " + "x" * 200)
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
    p.observe("mary", "2026-11-10")
    out = people.render(p)
    assert "# My own heading" in out
    assert "notes to self" in out
    assert "| Identity | Person | Display name | First seen |" in out
    assert people.parse(out).canonical("mary") == "mary"


def test_an_empty_file_gets_the_explanatory_default():
    out = people.render(people.parse(""))
    assert "Who is who" in out
    assert "| Identity |" in out


def test_parsing_tolerates_a_ragged_hand_edited_table():
    ragged = """| Identity | Person | Display name | First seen |
|---|---|---|---|
|mary|mary|Mary|
| github:m | mary |
|   |  |  |  |
| trailing | mary | Mary | 2026-01-01 | extra | cells |
"""
    p = people.parse(ragged)
    assert p.canonical("mary") == "mary"
    assert p.canonical("github:m") == "mary"
    assert p.canonical("trailing") == "mary"
    assert all(r.identity for r in p.rows), "a blank row is not an identity"


def test_a_pipe_in_a_display_name_cannot_break_the_table():
    p = People()
    p.assign("mary", "mary", "Mary | Saenz")
    again = people.parse(people.render(p))
    assert again.canonical("mary") == "mary"
    assert again.display_for("mary") == "Mary / Saenz", "the whole name survives"


def test_the_fullest_proxy_name_wins_when_several_offer_one():
    """Two directories, two spellings: "Mary Saenz" beats "Mary S"."""
    p = People([Row("github:m", "mary", "Mary S", "2026-11-09"),
                Row("mary@example.com", "mary", "Mary Saenz", "2026-11-09")])
    assert p.display_for("mary") == "Mary Saenz"


def test_a_name_set_on_the_person_itself_beats_any_proxy_guess():
    p = People([Row("mary", "mary", "Mop", "2026-11-09"),
                Row("mary@example.com", "mary", "Mary Saenz", "2026-11-09")])
    assert p.display_for("mary") == "Mop", "what someone typed wins outright"
