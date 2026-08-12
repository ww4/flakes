"""Open Library returns inconsistent record shapes; parsing must absorb that.

Regression: a full enrichment run died after 75 works on an ``authors`` entry
whose ``author`` was a bare key string rather than the usual nested object.
Open Library is crowd-edited, so shape variation is normal input, not a bug to
be surprised by — and a batch of thousands must never end on one odd record.
"""

from stacks.enrich.openlibrary import _author_key, _key_tail, _language_code, _text


class TestAuthorKey:
    def test_nested_object_the_common_shape(self):
        entry = {"author": {"key": "/authors/OL26320A"}, "type": {"key": "/type/author_role"}}
        assert _author_key(entry) == "OL26320A"

    def test_author_as_bare_string(self):
        """The shape that killed the run."""
        assert _author_key({"author": "/authors/OL26320A"}) == "OL26320A"

    def test_entry_itself_a_string(self):
        assert _author_key("/authors/OL26320A") == "OL26320A"

    def test_entry_is_the_key_object(self):
        assert _author_key({"key": "/authors/OL26320A"}) == "OL26320A"

    def test_junk_returns_none_rather_than_raising(self):
        assert _author_key(None) is None
        assert _author_key(42) is None
        assert _author_key({}) is None
        assert _author_key({"author": None}) is None


class TestKeyTail:
    def test_strips_path(self):
        assert _key_tail("/works/OL45804W") == "OL45804W"

    def test_tolerates_trailing_slash(self):
        assert _key_tail("/works/OL45804W/") == "OL45804W"

    def test_none(self):
        assert _key_tail(None) is None


class TestDescriptionText:
    def test_plain_string(self):
        assert _text("a desert planet") == "a desert planet"

    def test_typed_object(self):
        assert _text({"type": "/type/text", "value": "a desert planet"}) == "a desert planet"

    def test_list_takes_first(self):
        assert _text(["first", "second"]) == "first"

    def test_none(self):
        assert _text(None) is None


class TestLanguage:
    def test_extracts_code(self):
        assert _language_code([{"key": "/languages/eng"}]) == "eng"

    def test_absent(self):
        assert _language_code(None) is None
        assert _language_code([]) is None
        assert _language_code(["eng"]) is None
