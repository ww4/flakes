# tracker-signup-watch — low-frequency watcher that pings ntfy when a private
# tracker Chris cares about opens public signup.
#
# WHY: the good private trackers (TorrentLeech for movies/TV, etc.) only open
# registration a few times a year for short windows. Rather than have Chris
# check manually, this catches the window. Built 2026-07-28 to leverage the
# AirVPN forwarded port (private trackers = the payoff for port forwarding).
#
# TRAFFIC ETHOS (Chris, 2026-07-28: "AI-driven traffic is weighing down the
# internet — don't contribute to that"): deliberately gentle. It polls ONE
# lightweight source — the opentrackers.org RSS feed, which exists precisely to
# announce open signups — TWICE a day (not the tracker sites directly, no
# headless browser). It notifies only on a NEW open announcement (dedup by feed
# item), so a week-long open window pings once, not every run. Identifies itself
# in the User-Agent. If you want it even lighter, drop the timer to daily.
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
    { name = "SpeedApp";      regex = "speedapp"; }                   # movies/TV/general, open application
    { name = "SuperBits";     regex = "superbits|\\(SBS\\)"; }        # general/scene, open application
  ];

  watchJson = pkgs.writeText "tracker-watch.json" (builtins.toJSON watchlist);

  watcher = pkgs.writeShellApplication {
    name = "tracker-signup-watch";
    runtimeInputs = [ pkgs.curl pkgs.gnugrep pkgs.gnused pkgs.coreutils pkgs.jq ];
    text = ''
      FEED="https://opentrackers.org/feed/"
      STATE="''${STATE_DIR:-/var/lib/tracker-signup-watch}"
      SEEN="$STATE/seen"            # feed items already alerted on (dedup)
      FAILS="$STATE/consecutive_fails"
      NTFY="http://127.0.0.1:8090/gromit-alerts"
      mkdir -p "$STATE"; touch "$SEEN"

      # One lightweight GET, identified.
      body="$(curl -sS --max-time 25 -A 'rosemaryacres homelab tracker-signup-watch (contact: chris)' "$FEED" 2>/dev/null || true)"

      if [ -z "$body" ]; then
        n="$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))"
        echo "$n" > "$FAILS"
        # No silent rot: after 4 straight failures (~2 days) say so ONCE.
        if [ "$n" -eq 4 ]; then
          curl -s --max-time 10 -H "Title: tracker-signup-watch can't read the feed" \
            -H "Tags: warning" -d "opentrackers.org feed unreachable 4x running — the signup watcher is blind until it recovers." "$NTFY" >/dev/null || true
        fi
        exit 0
      fi
      echo 0 > "$FAILS"

      # Feed item titles carry the status, e.g. "TorrentLeech (TL) is Open for
      # Donation/Application Signup!". Extract titles, keep only ones that BOTH
      # match a watched tracker AND announce an open signup.
      titles="$(printf '%s' "$body" | grep -oiE '<title>[^<]+</title>' | sed -E 's|</?title>||g')"

      while IFS= read -r title; do
        [ -n "$title" ] || continue
        # Must look like an open-signup announcement.
        printf '%s' "$title" | grep -qiE 'open for .*signup|signup.*open|open signup' || continue
        # ...for a tracker on the watchlist.
        while IFS= read -r rx; do
          if printf '%s' "$title" | grep -qiE "$rx"; then
            key="$(printf '%s' "$title" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')"
            if ! grep -qxF "$key" "$SEEN"; then
              echo "$key" >> "$SEEN"
              curl -s --max-time 10 \
                -H "Title: Tracker signup OPEN: $title" \
                -H "Tags: tada,inbox_tray" \
                -H "Priority: default" \
                -d "$title — register now while the window is open: https://opentrackers.org/  (seed 24/7, the AirVPN forwarded port is ready)." \
                "$NTFY" >/dev/null || true
            fi
          fi
        done < <(jq -r '.[].regex' ${watchJson})
      done <<< "$titles"
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
    description = "Twice-daily tracker signup-window check (deliberately low-frequency)";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "08,20:00";       # 08:00 and 20:00 — 2 GETs/day
      RandomizedDelaySec = "45m";    # be a polite, non-synchronized caller
      Persistent = true;             # catch up one missed run after downtime
    };
  };
}
