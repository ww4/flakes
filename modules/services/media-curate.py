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

# Strict canonical-name patterns. No loose matching: a near-miss is a failure.
MOVIE_RE = re.compile(r"^(?P<title>.+) \((?P<year>(?:19|20)\d{2})\)$")
TV_RE = re.compile(r"^(?P<show>.+?) [Ss](?P<s>\d{1,2})[Ee](?P<e>\d{1,3})$")

VIDEO_TYPES = "Movie,Episode,Video,MusicVideo,Audio"
LEAF_TYPES = {"Movie", "Episode", "Video", "MusicVideo", "Audio"}


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
        die(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def notify(title, message, priority="default", tags=""):
    try:
        subprocess.run([NOTIFY_BIN, title, message, priority, tags], check=False)
    except FileNotFoundError:
        print(f"[ntfy unavailable] {title}: {message}", file=sys.stderr)


def all_items():
    """Every media item with a file path + its tags."""
    res = api("GET", "/Items", {
        "Recursive": "true",
        "IncludeItemTypes": VIDEO_TYPES,
        "Fields": "Path,Tags",
        "EnableTotalRecordCount": "false",
    })
    return [i for i in (res or {}).get("Items", []) if i.get("Path")]


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


def collection_members(cid):
    res = api("GET", "/Items", {
        "ParentId": cid,
        "Fields": "Path,Tags",
        "EnableTotalRecordCount": "false",
    })
    return (res or {}).get("Items", [])


def leaf_items(member):
    """A collection member may be a single video OR a folder/season/series
    (much faster to add a whole playlist). Expand containers into their actual
    video files so each is handled individually."""
    if member.get("IsFolder") or member.get("Type") not in LEAF_TYPES:
        res = api("GET", "/Items", {
            "ParentId": member["Id"],
            "Recursive": "true",
            "IncludeItemTypes": VIDEO_TYPES,
            "Fields": "Path",
            "EnableTotalRecordCount": "false",
        })
        return [i for i in (res or {}).get("Items", []) if i.get("Path")]
    return [member] if member.get("Path") else []


def set_tags(item_id, tags):
    """Replace an item's Tags. Jellyfin wants the full DTO POSTed back."""
    dto = api("GET", f"/Items/{item_id}")
    dto["Tags"] = sorted(set(tags))
    api("POST", f"/Items/{item_id}", body=dto)


def remove_from_collection(cid, item_id):
    api("DELETE", f"/Collections/{cid}/Items", {"ids": item_id})


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
    for it, tags in add:
        set_tags(it["Id"], list(tags) + [BACKUP_TAG])
    for it, tags in drop:
        set_tags(it["Id"], [t for t in tags if t != BACKUP_TAG])
    print(f"applied: +{len(add)} / -{len(drop)}")


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
        info, channel = None, "Unknown"
        if os.path.exists(base + ".info.json"):
            try:
                info = json.load(open(base + ".info.json"))
                channel = info.get("uploader") or info.get("channel") or "Unknown"
            except (OSError, ValueError):
                pass
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

    for coll_name, handler in ((COLL_LIBRARY, handle_library_leaf),
                               (COLL_KEEP, handle_keep_leaf)):
        cid = collection_id(coll_name)
        if cid is None:
            print(f"(collection '{coll_name}' not found — skipping)")
            continue
        for member in collection_members(cid):
            leaves = leaf_items(member)
            if not leaves:
                failures.append((member.get("Name", "?"), "no video files found in folder"))
                continue
            kind = "folder" if (member.get("IsFolder") or member.get("Type") not in LEAF_TYPES) else "item"
            if kind == "folder":
                print(f"== {coll_name}: folder '{member['Name']}' -> {len(leaves)} videos ==")
            results = [handler(it) for it in leaves]
            # Clear the member from the collection only if every leaf was handled,
            # so a folder with some still-unnamed videos stays queued.
            if apply and all(results):
                remove_from_collection(cid, member["Id"])

    if failures:
        brief = "; ".join(f"{n} [{r}]" for n, r in failures[:10])
        more = "" if len(failures) <= 10 else f" (+{len(failures) - 10} more)"
        msg = f"{len(failures)} item(s) need a canonical title before promotion: {brief}{more}"
        print(msg)
        notify("media-curate: items need naming", msg, "default", "clapper,warning")
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
    for name in (COLL_LIBRARY, COLL_KEEP):
        cid = collection_id(name)
        members = collection_members(cid) if cid else []
        print(f"{name}: {'(missing)' if cid is None else len(members)}")
        for it in members:
            print(f"  - {it['Name']}")
    items = all_items()
    tagged = sum(1 for i in items if BACKUP_TAG in (i.get("Tags") or []))
    print(f"backed-up tag: {tagged}/{len(items)} items")


def main():
    if not API_KEY:
        print("media-curate: JELLYFIN_API_KEY not set — skipping (add it to "
              "/var/lib/media-curate/secrets.env to activate).", file=sys.stderr)
        sys.exit(0)
    ap = argparse.ArgumentParser(prog="media-curate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("tag-sweep", "promote", "status"):
        p = sub.add_parser(c)
        p.add_argument("--apply", action="store_true", help="make changes (default: dry-run)")
    args = ap.parse_args()
    {"tag-sweep": cmd_tag_sweep, "promote": cmd_promote, "status": cmd_status}[args.cmd](args.apply)


if __name__ == "__main__":
    main()
