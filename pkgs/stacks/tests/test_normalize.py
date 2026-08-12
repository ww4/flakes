from stacks.normalize import (
    is_valid_isbn10,
    is_valid_isbn13,
    isbn10_to_13,
    normalize_author,
    normalize_title,
    split_creators,
    to_isbn10,
    to_isbn13,
    year_from,
)


class TestIsbn10Compaction:
    """Regression: Open Library hands back formatted ISBN-10s.

    '0-553-11609-6' is 13 characters and overflowed the isbn10 column, aborting
    a live enrichment run mid-import.
    """

    def test_hyphenated_isbn10_is_compacted(self):
        assert to_isbn10("0-553-11609-6") == "0553116096"
        assert len(to_isbn10("0-553-11609-6")) == 10

    def test_spaces_stripped(self):
        assert to_isbn10(" 0441172717 ") == "0441172717"

    def test_invalid_returns_none(self):
        assert to_isbn10("not-an-isbn") is None
        assert to_isbn10("9780441172719") is None  # that's a 13
        assert to_isbn10(None) is None


class TestYearFrom:
    def test_plain_year(self):
        assert year_from("1975") == 1975

    def test_messy_open_library_dates(self):
        assert year_from("March 1975") == 1975
        assert year_from("1975-03-01") == 1975
        assert year_from("c1987") == 1987

    def test_no_year(self):
        assert year_from(None) is None
        assert year_from("undated") is None


class TestIsbn:
    def test_known_isbn10_to_13(self):
        # Dune, Ace 1990 paperback.
        assert isbn10_to_13("0441172717") == "9780441172719"

    def test_validity(self):
        assert is_valid_isbn10("0441172717")
        assert is_valid_isbn13("9780441172719")
        assert not is_valid_isbn10("0441172718")
        assert not is_valid_isbn13("9780441172718")

    def test_to_isbn13_accepts_both_forms(self):
        assert to_isbn13("0441172717") == "9780441172719"
        assert to_isbn13("9780441172719") == "9780441172719"

    def test_to_isbn13_strips_formatting(self):
        assert to_isbn13("978-0-441-17271-9") == "9780441172719"
        assert to_isbn13(" 0-441-17271-7 ") == "9780441172719"

    def test_isbn10_with_x_check_digit(self):
        assert is_valid_isbn10("043942089X")
        assert to_isbn13("043942089X") == isbn10_to_13("043942089X")

    def test_garbage_returns_none_not_raises(self):
        # Dirty catalog data must degrade to title matching, never abort.
        assert to_isbn13(None) is None
        assert to_isbn13("") is None
        assert to_isbn13("n/a") is None
        assert to_isbn13("12345") is None

    def test_mangled_check_digit_is_repaired(self):
        # Spreadsheets mangle the last digit; the 978 body is still good.
        assert to_isbn13("9780441172710") == "9780441172719"


class TestTitles:
    def test_leading_article_stripped(self):
        assert normalize_title("The Hobbit") == "hobbit"
        assert normalize_title("A Wizard of Earthsea") == "wizard of earthsea"

    def test_punctuation_and_case_folded(self):
        assert normalize_title("Slaughterhouse-Five!") == "slaughterhouse five"

    def test_accents_folded(self):
        assert normalize_title("Les Misérables") == "les miserables"

    def test_distinct_titles_stay_distinct(self):
        # Subtitles are NOT stripped — these are different books.
        assert normalize_title("Dune") != normalize_title("Dune Messiah")

    def test_empty(self):
        assert normalize_title(None) == ""


class TestAuthors:
    def test_name_order_does_not_matter(self):
        assert normalize_author("Ursula K. Le Guin") == normalize_author("Le Guin, Ursula K.")

    def test_initials_dropped_consistently(self):
        # Single characters are dropped, so "K." never distinguishes.
        assert normalize_author("Ursula Le Guin") == normalize_author("Ursula K. Le Guin")


class TestSplitCreators:
    def test_lastname_firstname_is_one_author(self):
        # The bug worth guarding: comma-splitting invents "Ursula K."
        assert split_creators("Le Guin, Ursula K.") == ["Le Guin, Ursula K."]

    def test_unambiguous_separators_split(self):
        assert split_creators("Gaiman, Neil; Pratchett, Terry") == [
            "Gaiman, Neil",
            "Pratchett, Terry",
        ]
        assert split_creators("Neil Gaiman & Terry Pratchett") == [
            "Neil Gaiman",
            "Terry Pratchett",
        ]

    def test_empty(self):
        assert split_creators(None) == []
        assert split_creators("") == []
