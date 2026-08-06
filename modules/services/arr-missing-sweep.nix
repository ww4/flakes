# arr-missing-sweep — weekly "search for what's still missing" across Sonarr/Radarr.
#
# WHY THIS EXISTS: Sonarr has NO recurring missing-episode search. Its RSS Sync
# (15 min) only sees releases *newly posted* to indexer feeds, so any back-catalog
# request whose one-shot search-on-add didn't fire sits monitored and untouched
# forever. Found 2026-08-05: Seeking Persephone waited 29 days with a 360-seeder
# release available, and Seinfeld 15 days with 29 usable releases — neither had a
# single grab attempt in its history. Radarr has the same gap.
#
# POLITENESS (see the polite-polling-ethos memory): weekly, not daily; a large
# RandomizedDelaySec; and it only searches what is genuinely missing.
#
# ⚠️ THE SKIP RULE IS THE IMPORTANT PART. A blanket "search all missing" is
# actively harmful here: TVDB numbers some shows by segment rather than by
# broadcast episode (The Bullwinkle Show = 792 "missing" episodes that no release
# can ever satisfy, because the packs contain 13 files per season). Searching
# those hammers every indexer, every week, forever, for nothing. Any series with
# more than maxMissingPerSeries outstanding is treated as a metadata mismatch,
# skipped, and reported so a human can look — rather than silently retried.
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };
  maxMissingPerSeries = 200;

  sweep = pkgs.writeShellApplication {
    name = "arr-missing-sweep";
    runtimeInputs = [ pkgs.curl pkgs.jq gromit-notify ];
    excludeShellChecks = [ "SC1091" ];
    text = ''
      set -euo pipefail
      . ${config.sops.secrets."arr-api".path}
      S=http://127.0.0.1:8989/api/v3
      R=http://127.0.0.1:7878/api/v3
      MAX=${toString maxMissingPerSeries}
      searched=0; skipped=""

      miss=$(curl -sS -m 120 -H "X-Api-Key: $SONARR_API_KEY" \
        "$S/wanted/missing?pageSize=5000&monitored=true&includeSeries=true")

      # series title -> outstanding count, biggest first
      while IFS=$'\t' read -r n title; do
        [ -z "$title" ] && continue
        id=$(curl -sS -m 60 -H "X-Api-Key: $SONARR_API_KEY" "$S/series" \
             | jq -r --arg t "$title" '.[]|select(.title==$t)|.id')
        [ -z "$id" ] && continue
        if [ "$n" -gt "$MAX" ]; then
          skipped="''${skipped}\n  $title ($n missing — likely TVDB numbering mismatch)"
          continue
        fi
        curl -sS -m 60 -X POST "$S/command" -H "X-Api-Key: $SONARR_API_KEY" \
          -H 'Content-Type: application/json' \
          -d "{\"name\":\"MissingEpisodeSearch\",\"seriesId\":$id}" >/dev/null
        searched=$((searched + 1))
        sleep 20   # stagger: never burst every indexer at once
      done < <(echo "$miss" | jq -r '[.records[]?|.series.title]|group_by(.)
                 |map({t:.[0],n:length})|sort_by(-.n)[]|"\(.n)\t\(.t)"')

      movies=$(curl -sS -m 60 -H "X-Api-Key: $RADARR_API_KEY" "$R/movie" \
               | jq '[.[]|select(.monitored and (.hasFile|not))]|length')
      if [ "$movies" -gt 0 ]; then
        curl -sS -m 60 -X POST "$R/command" -H "X-Api-Key: $RADARR_API_KEY" \
          -H 'Content-Type: application/json' -d '{"name":"MissingMoviesSearch"}' >/dev/null
      fi

      msg="Searched $searched series + $movies missing movie(s)."
      if [ -n "$skipped" ]; then
        msg="$msg"$'\n'"Skipped (needs a human):"$(printf '%b' "$skipped")
      fi
      echo "$msg"
      gromit-notify "Weekly *arr missing sweep" "$msg" default "mag,tv" || true
    '';
  };
in
{
  environment.systemPackages = [ sweep ];

  systemd.services.arr-missing-sweep = {
    description = "Weekly search for still-missing episodes/movies";
    serviceConfig = {
      Type = "oneshot";
      # Runs as the agent user because the *arr API keys are claude-owned sops.
      User = "claude";
      ExecStart = "${sweep}/bin/arr-missing-sweep";
    };
  };

  systemd.timers.arr-missing-sweep = {
    description = "Weekly *arr missing sweep";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      # Sunday 09:00, after the weekly media-mirror (08:00) so the two heavy
      # jobs don't overlap. Wide jitter so indexers never see a clockwork hit.
      OnCalendar = "Sun *-*-* 09:00:00";
      RandomizedDelaySec = "45m";
      Persistent = true;
    };
  };
}
