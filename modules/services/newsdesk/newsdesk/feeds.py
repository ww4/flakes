"""Fetching and parsing RSS/Atom, with no third-party dependencies.

Everything here is stdlib on purpose. The archive updaters next door made the
same choice and it has aged well: a feed reader that pulls in half of PyPI is a
feed reader that breaks on a nixpkgs bump, and this one has to run unattended
for months.

Conditional requests (ETag / If-Modified-Since) are not an optimisation here so
much as manners — 86 feeds polled four times a day is 344 requests, and most of
them should be a 304. See the polite-polling house rule.
"""
from __future__ import annotations

import gzip
import re
import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (X11; Linux x86_64) newsdesk/1.0 "
      "(+https://digest.rosemaryacres.com/news/)")

ATOM = "http://www.w3.org/2005/Atom"
DC = "http://purl.org/dc/elements/1.1/"
CONTENT = "http://purl.org/rss/1.0/modules/content/"


class NotModified(Exception):
    """The server said 304. Nothing to do, and NOT a failure."""


@dataclass
class Entry:
    guid: str
    url: str
    title: str
    summary: str = ""
    body: str = ""
    published: datetime | None = None


@dataclass
class FeedResult:
    entries: list[Entry] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None


def _open(url: str, headers: dict, insecure: bool, timeout: int):
    ctx = ssl._create_unverified_context() if insecure else None
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def fetch_raw(url: str, *, etag: str | None = None, last_modified: str | None = None,
              insecure: bool = False, timeout: int = 30) -> tuple[bytes, str | None, str | None]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        with _open(url, headers, insecure, timeout) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body, r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            raise NotModified from None
        raise


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # Atom/ISO variants, including the trailing-Z form strptime cannot take
    # with %z on every Python.
    iso = s.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(iso)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class _Strip(HTMLParser):
    """Minimal HTML -> text. Feed summaries are full of markup and the scorer
    should not be counting the word 'div'."""

    SKIP = {"script", "style", "head", "nav", "footer", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "br", "li", "div", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def strip_html(s: str) -> str:
    if not s:
        return ""
    if "<" not in s:
        return s.strip()
    p = _Strip()
    try:
        p.feed(s)
        p.close()
    except Exception:
        # A malformed fragment must not lose the item; fall back to a blunt
        # tag strip rather than dropping the text entirely.
        return re.sub(r"<[^>]+>", " ", s).strip()
    return p.text()


def _txt(el) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def parse_feed(body: bytes) -> list[Entry]:
    """Parse RSS 2.0 or Atom. Raises on anything that is not a feed."""
    root = ET.fromstring(body)
    entries: list[Entry] = []

    if root.tag == f"{{{ATOM}}}feed":
        for e in root.findall(f"{{{ATOM}}}entry"):
            link = ""
            for ln in e.findall(f"{{{ATOM}}}link"):
                rel = ln.get("rel", "alternate")
                if rel == "alternate" and ln.get("href"):
                    link = ln.get("href", "")
                    break
            if not link:
                ln = e.find(f"{{{ATOM}}}link")
                link = ln.get("href", "") if ln is not None else ""
            summary = _txt(e.find(f"{{{ATOM}}}summary"))
            content = _txt(e.find(f"{{{ATOM}}}content"))
            guid = _txt(e.find(f"{{{ATOM}}}id")) or link
            pub = (_txt(e.find(f"{{{ATOM}}}published"))
                   or _txt(e.find(f"{{{ATOM}}}updated")))
            entries.append(Entry(
                guid=guid, url=link,
                title=strip_html(_txt(e.find(f"{{{ATOM}}}title"))),
                summary=strip_html(summary or content)[:4000],
                body=strip_html(content)[:20000],
                published=parse_date(pub),
            ))
        return entries

    channel = root.find("channel")
    if channel is None:
        raise ValueError("not an RSS or Atom feed")
    for e in channel.findall("item"):
        link = _txt(e.find("link"))
        guid = _txt(e.find("guid")) or link
        desc = _txt(e.find("description"))
        content = _txt(e.find(f"{{{CONTENT}}}encoded"))
        pub = _txt(e.find("pubDate")) or _txt(e.find(f"{{{DC}}}date"))
        entries.append(Entry(
            guid=guid, url=link,
            title=strip_html(_txt(e.find("title"))),
            summary=strip_html(desc)[:4000],
            body=strip_html(content or desc)[:20000],
            published=parse_date(pub),
        ))
    return entries


def fetch_feed(url: str, *, etag=None, last_modified=None, insecure=False,
               timeout=30) -> FeedResult:
    body, new_etag, new_lm = fetch_raw(
        url, etag=etag, last_modified=last_modified, insecure=insecure,
        timeout=timeout)
    return FeedResult(entries=parse_feed(body), etag=new_etag, last_modified=new_lm)


def fetch_article_text(url: str, *, insecure: bool = False, timeout: int = 20,
                       limit: int = 20000) -> str:
    """Best-effort article text, for items whose feed carries only a teaser.

    Deliberately crude — no readability heuristics, no boilerplate stripping.
    The judge is a competent reader and would rather have the whole page than a
    clever extraction that silently dropped the argument.
    """
    body, _, _ = fetch_raw(url, insecure=insecure, timeout=timeout)
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return ""
    return strip_html(text)[:limit]
