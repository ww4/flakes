"""Volume-number vetoes on fuzzy title matching.

Regression: loading the flood document merged three distinct books into one.
"I can read it! Book 2" scores 0.82 trigram similarity against "Book 1" — well
above the merge threshold — and trigrams cannot see that the number is the
entire identity of the book. Books 1, 2 and 3 collapsed into a single work,
silently destroying two titles from the loss record.
"""

from stacks.normalize import numeric_conflict, numeric_tokens


class TestNumericTokens:
    def test_extracts_digit_runs(self):
        assert numeric_tokens("I can read it! Book 2") == {"2"}
        assert numeric_tokens("Sing Spell Read 1-17") == {"1", "17"}

    def test_none_when_no_digits(self):
        assert numeric_tokens("The Hobbit") == frozenset()
        assert numeric_tokens(None) == frozenset()


class TestNumericConflict:
    def test_different_volume_numbers_conflict(self):
        assert numeric_conflict("i can read it book 2", "i can read it book 1")
        assert numeric_conflict("i can read it book 3", "i can read it book 1")

    def test_same_volume_number_does_not_conflict(self):
        assert not numeric_conflict("i can read it book 1", "i can read it bk 1")

    def test_one_sided_numbers_do_not_conflict(self):
        """Edition packaging is not identity.

        "Snakes" matching "Snakes! (National Geographic Kids, Level 2)" is a
        GOOD merge — the 2 is a reading level on the packaging, not a different
        book. Requiring both sides to agree would reject it.
        """
        assert not numeric_conflict("national geographic kids snakes",
                                    "national geographic kids snakes level 2")
        assert not numeric_conflict("penguins", "penguins national geographic kids level 1")

    def test_no_numbers_anywhere(self):
        assert not numeric_conflict("the hobbit", "the hobbit")
        assert not numeric_conflict("george washington mother", "george washington")

    def test_empty_and_none_safe(self):
        assert not numeric_conflict(None, "book 1")
        assert not numeric_conflict("", "")
