"""ISBN and title normalisation.

Everything in the catalog is keyed on **ISBN-13**. Libib exports both ISBN-10
and ISBN-13 columns, Open Library records carry either, and the barcode on a
book printed before 2007 is an ISBN-10 (or a 10-digit code embedded in an
EAN-13 with a 978 prefix). Folding all of that to one canonical form up front is
what makes the sale-day lookup a single set membership test.
"""

from __future__ import annotations

import re
import unicodedata

_LEADING_ARTICLES = ("the ", "a ", "an ")
_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")


def _digits(raw: str) -> str:
    """Keep digits and a trailing X (ISBN-10 check character)."""
    return re.sub(r"[^0-9Xx]", "", raw or "").upper()


def isbn10_check_digit(body9: str) -> str:
    total = sum((10 - i) * int(c) for i, c in enumerate(body9))
    rem = (11 - (total % 11)) % 11
    return "X" if rem == 10 else str(rem)


def isbn13_check_digit(body12: str) -> str:
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(body12))
    return str((10 - (total % 10)) % 10)


def is_valid_isbn10(raw: str) -> bool:
    s = _digits(raw)
    if len(s) != 10 or not s[:9].isdigit():
        return False
    return isbn10_check_digit(s[:9]) == s[9]


def is_valid_isbn13(raw: str) -> bool:
    s = _digits(raw)
    if len(s) != 13 or not s.isdigit():
        return False
    return isbn13_check_digit(s[:12]) == s[12]


def isbn10_to_13(raw: str) -> str | None:
    s = _digits(raw)
    if len(s) != 10:
        return None
    body = "978" + s[:9]
    return body + isbn13_check_digit(body)


def to_isbn13(raw: str | None) -> str | None:
    """Canonicalise any ISBN-ish string to a valid ISBN-13, or None.

    Returns None rather than raising: catalog data is dirty by nature, and a bad
    identifier should degrade to "match by title" rather than abort an import.
    """
    if not raw:
        return None
    s = _digits(raw)
    if len(s) == 13 and is_valid_isbn13(s):
        return s
    if len(s) == 10 and is_valid_isbn10(s):
        return isbn10_to_13(s)
    # Some exports carry a 13-digit EAN whose check digit was mangled in a
    # spreadsheet. If the 978/979 prefix is right, trust the body and rebuild.
    if len(s) == 13 and s.isdigit() and s.startswith(("978", "979")):
        return s[:12] + isbn13_check_digit(s[:12])
    return None


def to_isbn10(raw: str | None) -> str | None:
    """Canonicalise to a compact 10-character ISBN-10, or None.

    Sources hand these over formatted ("0-553-11609-6"), which is 13 characters
    and does not fit an isbn10 column. Always fold before storing.
    """
    if not raw:
        return None
    s = _digits(raw)
    if len(s) == 10 and is_valid_isbn10(s):
        return s
    return None


# Digit lookaround, not \b: "c1987" has no word boundary between 'c' and '1',
# and \d{4} inside a longer run (an ISBN, say) must not match either.
_YEAR = re.compile(r"(?<!\d)(1[4-9]\d{2}|20\d{2}|2100)(?!\d)")


def year_from(raw: str | None) -> int | None:
    """Pull a plausible publication year out of a free-text date.

    Open Library publish_date is unconstrained: "1975", "March 1975",
    "1975-03-01", "c1975". A year is the only part worth indexing.
    """
    if not raw:
        return None
    m = _YEAR.search(str(raw))
    return int(m.group(1)) if m else None


def normalize_title(title: str | None) -> str:
    """Fold a title for fuzzy comparison.

    Lowercase, strip a leading article, drop punctuation, collapse whitespace.
    Deliberately *not* stripping subtitles — "Dune" and "Dune Messiah" must stay
    distinct, and trigram similarity handles the rest.
    """
    if not title:
        return ""
    s = unicodedata.normalize("NFKD", title).lower().strip()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    for art in _LEADING_ARTICLES:
        if s.startswith(art):
            s = s[len(art):]
            break
    return s


_NUM = re.compile(r"\d+")


def numeric_tokens(title: str | None) -> frozenset[str]:
    """Digit runs in a title — usually a volume, book or level number."""
    return frozenset(_NUM.findall(title or ""))


def numeric_conflict(a: str | None, b: str | None) -> bool:
    """True when two titles carry *different* volume numbers.

    Trigram similarity cannot see the difference between "I can read it! Book 1"
    and "I can read it! Book 2" — they score 0.82, high enough to merge, and
    merging them silently destroys a distinct book. Any title-similarity match
    must be vetoed when both sides carry numbers that disagree.

    Only a conflict when *both* sides have numbers. "Snakes" matching
    "Snakes! (Level 2)" is a good merge — the number is edition packaging, not
    an identity, and requiring both sides to agree would reject it.
    """
    na, nb = numeric_tokens(a), numeric_tokens(b)
    if not na or not nb:
        return False
    return na != nb


#: Descriptors the flood document appends to categorise a book, which are not
#: part of its title. "The big balloon race reader" is really "The Big Balloon
#: Race"; leaving the suffix on makes an exact-match search fail.
_TRAILING_DESCRIPTORS = re.compile(
    r"\s+(readers?|mini\s+book|board\s+book|picture\s+book)\s*$", re.I
)


def title_variants(title: str) -> list[str]:
    """Progressively looser forms of a title, best first.

    Used when searching an external catalog: try what was written, then strip
    the hand-added descriptor, then drop a parenthetical. Returned in order so
    a caller can stop at the first hit and keep the strongest match.
    """
    out: list[str] = []
    t = (title or "").strip()
    if not t:
        return out
    out.append(t)

    stripped = _TRAILING_DESCRIPTORS.sub("", t).strip()
    if stripped and stripped != t:
        out.append(stripped)

    no_parens = re.sub(r"\s*\([^)]*\)", "", stripped or t).strip()
    if no_parens and no_parens not in out:
        out.append(no_parens)

    # Drop a trailing "by <author>" — the document sometimes inlines it.
    no_by = re.sub(r"\s+by\s+.*$", "", no_parens or t, flags=re.I).strip()
    if no_by and no_by not in out and len(no_by) > 3:
        out.append(no_by)

    return out


def normalize_author(name: str | None) -> str:
    """Fold an author name to 'lastname firstname' word-set form.

    Libib gives "Ursula K. Le Guin"; Open Library may give "Le Guin, Ursula K.".
    Sorting the tokens makes both land in the same place.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT.sub(" ", s)
    tokens = [t for t in _WS.split(s) if t and len(t) > 1]
    return " ".join(sorted(tokens))


def split_creators(raw: str | None) -> list[str]:
    """Split Libib's packed ``creators`` field into individual names.

    Deliberately does NOT split on comma. "Le Guin, Ursula K." is one author in
    Lastname, Firstname form, and comma is also used to separate co-authors —
    the two cases are genuinely ambiguous from the string alone, and splitting
    would silently invent an author named "Ursula K." Only unambiguous
    separators are honoured; refine once we have seen the real export.
    """
    if not raw:
        return []
    parts = re.split(r";| and | & |\|", raw)
    return [p.strip() for p in parts if p and p.strip()]
