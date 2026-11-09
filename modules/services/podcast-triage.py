"""podcast-triage — surface the occasional worthwhile episode from shows Chris
does NOT listen to.

Four Bitcoin shows (TFTC, Rabbit Hole Recap, Citadel Dispatch, Bitcoin
Explained) are archived purely as a mining ground: Chris's own description is
that they carry "way too much opinion and honestly slightly arrogant junk, but
genuinely good content comes through now and again". The archive exists to find
that signal without him having to sit through the rest.

This is the CHEAP half — deterministic scoring over the whole corpus to rank
candidates. Judging substance-versus-punditry is not something keywords can do,
so the top of this ranking is handed to `claude -p` (see podcast-triage.nix),
which reads the transcripts and writes the actual recommendation.

Design notes:
  - the interest profile is DATA, not code: /var/lib/podcast-triage/interests.json
    is seeded from the packaged default and is meant to be edited. Scoring you
    cannot tune is scoring you stop trusting.
  - scores are normalised by transcript length. Without that, a rambling 3-hour
    episode outranks a tight 40-minute technical one purely on volume, which is
    exactly backwards for this use case.
  - already-triaged episodes are recorded so weekly runs only surface new ones.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("TRIAGE_STATE", "/var/lib/podcast-triage"))
SEEN_FILE = STATE_DIR / "triaged.json"
PROFILE_FILE = STATE_DIR / "interests.json"
DEFAULT_PROFILE = Path(os.environ.get("TRIAGE_DEFAULT_PROFILE", "")) \
    if os.environ.get("TRIAGE_DEFAULT_PROFILE") else None


def load_profile() -> dict:
    """State-dir profile wins; packaged default seeds it on first run."""
    if PROFILE_FILE.exists():
        return json.loads(PROFILE_FILE.read_text())
    if DEFAULT_PROFILE and DEFAULT_PROFILE.exists():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        text = DEFAULT_PROFILE.read_text()
        PROFILE_FILE.write_text(text)
        return json.loads(text)
    raise SystemExit("podcast-triage: no interest profile found")


def load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(SEEN_FILE)


def parse_episode(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta: dict = {"path": str(path), "title": path.stem, "date": ""}
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end > 0:
            for line in raw[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
            raw = raw[end + 4:]
    links = re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", raw)
    meta["links"] = [t for t, _ in links][:40]
    meta["body"] = raw
    meta["words"] = len(raw.split())
    return meta


def score_episode(meta: dict, profile: dict) -> tuple[float, list[str]]:
    """Positive terms lift, negative terms suppress, normalised by length."""
    blob = (meta.get("title", "") + " " + " ".join(meta.get("links", []))
            + " " + meta.get("body", "")).lower()
    words = max(meta.get("words", 1), 1)
    hits: dict[str, int] = {}
    pos = 0.0
    for term, weight in profile.get("interests", {}).items():
        n = blob.count(term.lower())
        if n:
            hits[term] = n
            # Diminishing returns: a term said 50 times is not 50x the signal
            # of one said once, it just means it is the episode's topic.
            pos += weight * (1 + (n - 1) ** 0.5)
    neg = 0.0
    for term, weight in profile.get("penalties", {}).items():
        n = blob.count(term.lower())
        if n:
            neg += weight * (1 + (n - 1) ** 0.5)
    # Per-1000-words so a long ramble does not beat a tight technical episode.
    density = (pos - neg) / (words / 1000.0) if words > 400 else 0.0
    # Title matches are worth more: they signal what the episode is ABOUT.
    title = meta.get("title", "").lower()
    title_bonus = sum(w * 2 for t, w in profile.get("interests", {}).items()
                      if t.lower() in title)
    top = sorted(hits.items(), key=lambda kv: -kv[1])[:8]
    return round(density + title_bonus, 2), [f"{k}x{v}" for k, v in top]


def main() -> int:
    args = sys.argv[1:]
    limit = 25
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    archives = [a for a in args if "/" in a]
    if not archives:
        archives = os.environ.get("TRIAGE_ARCHIVES", "").split()
    if not archives:
        print("usage: podcast-triage [--limit N] <archive-dir>...", file=sys.stderr)
        return 2

    profile = load_profile()
    seen = load_seen()
    cands = []
    scanned = 0
    for adir in archives:
        epdir = Path(adir) / "episodes"
        if not epdir.is_dir():
            print(f"podcast-triage: {epdir} missing — skipping", file=sys.stderr)
            continue
        show = Path(adir).name.replace("-archive", "")
        for path in sorted(epdir.glob("*.md")):
            key = f"{show}/{path.name}"
            if key in seen:
                continue
            meta = parse_episode(path)
            if not meta or meta["words"] < 400:
                seen.add(key)
                continue
            scanned += 1
            score, why = score_episode(meta, profile)
            cands.append({
                "key": key, "show": show, "title": meta.get("title", path.stem),
                "date": meta.get("date", ""), "path": str(path),
                "words": meta["words"], "score": score, "signals": why,
                "links": meta.get("links", [])[:12],
            })

    cands.sort(key=lambda c: -c["score"])
    top = cands[:limit]

    # Everything scanned is now triaged — including the rejects, so they are
    # never reconsidered. Only the shortlist goes forward.
    for c in cands:
        seen.add(c["key"])
    save_seen(seen)

    out = {"scanned": scanned, "shortlisted": len(top), "candidates": top}
    print(json.dumps(out, indent=1))
    print(f"podcast-triage: scanned {scanned} new episode(s), "
          f"shortlisted {len(top)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
