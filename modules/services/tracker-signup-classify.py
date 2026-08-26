"""Classify open-signup announcements into RECOMMEND / SKIP, with evidence.

Reads a feed on stdin, writes one TAB-separated record per candidate to stdout:

    key <TAB> verdict <TAB> reason <TAB> name <TAB> focus <TAB> signup <TAB> prowlarr

The caller (tracker-signup-watch) dedups on `key` and notifies only for
RECOMMEND and UNKNOWN. SKIP rows are journalled, never pushed.

WHY THIS EXISTS: the bare watcher pinged for every open signup with nothing but
a title, so most alerts were for adult/sports/regional trackers, paid "donation"
signups, or trackers already in the lineup. Chris asked for the name, the size,
a fit judgement, and a ping only when it is actually worth taking.

ON TORRENT COUNTS -- deliberately absent, not forgotten. There is no honest
source. opentrackers.org posts carry no stats (verified across a full 20-item
feed), and the tracker sites themselves are login-gated (seedcore.net answers
302/403 to anonymous fetches). Rather than fabricate a number or scrape behind
a login, fit is judged on evidence that IS real and local: Prowlarr's 582
bundled definitions, which state each tracker's content focus, language and
type. A tracker too small or too obscure to have a Prowlarr definition is
reported as such, which is the honest available proxy for "is this worth it".

FAIL-OPEN BY DESIGN: anything that cannot be classified confidently emits
UNKNOWN, which still notifies. A signup window is ~3 days; silently swallowing
one because a regex missed is far worse than one extra ping. Every SKIP must be
a positive, stated reason -- never the absence of a match.
"""

import html
import json
import os
import re
import subprocess
import sys

# Size gates, in torrents. A tracker in a category we already cover has to be
# big enough to be worth another account and another set of H&R obligations;
# one that fills the books/audiobooks gap is worth taking at any size. Both are
# tunable from the module -- the right bar is a judgement call, and the point
# of extracting real numbers is that it can now be argued about with evidence.
MIN_TORRENTS = int(os.environ.get("MIN_TORRENTS", "5000"))
NOTABLE_TORRENTS = int(os.environ.get("NOTABLE_TORRENTS", "50000"))

EXTRACTOR = os.environ.get("EXTRACTOR", "")

# --- fit policy -----------------------------------------------------------
# Categories that are simply not what Chris collects. Hard reject: these are
# the bulk of the noise (the feed is a firehose for every tracker alive).
ADULT = {"porn", "xxx", "hentai", "jav", "gay", "adult"}
OFFTOPIC = {"sports", "racing", "software", "mac", "apple", "wrestling", "mma"}

# The one real gap in the lineup: books/audiobooks. Anything landing here is
# worth waking up for even though the movie/TV side is saturated.
GAP = {
    "audiobooks", "audiobook", "e-learning", "elearning", "ebooks", "e-books",
    "books", "book", "literature", "comics", "magazines",
}

# Already well covered -- five private movie/TV/general trackers as of
# 2026-08-25. Another general tracker adds ~nothing, so these only earn a ping
# when the tracker is explicitly watchlisted.
SATURATED = {"general", "0day", "movies", "tv", "hd", "video", "uhd", "bluray"}

# Region-skewed trackers carry little English movie/TV. This is the SeedCore
# lesson generalised (Romanian general tracker, ~398 torrents, rejected twice).
FOREIGN_TAGS = {
    "romanian", "polish", "hebrew", "bollywood", "desi", "hindi", "asian",
    "turkish", "greek", "russian", "chinese", "japanese", "korean", "french",
    "german", "spanish", "italian", "brazilian", "portuguese", "swedish",
    "danish", "norwegian", "finnish", "dutch", "czech", "hungarian",
    "bulgarian", "serbian", "croatian", "ukrainian", "arabic", "persian",
    "thai", "vietnamese", "indonesian",
}


def norm(s):
    """Squash to comparable form: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def base(s):
    """Drop trailing parentheticals before comparing.

    Feed titles carry the abbreviation -- "DigitalCore (DC)" -- while Prowlarr
    names carry the protocol -- "DigitalCore (API)". Comparing them raw makes
    every tracker look unknown and un-held, which is how a tracker already in
    the lineup got reported as merely "saturated" during testing.
    """
    s = re.sub(r"\([^)]*\)", " ", s or "")
    # Reddit titles name the site ("SeedCore.net") while Prowlarr names the
    # tracker ("SeedCore"), so a TLD alone would defeat every lookup.
    s = re.sub(r"\.(net|org|com|club|io|me|to|cc|tv|eu|ro|se|is|ws|sx)\b", " ",
               s, flags=re.I)
    return norm(s)


def load_catalog(path):
    """Map normalised tracker name -> (display name, description, language).

    Source is Prowlarr's /api/v1/indexer/schema, NOT the Definitions directory.
    That distinction matters: the directory holds only the YAML (Cardigann)
    indexers, while the API also lists the built-in ones. MyAnonaMouse and
    IPTorrents -- the two Chris most wants -- have no YAML file, so a
    directory scan reports them as unsupported. Verified 2026-08-25:
    625 via the API vs 582 on disk.
    """
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("name") or r.get("definitionName") or ""
        entry = (name, r.get("description") or "", r.get("language") or "")
        for k in {norm(name), base(name), norm(r.get("definitionName") or "")}:
            if k and k not in out:
                out[k] = entry
    return out


def parse_items(xml):
    """Yield (title, body_text, categories[], link) per feed entry.

    Handles BOTH shapes: RSS <item> (opentrackers.org) and Atom <entry>
    (old.reddit). Reddit serves Atom, so an RSS-only parser silently yields
    nothing -- a zero-result that looks identical to "no openings today".
    """
    blocks = (re.findall(r"<item>(.*?)</item>", xml, re.S)
              or re.findall(r"<entry>(.*?)</entry>", xml, re.S))
    for raw in blocks:
        def field(tag):
            m = re.search(
                r"<" + tag + r"(?:\s[^>]*)?>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</"
                + tag + r">", raw, re.S)
            return html.unescape(m.group(1)) if m else ""

        title = re.sub(r"\s+", " ", field("title")).strip()
        body_html = (field("content:encoded") or field("description")
                     or field("content"))
        body = re.sub(r"<[^>]+>", " ", html.unescape(body_html))
        body = re.sub(r"\s+", " ", body).strip()
        cats = [html.unescape(c).strip().lower() for c in re.findall(
            r"<category[^>]*>(?:<!\[CDATA\[)?([^<\]]+)", raw)]
        yield title, body, cats, field("link").strip()


def tracker_name(title):
    """Strip the announcement suffix to leave the tracker's own name.

    Covers the phrasings actually seen in both feeds: "X (AB) is Open for
    Limited Signup!", "X Opens for Application Signup", "X.net | Open Signup
    for 3 Days!", "X 2nd Anniversary Open Signup".
    """
    t = re.sub(r"\s+(is|has)\s+(open|shut).*$", "", title, flags=re.I)
    t = re.sub(r"\s+opens?\s+(for|its)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*[-|:]\s*open.*$", "", t, flags=re.I)
    t = re.sub(r"\b(open|free)\s+(sign.?ups?|registrations?).*$", "", t, flags=re.I)
    t = re.sub(r"\s+(sign.?ups?|registrations?)\s+(are|is)\s+open.*$", "", t, flags=re.I)
    return t.strip(" -–—:|!,") or title


def abbrev(name):
    m = re.search(r"\(([A-Za-z0-9]{2,6})\)", name)
    return m.group(1) if m else ""


def signup_kind(title, cats):
    joined = " ".join(cats)
    for tag, label in (("donation signup", "donation (PAID)"),
                       ("application signup", "application"),
                       ("limited signup", "limited registration"),
                       ("open signup", "registration")):
        if tag in joined:
            return label
    if re.search(r"application", title, re.I):
        return "application"
    if re.search(r"donation", title, re.I):
        return "donation (PAID)"
    return "registration"


def is_announcement(title, body, cats):
    """True only for a post announcing a signup that is OPEN right now."""
    if "shut down" in " ".join(cats) or re.search(r"has shut down", title, re.I):
        return False
    if re.search(r"signup has closed|signups? (are|is) closed", body, re.I):
        return False
    if re.search(r"open for .{0,20}signup|open sign.?ups?|"
                 r"sign.?ups? (are |is )?open|open registration", title, re.I):
        return True
    return bool(re.search(r"(donation|application|limited|open) signup",
                          " ".join(cats)))


def classify(name, cats, defn, lineup, watchlist, kind, prowlarr_ok):
    """Return (verdict, reason). Every SKIP states a positive reason.

    `prowlarr_ok` is False when Prowlarr could not be queried. In that state the
    lineup is unknown -- NOT empty -- so no verdict may lean on it. An
    unreachable Prowlarr must never read as "we own nothing" (which would
    over-recommend) nor as "nothing is worth it" (which would silently drop a
    live window). Unclassifiable then means UNKNOWN, which still notifies.
    """
    catset = set()
    for c in cats:
        catset.update(re.split(r"[\s/,]+", c))

    # Reddit's Atom <category> is the SUBREDDIT, not the content type, so
    # Reddit-sourced items arrive with no usable categories at all. Recover the
    # content type from Prowlarr's description ("... Tracker for HD MOVIES /
    # TV"). Kept in a SEPARATE set used only for the saturation test: folding it
    # into catset would let a general tracker that merely lists SPORTS among its
    # categories trip the hard adult/off-topic rejects.
    desc_tokens = set()
    if defn and defn[1]:
        m = re.search(r"[Tt]racker for ([A-Za-z0-9 /&+-]+)", defn[1])
        if m:
            desc_tokens = {t for t in re.split(r"[\s/,&+-]+", m.group(1).lower()) if t}

    n_name, n_abbr = base(name), norm(abbrev(name))

    if catset & ADULT:
        return "SKIP", "adult content - outside the collection"
    if catset & OFFTOPIC:
        return "SKIP", "sports/software - outside the collection"
    if "PAID" in kind:
        return "SKIP", "donation signup - costs money, and free windows recur"

    # Held-already check runs before every "worth it" test: owning it is a
    # complete answer regardless of how attractive it would otherwise look.
    for held in lineup:
        h = base(held)
        if not h or not n_name:
            continue
        if h == n_name or (n_abbr and h == n_abbr) or (len(h) > 4 and h in n_name):
            return "SKIP", "already in the Prowlarr lineup as '%s'" % held

    for w in watchlist:
        if re.search(w.get("regex", r"(?!)"), name, re.I):
            return "RECOMMEND", "on the watchlist (%s) - explicitly wanted" % w.get("name", "?")

    if catset & GAP:
        return "RECOMMEND", "books/audiobooks - the one gap in the lineup"

    desc = defn[1] if defn else ""
    if re.search(r"audiobook|e-?book|e-?learning|comics|magazines",
                 desc, re.I):
        return "WANTED", "definition says books/audiobooks - fills the gap"

    # NOTE: deliberately no language decision here. Prowlarr's `language` field
    # and the feed's region tags describe who RUNS a tracker, not what is on
    # it -- SeedCore is tagged ro-RO while its content and site are English,
    # and skipping on that tag was wrong twice. Content language is decided
    # after extraction, from what the announcement itself says.

    # Requires a REAL saturated category, not merely the absence of anything
    # else: a post tagged only "open signup" carries no content information at
    # all, and treating that as "general, already covered" would silently drop
    # an unknown tracker. Caught in testing by a bare-category fixture.
    noise = {"limited", "signup", "signups", "application", "open", "opensignups",
             "registration", "internal", "donation", "anniversary", "days"}
    is_saturated = ((catset & SATURATED) and catset <= (SATURATED | noise))
    # Description-derived fallback for feeds that carry no content categories.
    if not is_saturated and desc_tokens:
        is_saturated = (desc_tokens & SATURATED) and desc_tokens <= (SATURATED | noise)
    if is_saturated:
        return "CANDIDATE", "general/movie/TV - size and language decide it"

    # Nothing matched from the feed alone. Worth a look, and worth the numbers.
    return "CANDIDATE", "could not classify from the feed alone"


def final_verdict(stage, reason, stats, lineup, prowlarr_ok):
    """Decide using the extracted stats. Never silently drops a live window.

    `stage` is the prescreen outcome: WANTED (watchlisted, or fills the
    books/audiobooks gap -- taken at any size) or CANDIDATE (needs numbers).
    """
    lang = (stats.get("language") or "").strip()
    torrents = stats.get("torrents")

    # Language as the announcement states it, not as a tag describes the staff.
    if lang and not re.match(r"(?i)english|en$|en[-_]", lang):
        return "SKIP", "content language is %s - little English movie/TV" % lang

    if stage == "WANTED":
        return "RECOMMEND", reason

    if torrents is None:
        # Distinguish "the post states no size" from "we could not ask".
        # Reporting the former when the latter is true sends the reader after
        # the wrong fault -- the same misattribution that cost real debugging
        # time in pool-autoremount's "device not ready" message.
        why = ("could not extract stats (extractor unavailable)"
               if stats.get("_extractor_failed")
               else "no size stated in the post")
        if not prowlarr_ok:
            why += "; Prowlarr also unreachable"
        return "UNKNOWN", why + " - reporting rather than dropping"

    if torrents < MIN_TORRENTS:
        return "SKIP", ("only %s torrents - too small to be worth an account"
                        % format(torrents, ","))
    if torrents < NOTABLE_TORRENTS:
        return "SKIP", ("%s torrents - below the %s bar for another general "
                        "tracker (already have %d private)"
                        % (format(torrents, ","), format(NOTABLE_TORRENTS, ","),
                           len(lineup)))
    return "RECOMMEND", ("%s torrents, English - large enough to add on top of "
                         "%d private tracker(s)"
                         % (format(torrents, ","), len(lineup)))


def extract_stats(body):
    """Run the LLM extractor. Returns {} on any failure -- never raises.

    Called only for candidates that survived the cheap deterministic screen,
    so adult/sports/paid/already-held trackers cost nothing at all.
    """
    if not EXTRACTOR or not body:
        return {"_extractor_failed": True}
    try:
        proc = subprocess.run([sys.executable, EXTRACTOR], input=body,
                              capture_output=True, text=True, timeout=300)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.returncode != 0 or not proc.stdout.strip():
            sys.stderr.write("classify: extractor exited %d with no result\n"
                             % proc.returncode)
            return {"_extractor_failed": True}
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        sys.stderr.write("classify: extractor unavailable (%s)\n" % e)
        return {"_extractor_failed": True}


def render_stats(stats):
    """One-line human summary. Says 'not stated' rather than implying zero."""
    if not stats:
        return "not stated in the post"
    bits = []
    if stats.get("torrents") is not None:
        bits.append("%s torrents" % format(stats["torrents"], ","))
    if stats.get("users") is not None:
        bits.append("%s users" % format(stats["users"], ","))
    if stats.get("size_tib") is not None:
        bits.append("%.5g TiB" % stats["size_tib"])
    if stats.get("seeders") is not None:
        s = "%s seeders" % format(stats["seeders"], ",")
        if stats.get("leechers") is not None:
            s += "/%s leechers" % format(stats["leechers"], ",")
        bits.append(s)
    return " | ".join(bits) if bits else "not stated in the post"


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else "feed"
    defs = load_catalog(os.environ.get("CATALOG_FILE", ""))
    try:
        lineup = json.loads(os.environ.get("LINEUP_JSON", "[]"))
    except ValueError:
        lineup = []
    # A populated catalog is the proof Prowlarr answered. An empty one is
    # ambiguous (unreachable vs genuinely nothing), so it is treated as
    # unreachable -- the conservative reading, since it only costs extra pings.
    prowlarr_ok = bool(defs)
    try:
        watchlist = json.loads(os.environ.get("WATCHLIST_JSON", "[]"))
    except ValueError:
        watchlist = []

    # Read the caller's dedup list so an already-alerted candidate never costs
    # a second LLM call. The caller still owns notification dedup; this is
    # purely a cost gate, and a missing file simply means "nothing seen yet".
    already_seen = set()
    try:
        with open(os.environ.get("SEEN_FILE", ""), "r", encoding="utf-8") as fh:
            already_seen = {ln.strip() for ln in fh if ln.strip()}
    except OSError:
        pass

    for title, body, cats, link in parse_items(sys.stdin.read()):
        if not title or not is_announcement(title, body, cats):
            continue

        name = tracker_name(title)
        defn = (defs.get(base(name)) or defs.get(norm(name))
                or defs.get(norm(abbrev(name))) or None)
        kind = signup_kind(title, cats)
        verdict, reason = classify(name, cats, defn, lineup, watchlist, kind,
                                   prowlarr_ok)

        # Two-phase by design. The cheap deterministic screen above rejects
        # adult/sports/paid/already-held trackers for free; only what survives
        # is worth an LLM call. Measured on the live feeds that is 2 of 22, so
        # the extractor runs on the handful that could actually matter.
        stats = {}
        if verdict in ("WANTED", "CANDIDATE"):
            seen_key = "%s:%s" % (source, norm(title))
            if seen_key in already_seen:
                # Already alerted on; re-extracting would spend tokens to
                # re-derive a decision nobody will be shown again.
                continue
            stats = extract_stats(body)
            verdict, reason = final_verdict(verdict, reason, stats, lineup,
                                            prowlarr_ok)

        # Focus: prefer Prowlarr's curated description, fall back to the post's
        # own "is a Private Torrent Tracker for X" line, then to categories.
        focus = ""
        if defn and defn[1]:
            focus = defn[1]
        else:
            # Stop at the last all-caps token so the next sentence's leading
            # capital is not swallowed ("... for 0DAY / GENERAL T<racker>").
            m = re.search(r"is an? (.*?Tracker for (?:[A-Z0-9&+-]+(?:\s*/\s*)?)+)", body)
            focus = m.group(1).strip() if m else (", ".join(cats[:4]) or "unstated")
            focus = re.sub(r"\s+[A-Z]$", "", focus)

        # Prefer the announcement's own content description over Prowlarr's
        # curated one: the post says what is on the tracker today.
        if stats.get("content"):
            focus = stats["content"]

        row = [
            "%s:%s" % (source, norm(title)),
            verdict,
            reason,
            name,
            re.sub(r"\s+", " ", focus)[:160],
            kind,
            ("supported" if defn else
             ("unknown - Prowlarr unreachable" if not prowlarr_ok
              else "NOT in Prowlarr - manual wiring only")),
            render_stats(stats),
            stats.get("hit_and_run") or "not stated",
        ]
        sys.stdout.write("\t".join(c.replace("\t", " ") for c in row) + "\n")


if __name__ == "__main__":
    main()
