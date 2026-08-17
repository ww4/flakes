"""The failure half of talking to Open Library.

The audit found the success paths solid and the failure paths wrong in three
ways: a rate-limited cover fetch was recorded as a PERMANENT "no cover"; a
retry-after header in HTTP-date form crashed the whole batch run with
ValueError; and retries-exhausted was indistinguishable from "this record
does not exist". Each of those is pinned here, without touching the network.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


class _CannedClient:
    """Stands in for httpx.AsyncClient inside covers.py."""

    def __init__(self, response: httpx.Response):
        self._response = response

    def __call__(self, *a, **kw):  # covers.py instantiates it
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, *a, **kw):
        return self._response


def _fetch(settings, isbn):
    from stacks import covers

    return asyncio.run(covers.fetch(settings, isbn, "M"))


@pytest.fixture
def settings(tmp_path):
    from stacks.config import Settings

    return Settings(cover_cache_dir=str(tmp_path))


def _prime(monkeypatch, status, content=b""):
    from stacks import covers

    covers._MISSES.clear()
    covers._MISSES_LOADED = True
    monkeypatch.setattr(
        covers.httpx, "AsyncClient",
        _CannedClient(httpx.Response(status, content=content)),
    )


class TestCoverMissSemantics:
    def test_429_is_not_remembered(self, settings, monkeypatch):
        """A rate limit is a fact about the moment, not about the cover."""
        from stacks import covers

        _prime(monkeypatch, 429)
        assert _fetch(settings, "9780000000010") is None
        assert "M:9780000000010" not in covers._MISSES

    def test_500_is_not_remembered(self, settings, monkeypatch):
        from stacks import covers

        _prime(monkeypatch, 500)
        assert _fetch(settings, "9780000000011") is None
        assert "M:9780000000011" not in covers._MISSES

    def test_404_is_remembered(self, settings, monkeypatch):
        from stacks import covers

        _prime(monkeypatch, 404)
        assert _fetch(settings, "9780000000012") is None
        assert "M:9780000000012" in covers._MISSES

    def test_placeholder_200_is_remembered(self, settings, monkeypatch):
        from stacks import covers

        _prime(monkeypatch, 200, b"tiny")
        assert _fetch(settings, "9780000000013") is None
        assert "M:9780000000013" in covers._MISSES

    def test_a_real_cover_lands_whole(self, settings, monkeypatch):
        from stacks import covers

        blob = b"J" * 2048
        _prime(monkeypatch, 200, blob)
        path = _fetch(settings, "9780000000014")
        assert path is not None and path.read_bytes() == blob
        # atomic write leaves no torso behind
        assert not list(path.parent.glob("*.tmp"))


class _SequencedTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        status, headers, body = self._responses.pop(0)
        return httpx.Response(status, headers=headers, content=body)


def _ol_client(responses):
    from stacks.config import Settings
    from stacks.enrich.openlibrary import OpenLibraryClient

    settings = Settings(ol_min_interval_ms=0)
    transport = _SequencedTransport(responses)
    client = httpx.AsyncClient(transport=transport, base_url="https://ol.test")
    return OpenLibraryClient(settings, client=client), transport


class TestOpenLibraryFailureModes:
    def test_http_date_retry_after_backs_off_instead_of_crashing(self, monkeypatch):
        """float('Wed, 21 Oct 2026 07:28:00 GMT') was a ValueError mid-batch."""
        import stacks.enrich.openlibrary as ol_mod

        monkeypatch.setattr(ol_mod.asyncio, "sleep", _instant_sleep)
        ol, transport = _ol_client([
            (429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, b""),
            (200, {}, b'{"ok": true}'),
        ])
        result = asyncio.run(ol._get("/isbn/x.json"))
        assert result == {"ok": True}
        assert transport.calls == 2

    def test_retries_exhausted_is_an_outage_not_a_404(self, monkeypatch):
        import stacks.enrich.openlibrary as ol_mod
        from stacks.enrich.openlibrary import OpenLibraryUnavailable

        monkeypatch.setattr(ol_mod.asyncio, "sleep", _instant_sleep)
        ol, _ = _ol_client([(503, {}, b"")] * 3)
        with pytest.raises(OpenLibraryUnavailable):
            asyncio.run(ol._get("/isbn/x.json"))

    def test_a_genuine_404_is_still_none(self):
        ol, _ = _ol_client([(404, {}, b"")])
        assert asyncio.run(ol._get("/isbn/x.json")) is None


async def _instant_sleep(_seconds):
    return None
