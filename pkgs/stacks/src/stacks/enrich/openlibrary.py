"""Open Library client.

Two jobs:

1. **Resolve** an ISBN to a work ("what book is this?").
2. **Expand** a work to *every* edition of it ("what other ISBNs mean the same
   book?"). The expansion is the heart of the sale-day check: we precompute the
   union of every ISBN of every work we own, so a scan is a set membership test
   rather than a fuzzy search.

Open Library is a free non-profit service with no API key. In exchange they ask
for a descriptive User-Agent, cached results, and restraint. This client honours
all three: a shared rate limiter, a bounded concurrency semaphore, and callers
are expected to persist what they fetch rather than re-request it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from stacks.config import Settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class OLEdition:
    ol_edition_key: str | None
    isbn13: str | None
    isbn10: str | None
    publisher: str | None
    publish_date: str | None
    binding: str | None
    page_count: int | None
    language: str | None
    #: Open Library's internal cover id, captured here so cover images can be
    #: fetched by id (unlimited) rather than by ISBN (100 per IP per 5 min).
    cover_id: int | None = None


@dataclass(slots=True)
class OLWork:
    ol_work_key: str
    title: str
    subtitle: str | None = None
    description: str | None = None
    authors: list[str] = field(default_factory=list)
    ol_author_keys: list[str] = field(default_factory=list)
    editions: list[OLEdition] = field(default_factory=list)


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _text(value: Any) -> str | None:
    """OL descriptions are sometimes a bare string, sometimes {type, value}."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("value")
    if isinstance(value, list):
        return _text(value[0]) if value else None
    return str(value)


def _key_tail(key: str | None) -> str | None:
    """'/works/OL45804W' -> 'OL45804W'."""
    if not key:
        return None
    return key.rstrip("/").rsplit("/", 1)[-1]


def _author_key(entry: Any) -> str | None:
    """Pull an author key out of one ``authors`` entry.

    Open Library is not consistent here. The common shape is
    ``{"author": {"key": "/authors/OL123A"}, "type": {...}}``, but records also
    appear with ``"author"`` as a bare key string, and occasionally the entry
    itself is the key. A 3-hour enrichment run died on the second form after 75
    works, so all three are handled.
    """
    if isinstance(entry, str):
        return _key_tail(entry)
    if not isinstance(entry, dict):
        return None
    author = entry.get("author", entry)
    if isinstance(author, str):
        return _key_tail(author)
    if isinstance(author, dict):
        return _key_tail(author.get("key"))
    return None


def _first_cover(covers: Any) -> int | None:
    """First usable cover id from an edition record.

    Open Library uses -1 to mean "we know there is no cover", so a bare
    ``covers[0]`` would store a sentinel as if it were an id.
    """
    if not isinstance(covers, list):
        return None
    for c in covers:
        if isinstance(c, int) and c > 0:
            return c
    return None


def _language_code(languages: Any) -> str | None:
    """OL languages are [{'key': '/languages/eng'}]; we want 'eng'."""
    first = _first(languages)
    if isinstance(first, dict):
        return _key_tail(first.get("key"))
    return None


class RateLimiter:
    """Minimum interval between requests, shared across all tasks."""

    def __init__(self, min_interval_s: float) -> None:
        self._min = min_interval_s
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            delta = loop.time() - self._last
            if delta < self._min:
                await asyncio.sleep(self._min - delta)
            self._last = asyncio.get_running_loop().time()


class OpenLibraryClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.ol_base,
            headers={"User-Agent": settings.ol_user_agent},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(settings.ol_max_concurrency)
        self._limiter = RateLimiter(settings.ol_min_interval_ms / 1000.0)

    async def __aenter__(self) -> OpenLibraryClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any] | None:
        """GET with rate limiting and one retry on 429/5xx.

        Returns None for 404 — a missing record is a normal outcome here, not an
        error, and must be distinguishable from a failed lookup by the caller.
        """
        for attempt in range(3):
            async with self._sem:
                await self._limiter.wait()
                try:
                    r = await self._client.get(path, params=params or None)
                except httpx.HTTPError as e:
                    log.warning("OL request error %s: %s", path, e)
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue

            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("retry-after", 2 * (attempt + 1)))
                log.info("OL %s on %s — backing off %.1fs", r.status_code, path, wait)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                log.warning("OL returned non-JSON for %s", path)
                return None
        return None

    # -- resolution -------------------------------------------------------

    async def work_key_for_isbn(self, isbn13: str) -> str | None:
        """ISBN -> OL work key, via the edition record."""
        data = await self._get(f"/isbn/{isbn13}.json")
        if not data:
            return None
        works = data.get("works") or []
        if not works:
            return None
        return _key_tail(_first(works).get("key"))

    async def search_work(
        self, title: str, author: str | None = None, limit: int = 5
    ) -> list[dict]:
        """Title search, for books that have no ISBN to resolve through.

        This is the only route available for the flood losses: they were
        destroyed before anyone catalogued them, so there is no barcode to
        scan — just a hand-written title. Results are returned raw with their
        scores so the caller can decide how much to trust a match.
        """
        params = {
            "title": title,
            "limit": limit,
            "fields": "key,title,author_name,first_publish_year,edition_count,isbn",
        }
        if author:
            params["author"] = author
        data = await self._get("/search.json", **params)
        if not data:
            return []
        return data.get("docs") or []

    async def fetch_work(self, work_key: str) -> OLWork | None:
        data = await self._get(f"/works/{work_key}.json")
        if not data:
            return None
        author_keys = [k for k in map(_author_key, data.get("authors") or []) if k]
        names = await asyncio.gather(*(self.author_name(k) for k in author_keys))
        return OLWork(
            ol_work_key=work_key,
            title=data.get("title") or "",
            subtitle=data.get("subtitle"),
            description=_text(data.get("description")),
            authors=[n for n in names if n],
            ol_author_keys=author_keys,
        )

    async def author_name(self, author_key: str) -> str | None:
        data = await self._get(f"/authors/{author_key}.json")
        return (data or {}).get("name")

    # -- expansion --------------------------------------------------------

    async def editions_for_work(self, work_key: str, limit: int = 500) -> list[OLEdition]:
        """Every edition OL knows for a work.

        Paginates; ``limit`` caps total records so one pathological work (some
        classics have 1,000+ editions) cannot stall a whole import run.
        """
        out: list[OLEdition] = []
        offset = 0
        page = 100
        while len(out) < limit:
            data = await self._get(
                f"/works/{work_key}/editions.json", limit=page, offset=offset
            )
            if not data:
                break
            entries = data.get("entries") or []
            if not entries:
                break
            for e in entries:
                out.append(
                    OLEdition(
                        ol_edition_key=_key_tail(e.get("key")),
                        isbn13=_first(e.get("isbn_13")),
                        isbn10=_first(e.get("isbn_10")),
                        publisher=_first(e.get("publishers")),
                        publish_date=e.get("publish_date"),
                        binding=e.get("physical_format"),
                        page_count=e.get("number_of_pages"),
                        language=_language_code(e.get("languages")),
                        cover_id=_first_cover(e.get("covers")),
                    )
                )
            if len(entries) < page:
                break
            offset += page
        if len(out) >= limit:
            log.info("work %s capped at %d editions", work_key, limit)
        return out[:limit]

    @staticmethod
    def cover_url(isbn13: str, size: str = "M") -> str:
        """Cover by ISBN.

        Note: OL rate-limits cover lookups *by identifier* to 100 per IP per 5
        minutes (unlimited by internal cover ID). We only ever build these URLs
        for display; we do not fetch them server-side in bulk.
        """
        return f"https://covers.openlibrary.org/b/isbn/{isbn13}-{size}.jpg"
