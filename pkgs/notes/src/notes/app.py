"""HTTP API + the single-page front end.

Unauthenticated by design, exactly like the SilverBullet vhost it sits beside:
the perimeter is the nginx source-gate (Tailscale/LAN) plus the tailnet ACL,
and adding a login here would reintroduce the friction that made the notes
app go unused in the first place (Chris, 2026-07-09).

There are no accounts and there will not be. This app has no per-note
permissions to express, so access is all-or-nothing — and a reverse proxy
does all-or-nothing better than we could, before a request reaches Python.
What it does support is BELIEVING a proxy that has already authenticated
someone (``trust_proxy_auth``): that name then becomes the identity that
archives and pins notes, so authentication and attribution are one thing
instead of two stacked on each other.

Note ids travel in the request BODY rather than the URL path. An id is a
space-relative path like ``Keep/2017-03-04 Bring from Kentucky.md`` — spaces,
unicode and slashes in a path segment make URL-encoded ids a source of
mismatched-decoding bugs, and nothing here needs REST-shaped URLs.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import paths, store
from .config import Settings

log = logging.getLogger("notes")

# Who is using this browser. Not authentication -- there is nothing here to
# protect, and Chris explicitly did not want accounts. It replaces Google
# Keep's "add collaborator", which he has found annoying for years: the point
# is attribution between two people who already trust each other, so a cookie
# the browser sets for itself is the right weight. An unknown device stays
# unlabelled, which reads as Chris and preserves the 545 notes predating this.
WHO_COOKIE = "notes_who"
WHO_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def normalize_who(raw: str | None) -> str | None:
    """A display name from any source, reduced to a usable identity or None.

    Proxies hand back whatever their directory holds — ``mary``,
    ``mary@example.com``, ``MARY``. The part before the ``@`` is the name
    worth keeping; the rest is folded to the same shape a cookie would carry,
    since this string ends up in frontmatter as ``archived_by: mary``.
    """
    name = (raw or "").strip().lower().split("@", 1)[0]
    name = re.sub(r"[^a-z0-9_-]+", "-", name).strip("-")[:32]
    return name if WHO_RE.match(name) else None


def proxy_who(request: Request, cfg: Settings) -> str | None:
    """The user an upstream proxy says this is — only when told to believe it.

    ⚠️ This header is trustworthy ONLY because nothing can reach the port
    except the proxy. If the app is directly reachable, anyone can send it.
    ``trust_proxy_auth`` is therefore opt-in, and ``main()`` refuses to bind a
    public address without either it or an explicit override.
    """
    if not cfg.trust_proxy_auth:
        return None
    return normalize_who(request.headers.get(cfg.auth_user_header))


def who(request: Request, cfg: Settings) -> str | None:
    """Who this request is from: the proxy's word first, else the browser's.

    The proxy WINS. A cookie is a claim the browser makes about itself, which
    is the right weight when there is nothing to protect; once something has
    actually authenticated the person, that answer outranks it.
    """
    return proxy_who(request, cfg) or normalize_who(request.cookies.get(WHO_COOKIE))


def viewer_for(request: Request, cfg: Settings, me: str | None) -> str:
    """Whose feed the listing should be built for.

    ``?me=`` exists so one browser can preview another person's wall, and so
    the service worker's cache entry is keyed per person rather than shared.
    It is a QUERY PARAMETER, so it must never outrank an authenticated
    identity — otherwise proxy auth would be bypassable with a URL.
    """
    authed = proxy_who(request, cfg)
    if authed:
        return authed
    if me and WHO_RE.match(me):
        return me
    return who(request, cfg) or store.HUMAN

STATIC = Path(__file__).parent / "static"


class NoteRef(BaseModel):
    id: str = Field(min_length=1, max_length=1024)


class NoteWrite(NoteRef):
    title: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=1_000_000)
    pinned: bool | None = None
    archived: bool | None = None
    shared: bool | None = None
    order: int | None = Field(default=None, ge=-1, le=100000)
    category: str | None = Field(default=None, max_length=60)
    author: str | None = Field(default=None, max_length=40)


class NoteNew(BaseModel):
    title: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=1_000_000)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    space = Path(cfg.space_dir)
    app = FastAPI(title="Notes", docs_url=None, redoc_url=None)

    @app.exception_handler(paths.PathError)
    async def _path_error(_request, exc: paths.PathError):
        # Deliberately terse: the client never needs to learn which of the
        # three scope conditions it tripped.
        log.warning("rejected out-of-scope path: %s", exc)
        return JSONResponse({"error": "no such note"}, status_code=404)

    @app.get("/api/notes")
    async def list_notes(
        request: Request,
        q: str | None = None,
        scope: str = "all",
        shared: int = 1,
        me: str | None = None,
    ):
        """The wall: summaries only, and the search runs here.

        Sending whole notes was 546 KB for 586 cards. Previews cut that to a
        fraction, but they also mean the client CANNOT do the matching -- a
        hit inside a long note would silently not be found -- so q is handled
        server-side against the full text.
        """
        # ONE walk of the space, not three. This called listing(), census()
        # and authors() separately, and census/authors each walked the whole
        # space AGAIN — 1412 notes read three times per request, which is most
        # of why a fresh load sat blank (Chris, 2026-11-09).
        #
        # The query param keeps the CACHE key honest: the service worker stores
        # this response, and two people on one box must not share an entry. The
        # cookie stays authoritative for writes, which are never cached.
        viewer = viewer_for(request, cfg, me)
        notes = store.listing(space, q)
        # The census has to describe the WHOLE space, not the search result, or
        # a category vanishes from the chips the moment you type its name.
        everything = notes if not q else store.listing(space)
        return {
            "notes": [n.summary() for n in notes],
            "cats": store.census_of(
                everything,
                scope=scope if scope in ("mine", "shared", "all") else "all",
                me=viewer,
                show_shared=bool(shared),
            ),
            "authors": store.authors_of(everything),
        }

    @app.get("/api/note")
    async def get_note(id: str):
        """One note, with its real body. The editor calls this on open."""
        if not paths.resolve(space, id).exists():
            raise HTTPException(404, "no such note")
        return store.read(space, id).public()

    @app.post("/api/notes")
    async def new_note(req: NoteNew, request: Request):
        if not req.title.strip() and not req.body.strip():
            raise HTTPException(400, "an empty note is not saved")
        # Attribution comes from the DEVICE, not from the client asking nicely:
        # the cookie is read here so a note is stamped the same way whichever
        # code path created it.
        # No invented "Untitled": a Keep note may have only a body, and so
        # may one made here.
        return store.create(space, req.title, req.body, author=who(request, cfg)).summary()

    @app.put("/api/notes")
    async def update_note(req: NoteWrite, request: Request):
        if not paths.resolve(space, req.id).exists():
            raise HTTPException(404, "no such note")
        return store.write(
            space, req.id,
            title=req.title, body=req.body,
            pinned=req.pinned, archived=req.archived, shared=req.shared, order=req.order,
            category=req.category, author=req.author,
            actor=who(request, cfg) or store.HUMAN,
        ).summary()

    @app.get("/api/trash")
    async def list_trash():
        """What is recoverable. Paired with /api/notes/restore."""
        return {"notes": [n.summary() for n in store.trash_listing(space)]}

    @app.post("/api/notes/trash")
    async def trash_note(req: NoteRef):
        return {"trashed": store.trash(space, req.id)}

    @app.post("/api/notes/restore")
    async def restore_note(req: NoteRef):
        return {"restored": store.restore(space, req.id)}

    @app.get("/healthz")
    async def healthz():
        ok = space.is_dir()
        return JSONResponse({"ok": ok, "space": str(space)}, status_code=200 if ok else 503)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/sw.js")
    async def service_worker():
        """The worker must be served from the ROOT to control the whole origin.

        Service-Worker-Allowed is set explicitly rather than relied upon: a
        worker served from a subpath can only control that subpath, and the
        failure mode is a worker that registers fine and then never intercepts
        anything. no-store because a cached worker is how a stale app survives
        a deploy -- this origin already has one dead PWA on it (SilverBullet
        served notes.rosemaryacres.com until 2026-11-09).
        """
        return FileResponse(
            STATIC / "sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
        )

    @app.get("/service_worker.js")
    async def silverbullet_worker_killer():
        """Evict SilverBullet's worker, which still owns this origin.

        See static/service_worker.js. This path is not ours; it is the one the
        previous occupant registered, and the only thing a poisoned browser
        still fetches from the network.
        """
        return FileResponse(
            STATIC / "service_worker.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
        )

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon.svg")
    async def icon_svg():
        return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")

    @app.get("/icon-192.png")
    async def icon_192():
        return FileResponse(STATIC / "icon-192.png", media_type="image/png")

    @app.get("/icon-512.png")
    async def icon_512():
        return FileResponse(STATIC / "icon-512.png", media_type="image/png")

    return app
