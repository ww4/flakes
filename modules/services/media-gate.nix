# media-gate — does what the download client finished actually parse as media?
#
# The gap this fills, found 2026-09-27: Sonarr grabbed The Ark S03E07/E08 from a
# public indexer three days BEFORE the episodes aired, and the entire payload of
# each torrent was a single Windows .exe. Sonarr refused the import and then
# wedged, decluttarr's pattern list missed the wording, and for three weeks the
# only component that knew anything was wrong was the queue view nobody reads —
# while qBittorrent seeded 12.7 GB of the payload back out.
#
# The check is deliberately an ALLOWLIST ("does this parse as video?") rather
# than a blocklist ("is this known-bad?"). Signature scanning has poor odds on
# fresh torrent bait, but a renamed executable cannot fake an EBML header, so
# ffprobe answers the question that actually matters and novelty does not help
# the attacker. See media-gate.py for the two cost tiers.
#
# REPORTS ONLY — it never moves, deletes or pauses anything. Yanking files from
# under a seeding torrent is how the library fills with `missingFiles`.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.mediaGate;
in
{
  options.homelab.mediaGate = {
    enable = lib.mkEnableOption "media-gate download content verification";

    qbUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://127.0.0.1:8085";
      description = "qBittorrent WebUI base URL (loopback needs no auth).";
    };

    pathMap = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "/data=/mnt/fusion/arr" ];
      description = ''
        CONTAINER=HOST prefix rewrites. qBittorrent reports the paths it sees
        inside its container; ffprobe runs on the host and needs the real ones.
      '';
    };

    managedCategories = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "tv-sonarr" "radarr" "lidarr" ];
      description = ''
        Download-client categories whose contents are expected to BE media, so
        a torrent with nothing playable in it is a fault. Categories outside
        this list are still checked for executables — an .exe we are seeding is
        an .exe regardless of who asked for it — but not for content, because
        there is no expectation to measure a manual download against.
      '';
    };

    interval = lib.mkOption {
      type = lib.types.str;
      default = "hourly";
      description = "systemd OnCalendar for the scan.";
    };

    maxProbes = lib.mkOption {
      type = lib.types.int;
      default = 120;
      description = ''
        Ceiling on ffprobe calls per run. Verdicts are cached by infohash, so
        steady state is a handful of probes for newly-completed torrents; the
        cap only bounds the first pass over an unseen pool.
      '';
    };

    ntfyUrl = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "ntfy base URL. Empty disables notification (findings still land in the state file and the journal).";
    };

    ntfyTopic = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "ntfy topic for new high-severity findings.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "claude";
      description = "User to run as; needs read access to the download tree.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.tmpfiles.rules = [
      "d /var/lib/media-gate 0755 ${cfg.user} users - -"
    ];

    systemd.services.media-gate = {
      description = "Verify finished downloads are really media";
      after = [ "network-online.target" "docker-qbittorrent.service" ];
      wants = [ "network-online.target" ];

      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        # Header reads across the whole pool will wake spun-down drives. This
        # is never urgent work, so it yields to anything real.
        Nice = 19;
        IOSchedulingClass = "idle";
        # A pool member gone unresponsive must not wedge the timer forever.
        TimeoutStartSec = "30min";

        ExecStart = lib.escapeShellArgs ([
          (lib.getExe pkgs.python3) "${./media-gate.py}"
          "--qb-url" cfg.qbUrl
          "--state" "/var/lib/media-gate/state.json"
          "--max-probes" (toString cfg.maxProbes)
        ]
        # Lists go through argv, one flag per element. A space-separated list
        # in Environment= silently truncates to its first entry and the unit
        # still exits 0 — that bug cost a fortnight of unrefreshed archives.
        ++ lib.concatMap (m: [ "--path-map" m ]) cfg.pathMap
        ++ lib.concatMap (c: [ "--managed-category" c ]) cfg.managedCategories
        ++ lib.optionals (cfg.ntfyUrl != "" && cfg.ntfyTopic != "") [
          "--ntfy-url" cfg.ntfyUrl "--ntfy-topic" cfg.ntfyTopic
        ]);
      };

      path = [ pkgs.ffmpeg-headless ];
    };

    systemd.timers.media-gate = {
      description = "Periodic download content verification";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.interval;
        # Nothing here is time-critical, and a boot-time burst competing with
        # the arr stack coming up helps nobody.
        RandomizedDelaySec = "10m";
        Persistent = true;
      };
    };
  };
}
