# Recyclarr — sync TRaSH-Guides quality profiles + custom formats into
# Sonarr & Radarr on a daily schedule.
#
# Templates pulled from https://github.com/TRaSH-Guides/Guides — Recyclarr
# clones that repo and applies the chosen profile/custom-format set to each
# *arr's API. Daily timer keeps you in sync with upstream tweaks (e.g.,
# new release groups added to "release group" custom formats).
#
# Initial templates wired up (sensible defaults — change to taste):
#   Sonarr: WEB-1080p profile + matching custom formats (good for streamy TV)
#   Radarr: HD Bluray + WEB profile (Bluray with WEB fallback)
#
# Secrets — NOT in git. Both *arrs generate their own API keys; grab them:
#   Sonarr UI → Settings → General → API Key
#   Radarr UI → Settings → General → API Key
# Then drop into /var/lib/recyclarr/secrets.yml:
#   sonarr_api_key: <your-key>
#   radarr_api_key: <your-key>
# (file is created with empty keys on first activation; service no-ops
# until populated, so no failure-notification spam during the wait.)
{ config, lib, pkgs, ... }:

let
  appData = "/var/lib/recyclarr";

  configFile = pkgs.writeText "recyclarr.yml" ''
    sonarr:
      main:
        base_url: http://localhost:8989
        api_key: !secret sonarr_api_key

        # Tells Recyclarr to delete custom formats not listed below — keeps
        # the *arr config in sync with this file, not additive forever.
        delete_old_custom_formats: true
        replace_existing_custom_formats: true

        include:
          - template: sonarr-quality-definition-series
          - template: sonarr-v4-quality-profile-web-1080p
          - template: sonarr-v4-custom-formats-web-1080p

    radarr:
      main:
        base_url: http://localhost:7878
        api_key: !secret radarr_api_key

        delete_old_custom_formats: true
        replace_existing_custom_formats: true

        include:
          - template: radarr-quality-definition-movie
          - template: radarr-quality-profile-hd-bluray-web
          - template: radarr-custom-formats-hd-bluray-web
  '';

  # Wrapper: copies config from /nix/store into the app-data dir,
  # seeds an empty secrets file on first run, and no-ops cleanly until
  # the user fills in their API keys.
  syncWrapper = pkgs.writeShellScript "recyclarr-sync-wrapper" ''
    set -eu
    install -d -m 0700 -o root -g root ${appData}
    install -m 0644 -o root -g root ${configFile} ${appData}/recyclarr.yml

    if [ ! -f ${appData}/secrets.yml ]; then
      cat > ${appData}/secrets.yml <<'SECRETS_EOF'
    # Recyclarr secrets — fill these in after generating API keys:
    #   Sonarr UI → Settings → General → API Key
    #   Radarr UI → Settings → General → API Key
    # Once both are populated, the daily timer (05:30) will run sync.
    sonarr_api_key:
    radarr_api_key:
    SECRETS_EOF
      chmod 0600 ${appData}/secrets.yml
    fi

    # Skip silently if keys aren't filled in yet — avoids notification noise
    # during the period between deploying this module and the user pasting keys.
    if ! grep -qE '^sonarr_api_key: \S' ${appData}/secrets.yml \
       || ! grep -qE '^radarr_api_key: \S' ${appData}/secrets.yml ; then
      echo "  /var/lib/recyclarr/secrets.yml has empty API keys — skipping sync."
      echo "  Fill in sonarr_api_key + radarr_api_key from the *arr UIs to enable."
      exit 0
    fi

    exec ${pkgs.recyclarr}/bin/recyclarr sync --app-data ${appData}
  '';
in
{
  environment.systemPackages = [ pkgs.recyclarr ];

  systemd.services.recyclarr-sync = {
    description = "Recyclarr — sync TRaSH-Guides profiles into Sonarr & Radarr";
    onFailure = [ "notify-failure@%N.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${syncWrapper}";
      User = "root";  # needs to read secrets.yml at 0600
    };
  };

  systemd.timers.recyclarr-sync = {
    description = "Daily Recyclarr sync";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      # Daily at 05:30 — after snapraid sync (04:00) + scrub (Mon 05:00)
      # so the disks aren't being hammered when Recyclarr pulls templates.
      OnCalendar = "*-*-* 05:30:00";
      Persistent = true;
    };
  };
}
