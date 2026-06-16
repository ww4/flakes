#!/usr/bin/env python3
"""media-curate — maintain the `backed-up` tag and promote YouTube downloads.

Subcommands:
  tag-sweep   Set/clear the backup tag on every library item based on whether
              its file ACTUALLY exists in the backup pool. This both backfills
              existing items and continuously verifies (a tagged item whose file
              vanished from the pool gets un-tagged). Truth is derived from the
              filesystem, never hand-maintained.

  promote     Act on the two promote collections:
                Promote→Library: file a YouTube download into Movies / TV Shows,
                  but ONLY if it's been named canonically in Jellyfin
                  ("Name (Year)" → movie, "Show S01E01" → TV). Anything else is
                  left in place and reported (with the reason) via ntfy.
                Promote→Keep: organize as a home video under <channel>/ and
                  build a Jellyfin NFO + poster from the captured .info.json.

  status      Print collection contents and tag stats.

Dry-run by default. Pass --apply to make changes. Config is via environment
(see media-curate.nix).
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096").rstrip("/")
API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()
FUSION = os.environ.get("FUSION", "/mnt/fusion").rstrip("/")
BACKUP = os.environ.get("BACKUP", "/mnt/backup/all").rstrip("/")
BACKUP_TAG = os.environ.get("BACKUP_TAG", "backed-up")
MOVIES_DIR = os.environ.get("MOVIES_DIR", FUSION + "/Movies")
TV_DIR = os.environ.get("TV_DIR", FUSION + "/TV Shows")
KEEP_DIR = os.environ.get("KEEP_DIR", FUSION + "/youtube/promoted")
COLL_LIBRARY = os.environ.get("COLL_LIBRARY", "Promote Library")
COLL_KEEP = os.environ.get("COLL_KEEP", "Promote Keep")
NOTIFY_BIN = os.environ.get("NOTIFY_BIN", "gromit-notify")

# Paths under FUSION that media-mirror does NOT back up (tier 3). Mirrors the
# EXCLUDES in media-mirror.sh — keep in sync.
EXCLUDES = ["arr", "pinchflat", "bitcoind", "archive", "legacy", "restic",
            ".graveyard", "rick-offsite"]

# Canonical-name patterns. Movies stay strict ("Name (Year)"). TV matches a
# SxxExx token with an optional episode title after it, e.g.
#   "DARK MATTER S2E1 'Welcome to Your New Home'"  ->  show / s2 / e1
# Separator before SxxExx may be space/dot/dash/underscore; the episode number
# must be followed by end-of-title or a non-word char (so S2E1 vs S2E15 parse
# right and "S2E1abc" is rejected as ambiguous).
MOVIE_RE = re.compile(r"^(?P<title>.+) \((?P<year>(?:19|20)\d{2})\)$")
TV_RE = re.compile(r"^(?P<show>.+?)[ ._-]+[Ss](?P<s>\d{1,2})[Ee](?P<e>\d{1,3})(?:\W.*)?$")

VIDEO_TYPES = "Movie,Episode,Video,MusicVideo,Audio"


class ApiError(Exception):
    pass


def die(msg):
    print(f"media-curate: error: {msg}", file=sys.stderr)
    sys.exit(1)


def api(method, path, params=None, body=None):
    url = JELLYFIN_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Emby-Token", API_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise ApiError(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def notify(title, message, priority="default", tags=""):
    try:
        subprocess.run([NOTIFY_BIN, title, message, priority, tags], check=False)
    except FileNotFoundError:
        print(f"[ntfy unavailable] {title}: {message}", file=sys.stderr)


def all_items():
    """Every media item with a file path + its tags, fetched in pages (the
    whole library in one request times out)."""
    out, start, page = [], 0, 500
    while True:
        res = api("GET", "/Items", {
            "Recursive": "true",
            "IncludeItemTypes": VIDEO_TYPES,
            "Fields": "Path,Tags",
            "EnableTotalRecordCount": "false",
            "StartIndex": str(start),
            "Limit": str(page),
        })
        batch = (res or {}).get("Items", [])
        out.extend(i for i in batch if i.get("Path"))
        if len(batch) < page:
            return out
        start += page


def collection_id(name):
    res = api("GET", "/Items", {
        "Recursive": "true",
        "IncludeItemTypes": "BoxSet",
        "Fields": "Name",
        "EnableTotalRecordCount": "false",
    })
    for c in (res or {}).get("Items", []):
        if c.get("Name") == name:
            return c["Id"]
    return None


_USER_ID = None


def user_id():
    """Collection-membership queries need a user context (an API key alone
    returns nothing). Cache the first admin (or first) user's id."""
    global _USER_ID
    if _USER_ID is None:
        users = api("GET", "/Users") or []
        admins = [u for u in users if u.get("Policy", {}).get("IsAdministrator")]
        _USER_ID = (admins or users)[0]["Id"]
    return _USER_ID


def collection_videos(cid):
    """The leaf video files in a collection. Recursive+userId flattens any
    folders/seasons the user added, so we get the actual files directly."""
    res = api("GET", "/Items", {
        "ParentId": cid,
        "Recursive": "true",
        "userId": user_id(),
        "IncludeItemTypes": VIDEO_TYPES,
        "Fields": "Path,ProductionYear",
        "EnableTotalRecordCount": "false",
    })
    return [i for i in (res or {}).get("Items", []) if i.get("Path")]


def remove_from_collection(cid, item_id, apply):
    """Detach an item from a collection (BoxSet). A Jellyfin library rescan does
    NOT prune a collection's linked children when their files move away, so
    promote must remove them explicitly — otherwise the BoxSet keeps dangling
    path-links that Jellyfin re-warns about ("Unable to find linked item") on
    every scan. Endpoint verified against the live server's OpenAPI spec."""
    if not apply:
        print(f"  (would detach item {item_id} from collection {cid})")
        return
    try:
        api("DELETE", f"/Collections/{cid}/Items", {"ids": item_id})
        print(f"  detached item {item_id} from collection")
    except ApiError as e:
        print(f"  WARN: could not detach {item_id}: {e}", file=sys.stderr)


def set_tags(item_id, tags):
    """Replace an item's Tags. The full DTO must be fetched with a user context
    (the bare GET /Items/{id} is a 400), modified, and POSTed back."""
    dto = api("GET", f"/Users/{user_id()}/Items/{item_id}")
    dto["Tags"] = sorted(set(tags))
    api("POST", f"/Items/{item_id}", body=dto)


def under_fusion(path):
    return path == FUSION or path.startswith(FUSION + "/")


def is_excluded(rel):
    first = rel.split("/", 1)[0]
    return first in EXCLUDES


def backup_present(rel):
    """File exists in the backup pool with a matching size (cheap verify)."""
    bpath = os.path.join(BACKUP, rel)
    src = os.path.join(FUSION, rel)
    if not os.path.exists(bpath):
        return False
    try:
        return os.path.getsize(bpath) == os.path.getsize(src)
    except OSError:
        return os.path.exists(bpath)


# --------------------------------------------------------------------------- #
def cmd_tag_sweep(apply):
    items = all_items()
    add = []      # should be tagged but isn't
    drop = []     # tagged but shouldn't be
    pending = 0   # in scope, not yet in the pool
    for it in items:
        path = it["Path"]
        if not under_fusion(path):
            continue
        rel = os.path.relpath(path, FUSION)
        tags = it.get("Tags", []) or []
        has = BACKUP_TAG in tags
        if is_excluded(rel):
            should = False
        else:
            should = backup_present(rel)
            if not should:
                pending += 1
        if should and not has:
            add.append((it, tags))
        elif has and not should:
            drop.append((it, tags))

    print(f"tag-sweep: {len(items)} items | +tag {len(add)} | -tag {len(drop)} "
          f"| pending(in scope, not yet mirrored) {pending}")
    for it, _ in add:
        print(f"  + {it['Name']}")
    for it, _ in drop:
        print(f"  - {it['Name']}")
    if not apply:
        print("(dry-run; pass --apply to write tags)")
        return
    ok_add = ok_drop = errs = 0
    for it, tags in add:
        try:
            set_tags(it["Id"], list(tags) + [BACKUP_TAG]); ok_add += 1
        except ApiError as e:
            errs += 1
            if errs <= 5:
                print(f"  ! {it['Name']}: {e}")
    for it, tags in drop:
        try:
            set_tags(it["Id"], [t for t in tags if t != BACKUP_TAG]); ok_drop += 1
        except ApiError as e:
            errs += 1
            if errs <= 5:
                print(f"  ! {it['Name']}: {e}")
    print(f"applied: +{ok_add} / -{ok_drop}" +
          (f"; {errs} item(s) errored and were skipped" if errs else ""))


# --------------------------------------------------------------------------- #
def sidecars(path):
    """Companion files next to a media file: yt-dlp's info.json/description, an
    existing PinchFlat/Jellyfin .nfo, and thumbnails. Carried along on a move so
    metadata/posters aren't lost."""
    base, _ = os.path.splitext(path)
    out = []
    for suffix in (".info.json", ".nfo", ".description"):
        if os.path.exists(base + suffix):
            out.append(base + suffix)
    for ext in (".jpg", ".jpeg", ".webp", ".png"):
        for cand in (base + ext, base + "-thumb" + ext, base + "-poster" + ext, path + ext):
            if os.path.exists(cand):
                out.append(cand)
    return out


def move(src, dst, apply):
    print(f"  mv {src}\n   -> {dst}")
    if not apply:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)


def cmd_promote(apply):
    failures = []  # (name, reason)

    def handle_library_leaf(it):
        """File a single video into Movies/TV by its canonical title.
        Returns True if moved, False if it still needs naming."""
        name, path = it["Name"], it["Path"]
        ext = os.path.splitext(path)[1]
        base = os.path.splitext(path)[0]
        m, t = MOVIE_RE.match(name), TV_RE.match(name)
        if m:
            folder = os.path.join(MOVIES_DIR, name)
            print(f"[movie] {name}")
            move(path, os.path.join(folder, os.path.basename(path)), apply)
            for s in sidecars(path):
                move(s, os.path.join(folder, os.path.basename(s)), apply)
            return True
        if t:
            show, season, ep = t.group("show").strip(), int(t.group("s")), int(t.group("e"))
            # Keep the series YEAR so Jellyfin matches the right show — e.g. Dark
            # Matter (2015) vs the unrelated Dark Matter (2024); a year-less folder
            # gets mis-identified. If the title doesn't already carry "(YYYY)",
            # recover it from the source path (Sonarr/Radarr name folders
            # "Show (YYYY)") or the Jellyfin item's ProductionYear. If no year can
            # be found, refuse rather than create an ambiguous folder.
            if not re.search(r"\((?:19|20)\d{2}\)\s*$", show):
                pm = re.search(r"\((19|20)\d{2}\)", path)
                year = pm.group(0)[1:-1] if pm else (
                    str(it["ProductionYear"]) if it.get("ProductionYear") else None)
                if year:
                    show = f"{show} ({year})"
                else:
                    failures.append((name, f"TV show has no year — would mis-match in "
                                     f"Jellyfin (got '{show}'). Add (YYYY) to the title "
                                     f"and re-promote."))
                    return False
            folder = os.path.join(TV_DIR, show, f"Season {season:02d}")
            newbase = f"{show} S{season:02d}E{ep:02d}"
            print(f"[tv] {show} S{season:02d}E{ep:02d}")
            move(path, os.path.join(folder, newbase + ext), apply)
            for s in sidecars(path):
                move(s, os.path.join(folder, newbase + s[len(base):]), apply)
            return True
        failures.append((name, f"title not canonical (got '{name}')"))
        return False

    def handle_keep_leaf(it):
        """Organize a single video as a home video under its channel + NFO."""
        path = it["Path"]
        base = os.path.splitext(path)[0]
        info, channel = None, None
        if os.path.exists(base + ".info.json"):
            try:
                info = json.load(open(base + ".info.json"))
                channel = info.get("uploader") or info.get("channel")
            except (OSError, ValueError):
                pass
        if not channel and os.path.exists(base + ".nfo"):  # PinchFlat writes NFO
            try:
                mm = re.search(r"<(?:studio|channel|showtitle)>(.*?)</",
                               open(base + ".nfo").read())
                channel = mm.group(1).strip() if mm else None
            except OSError:
                pass
        if not channel:
            # Fall back to the containing folder (PinchFlat groups by channel on
            # disk), skipping a generic "Season YYYY" wrapper.
            parent = os.path.basename(os.path.dirname(path))
            if re.match(r"(?i)season[ _-]?\d", parent):
                parent = os.path.basename(os.path.dirname(os.path.dirname(path)))
            channel = parent or "Unknown"
        channel = re.sub(r"[/\\]", "_", channel).strip() or "Unknown"
        folder = os.path.join(KEEP_DIR, channel)
        dst = os.path.join(folder, os.path.basename(path))
        print(f"[keep] {it['Name']}  (channel: {channel})")
        move(path, dst, apply)
        for s in sidecars(path):
            move(s, os.path.join(folder, os.path.basename(s)), apply)
        # Only synthesize an NFO if none travelled with the file.
        nfo = os.path.splitext(dst)[0] + ".nfo"
        if apply and info is not None and not os.path.exists(nfo):
            write_nfo(nfo, info)
        return True

    did_move = False
    for coll_name, handler in ((COLL_LIBRARY, handle_library_leaf),
                               (COLL_KEEP, handle_keep_leaf)):
        cid = collection_id(coll_name)
        if cid is None:
            print(f"(collection '{coll_name}' not found — skipping)")
            continue
        vids = collection_videos(cid)  # recursive+userId flattens added folders
        print(f"== {coll_name}: {len(vids)} video(s) ==")
        for it in vids:
            if not os.path.exists(it["Path"]):
                # File was moved on a prior run but the item is still linked in
                # the collection — detach it now (self-heals stragglers within
                # the window before Jellyfin rescans the old item away).
                remove_from_collection(cid, it["Id"], apply)
                continue
            if handler(it):
                did_move = True
                # Detach while the item still resolves (its file just moved, but
                # Jellyfin hasn't rescanned yet) so the collection never keeps a
                # dangling link. A rescan alone does NOT prune collection links.
                remove_from_collection(cid, it["Id"], apply)
    # Index the new locations and drop the now-fileless source items. (The
    # detach above is what clears the collection — the refresh does not.)
    if apply and did_move:
        api("POST", "/Library/Refresh")

    state = "/var/lib/media-curate/pending.txt"
    if failures:
        brief = "; ".join(f"{n} [{r}]" for n, r in failures[:10])
        more = "" if len(failures) <= 10 else f" (+{len(failures) - 10} more)"
        msg = f"{len(failures)} item(s) need a canonical title before promotion: {brief}{more}"
        print(msg)
        # Only ntfy when the pending set changes, so the 30-min timer doesn't
        # re-ping about the same un-named items every run.
        if apply:
            cur = "\n".join(sorted(n for n, _ in failures))
            try:
                prev = open(state).read()
            except OSError:
                prev = ""
            if cur != prev:
                notify("media-curate: items need naming", msg, "default", "clapper,warning")
                try:
                    open(state, "w").write(cur)
                except OSError:
                    pass
    elif apply:
        try:
            open(state, "w").write("")  # nothing pending; reset so it re-alerts next time
        except OSError:
            pass
    if not apply:
        print("(dry-run; pass --apply to move files)")


def write_nfo(nfo_path, info):
    def esc(x):
        return html.escape(str(x or ""))
    up = info.get("upload_date", "")
    premiered = f"{up[0:4]}-{up[4:6]}-{up[6:8]}" if len(up) == 8 else ""
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<movie>
  <title>{esc(info.get('title'))}</title>
  <plot>{esc(info.get('description'))}</plot>
  <studio>{esc(info.get('uploader') or info.get('channel'))}</studio>
  <premiered>{esc(premiered)}</premiered>
  <year>{esc(up[0:4])}</year>
</movie>
"""
    with open(nfo_path, "w") as f:
        f.write(xml)


def cmd_status(_apply):
    # List ALL collections with Jellyfin's child count vs what our membership
    # query returns — reveals naming mismatches or items Jellyfin didn't accept.
    boxsets = api("GET", "/Items", {
        "Recursive": "true",
        "IncludeItemTypes": "BoxSet",
        "Fields": "ChildCount",
        "EnableTotalRecordCount": "false",
    }) or {}
    print("Collections (BoxSets):")
    for c in boxsets.get("Items", []):
        vids = collection_videos(c["Id"])
        print(f"  {c['Name']!r}  videos={len(vids)}  id={c['Id']}")
    items = all_items()
    tagged = sum(1 for i in items if BACKUP_TAG in (i.get("Tags") or []))
    print(f"items scanned: {len(items)}; backed-up tag: {tagged}")


def main():
    if not API_KEY:
        print("media-curate: JELLYFIN_API_KEY not set — skipping (add it via "
              "`sops secrets/media-curate-env.yaml` to activate).", file=sys.stderr)
        sys.exit(0)
    ap = argparse.ArgumentParser(prog="media-curate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("tag-sweep", "promote", "status"):
        p = sub.add_parser(c)
        p.add_argument("--apply", action="store_true", help="make changes (default: dry-run)")
    args = ap.parse_args()
    try:
        {"tag-sweep": cmd_tag_sweep, "promote": cmd_promote, "status": cmd_status}[args.cmd](args.apply)
    except ApiError as e:
        die(str(e))


if __name__ == "__main__":
    main()
