# unpackerr — extract RAR'd Scene releases so Sonarr/Radarr can import them.
#
# WHY: Scene release rules still require video be split into RAR volumes with
# SFV checksums — a BBS/FTP-era artifact preserved by rule inertia long after
# it stopped making sense. Neither Sonarr nor Radarr can extract an archive;
# they see `movie.r00`, `movie.r01`, … , fail the import, and decluttarr then
# reaps the download ("*Found potentially dangerous file with extension*" is
# already in its failed-import patterns). unpackerr watches the *arr queues,
# extracts completed archive sets in place, lets the import proceed, then
# removes only what it extracted.
#
# Added 2026-08-08 alongside the DigitalCore indexer. DC is Scene-heavy, and
# its `unrar_releases_only` filter was measured to remove 70-87% of its
# catalogue (solaris 39->5, "hail mary" 79->24, counts stable across repeats),
# so filtering RARs away is far more expensive than handling them.
#
# SEEDS ARE NOT AFFECTED. unpackerr extracts *alongside* the archive set and
# never touches the .rNN files, so qBittorrent keeps seeding the original
# payload throughout. This is the same rule the rest of the media tooling
# follows (see media-link / media-curate: hardlink, never move a seeded file).
{ config, lib, pkgs, ... }:

let
  TZ      = "America/New_York";
  arrNet  = "arr-net";

  # MUST match arr.nix exactly. unpackerr reads each queue item's path as the
  # *arr reports it (`/data/downloads/...`) and then opens that path itself, so
  # it only works if unpackerr sees the identical tree under the identical
  # mount point. Running it on the host instead — where the same tree is
  # /mnt/fusion/arr/downloads — would silently match nothing.
  arrRoot    = "/mnt/fusion/arr";
  dataVolume = "${arrRoot}:/data:rw";

  # chris:users, same as every other container in the *arr stack, so extracted
  # files land with the ownership Sonarr/Radarr already expect to import from.
  PUID = "1000";
  PGID = "100";
in
{
  # Only the two API keys are secret; URLs and paths are not, so they stay
  # readable in the nix `environment` below.
  sops.secrets."unpackerr-env" = {
    sopsFile = ../../secrets/unpackerr-env.yaml;
    key = "unpackerr-env";
  };

  virtualisation.oci-containers.containers.unpackerr = {
    # `:latest` matches the convention of the rest of the stack (arr.nix,
    # decluttarr.nix) — comin redeploys pull the current image.
    image = "golift/unpackerr:latest";

    environment = {
      inherit TZ;

      UN_SONARR_0_URL       = "http://sonarr:8989";
      UN_SONARR_0_PATHS_0   = "/data/downloads";
      UN_SONARR_0_PROTOCOLS = "torrent";

      UN_RADARR_0_URL       = "http://radarr:7878";
      UN_RADARR_0_PATHS_0   = "/data/downloads";
      UN_RADARR_0_PROTOCOLS = "torrent";

      # How long after a successful import before the EXTRACTED files are
      # removed. The archives themselves are never touched. Default is 5m;
      # 10m buys margin for a slow mergerfs import without leaving the
      # duplicate around long enough to matter for disk.
      UN_DELETE_DELAY = "10m";

      UN_INTERVAL    = "2m";
      UN_START_DELAY = "1m";
      UN_RETRY_DELAY = "5m";
      UN_MAX_RETRIES = "3";

      # One extraction at a time. These are USB-attached mergerfs drives, and
      # parallel extraction would thrash the pool for no wall-clock gain.
      UN_PARALLEL = "1";

      # Group-writable to match the rest of /mnt/fusion (media-link uses 0775).
      UN_FILE_MODE = "0664";
      UN_DIR_MODE  = "0775";

      # Log to stdout -> journald, so failures surface in /health like any
      # other unit rather than in a file nobody reads.
      UN_LOG_FILE = "";
      UN_DEBUG    = "false";
    };

    environmentFiles = [ config.sops.secrets."unpackerr-env".path ];
    volumes = [ dataVolume ];
    dependsOn = [ "sonarr" "radarr" ];
    extraOptions = [
      "--network=${arrNet}"
      "--user=${PUID}:${PGID}"
    ];
  };

  # Same ordering guard the other *arr containers use: the user-defined bridge
  # must exist before this starts, otherwise the container fails to attach.
  systemd.services.docker-unpackerr = {
    after = [ "docker-network-arr.service" ];
    requires = [ "docker-network-arr.service" ];
  };
}
