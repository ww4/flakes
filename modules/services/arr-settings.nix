# arr-settings — declarative Sonarr/Radarr/Prowlarr application settings.
#
# THE PROBLEM: these apps keep configuration in SQLite, not in files, so nothing
# below the quality-profile layer can be managed the way the rest of this host is.
# Every setting tuned through a UI or API is invisible to the flake and would be
# lost if a config volume were recreated. In one afternoon (2026-08-05) four such
# settings accumulated — propers/repacks, per-indexer minimum seeders,
# download-client removal, qBittorrent seeding — recorded nowhere but a chat log.
#
# THE APPROACH: declare the desired state here; a converger GETs what each app
# currently reports, PUTs back only what differs, and prints what it changed. A
# converged run makes no writes, so the daily timer is purely drift detection —
# if it ever reports a change, something outside the flake moved a setting and
# that is worth knowing about.
#
# ⚠️ DELIBERATELY NOT MANAGED HERE: quality profiles, custom formats, quality
# definitions. Recyclarr owns those (services/recyclarr.nix). Two tools writing
# the same resource would fight on every run.
#
# NOT YET COVERED: qBittorrent (different auth model — WebUI API on the gluetun
# netns, no X-Api-Key) and Jellyseerr. Both are worth a follow-up; qBittorrent in
# particular holds the seeding config from PR-era 2026-08-05.
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  # ── DESIRED STATE ────────────────────────────────────────────────────────
  desired = {
    sonarr = {
      url = "http://127.0.0.1:8989";
      apiVersion = "v3";
      keyEnv = "SONARR_API_KEY";
      config.mediamanagement = {
        # Repacks are a HIGHER quality REVISION, and revision outranks both
        # custom-format score and indexer priority. Left at the default
        # "preferAndUpgrade" it silently re-downloads bigger files forever and
        # overrode the RetroToon preference (a 16.2G repack beat a 7.2G release
        # with 3x the seeders). TRaSH recommends doNotPrefer + CF scoring.
        downloadPropersAndRepacks = "doNotPrefer";
      };
      downloadClients = {
        # Would delete a torrent from qBittorrent once imported AND seeding is
        # "complete". Inert today only because qBittorrent has no ratio/time
        # goal — the moment one is set, this silently ends seeds library-wide,
        # private tracker included. Off, so it cannot become a trap.
        removeCompletedDownloads = false;
      };
    };

    radarr = {
      url = "http://127.0.0.1:7878";
      apiVersion = "v3";
      keyEnv = "RADARR_API_KEY";
      config.mediamanagement.downloadPropersAndRepacks = "doNotPrefer";
      downloadClients.removeCompletedDownloads = false;
    };

    prowlarr = {
      url = "http://127.0.0.1:9696";
      apiVersion = "v1";
      keyEnv = "PROWLARR_API_KEY";
      # Set HERE, not in Sonarr/Radarr: all three apps sync at syncLevel=fullSync,
      # so a threshold written app-side is silently reverted on Prowlarr's next
      # push. appMinimumSeeders is the field that propagates down.
      indexerFields."torrentBaseSettings.appMinimumSeeders" = {
        # Public swarms over-report and die; a release advertising 48 seeders
        # was reachable by 2. 5 is a floor with some margin.
        default = 5;
        byName = {
          # Private tracker: genuinely rare retro content legitimately has 2-3
          # seeders, and they are accountable ones. A blanket floor would
          # exclude exactly the content the tracker exists for.
          RetroToon = 1;
        };
      };
    };
  };

  specFile = pkgs.writeText "arr-settings.json" (builtins.toJSON desired);

  arr-settings = pkgs.writeShellApplication {
    name = "arr-settings";
    runtimeInputs = [ pkgs.python3 gromit-notify ];
    excludeShellChecks = [ "SC1091" ];
    text = ''
      set -euo pipefail
      # Export so the python child inherits the *_API_KEY vars (sibling
      # arr-missing-sweep gets away without this because it consumes the vars
      # in-shell via curl; we hand them to a subprocess).
      set -a
      . ${config.sops.secrets."arr-api".path}
      set +a
      out=$(${pkgs.python3}/bin/python3 ${./arr-settings.py} ${specFile} 2>&1) || rc=$?
      echo "$out"
      # Only ping when something actually moved — a converged run is silent.
      if echo "$out" | grep -q '^CHANGED'; then
        gromit-notify "*arr settings drift corrected" "$out" default "gear" || true
      fi
      exit "''${rc:-0}"
    '';
  };
in
{
  environment.systemPackages = [ arr-settings ];

  systemd.services.arr-settings = {
    description = "Converge Sonarr/Radarr/Prowlarr settings to the declared state";
    serviceConfig = {
      Type = "oneshot";
      User = "claude";   # the *arr API keys are claude-owned sops (see agent/arr-api-secret.nix)
      ExecStart = "${arr-settings}/bin/arr-settings";
    };
  };

  systemd.timers.arr-settings = {
    description = "Daily *arr settings convergence / drift check";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      # After recyclarr (05:30) so the two never race on the same apps.
      OnCalendar = "*-*-* 06:00:00";
      RandomizedDelaySec = "10m";
      Persistent = true;
    };
  };
}
