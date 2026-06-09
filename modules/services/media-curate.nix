# media-curate — maintain the `backed-up` Jellyfin tag and promote YouTube
# downloads into the right library. See media-curate.py for the logic.
#
# Tiers (mirrors media-mirror.sh): tier 2 = on /mnt/fusion and NOT under an
# excluded prefix (arr, pinchflat, …) → mirrored to /mnt/backup/all. The tag
# sweep derives the `backed-up` tag purely from "does the file exist in the
# backup pool", so it both backfills and verifies.
#
# ACTIVATION: drop the Jellyfin API key in /var/lib/media-curate/secrets.env
# (root 0600):   JELLYFIN_API_KEY=<key>
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
      # JELLYFIN_API_KEY comes from the root-only secrets file.
      if [ -r /var/lib/media-curate/secrets.env ]; then
        set -a; . /var/lib/media-curate/secrets.env; set +a
      fi
      exec ${pkgs.python3}/bin/python3 ${./media-curate.py} "$@"
    '';
  };
in
{
  environment.systemPackages = [ media-curate ];

  # Holds secrets.env (Jellyfin API key). root-only.
  systemd.tmpfiles.rules = [ "d /var/lib/media-curate 0700 root root - -" ];

  # Tag sweep is non-destructive (only sets Jellyfin tags) → safe to schedule.
  # Promote moves files, so it stays manual (`sudo media-curate promote`) until
  # we've validated it; we can add a timer for it later.
  # NOTE: runs in DRY-RUN (no --apply) until the tag-writing path has been
  # validated live against the real library. Flipping to `--apply` is then a
  # one-line change. With no key present it exits 0 (no failed-unit alert).
  systemd.services.media-curate-tag-sweep = {
    description = "media-curate: maintain the backed-up tag (backfill + verify)";
    after = [ "media-mirror-sync.service" ];
    unitConfig.RequiresMountsFor = "/mnt/fusion /mnt/backup/all";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${media-curate}/bin/media-curate tag-sweep";
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
}
