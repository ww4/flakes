"""Cover images, stored locally.

Not a cache in the throwaway sense — these are kept. Once a cover is on
disk it is served from here forever and Open Library is never asked again,
which is both the polite thing to do and what makes browsing work offline.

Two routes in. By Open Library's internal cover id, which is unlimited and is
what we use whenever enrichment captured one; or by ISBN, which is capped at
100 requests per IP per five minutes and is therefore paced and treated as a
fallback.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from stacks.config import Settings

log = logging.getLogger(__name__)

#: Covers we asked for and Open Library does not have. Remembered so a missing
#: cover costs one request ever, rather than one per page view.
_MISSES: set[str] = set()

_locks: dict[str, asyncio.Lock] = {}
_global = asyncio.Semaphore(3)


class _Limiter:
    """Keep us under Open Library's cover rate limit.

    Their published ceiling is 100 requests per IP per five minutes for covers
    fetched *by identifier*, which is what an ISBN lookup is. That works out at
    one request per three seconds, so that is the pace — slow for a first
    browse, irrelevant afterwards because every fetch is cached forever.

    This path is the fallback. When an edition's cover_id is known — captured
    at ingest — :func:`fetch_by_id` is used instead and no limit applies.
    """

    def __init__(self, min_interval_s: float = 3.1) -> None:
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


_limiter = _Limiter()


def cache_dir(settings: Settings) -> Path:
    d = Path(settings.cover_cache_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


async def fetch_by_id(settings: Settings, cover_id: int, size: str = "M") -> Path | None:
    """Fetch a cover by Open Library's internal id.

    The preferred route. Requests by cover id are unlimited, while requests by
    ISBN are capped at 100 per IP per five minutes — so once an edition's
    cover_id is known at ingest, fetching art stops being a rationed activity.
    """
    path = stored_path(settings, f"id{cover_id}", size)
    if path.exists() and path.stat().st_size > 0:
        return path
    key = f"{size}:id{cover_id}"
    if key in _MISSES:
        return None

    url = f"{settings.ol_covers_base}/b/id/{cover_id}-{size}.jpg"
    try:
        async with _global:
            async with httpx.AsyncClient(
                headers={"User-Agent": settings.ol_user_agent},
                timeout=httpx.Timeout(15.0),
                follow_redirects=True,
            ) as client:
                r = await client.get(url)
    except httpx.HTTPError as exc:
        log.info("cover fetch failed for id %s: %s", cover_id, exc)
        return None

    if r.status_code != 200 or len(r.content) < 512:
        _MISSES.add(key)
        return None
    path.write_bytes(r.content)
    return path


def stored_path(settings: Settings, key: str, size: str = "M") -> Path:
    # Shard by the last two characters: 50k files in one directory is unkind to
    # both the filesystem and anyone trying to look at it.
    shard = key[-2:] if len(key) >= 2 else "00"
    d = cache_dir(settings) / size / shard
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.jpg"


def cached_path(settings: Settings, isbn13: str, size: str = "M") -> Path:
    return stored_path(settings, isbn13, size)


async def fetch(settings: Settings, isbn13: str, size: str = "M") -> Path | None:
    """Return a local path to the cover, fetching it once if needed.

    Returns None when Open Library has no cover for this ISBN — a normal
    outcome for the older children's books that make up much of this library.
    """
    path = cached_path(settings, isbn13, size)
    if path.exists() and path.stat().st_size > 0:
        return path
    if f"{size}:{isbn13}" in _MISSES:
        return None

    lock = _locks.setdefault(isbn13, asyncio.Lock())
    async with lock:
        if path.exists() and path.stat().st_size > 0:
            return path

        url = f"{settings.ol_covers_base}/b/isbn/{isbn13}-{size}.jpg"
        try:
            async with _global:
                await _limiter.wait()
                async with httpx.AsyncClient(
                    headers={"User-Agent": settings.ol_user_agent},
                    timeout=httpx.Timeout(15.0),
                    follow_redirects=True,
                ) as client:
                    r = await client.get(url, params={"default": "false"})
        except httpx.HTTPError as exc:
            log.info("cover fetch failed for %s: %s", isbn13, exc)
            return None

        # Open Library answers 404 for "no cover", and sometimes returns a
        # 1x1 placeholder instead — treat both as a miss.
        if r.status_code != 200 or len(r.content) < 512:
            _MISSES.add(f"{size}:{isbn13}")
            return None

        path.write_bytes(r.content)
        return path


def stats(settings: Settings) -> dict:
    root = cache_dir(settings)
    files = list(root.rglob("*.jpg"))
    return {
        "cached": len(files),
        "bytes": sum(f.stat().st_size for f in files),
        "known_missing": len(_MISSES),
        "dir": str(root),
    }


def stored_keys(settings: Settings, size: str = "M") -> set[str]:
    """Every cover we actually hold, by filename stem.

    Keys are either an ISBN-13 or ``id<cover_id>``, because art arrives by both
    routes. Anything asking "does this book have a picture?" must consult this
    rather than the cover_id column — most covers were fetched by ISBN before
    ids existed, and counting ids alone reports thousands of books as art-less
    while their jackets sit on disk.
    """
    root = cache_dir(settings) / size
    if not root.is_dir():
        return set()
    return {p.stem for p in root.rglob("*.jpg") if p.stat().st_size > 0}
