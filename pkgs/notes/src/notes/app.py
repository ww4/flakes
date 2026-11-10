"""HTTP API + the single-page front end.

Unauthenticated by design, exactly like the SilverBullet vhost it sits beside:
the perimeter is the nginx source-gate (Tailscale/LAN) plus the tailnet ACL,
and adding a login here would reintroduce the friction that made the notes
app go unused in the first place (decided 2026-07-09).

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

from . import paths, people, store
from .config import Settings

log = logging.getLogger("notes")

# Who is using this browser. Not authentication -- there is nothing here to
# protect, and the owner explicitly did not want accounts. It replaces Google
# Keep's "add collaborator", which is annoying to live with for years: the point
# is attribution between two people who already trust each other, so a cookie
# the browser sets for itself is the right weight. An unknown device stays
# unlabelled, which reads as the owner and preserves the 545 notes predating this.
WHO_COOKIE = "notes_who"
WHO_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def normalize_who(raw: str | None) -> str | None:
    """Any sign-in string, reduced to the slug that can land in frontmatter.

    Proxies hand back whatever their directory holds — ``ana``,
    ``ana@example.com``, ``ANA``. One definition of "slug" lives in
    people.py, because the people table has to agree with this exactly.
    """
    return people.slug(raw)


def raw_identity(request: Request, cfg: Settings) -> str | None:
    """The sign-in string as it arrived, before any mapping.

    Kept distinct from the person it resolves to: ``github:aparker`` and
    ``google:ana@gmail.com`` are two identities and one person, and only the
    person is ever written to a note.
    """
    return (proxy_raw(request, cfg) or (request.cookies.get(WHO_COOKIE) or "").strip()) or None


def proxy_raw(request: Request, cfg: Settings) -> str | None:
    """The identity an upstream proxy says this is — only when told to believe it.

    ⚠️ This header is trustworthy ONLY because nothing can reach the port
    except the proxy. If the app is directly reachable, anyone can send it.
    ``trust_proxy_auth`` is therefore opt-in, and ``main()`` refuses to bind a
    public address without either it or an explicit override.
    """
    if not cfg.trust_proxy_auth:
        return None
    return (request.headers.get(cfg.auth_user_header) or "").strip() or None


def proxy_who(request: Request, cfg: Settings) -> str | None:
    """The proxy's identity as a person slug, ignoring the people table."""
    return normalize_who(proxy_raw(request, cfg))


def proxy_name(request: Request, cfg: Settings) -> str | None:
    """A human-readable name the proxy offers, if it sends one."""
    if not cfg.trust_proxy_auth:
        return None
    return (request.headers.get(cfg.auth_name_header) or "").strip() or None


def who(request: Request, cfg: Settings, ppl: people.People | None = None) -> str | None:
    """Which PERSON this request is from: the proxy's word first, else the browser's.

    The proxy WINS. A cookie is a claim the browser makes about itself, which
    is the right weight when there is nothing to protect; once something has
    actually authenticated the person, that answer outranks it.

    The people table maps the sign-in identity onto a person, so someone with
    a Google login on their phone and a GitHub one on a laptop archives a
    note once rather than twice.
    """
    raw = raw_identity(request, cfg)
    if ppl is not None:
        return ppl.canonical(raw)
    return normalize_who(raw)


def viewer_for(request: Request, cfg: Settings, me: str | None,
               ppl: people.People | None = None) -> str:
    """Whose feed the listing should be built for.

    ``?me=`` exists so one browser can preview another person's wall, and so
    the service worker's cache entry is keyed per person rather than shared.
    It is a QUERY PARAMETER, so it must never outrank an authenticated
    identity — otherwise proxy auth would be bypassable with a URL.
    """
    authed = proxy_raw(request, cfg)
    if authed:
        return (ppl.canonical(authed) if ppl is not None else normalize_who(authed)) or store.HUMAN
    if me and WHO_RE.match(me):
        return me
    return who(request, cfg, ppl) or store.HUMAN

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


class PersonWrite(BaseModel):
    identity: str = Field(min_length=1, max_length=128)
    person: str | None = Field(default=None, max_length=40)
    display: str | None = Field(default=None, max_length=60)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    space = Path(cfg.space_dir)
    # One module-level default rather than threading it through every call:
    # it is read in a dozen places and never changes after start-up.
    store.HUMAN = people.slug(cfg.default_author) or store.HUMAN
    app = FastAPI(title="Notes", docs_url=None, redoc_url=None)

    def roster() -> people.People:
        return people.load(space, paths.PEOPLE_FILE)

    def known_people(ppl: people.People) -> set[str]:
        """Everyone the app already has evidence for.

        A row in the table, the default author, or anyone notes are already
        attributed to. Walking the space for that last part is why this is
        only called when an identity is genuinely new.
        """
        found = set(ppl.people()) | {store.HUMAN}
        try:
            found |= set(store.authors_of(store.listing(space)))
        except OSError:
            pass
        return found

    def seen(request: Request) -> people.People:
        """Load the roster, recording this sign-in if it is a new one.

        This is what fills the Users page: every identity any authentication
        source has ever presented turns up there. One it can place — a name
        already on the table, or one that notes are attributed to — is
        recorded as that person, because the app would have resolved it there
        anyway and there is nothing to ask. Anything else stays unclaimed,
        and the front end asks on first load.

        Writing during a read is deliberate but rare: only the first time an
        identity appears at all.
        """
        ppl = roster()
        raw = raw_identity(request, cfg)
        if not raw or ppl.has(raw):
            return ppl
        if ppl.observe(raw, known=known_people(ppl)):
            name = proxy_name(request, cfg)
            if name:
                # A proxy's own name header is a far better first guess than
                # the login string, and it is only a default — the table wins
                # once anybody edits it.
                ppl.assign(raw, None, name)
            try:
                people.save(space, paths.PEOPLE_FILE, ppl)
            except OSError as e:          # a read-only space must still serve
                log.warning("could not record a new identity: %s", e)
        return ppl

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
        # of why a fresh load sat blank (decided 2026-11-09).
        #
        # The query param keeps the CACHE key honest: the service worker stores
        # this response, and two people on one box must not share an entry. The
        # cookie stays authoritative for writes, which are never cached.
        ppl = seen(request)
        viewer = viewer_for(request, cfg, me, ppl)
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
            # Labels only. The client renders these; what a note RECORDS is
            # always the person slug.
            "people": {p: ppl.display_for(p) for p in
                       sorted(set(ppl.people()) | {viewer} |
                              {n.author for n in notes if n.author})},
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
        return store.create(space, req.title, req.body,
                            author=who(request, cfg, seen(request))).summary()

    @app.put("/api/notes")
    async def update_note(req: NoteWrite, request: Request):
        if not paths.resolve(space, req.id).exists():
            raise HTTPException(404, "no such note")
        return store.write(
            space, req.id,
            title=req.title, body=req.body,
            pinned=req.pinned, archived=req.archived, shared=req.shared, order=req.order,
            category=req.category, author=req.author,
            actor=who(request, cfg, roster()) or store.HUMAN,
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

    @app.get("/api/whoami")
    async def whoami(request: Request):
        """Who this browser is, and whether anybody has said so yet.

        ⚠️ Deliberately NOT part of /api/notes. That response is cached by the
        service worker, and identity in a cached body is what made the app
        forget who you were on every reload — two people on one box would
        also share the entry. This endpoint is never cached.
        """
        ppl = seen(request)
        raw = raw_identity(request, cfg)
        row = ppl._by_identity().get(people.ident(raw)) if raw else None
        person = who(request, cfg, ppl)
        return {
            "identity": people.ident(raw) if raw else "",
            "person": person,
            # Nobody has said whose this sign-in is. The front end asks, once.
            "unclaimed": bool(raw) and not (row and row.person),
            # What to put in the box when it does: the name the proxy gave, or
            # the best guess from the login itself.
            "suggest": (row.display if row and row.display else "") or person or "",
            "people": {p: ppl.display_for(p) for p in ppl.people()},
            "fixed": cfg.trust_proxy_auth,   # the identity is the proxy's, not ours to change
        }

    @app.get("/api/people")
    async def list_people(request: Request):
        """Every identity ever seen, who it belongs to, and where that is stored.

        The file path comes back too, so the page can say where this actually
        lives — it is an ordinary markdown table in the space, editable by
        hand, and hiding that would misrepresent the design.
        """
        ppl = seen(request)
        return {
            "rows": [{"identity": r.identity, "person": r.person,
                      "display": r.display, "first_seen": r.first_seen}
                     for r in sorted(ppl.rows,
                                     key=lambda r: (r.person == "", r.person, r.identity))],
            "people": {p: ppl.display_for(p) for p in ppl.people()},
            "me": who(request, cfg, ppl) or store.HUMAN,
            "file": paths.PEOPLE_FILE,
            "proxy_auth": cfg.trust_proxy_auth,
        }

    @app.put("/api/people")
    async def put_person(request: Request, req: PersonWrite):
        """Link an identity to a person, and/or set that person's display name."""
        ppl = roster()
        ppl.assign(req.identity, req.person, req.display)
        people.save(space, paths.PEOPLE_FILE, ppl)
        fresh = roster()
        return {"ok": True, "me": who(request, cfg, fresh) or store.HUMAN}

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
        served notes.example.com until 2026-11-09).
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
