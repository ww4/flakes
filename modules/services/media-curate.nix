# media-curate — maintain the `backed-up` Jellyfin tag and promote YouTube
# downloads into the right library. See media-curate.py for the logic.
#
# Tiers (mirrors media-mirror.sh): tier 2 = on /mnt/fusion and NOT under an
# excluded prefix (arr, pinchflat, …) → mirrored to /mnt/backup/all. The tag
# sweep derives the `backed-up` tag purely from "does the file exist in the
# backup pool", so it both backfills and verifies.
#
# ACTIVATION: the Jellyfin API key lives in sops (secrets/media-curate-env.yaml,
# JELLYFIN_API_KEY=<key>); edit with `sops`. Decrypts to /run/secrets at runtime.
# Then `sudo media-curate status` to smoke-test, `media-curate promote` (dry-run)
# before `--apply`. The tag-sweep timer is safe (it only sets Jellyfin tags).
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };
  media-curate = pkgs.writeShellApplication {
    name = "media-curate";
    runtimeInputs = [ pkgs.python3 pkgs.yt-dlp pkgs.coreutils gromit-notify ];
    # SC1091: the secrets.env path is runtime-only, not available at build.
    excludeShellChecks = [ "SC1091" ];
    text = ''
      export JELLYFIN_URL="''${JELLYFIN_URL:-http://127.0.0.1:8096}"
      export FUSION=/mnt/fusion
      export BACKUP=/mnt/backup/all
      export BACKUP_TAG=backed-up
      export MOVIES_DIR="/mnt/fusion/Movies"
      export TV_DIR="/mnt/fusion/TV Shows"
      export KEEP_DIR="/mnt/fusion/youtube/promoted"
      export COLL_LIBRARY="Promote Library"
      export COLL_KEEP="Promote Keep"
      export NOTIFY_BIN="gromit-notify"
      # JELLYFIN_API_KEY comes from the sops secret (root-only, run as root).
      if [ -r ${config.sops.secrets."media-curate-env".path} ]; then
        set -a; . ${config.sops.secrets."media-curate-env".path}; set +a
      fi
      exec ${pkgs.python3}/bin/python3 ${./media-curate.py} "$@"
    '';
  };
in
{
  environment.systemPackages = [ media-curate ];

  # JELLYFIN_API_KEY via sops (migrated 2026-06-16). The tool runs as root
  # (`sudo media-curate`), so root:0400 is readable.
  sops.secrets."media-curate-env" = {
    sopsFile = ../../secrets/media-curate-env.yaml;
    key = "media-curate-env";
  };

  # State dir for pending.txt (the secret now comes from sops, not this dir).
  systemd.tmpfiles.rules = [ "d /var/lib/media-curate 0700 root root - -" ];

  # Tag sweep is non-destructive (only sets Jellyfin tags) → safe to schedule.
  # Promote moves files, so it stays manual (`sudo media-curate promote`) until
  # we've validated it; we can add a timer for it later.
  # Tag sweep: maintain the backed-up tag (backfill + verify). With no key it
  # exits 0 (no failed-unit alert). Daily, after the weekly media-mirror.
  systemd.services.media-curate-tag-sweep = {
    description = "media-curate: maintain the backed-up tag (backfill + verify)";
    after = [ "media-mirror-sync.service" ];
    unitConfig.RequiresMountsFor = "/mnt/fusion /mnt/backup/all";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${media-curate}/bin/media-curate tag-sweep --apply";
    };
  };
  systemd.timers.media-curate-tag-sweep = {
    description = "Daily media-curate tag sweep";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 06:30:00";
      Persistent = true;
    };
  };

  # Promote: process the two collections on a schedule so dropping an item into
  # a collection just works. Only canonically-named Library items move (others
  # wait + ntfy); a run that moves nothing is cheap (no rescan).
  systemd.services.media-curate-promote = {
    description = "media-curate: promote queued collection items";
    unitConfig.RequiresMountsFor = "/mnt/fusion";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${media-curate}/bin/media-curate promote --apply";
    };
  };
  systemd.timers.media-curate-promote = {
    description = "media-curate promote, every 30 min";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*:0/30";
      Persistent = true;
    };
  };
}
