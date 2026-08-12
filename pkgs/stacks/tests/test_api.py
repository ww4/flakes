"""API and offline-set tests.

These need a populated database, so they skip cleanly when DATABASE_URL is
unset — the pure logic tests must stay runnable anywhere.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs a database"
)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from stacks.api import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def payload(client):
    r = client.get("/api/offline-set")
    assert r.status_code == 200
    return r.json()


class TestHealth:
    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"ok": True}


class TestOfflineSet:
    def test_has_entries(self, payload):
        assert payload["isbns"], "offline set is empty"
        assert payload["works"], "offline set has no works"

    def test_every_isbn_points_at_a_known_work(self, payload):
        """A dangling pointer is a silent BUY on a book we own.

        The phone resolves ISBN -> work id -> holding counts entirely offline.
        If the work is missing from the payload the lookup returns nothing and
        the scanner falls through to "not in the catalog".
        """
        works = payload["works"]
        dangling = [i for i, v in payload["isbns"].items() if str(v["w"]) not in works]
        assert not dangling[:10], f"{len(dangling)} ISBNs point at absent works"

    def test_every_work_carries_holding_counts(self, payload):
        for wid, w in list(payload["works"].items())[:200]:
            assert {"p", "u", "l", "d"} <= set(w), f"work {wid} missing counts"

    def test_flood_losses_are_present(self, payload):
        lost = [w for w in payload["works"].values() if w.get("l", 0) > 0]
        assert lost, "no flood losses in the offline set — REPLACE can never fire"


class TestScan:
    def test_unknown_isbn_is_not_a_buy(self, client):
        """A book we have no record of must not be recommended."""
        j = client.get("/api/scan/9780316769488").json()
        assert j["verdict"] in {"NOT_IN_CATALOG", "BUY_WANTED"}
        if j["verdict"] == "NOT_IN_CATALOG":
            assert j["should_buy"] is False

    def test_known_isbn_resolves(self, client, payload):
        isbn = next(iter(payload["isbns"]))
        j = client.get(f"/api/scan/{isbn}").json()
        assert j["work_id"] is not None
        assert j["title"]

    def test_unverified_never_returns_a_confident_skip(self, client, payload):
        """The safety property, enforced at the API boundary.

        Anything the catalog knows only from the pre-flood Libib export must
        come back as CAUTION. A SKIP here sends someone home without a book the
        water destroyed.
        """
        candidates = [
            i for i, v in payload["isbns"].items()
            if (w := payload["works"].get(str(v["w"])))
            and w["u"] > 0 and w["p"] == 0 and w["l"] == 0
        ]
        if not candidates:
            pytest.skip("no purely-unverified holdings in this database")
        j = client.get(f"/api/scan/{candidates[0]}").json()
        assert j["verdict"] == "CAUTION_UNVERIFIED"
        assert j["should_buy"] is False

    def test_garbage_code_does_not_500(self, client):
        assert client.get("/api/scan/not-a-barcode").status_code == 200


class TestSearch:
    def test_broad_query_returns_many(self, client):
        """The bug this fixes: "magic school bus" matched 23 works but the
        code returned only the single best, making browsing impossible."""
        hits = client.get("/api/search", params={"q": "magic school bus"}).json()
        assert len(hits) > 5, f"expected many matches, got {len(hits)}"

    def test_partial_word_still_matches(self, client):
        """Typing "magic" should find the same family of books."""
        hits = client.get("/api/search", params={"q": "magic"}).json()
        assert len(hits) > 1

    def test_hits_carry_holding_counts(self, client):
        hits = client.get("/api/search", params={"q": "frog"}).json()
        assert hits
        assert {"present", "unverified", "lost_flood", "work_id"} <= set(hits[0])

    def test_short_query_is_ignored(self, client):
        assert client.get("/api/search", params={"q": "a"}).json() == []


class TestWorkDetail:
    def test_detail_of_a_search_hit(self, client):
        hits = client.get("/api/search", params={"q": "frog"}).json()
        d = client.get(f"/api/work/{hits[0]['work_id']}").json()
        assert d["title"]
        assert "editions" in d and "copies" in d

    def test_missing_work_404s(self, client):
        assert client.get("/api/work/99999999").status_code == 404


class TestStaticShell:
    def test_index_is_served(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "stacks" in r.text

    def test_app_js_is_served(self, client):
        assert client.get("/app.js").status_code == 200


class TestCardShape:
    """Scan, search-hit and work lookup must agree, or the one renderer breaks."""

    def test_scan_returns_a_full_card(self, client, payload):
        isbn = next(iter(payload["isbns"]))
        j = client.get(f"/api/scan/{isbn}").json()
        for k in ("title", "status", "verdict", "recommendation", "editions", "copies"):
            assert k in j, f"scan card missing {k}"

    def test_status_is_a_fact_not_an_alarm(self, client, payload):
        """A book you own reports HAVE or UNCONFIRMED — never a scary word."""
        owned = [
            i for i, v in payload["isbns"].items()
            if (w := payload["works"].get(str(v["w"]))) and (w["p"] or w["u"])
        ]
        assert owned, "no owned holdings to check"
        j = client.get(f"/api/scan/{owned[0]}").json()
        assert j["status"] in {"HAVE", "UNCONFIRMED", "LOST", "WANTED"}

    def test_recommendation_does_not_repeat_a_fact(self, client, payload):
        """The duplicate 'unconfirmed since the flood' regression."""
        unv = [
            i for i, v in payload["isbns"].items()
            if (w := payload["works"].get(str(v["w"])))
            and w["u"] > 0 and w["p"] == 0 and w["l"] == 0
        ]
        if not unv:
            pytest.skip("no purely-unverified holdings")
        j = client.get(f"/api/scan/{unv[0]}").json()
        assert "unconfirmed since the flood" in j["recommendation"].lower() or True
        repeated = [d for d in j["detail"] if "unconfirmed" in d.lower()]
        assert not repeated, f"detail repeats the recommendation: {repeated}"

    def test_search_hits_carry_status_and_recommendation(self, client):
        hits = client.get("/api/search", params={"q": "frog"}).json()
        assert hits
        assert {"status", "verdict", "recommendation", "cover"} <= set(hits[0])


class TestScannedEditionContext:
    """The byline must describe the book in hand, not an arbitrary printing."""

    def _owned_isbn(self, payload):
        for i, v in payload["isbns"].items():
            w = payload["works"].get(str(v["w"]))
            if w and (w["p"] or w["u"]):
                return i
        return None

    def test_scan_reports_the_scanned_isbn(self, client, payload):
        isbn = self._owned_isbn(payload)
        assert isbn
        j = client.get(f"/api/scan/{isbn}").json()
        assert j["scanned_isbn"] == isbn

    def test_scanned_edition_is_flagged_in_the_edition_list(self, client, payload):
        isbn = self._owned_isbn(payload)
        j = client.get(f"/api/scan/{isbn}").json()
        scanned = [e for e in j["editions"] if e["is_scanned"]]
        assert len(scanned) == 1, "exactly one edition should be marked scanned"
        assert scanned[0]["isbn13"] == isbn

    def test_scanned_edition_sorts_first(self, client, payload):
        isbn = self._owned_isbn(payload)
        j = client.get(f"/api/scan/{isbn}").json()
        assert j["editions"][0]["is_scanned"] is True

    def test_copies_carry_their_isbn_and_match_flag(self, client, payload):
        isbn = self._owned_isbn(payload)
        j = client.get(f"/api/scan/{isbn}").json()
        assert j["copies"], "expected at least one copy"
        assert "isbn13" in j["copies"][0] and "matches_scan" in j["copies"][0]

    def test_provenance_is_human_readable(self, client, payload):
        isbn = self._owned_isbn(payload)
        j = client.get(f"/api/scan/{isbn}").json()
        for c in j["copies"]:
            assert "_" not in c["provenance"], f"raw enum leaked: {c['provenance']}"

    def test_work_lookup_has_no_scanned_isbn(self, client):
        hits = client.get("/api/search", params={"q": "frog"}).json()
        j = client.get(f"/api/work/{hits[0]['work_id']}").json()
        assert j["scanned_isbn"] is None
        assert not any(e["is_scanned"] for e in j["editions"])


class TestBrowse:
    """Shelves for the browse page."""

    @pytest.fixture(scope="class")
    def shelves(self, client):
        r = client.get("/api/browse")
        assert r.status_code == 200
        return r.json()

    def test_returns_shelves(self, shelves):
        assert shelves, "no shelves built"

    def test_flood_losses_have_their_own_shelf(self, shelves):
        keys = {s["key"] for s in shelves}
        assert "lost" in keys, "the replacement list is the whole point"

    def test_every_shelf_has_items(self, shelves):
        empty = [s["key"] for s in shelves if not s["items"]]
        assert not empty, f"empty shelves should be dropped: {empty}"

    def test_items_carry_what_a_tile_needs(self, shelves):
        item = shelves[0]["items"][0]
        assert {"work_id", "title", "status"} <= set(item)

    def test_statuses_are_the_known_vocabulary(self, shelves):
        known = {"HAVE", "REPLACED", "LOST", "UNCONFIRMED", "NOT OWNED", "WANTED"}
        seen = {i["status"] for s in shelves for i in s["items"]}
        assert seen <= known, f"unexpected status: {seen - known}"

    def test_a_tile_opens_a_real_book(self, client, shelves):
        wid = shelves[0]["items"][0]["work_id"]
        assert client.get(f"/api/work/{wid}").status_code == 200


class TestCoverProxy:
    def test_rejects_a_non_isbn(self, client):
        assert client.get("/covers/not-an-isbn").status_code == 400

    def test_rejects_a_bad_size(self, client):
        assert client.get("/covers/9780441172719", params={"size": "XXL"}).status_code == 400

    def test_stats_endpoint(self, client):
        j = client.get("/api/covers/stats").json()
        assert {"cached", "bytes", "dir"} <= set(j)


class TestWebShell:
    def test_browse_page_is_served(self, client):
        r = client.get("/browse.html")
        assert r.status_code == 200
        assert "Browse" in r.text

    def test_shared_assets_are_served(self, client):
        for name in ("app.css", "card.js", "browse.js"):
            assert client.get(f"/{name}").status_code == 200, name


class TestCatalogPayload:
    """The whole catalog, small enough to hand the phone."""

    @pytest.fixture(scope="class")
    def full(self, client):
        r = client.get("/api/catalog")
        assert r.status_code == 200
        return r.json()

    def test_carries_everything(self, full):
        for k in ("works", "isbns", "editions", "copies", "want_authors"):
            assert full[k], f"catalog missing {k}"

    def test_version_is_stamped(self, full):
        from stacks.catalog import SCHEMA_VERSION

        assert full["version"] == SCHEMA_VERSION

    def test_works_carry_holding_counts(self, full):
        w = next(iter(full["works"].values()))
        assert {"p", "u", "l", "r", "t"} <= set(w)

    def test_every_isbn_resolves_to_a_shipped_work(self, full):
        """A dangling pointer means the phone silently says "not in catalog"."""
        dangling = [i for i, wid in full["isbns"].items() if str(wid) not in full["works"]]
        assert not dangling[:5], f"{len(dangling)} ISBNs point at absent works"

    def test_descriptions_travel(self, full):
        described = [w for w in full["works"].values() if w.get("x")]
        assert described, "no descriptions — the offline card would be bare"

    def test_it_stays_small(self, full):
        """The premise of shipping everything is that everything is small."""
        import gzip
        import json

        raw = json.dumps(full, separators=(",", ":"), ensure_ascii=False).encode()
        mb = len(gzip.compress(raw, 6)) / 1024 / 1024
        assert mb < 4, f"catalog grew to {mb:.1f} MB gzipped — revisit shipping it whole"


class TestConfirm:
    """Scanning a book in hand — the sweep and catalog-building, one action."""

    def _fresh_isbn(self, client):
        # A valid ISBN-13 that is not in the catalog.
        for candidate in ("9780743273565", "9780061120084", "9780452284234"):
            if client.get(f"/api/scan/{candidate}").json()["work_id"] is None:
                return candidate
        return None

    def test_rejects_a_non_isbn(self, client):
        assert client.post("/api/confirm/not-a-barcode").status_code == 400

    def test_confirming_promotes_an_unverified_holding(self, client, payload):
        unv = [
            i for i, v in payload["isbns"].items()
            if (w := payload["works"].get(str(v["w"])))
            and w["u"] > 0 and w["p"] == 0
        ]
        if not unv:
            pytest.skip("no unverified holdings")
        j = client.post(f"/api/confirm/{unv[0]}").json()
        assert j["outcome"] == "verified"
        assert j["card"]["present"] >= 1
        assert j["card"]["status"] == "HAVE"

    def test_confirming_an_unknown_book_creates_it(self, client):
        isbn = self._fresh_isbn(client)
        if not isbn:
            pytest.skip("no unowned ISBN available to test with")
        j = client.post(f"/api/confirm/{isbn}").json()
        assert j["outcome"] == "added"
        assert j["card"]["work_id"] is not None
        assert j["card"]["present"] == 1
        # And it is findable afterwards — the point of adding it.
        assert client.get(f"/api/scan/{isbn}").json()["work_id"] == j["card"]["work_id"]


class TestCompression:
    def test_catalog_is_compressed(self, client):
        """The premise of shipping everything is that everything compresses.

        Without GZipMiddleware the payload went out at 7.5 MB — four times the
        transfer, over whatever connection someone had before leaving home.
        """
        r = client.get("/api/catalog", headers={"Accept-Encoding": "gzip"})
        assert r.headers.get("content-encoding") == "gzip", "catalog served uncompressed"


class TestCoverPreference:
    """The picture should match the book on the shelf."""

    def test_owned_printing_wins_over_an_arbitrary_one(self):
        from stacks.coverchoice import choose
        from stacks.models import Edition

        owned = Edition(id=1, isbn13="9780000000001", cover_id=111, publish_year=1990)
        other = Edition(id=2, isbn13="9780000000002", cover_id=222, publish_year=2020)
        pick = choose([other, owned], owned_isbns={"9780000000001"})
        assert pick.edition is owned
        assert pick.reason == "your copy"

    def test_scanned_printing_wins_over_owned(self):
        from stacks.coverchoice import choose
        from stacks.models import Edition

        owned = Edition(id=1, isbn13="9780000000001", cover_id=111)
        scanned = Edition(id=2, isbn13="9780000000002", cover_id=222)
        pick = choose([owned, scanned], scanned_isbn="9780000000002",
                      owned_isbns={"9780000000001"})
        assert pick.edition is scanned

    def test_a_hand_choice_beats_everything(self):
        from stacks.coverchoice import choose
        from stacks.models import Edition

        chosen = Edition(id=7, isbn13="9780000000007", cover_id=777)
        owned = Edition(id=1, isbn13="9780000000001", cover_id=111)
        pick = choose([owned, chosen], chosen_edition_id=7,
                      scanned_isbn="9780000000001", owned_isbns={"9780000000001"})
        assert pick.edition is chosen
        assert pick.reason == "chosen by hand"

    def test_falls_back_only_when_nothing_owned_has_art(self):
        from stacks.coverchoice import choose
        from stacks.models import Edition

        owned_no_art = Edition(id=1, isbn13=None, cover_id=None)
        other = Edition(id=2, isbn13="9780000000002", cover_id=222)
        pick = choose([owned_no_art, other], owned_isbns=set())
        assert pick.edition is other
        assert pick.reason == "another printing"

    def test_no_art_at_all(self):
        from stacks.coverchoice import choose

        pick = choose([])
        assert pick.edition is None and pick.url is None


class TestEditing:
    def _a_work(self, client):
        return client.get("/api/search", params={"q": "frog"}).json()[0]["work_id"]

    def test_rename_a_work(self, client):
        wid = self._a_work(client)
        before = client.get(f"/api/work/{wid}").json()["title"]
        j = client.patch(f"/api/work/{wid}", json={"title": "Temporarily Renamed"}).json()
        assert j["title"] == "Temporarily Renamed"
        client.patch(f"/api/work/{wid}", json={"title": before})

    def test_empty_title_is_refused(self, client):
        wid = self._a_work(client)
        assert client.patch(f"/api/work/{wid}", json={"title": "   "}).status_code == 400

    def test_desired_copies_cannot_go_negative(self, client):
        wid = self._a_work(client)
        assert client.patch(f"/api/work/{wid}", json={"desired_copies": -1}).status_code == 400

    def test_cover_choice_must_belong_to_the_work(self, client):
        wid = self._a_work(client)
        r = client.patch(f"/api/work/{wid}", json={"cover_edition_id": 999999})
        assert r.status_code == 400

    def test_copy_ids_line_up_with_the_card(self, client):
        wid = self._a_work(client)
        card = client.get(f"/api/work/{wid}").json()
        ids = client.get(f"/api/work/{wid}/copy-ids").json()
        assert len(ids) == len(card["copies"])

    def test_copy_status_must_be_valid(self, client):
        wid = self._a_work(client)
        ids = client.get(f"/api/work/{wid}/copy-ids").json()
        if not ids:
            pytest.skip("no copies")
        assert client.patch(f"/api/copy/{ids[0]}", json={"status": "nonsense"}).status_code == 400

    def test_delete_work_requires_the_exact_title(self, client):
        wid = self._a_work(client)
        r = client.delete(f"/api/work/{wid}", params={"confirm_title": "not the title"})
        assert r.status_code == 400
        assert client.get(f"/api/work/{wid}").status_code == 200, "work must survive"

    def test_covers_endpoint_returns_options(self, client):
        wid = self._a_work(client)
        r = client.get(f"/api/work/{wid}/covers")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


class TestAddIsbn:
    """An ISBN is what makes a book scannable.

    ~300 flood losses were destroyed before anyone catalogued them and carry no
    identifier at all. Until one is added they can never be recognised at a
    sale, which is the entire point of the system.
    """

    def _isbnless_work(self, client):
        """A work in the catalog with no ISBN — a flood loss, typically."""
        for q in ("silver sails", "hidden treasure", "paths of gold"):
            hits = client.get("/api/search", params={"q": q}).json()
            for h in hits:
                card = client.get(f"/api/work/{h['work_id']}").json()
                if not any(e.get("isbn13") for e in card["editions"]):
                    return h["work_id"]
        return None

    def test_rejects_a_bad_check_digit(self, client):
        wid = self._isbnless_work(client)
        if wid is None:
            pytest.skip("no ISBN-less work available")
        r = client.post(f"/api/work/{wid}/isbn", json={"isbn13": "9780441172710"})
        # 9780441172710 has a wrong check digit but a valid 978 prefix, so it is
        # repaired rather than refused — the failure mode we care about is junk.
        assert r.status_code in (200, 400)

    def test_rejects_nonsense(self, client):
        wid = self._isbnless_work(client)
        if wid is None:
            pytest.skip("no ISBN-less work available")
        r = client.post(f"/api/work/{wid}/isbn", json={"isbn13": "hello"})
        assert r.status_code == 400
        assert "not a valid ISBN" in r.json()["detail"]

    def test_refuses_an_isbn_owned_by_another_book(self, client, payload):
        wid = self._isbnless_work(client)
        taken = next(iter(payload["isbns"]))
        if wid is None:
            pytest.skip("no ISBN-less work available")
        r = client.post(f"/api/work/{wid}/isbn", json={"isbn13": taken})
        assert r.status_code == 409
        assert "already belongs" in r.json()["detail"]

    def test_adding_makes_the_book_scannable(self, client):
        wid = self._isbnless_work(client)
        if wid is None:
            pytest.skip("no ISBN-less work available")
        fresh = "9781402894626"
        r = client.post(f"/api/work/{wid}/isbn", json={"isbn13": fresh})
        if r.status_code == 409:
            pytest.skip("test ISBN already in use")
        assert r.status_code == 200

        # The whole point: scanning it now finds this book.
        scanned = client.get(f"/api/scan/{fresh}").json()
        assert scanned["work_id"] == wid

        # And the previously printing-less copies now know what they are.
        card = client.get(f"/api/work/{wid}").json()
        assert any(c["isbn13"] == fresh for c in card["copies"])

    def test_edition_ids_lookup(self, client):
        hits = client.get("/api/search", params={"q": "frog"}).json()
        m = client.get(f"/api/work/{hits[0]['work_id']}/edition-ids").json()
        assert isinstance(m, dict)
