#!/usr/bin/env python3
"""media-link — hardlink completed downloads into the Jellyfin library.

The point is to get seeded content into the library WITHOUT moving it: a move
takes the file out from under qBittorrent and kills the seed (fatal on a
private tracker). A hardlink gives one copy of the bytes two names — seeds from
/mnt/fusion/arr, plays from /mnt/fusion/{Movies,TV Shows}, and media-mirror
(rsync -aH, with /arr excluded) copies it to the backup pool exactly once.

  media-link <src-dir> --show "Tom Terrific (1957)" [--season N] [--extras]
  media-link <src-dir> --movie "Some Film (1974)"

Dry-run by default; --apply to act. Nothing is ever deleted or overwritten.

Safety (per the promote.sh near-miss): every target is computed and validated
BEFORE anything is linked, and the run aborts as a whole if any target looks
wrong — empty, non-absolute, escaping the library root, or with a degenerate
basename. Sources that don't parse are reported, never guessed at.
"""
import argparse
import glob
import os
import re
import sys

FUSION = os.environ.get("FUSION", "/mnt/fusion").rstrip("/")
TV_DIR = os.environ.get("TV_DIR", FUSION + "/TV Shows")
MOVIES_DIR = os.environ.get("MOVIES_DIR", FUSION + "/Movies")
# mergerfs hardlinks only work WITHIN one branch, and /mnt/fusion is mounted
# category.create=mfs — so a fresh season dir would land on whichever branch has
# the most free space, not necessarily the one holding the source. We therefore
# resolve the source's real branch and create the link there directly, which
# makes placement deterministic instead of free-space-dependent.
BRANCHES = sorted(glob.glob(os.environ.get("FUSION_BRANCHES", "/mnt/primary/D*")))

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts", ".mpg", ".mpeg"}
SIDECAR_EXT = {".srt", ".sub", ".idx", ".ass", ".nfo", ".jpg", ".png"}

# "S02E09", "s2e9", "2x09" — the two patterns worth trusting. Anything else is
# reported rather than guessed: a wrong episode number is worse than no link.
EP_RE = [
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})"),
    re.compile(r"(?<![\dxX])(\d{1,2})[xX](\d{1,3})(?!\d)"),
]
NAME_YEAR_RE = re.compile(r"^.+ \((?:19|20)\d{2}\)$")


def parse_ep(fname, forced_season=None):
    """(season, episode) or None. forced_season overrides a parsed season."""
    for rx in EP_RE:
        m = rx.search(fname)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            return (forced_season if forced_season is not None else s), e
    return None


def validate_target(path, root):
    """Fail closed. Returns an error string, or None if the target is sane."""
    if not path or not path.strip():
        return "empty target"
    if not os.path.isabs(path):
        return f"not absolute: {path}"
    real_root = os.path.realpath(root)
    if os.path.commonpath([os.path.realpath(os.path.dirname(path)), real_root]) != real_root:
        return f"escapes library root: {path}"
    base = os.path.basename(path)
    if len(base) < 5 or base.startswith("."):
        return f"degenerate basename: {base!r}"
    if os.path.splitext(base)[1].lower() not in (VIDEO_EXT | SIDECAR_EXT):
        return f"unexpected extension: {base!r}"
    return None


def branch_of(path):
    """Which physical mergerfs branch actually holds `path`?

    Returns (branch_root, path_on_branch) or None. Two things that do NOT work
    here: st_dev (every path under the FUSE mount reports the same device) and
    st_ino (mergerfs synthesises inodes via inodecalc=hybrid-hash, so the pool
    inode never equals the branch inode). Match on size + mtime instead, which
    is exact for a file mergerfs is merely passing through.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    rel = os.path.relpath(path, FUSION)
    if rel.startswith(".."):
        return None
    for b in BRANCHES:
        cand = os.path.join(b, rel)
        try:
            cst = os.stat(cand)
        except OSError:
            continue
        if cst.st_size == st.st_size and cst.st_mtime_ns == st.st_mtime_ns:
            return b, cand
    return None


def plan_show(src, show, forced_season, want_extras):
    """Build (src, dst) pairs plus a list of (file, reason) skips."""
    plan, skipped = [], []
    for dirpath, _, files in os.walk(src):
        for f in sorted(files):
            full = os.path.join(dirpath, f)
            ext = os.path.splitext(f)[1].lower()
            if ext not in VIDEO_EXT:
                continue
            se = parse_ep(f, forced_season)
            if se is None:
                if want_extras:
                    dst = os.path.join(TV_DIR, show, "extras", f)
                    plan.append((full, dst))
                else:
                    skipped.append((f, "no SxxExx / NxNN episode marker"))
                continue
            s, e = se
            season_dir = os.path.join(TV_DIR, show, f"Season {s:02d}")
            dst = os.path.join(season_dir, f"{show} S{s:02d}E{e:02d}{ext}")
            plan.append((full, dst))
    return plan, skipped


def plan_movie(src, movie):
    plan, skipped = [], []
    for dirpath, _, files in os.walk(src):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() not in VIDEO_EXT:
                continue
            full = os.path.join(dirpath, f)
            dst = os.path.join(MOVIES_DIR, movie, f"{movie}{os.path.splitext(f)[1].lower()}")
            plan.append((full, dst))
    if len(plan) > 1:
        skipped = [(os.path.basename(s), "multiple videos map to one movie name") for s, _ in plan]
        plan = []
    return plan, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", help='series folder name, e.g. "Tom Terrific (1957)"')
    g.add_argument("--movie", help='movie folder name, e.g. "Some Film (1974)"')
    ap.add_argument("--season", type=int, help="override the parsed season number")
    ap.add_argument("--extras", action="store_true",
                    help="link unparseable videos into <show>/extras/ instead of skipping")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    src = os.path.realpath(a.src)
    if not os.path.isdir(src):
        sys.exit(f"ERROR: source is not a directory: {src}")

    name = a.show or a.movie
    if not NAME_YEAR_RE.match(name):
        sys.exit(f'ERROR: name must end in a year, e.g. "Tom Terrific (1957)" — got "{name}"')

    root = TV_DIR if a.show else MOVIES_DIR
    plan, skipped = (plan_show(src, a.show, a.season, a.extras) if a.show
                     else plan_movie(src, a.movie))

    if not plan and not skipped:
        sys.exit(f"ERROR: no video files found under {src} — refusing to continue")

    # ---- validate the WHOLE plan before touching anything -------------------
    errors, todo, done = [], [], []
    for s, d in plan:
        err = validate_target(d, root)
        if err:
            errors.append(err)
            continue
        if os.path.exists(d):
            if os.path.samefile(s, d):
                done.append((s, d))
            else:
                errors.append(f"target exists and is a DIFFERENT file: {d}")
            continue
        bo = branch_of(s)
        if bo is None:
            errors.append(f"cannot resolve mergerfs branch for {s}")
            continue
        branch, src_on_branch = bo
        # Same relative path, but rooted at the source's own branch.
        dst_on_branch = os.path.join(branch, os.path.relpath(d, FUSION))
        todo.append((s, d, src_on_branch, dst_on_branch, branch))

    for s, d, _, _, branch in todo:
        print(f"  ln {os.path.basename(s)}\n   -> {d}  [{os.path.basename(branch)}]")
    for _, d in done:
        print(f"  (already linked) {os.path.basename(d)}")
    for f, why in skipped:
        print(f"  SKIP {f}  [{why}]")
    if errors:
        print("\nERRORS — nothing was linked:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{len(todo)} to link, {len(done)} already linked, {len(skipped)} skipped")
    if not a.apply:
        print("(dry run — pass --apply to link)")
        return

    linked = 0
    for s, d, src_on_branch, dst_on_branch, _ in todo:
        # Create the dir and the link on the branch itself, so mergerfs's mfs
        # create-policy can't place them somewhere the link would fail (EXDEV).
        os.makedirs(os.path.dirname(dst_on_branch), exist_ok=True)
        os.link(src_on_branch, dst_on_branch)
        # Verify through the POOL view: the link must be visible at the path
        # Jellyfin will actually read, share the source's inode, and the source
        # must still be there (seed intact).
        if not (os.path.exists(s) and os.path.exists(d) and os.path.samefile(s, d)):
            sys.exit(f"ERROR: verification failed for {d}")
        linked += 1
    print(f"linked {linked} file(s); sources untouched (seeds intact)")


if __name__ == "__main__":
    main()
