# tracker-signup-watch — low-frequency watcher that pings ntfy when a private
# tracker Chris cares about opens public signup.
#
# WHY: the good private trackers (TorrentLeech for movies/TV, etc.) only open
# registration a few times a year for short windows. Rather than have Chris
# check manually, this catches the window. Built 2026-07-28 to leverage the
# AirVPN forwarded port (private trackers = the payoff for port forwarding).
#
# TRAFFIC ETHOS (Chris, 2026-07-28: "AI-driven traffic is weighing down the
# internet — don't contribute to that"): deliberately light. Two lightweight RSS
# sources — opentrackers.org (watchlist-filtered) and r/OpenSignups via
# old.reddit (broad open-registration) — polled every 3h (cadence matched to how
# fast signup windows move; ~16 tiny GETs/day total). Not the tracker sites, no
# headless browser; identifies itself in the User-Agent. Notifies only on a NEW
# post (dedup), and PRIMES silently on first run so Chris never gets a backfill
# dump of already-posted openings — only genuinely new ones alert.
#
# old.reddit.com is Reddit's legacy UI, NOT stale data — the /new/.rss feed is
# live (www.reddit.com/.json is 403 for us; old.reddit RSS returns 200 UA'd).
{ config, lib, pkgs, ... }:

let
  # Trackers to watch: name shown in the alert + a case-insensitive regex that
  # must match the feed item title (word-boundary-ish on abbreviations to avoid
  # false hits). Add/remove entries here — no other change needed. Curated for
  # Chris's ask (movies/TV, low-friction entry: open registration OR open
  # application, no invite/IRC-interview). Expanded 2026-07-28 beyond just TL to
  # the reputable entry-level set + the UNIT3D-ecosystem trackers that cycle
  # through open-application windows. It's the same single feed fetch either way
  # (zero extra traffic), so widening coverage is free. Trim any you don't want.
  watchlist = [
    { name = "TorrentLeech";  regex = "torrentleech|\\(TL\\)"; }      # periodic open registration
    { name = "IPTorrents";    regex = "iptorrents|\\(IPT\\)"; }       # classic low-friction general
    { name = "TorrentDay";    regex = "torrentday|\\(TD\\)"; }        # general, opens periodically
    { name = "AlphaRatio";    regex = "alpharatio|\\(AR\\)"; }        # general
    { name = "DigitalCore";   regex = "digitalcore|\\(DC\\)"; }       # general
    { name = "Blutopia";      regex = "blutopia|\\(BLU\\)"; }         # UNIT3D, HD movies/TV
    { name = "Aither";        regex = "aither"; }                     # UNIT3D, movies/TV
    { name = "Anthelion";     regex = "anthelion|\\(ATH\\)"; }        # UNIT3D, movies
    { name = "FileList";      regex = "filelist|\\(FL\\)"; }          # general, application
    { name = "ReelFliX";      regex = "reelflix|\\(RF\\)"; }          # UNIT3D, movies
    # NOTE: SpeedApp + SuperBits removed 2026-07-28 — both confirmed to gate on
    # PROOF of an existing tracker account (SpeedApp: profile URL + screenshots,
    # form openly hostile to newbies; SuperBits: 2TB upload + >1.0 ratio on
    # TL/IPT/TD/etc.). Noise until Chris has a first account + ratio; re-add then.
    # The alert wording ("Open for Registration" = grab-and-go for a newbie, vs
    # "Application Signup" = may need prior-tracker proof) is the hint for the rest.
  ];

  watchJson = pkgs.writeText "tracker-watch.json" (builtins.toJSON watchlist);

  watcher = pkgs.writeShellApplication {
    name = "tracker-signup-watch";
    runtimeInputs = [ pkgs.curl pkgs.gnugrep pkgs.gnused pkgs.coreutils pkgs.jq ];
    text = ''
      STATE="''${STATE_DIR:-/var/lib/tracker-signup-watch}"
      SEEN="$STATE/seen"            # feed items already alerted on (dedup)
      NTFY="http://127.0.0.1:8090/gromit-alerts"
      UA='rosemaryacres homelab tracker-signup-watch/1.0 (contact: chris)'
      mkdir -p "$STATE"; touch "$SEEN"

      # dedup + notify: key is source-prefixed so the same tracker can ping from
      # both sources but never twice from one. Sends only the FIRST time a post
      # is seen. On a source's FIRST-EVER run (PRIME=1) it seeds the seen-list
      # SILENTLY — so Chris never gets a backfill dump of posts already sitting
      # in the feed; only genuinely NEW openings alert thereafter.
      alert() {  # $1=dedup-key  $2=ntfy-title  $3=ntfy-body
        grep -qxF "$1" "$SEEN" && return 0
        echo "$1" >> "$SEEN"
        [ "''${PRIME:-0}" = "1" ] && return 0
        curl -s --max-time 10 -H "Title: $2" -H "Tags: tada,inbox_tray" \
          -H "Priority: default" -d "$3" "$NTFY" >/dev/null || true
      }

      # per-source failure counter → one health notice after 4 straight misses
      # (no silent rot), independent so one source dying doesn't blind the other.
      note_fail() {  # $1=source-id  $2=human label
        f="$STATE/fail_$1"
        n="$(( $(cat "$f" 2>/dev/null || echo 0) + 1 ))"
        echo "$n" > "$f"
        if [ "$n" -eq 4 ]; then
          curl -s --max-time 10 -H "Title: signup-watch: $2 unreachable" -H "Tags: warning" \
            -d "$2 unreachable 4x running — that source is blind until it recovers (the other source still runs)." "$NTFY" >/dev/null || true
        fi
      }

      titles_of() { printf '%s' "$1" | grep -oiE '<title>[^<]+</title>' | sed -E 's|</?title>||g; s/&amp;/\&/g'; }

      # ── Source A: opentrackers.org RSS — high-volume firehose (every tracker's
      #    every signup event) → filter to the curated watchlist to avoid noise.
      a="$(curl -sS --max-time 25 -A "$UA" 'https://opentrackers.org/feed/' 2>/dev/null || true)"
      if [ -z "$a" ]; then
        note_fail opentrackers "opentrackers.org feed"
      else
        echo 0 > "$STATE/fail_opentrackers"
        PRIME=0; [ -f "$STATE/primed_ot" ] || PRIME=1   # first successful run: seed silently
        while IFS= read -r title; do
          [ -n "$title" ] || continue
          printf '%s' "$title" | grep -qiE 'open for .*signup|signup.*open|open signup' || continue
          while IFS= read -r rx; do
            printf '%s' "$title" | grep -qiE "$rx" || continue
            key="ot:$(printf '%s' "$title" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')"
            alert "$key" "Tracker signup OPEN: $title" \
              "$title — https://opentrackers.org/  (seed 24/7, AirVPN forwarded port ready)."
          done < <(jq -r '.[].regex' ${watchJson})
        done <<< "$(titles_of "$a")"
        touch "$STATE/primed_ot"
      fi

      # ── Source B: r/OpenSignups (old.reddit RSS — old.reddit works UA'd; www
      #    .json + all /about/*.json are 403 for us, so NO flair/rules access —
      #    the feed carries POST TITLES only, never comments). Human-curated +
      #    lower-volume, so match ANY post ANNOUNCING an open registration (not
      #    just the watchlist) to catch the long tail. This is TITLE PATTERN
      #    matching, not semantics, so precision comes from two filters:
      #      NEG_RE — drop questions / discussion / "missed it" laments / invite
      #               requests (the posts Chris explicitly doesn't want);
      #      POS_RE — require "open" and "signup/registration" IN PROXIMITY
      #               (≤12 chars), matching real announcements ("Open for Signup",
      #               "open registration", "Open SignUps") while rejecting posts
      #               where the words are just scattered in a sentence.
      #    Both verified against real feed titles + synthetic false positives.
      #    Residual false positives are possible (not semantic) — each pings once
      #    (dedup); add patterns to NEG_RE if a junk shape slips through.
      NEG_RE='\?|(^|[^a-z])(anyone|discussion|request|help|missed|closed|looking for|need (an? )?invite|want (an? )?invite|how (do|to|can)|when (will|does|is)|why (did|is|are))'
      POS_RE='(open[a-z]*.{0,12}(sign.?ups?|registration))|((sign.?ups?|registration).{0,12}open)'
      b="$(curl -sS --max-time 25 -A "$UA" 'https://old.reddit.com/r/OpenSignups/new/.rss' 2>/dev/null || true)"
      if [ -z "$b" ]; then
        note_fail reddit "r/OpenSignups (Reddit)"
      else
        echo 0 > "$STATE/fail_reddit"
        PRIME=0; [ -f "$STATE/primed_rd" ] || PRIME=1   # first successful run: seed silently
        while IFS= read -r title; do
          [ -n "$title" ] || continue
          printf '%s' "$title" | grep -qiE 'newest submissions' && continue   # feed header
          printf '%s' "$title" | grep -qiE "$NEG_RE" && continue              # question/discussion/lament/request
          printf '%s' "$title" | grep -qiE "$POS_RE" || continue              # a real open-signup announcement
          key="rd:$(printf '%s' "$title" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')"
          alert "$key" "r/OpenSignups: $title" \
            "$title — via r/OpenSignups: https://old.reddit.com/r/OpenSignups/new/  (check if it's open registration vs needs prior-tracker proof)."
        done <<< "$(titles_of "$b")"
        touch "$STATE/primed_rd"
      fi
    '';
  };

in
{
  systemd.services.tracker-signup-watch = {
    description = "Notify when a watched private tracker opens public signup";
    serviceConfig = {
      Type = "oneshot";
      DynamicUser = true;
      StateDirectory = "tracker-signup-watch";
      Environment = "STATE_DIR=/var/lib/tracker-signup-watch";
      ExecStart = "${watcher}/bin/tracker-signup-watch";
    };
  };

  systemd.timers.tracker-signup-watch = {
    description = "Tracker signup-window check (every 3h — responsive but light)";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      # Every 3h bounds the worst-case miss of a short window to ~3h while
      # staying trivially light (two ~50-90KB RSS GETs per run = ~16 tiny
      # fetches/day total). Cadence matched to how fast signup windows move,
      # per the polite-polling ethos — not "as rare as possible".
      OnCalendar = "*-*-* 00/3:00:00";
      RandomizedDelaySec = "20m";    # non-synchronized caller
      Persistent = true;             # catch up one missed run after downtime
    };
  };
}
