# tracker-signup-watch — low-frequency watcher that pings ntfy when a private
# tracker opens public signup AND that tracker is actually worth taking.
#
# WHY: the good private trackers only open registration a few times a year for
# short windows. Rather than have Chris check manually, this catches the window.
# Built 2026-07-28 to leverage the AirVPN forwarded port (private trackers = the
# payoff for port forwarding).
#
# ⚠️ REWORKED 2026-08-25 (Chris: "it's not valuable to keep getting
# notifications of trackers without any sort of information about them ... only
# recommend it if it's worth getting"). The original pinged on a bare TITLE
# match, so nearly every alert was noise: adult/sports/regional trackers, paid
# "donation" signups, and trackers already in the lineup. Measured against the
# live feeds at rework time: 22 open-signup posts, of which 20 were junk for
# Chris. It now classifies each candidate and pushes only what survives.
#
# Every notification carries: tracker name, content focus, signup type, whether
# Prowlarr can even index it, and the one-line reason for the verdict.
#
# ⚠️ SIZE COMES FROM THE POST, VIA AN LLM — corrected 2026-08-25. An earlier
# revision of this module claimed no honest source for torrent counts existed.
# That was wrong: r/OpenSignups posts carry a stats block (18 of 19 real
# announcements), and it was sitting in the Atom <content> this module already
# fetched. Only opentrackers.org lacks stats; that finding was over-generalised
# onto Reddit after inspecting titles and never bodies.
#
# The counts are extracted by `claude -p` (Sonnet), not a regex, because these
# blocks are free-form and emoji-laden. A proximity regex over them is not just
# imprecise, it is confidently wrong: measured against the live feed it read
# Seedpool as "4.19 torrents / 230,050 users" — the first being its 4.19 PiB
# size and the second a per-category TV-show count — and read minimum-ratio
# requirements as tracker sizes. Sonnet got all of those right, converted
# PiB→TiB correctly, and invented nothing on a post with no stats.
#
# EVERY EXTRACTED NUMBER IS VALIDATED before use — type, physical range,
# integrality for counts, and a mean-torrent-size cross-check that catches a
# confidently-paired hallucination. A field that fails is dropped and reported
# as "not stated", which is always safe. See tracker-signup-extract.py.
#
# The LLM also answers the one question no tag lookup gets right: a tracker run
# by a Romanian team whose content and site are English is an ENGLISH tracker.
# Prowlarr's `language: ro-RO` describes the operators, and trusting it is what
# made this module skip SeedCore for a reason that was not true.
#
# ⚠️ Use the Prowlarr /api/v1/indexer/schema API, NOT /var/lib/prowlarr/
# Definitions. The directory holds only the YAML (Cardigann) indexers; the API
# also lists built-ins. MyAnonaMouse and IPTorrents — the two Chris most wants —
# have NO YAML file, so a directory scan reports them unsupported. 625 via API
# vs 582 on disk.
#
# TRAFFIC ETHOS (Chris, 2026-07-28: "AI-driven traffic is weighing down the
# internet — don't contribute to that"): unchanged and still light. Two RSS
# GETs every 3h; the added Prowlarr calls are localhost. Not the tracker sites,
# no headless browser; identifies itself in the User-Agent.
#
# FAIL-OPEN: an unclassifiable post notifies as UNKNOWN rather than being
# dropped. A window is ~3 days; one extra ping costs nothing, a silently
# swallowed opening costs the window. Likewise an unreachable Prowlarr never
# reads as "we own nothing" or "nothing is worth it" — see the classifier.
{ config, lib, pkgs, ... }:

let
  # Trackers Chris explicitly wants — a match here is an automatic RECOMMEND,
  # bypassing the saturation rule. Curated for his ask (movies/TV, low-friction
  # entry: open registration OR open application, no invite/IRC-interview).
  watchlist = [
    { name = "IPTorrents";    regex = "iptorrents|\\(IPT\\)"; }       # classic low-friction general
    { name = "TorrentDay";    regex = "torrentday|\\(TD\\)"; }        # general, opens periodically
    { name = "AlphaRatio";    regex = "alpharatio|\\(AR\\)"; }        # general
    { name = "Blutopia";      regex = "blutopia|\\(BLU\\)"; }         # UNIT3D, HD movies/TV
    { name = "Aither";        regex = "aither"; }                     # UNIT3D, movies/TV
    { name = "Anthelion";     regex = "anthelion|\\(ATH\\)"; }        # UNIT3D, movies
    { name = "FileList";      regex = "filelist|\\(FL\\)"; }          # general, application
    { name = "ReelFliX";      regex = "reelflix|\\(RF\\)"; }          # UNIT3D, movies
    { name = "MyAnonaMouse";  regex = "myanonamouse|anonamouse|\\(MAM\\)"; }  # THE book/audiobook tracker
    # Trackers Chris now HOLDS are no longer listed or hand-removed: the
    # classifier suppresses anything already configured in Prowlarr by querying
    # the live lineup. That is what previously required manual edits here
    # (TorrentLeech + DigitalCore removed 2026-08-08 by hand) and it now
    # maintains itself. SpeedApp/SuperBits likewise need no entry — both are
    # non-English and get skipped on language.
  ];

  watchJson = pkgs.writeText "tracker-watch.json" (builtins.toJSON watchlist);
  classifier = ./tracker-signup-classify.py;
  extractor = ./tracker-signup-extract.py;

  # Size gates in torrents, used only for trackers in a category already
  # covered. A tracker that fills the books/audiobooks gap is taken at any
  # size. These are judgement calls, deliberately surfaced here so they can be
  # argued about — which is the whole point of extracting real numbers.
  minTorrents = 5000;       # below this, not worth an account at all
  notableTorrents = 50000;  # below this, not worth ANOTHER general tracker

  watcher = pkgs.writeShellApplication {
    name = "tracker-signup-watch";
    runtimeInputs = [ pkgs.curl pkgs.gnugrep pkgs.gnused pkgs.coreutils pkgs.jq pkgs.python3 ];
    text = ''
      STATE="''${STATE_DIR:-/var/lib/tracker-signup-watch}"
      SEEN="$STATE/seen"            # candidates already alerted on (dedup)
      NTFY="http://127.0.0.1:8090/gromit-alerts"
      UA='rosemaryacres homelab tracker-signup-watch/1.0 (contact: chris)'
      mkdir -p "$STATE"; touch "$SEEN"

      WORK="$(mktemp -d)"
      trap 'rm -rf "$WORK"' EXIT

      # ── Prowlarr context: the catalog of supported indexers (fit + support)
      #    and the PRIVATE indexers already configured (so a tracker Chris
      #    already holds is never offered again). The API key is handed over by
      #    systemd LoadCredential — the service is DynamicUser and cannot read
      #    /var/lib/prowlarr itself (0750 chris:users).
      CATALOG="$WORK/catalog.json"
      LINEUP_JSON='[]'
      PROWLARR_NOTE=""
      key=""
      if [ -r "''${CREDENTIALS_DIRECTORY:-/nonexistent}/prowlarr-config" ]; then
        key="$(grep -oE '<ApiKey>[^<]+' "$CREDENTIALS_DIRECTORY/prowlarr-config" | sed 's/<ApiKey>//' || true)"
      fi
      if [ -n "$key" ]; then
        curl -sS --max-time 15 "http://127.0.0.1:9696/api/v1/indexer/schema" \
          -H "X-Api-Key: $key" -o "$CATALOG" 2>/dev/null || true
        LINEUP_JSON="$(curl -sS --max-time 15 "http://127.0.0.1:9696/api/v1/indexer" \
          -H "X-Api-Key: $key" 2>/dev/null \
          | jq -c '[.[] | select(.privacy=="private") | .name]' 2>/dev/null || echo '[]')"
      fi
      # Guard the ambiguous-empty case explicitly: a missing/!valid catalog must
      # be reported, not silently treated as "no tracker is supported".
      if ! jq -e 'length > 0' "$CATALOG" >/dev/null 2>&1; then
        : > "$CATALOG"
        PROWLARR_NOTE=" (NOTE: Prowlarr unreachable — lineup/support unverified)"
        echo "WARNING: Prowlarr catalog unavailable; classification degraded to fail-open"
      fi
      echo "lineup (private): $LINEUP_JSON"

      # dedup + notify. Sends only the FIRST time a candidate is seen. On a
      # source's FIRST-EVER run (PRIME=1) it seeds the seen-list SILENTLY, so
      # Chris never gets a backfill dump of posts already in the feed.
      alert() {  # $1=dedup-key $2=priority $3=title $4=body $5=click-url
        grep -qxF "$1" "$SEEN" && return 0
        echo "$1" >> "$SEEN"
        [ "''${PRIME:-0}" = "1" ] && return 0
        # Click: makes the whole notification tap through to the announcement
        # thread. Sent ONLY when a url is present — an empty Click header is
        # malformed rather than absent, so it must not be emitted blank.
        hdrs=( -H "Title: $3" -H "Tags: tada,inbox_tray" -H "Priority: $2" )
        if [ -n "''${5:-}" ]; then
          hdrs+=( -H "Click: $5" )
        fi
        curl -s --max-time 10 "''${hdrs[@]}" -d "$4" "$NTFY" >/dev/null || true
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

      # Classify one feed and push only what is worth Chris's attention.
      # SKIP rows are journalled with their reason and never notified, so a
      # wrong suppression is always auditable after the fact.
      run_source() {  # $1=source-id  $2=human label  $3=url  $4=primed-flag
        body="$(curl -sS --max-time 25 -A "$UA" "$3" 2>/dev/null || true)"
        if [ -z "$body" ]; then
          note_fail "$1" "$2"
          return 0
        fi
        echo 0 > "$STATE/fail_$1"
        PRIME=0; [ -f "$STATE/$4" ] || PRIME=1
        export PRIME

        printf '%s' "$body" \
          | CATALOG_FILE="$CATALOG" LINEUP_JSON="$LINEUP_JSON" \
            WATCHLIST_JSON="$(cat ${watchJson})" \
            SEEN_FILE="$SEEN" EXTRACTOR=${extractor} \
            MIN_TORRENTS=${toString minTorrents} \
            NOTABLE_TORRENTS=${toString notableTorrents} \
            python3 ${classifier} "$1" > "$WORK/out.tsv" || true

        while IFS="$(printf '\t')" read -r k verdict reason name focus signup prowlarr stats hnr link; do
          [ -n "$k" ] || continue
          if [ "$verdict" = "SKIP" ]; then
            echo "SKIP  $name — $reason"
            continue
          fi
          prio=default; [ "$verdict" = "UNKNOWN" ] && prio=low
          echo "$verdict  $name — $reason  [$stats]"
          alert "$k" "$prio" "$verdict: $name signups OPEN" \
            "$name — $reason.
Size: $stats
Focus: $focus
Seeding rules: $hnr
Signup: $signup
Prowlarr: $prowlarr
Source: $2$PROWLARR_NOTE
Post: ''${link:-no link in feed}" \
            "$link"
        done < "$WORK/out.tsv"

        touch "$STATE/$4"
      }

      # Source A: opentrackers.org — the firehose; every tracker's every signup
      # event, with content categories and an explicit open/closed state.
      run_source opentrackers "opentrackers.org" 'https://opentrackers.org/feed/' primed_ot

      # Source B: r/OpenSignups via old.reddit (legacy UI, LIVE data — www .json
      # is 403 for us, old.reddit RSS returns 200 UA'd). Human-curated and
      # lower-volume, and it serves ATOM, not RSS. It carries no content
      # categories at all (its <category> is the subreddit), so fit for these
      # comes entirely from the Prowlarr catalog lookup.
      run_source reddit "r/OpenSignups" 'https://old.reddit.com/r/OpenSignups/new/.rss' primed_rd
    '';
  };

in
{
  # ⚠️ Left over from the DynamicUser era. When the unit moved to User=claude,
  # systemd migrated /var/lib/private/tracker-signup-watch out to
  # /var/lib/tracker-signup-watch but kept the OLD dynamic UID on the directory
  # and its contents (65534), so the very next run died on
  # `touch /var/lib/tracker-signup-watch/seen: Permission denied`. systemd only
  # chowns a StateDirectory it creates itself, not one it inherits — so state
  # ownership has to be asserted here. `Z` is recursive and the `-` mode leaves
  # each file's existing permissions alone; it only fixes the owner. Keeping the
  # existing `seen` file matters: losing it re-alerts on every candidate already
  # reported.
  systemd.tmpfiles.rules = [
    "Z /var/lib/tracker-signup-watch - claude users -"
  ];

  systemd.services.tracker-signup-watch = {
    description = "Notify when a WORTHWHILE private tracker opens public signup";
    serviceConfig = {
      Type = "oneshot";
      # NOT DynamicUser: `claude -p` authenticates with the claude user's
      # subscription OAuth credentials under /home/claude, exactly as the
      # newsdesk reader and the sentinel diagnosis stage do. A dynamic user has
      # no such profile and every extraction would fail.
      User = "claude";
      StateDirectory = "tracker-signup-watch";
      Environment = [
        "STATE_DIR=/var/lib/tracker-signup-watch"
        "HOME=/home/claude"
        # PATH is set wholesale because `claude` lives in the per-user profile
        # and is not on the default unit PATH. Setting Environment=PATH defeats
        # the systemd `path` option, so everything the script needs is listed.
        "PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin"
      ];
      # systemd reads this as root and drops a copy into $CREDENTIALS_DIRECTORY
      # readable only by this unit — so the Prowlarr API key arrives without
      # depending on the claude user's group membership on /var/lib/prowlarr.
      LoadCredential = "prowlarr-config:/var/lib/prowlarr/config.xml";
      # An LLM extraction is minutes, not seconds, when several candidates are
      # new at once; the default has no timeout but a stuck run should not sit
      # forever holding the timer's slot.
      TimeoutStartSec = "30min";
      ExecStart = "${watcher}/bin/tracker-signup-watch";
    };
  };

  systemd.timers.tracker-signup-watch = {
    description = "Tracker signup-window check (every 3h — responsive but light)";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      # Every 3h bounds the worst-case miss of a short window to ~3h while
      # staying trivially light (two ~50-90KB RSS GETs per run). Cadence matched
      # to how fast signup windows move, per the polite-polling ethos.
      OnCalendar = "*-*-* 00/3:00:00";
      RandomizedDelaySec = "20m";    # non-synchronized caller
      Persistent = true;             # catch up one missed run after downtime
    };
  };
}
