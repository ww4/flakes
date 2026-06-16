# Decluttarr — auto-reaps stalled/failed downloads from Sonarr/Radarr and
# triggers a re-search, so dead public torrents don't clog the queue.
#
# Conservative config:
#   - private_tracker_handling: skip  → NEVER touches private-tracker torrents
#     (protects ratio; it reads the private flag from qBittorrent). Only public
#     dead torrents get reaped.
#   - 3 strikes before removing a stalled torrent (≈45 min at the 15-min timer).
#   - Only the safe jobs are enabled: failed downloads, failed imports (matched
#     to known-unrecoverable messages), missing metadata, and stalled. NOT
#     remove_slow / remove_orphans / remove_unmonitored / remove_done_seeding
#     (those risk killing slow-but-alive grabs, manual downloads, or seeding
#     torrents).
#
# Runs as a container on arr-net so it resolves sonarr/radarr by name and
# reaches qBittorrent via gluetun:8085 (qBit shares gluetun's netns; the
# arr-net subnet is in qBit's WebUI auth whitelist, so no qBit password).
#
# Secrets — NOT in git. Create before first rebuild (root 0600):
#   /var/lib/decluttarr/secrets.env
#     SONARR_API_KEY=<Sonarr → Settings → General → API Key>
#     RADARR_API_KEY=<Radarr → Settings → General → API Key>
{ config, lib, pkgs, ... }:

let
  TZ = "America/New_York";
  arrNet = "arr-net";

  # Config lives at /app/config/config.yaml inside the image (WorkDir /app).
  # No secrets here — api_key uses the !ENV tag (yaml_env_tag: bare var name),
  # resolved from the environmentFile at container start.
  configYaml = pkgs.writeText "decluttarr-config.yaml" ''
    general:
      log_level: INFO
      test_run: false
      timer: 15
      detect_deletions: false
      private_tracker_handling: skip
      public_tracker_handling: remove
    job_defaults:
      max_strikes: 3
    jobs:
      remove_failed_downloads:
      remove_failed_imports:
        message_patterns:
          - "Not a Custom Format upgrade for existing*"
          - "Not an upgrade for existing*"
          - "*Found potentially dangerous file with extension*"
          - "Invalid video file*"
          - "No files found are eligible for import*"
          - "One or more episodes expected in this release were not imported or missing from the release"
      remove_metadata_missing:
      remove_stalled:
    instances:
      sonarr:
        - base_url: "http://sonarr:8989"
          api_key: !ENV SONARR_API_KEY
      radarr:
        - base_url: "http://radarr:7878"
          api_key: !ENV RADARR_API_KEY
    download_clients:
      qbittorrent:
        - base_url: "http://gluetun:8085"
          name: "qBittorrent"
  '';
in
{
  # Decluttarr API keys via sops (migrated 2026-06-16). Read by root (docker
  # --env-file) before the container starts → root:0400.
  sops.secrets."decluttarr-env" = {
    sopsFile = ../../secrets/decluttarr-env.yaml;
    key = "decluttarr-env";
  };

  systemd.tmpfiles.rules = [
    "d /var/lib/decluttarr            0700 root root - -"
    "f /var/lib/decluttarr/secrets.env 0600 root root - -"
  ];

  virtualisation.oci-containers.containers.decluttarr = {
    image = "ghcr.io/manimatter/decluttarr:latest";
    environment = {
      inherit TZ;
      IN_DOCKER = "true";
    };
    environmentFiles = [ config.sops.secrets."decluttarr-env".path ];
    volumes = [ "${configYaml}:/app/config/config.yaml:ro" ];
    dependsOn = [ "sonarr" "radarr" "gluetun" ];
    extraOptions = [ "--network=${arrNet}" ];
  };

  # Ensure the arr-net bridge exists before this container starts (the
  # network is created by docker-network-arr in arr.nix).
  systemd.services.docker-decluttarr = {
    after = [ "docker-network-arr.service" ];
    requires = [ "docker-network-arr.service" ];
  };
}
