"""Sale-document parsing: label cleanup, list extraction, continuations."""

from pathlib import Path

import pytest

from stacks.importers.sale_doc import (
    _clean_label,
    _join_continuations,
    _split_list,
    load_overrides,
    parse,
)
from stacks.models import WantKind

DOC = Path(__file__).resolve().parents[1] / "data" / "source" / "Books to look for at sale.docx"
OVERRIDES = Path(__file__).resolve().parents[1] / "data" / "want-overrides.toml"


class TestCleanLabel:
    def test_strips_trailing_sentence_words(self):
        # "Animal ark have:" — the label is the series, not the verb.
        assert _clean_label("Animal ark have") == "Animal ark"
        assert _clean_label("Janette oke missing") == "Janette oke"
        assert _clean_label("H. E. Marshall history books") == "H. E. Marshall history"

    def test_strips_trailing_parenthetical(self):
        assert _clean_label("Draw write now (have #1,2,8)") == "Draw write now"

    def test_truncates_at_unclosed_paren(self):
        # The head split can land mid-list, leaving an unbalanced "(".
        got = _clean_label("Nature's children or other animal books (have bighorn sheep")
        assert got == "Nature's children or other animal"


class TestJoinContinuations:
    def test_comma_leading_line_folds_into_previous(self):
        """The Love Comes Softly entry wraps across two paragraphs.

        Treating the second as its own rule invents a want literally named
        ", When Breaks the Dawn, When Hope Springs New".
        """
        got = _join_continuations(["Love comes softly series:, love finds a home",
                                   ", When Breaks the Dawn"])
        assert len(got) == 1
        assert "When Breaks the Dawn" in got[0]

    def test_normal_lines_are_untouched(self):
        assert _join_continuations(["DK books", "Usborne"]) == ["DK books", "Usborne"]


class TestSplitList:
    def test_separates_numbers_from_titles(self):
        labels, positions = _split_list("1,2,3,4,6,10")
        assert labels == []
        assert positions == [1, 2, 3, 4, 6, 10]

    def test_titles(self):
        labels, positions = _split_list("The Alamo, Ellis Island, Liberty Bell")
        assert labels == ["The Alamo", "Ellis Island", "Liberty Bell"]
        assert positions == []

    def test_hash_prefixed_numbers(self):
        _, positions = _split_list("#1,2,8")
        assert positions == [1, 2, 8]


@pytest.mark.skipif(not DOC.exists(), reason="source document not present")
class TestRealDocument:
    @pytest.fixture(scope="class")
    def wants(self):
        return {w.label: w for w in parse(DOC)}

    def test_have_lists_are_extracted(self, wants):
        assert len(wants["Cornerstones of freedom"].have) == 50
        assert len(wants["Hardy boys"].positions_have) == 24
        assert len(wants["Redwall"].have) == 13

    def test_missing_lists_are_extracted(self, wants):
        assert wants["Rangers apprentice"].positions_missing == [10]
        assert len(wants["Lori wick"].missing) == 5

    def test_suggested_titles_become_wants(self, wants):
        """"already have A, B (suggested titles: C, D)" — C and D are wanted."""
        w = wants["Wonders of Creation"]
        assert len(w.have) == 4
        assert len(w.missing) == 4

    def test_explicit_author_phrasing(self, wants):
        assert wants["Seymour Simon"].kind is WantKind.author

    def test_publishers_detected(self, wants):
        assert wants["DK"].kind is WantKind.publisher
        assert wants["Usborne"].kind is WantKind.publisher


@pytest.mark.skipif(not OVERRIDES.exists(), reason="overrides not present")
class TestOverrides:
    def test_overrides_load(self):
        ov = load_overrides(OVERRIDES)
        assert ov, "override file parsed to nothing"

    def test_every_override_targets_a_real_rule(self):
        """An override whose key matches nothing is dead weight.

        Normalisation is easy to get wrong by hand — "Nature's" folds to
        "nature s", not "natures" — and a stale key fails silently, leaving the
        rule misclassified with no warning.
        """
        if not DOC.exists():
            pytest.skip("source document not present")
        from stacks.importers.sale_doc import match_key_for

        parsed_keys = {match_key_for(w.label) for w in parse(DOC)}
        stale = [k for k in load_overrides(OVERRIDES) if k not in parsed_keys]
        assert not stale, f"override keys matching no rule: {stale}"
