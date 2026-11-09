"""HTTP API + the single-page front end.

Unauthenticated by design, exactly like the SilverBullet vhost it sits beside:
the perimeter is the nginx source-gate (Tailscale/LAN) plus the tailnet ACL,
and adding a login here would reintroduce the friction that made the notes
app go unused in the first place (Chris, 2026-07-09).

Note ids travel in the request BODY rather than the URL path. An id is a
space-relative path like ``Keep/2017-03-04 Bring from Kentucky.md`` — spaces,
unicode and slashes in a path segment make URL-encoded ids a source of
mismatched-decoding bugs, and nothing here needs REST-shaped URLs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import paths, store
from .config import Settings

log = logging.getLogger("notes")

STATIC = Path(__file__).parent / "static"


class NoteRef(BaseModel):
    id: str = Field(min_length=1, max_length=1024)


class NoteWrite(NoteRef):
    title: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=1_000_000)
    pinned: bool | None = None
    archived: bool | None = None
    category: str | None = Field(default=None, max_length=60)


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
    async def list_notes(q: str | None = None):
        """The wall: summaries only, and the search runs here.

        Sending whole notes was 546 KB for 586 cards. Previews cut that to a
        fraction, but they also mean the client CANNOT do the matching -- a
        hit inside a long note would silently not be found -- so q is handled
        server-side against the full text.
        """
        return {
            "notes": [n.summary() for n in store.listing(space, q)],
            "cats": store.census(space),
        }

    @app.get("/api/note")
    async def get_note(id: str):
        """One note, with its real body. The editor calls this on open."""
        if not paths.resolve(space, id).exists():
            raise HTTPException(404, "no such note")
        return store.read(space, id).public()

    @app.post("/api/notes")
    async def new_note(req: NoteNew):
        if not req.title.strip() and not req.body.strip():
            raise HTTPException(400, "an empty note is not saved")
        return store.create(space, req.title, req.body).summary()

    @app.put("/api/notes")
    async def update_note(req: NoteWrite):
        if not paths.resolve(space, req.id).exists():
            raise HTTPException(404, "no such note")
        return store.write(
            space, req.id,
            title=req.title, body=req.body,
            pinned=req.pinned, archived=req.archived, category=req.category,
        ).summary()

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
