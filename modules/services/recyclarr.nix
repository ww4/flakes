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
      series:
        base_url: http://localhost:8989
        api_key: !secret sonarr_api_key

        # Tells Recyclarr to delete custom formats not listed below — keeps
        # the *arr config in sync with this file, not additive forever.
        delete_old_custom_formats: true

        # NOTE: this used to `include:` three upstream templates. That mechanism
        # is GONE — recyclarr's config-templates repo now ships an EMPTY includes
        # registry ({"radarr": [], "sonarr": []}), so *no* `- template: <name>`
        # resolves and every sync dies with "Unable to find include template".
        # templates.json still lists names, but those are starter configs for
        # `recyclarr config create`, not includes. The WEB-1080p template's
        # contents are therefore inlined below.

        # `anime`, not `series` — this is a SIZE FLOOR, and TRaSH's series floors
        # are calibrated for live-action bitrates. Efficient x265 cartoon encodes
        # fall under them and are rejected outright, which silently defeated the
        # entire RetroToon preference: every RetroToon release for The Jetsons was
        # thrown out with e.g. "7.2 GB is smaller than minimum allowed 30.7 GB
        # (for 24x 624min)", so a 37 GB x264 public release won by default.
        # RetroToon's encodes run 11.5-11.8 MB/min; the series floors are 15 for
        # 1080p WEB and 50.4 for Bluray-1080p, while the anime floors are 5.
        #
        # ⚠️ Quality definitions are GLOBAL — Sonarr has no per-profile size
        # limit — so this lowers the floor for live-action too. Real trade-off,
        # mitigated by the fact that a floor only decides what is ELIGIBLE, never
        # what is preferred: ranking still favours the better release, and
        # TRaSH's LQ / LQ (Release Title) / Upscaled custom formats (applied to
        # WEB-1080p) filter junk far more precisely than a size proxy does.
        quality_definition:
          type: anime

        custom_format_groups:
          add:
            - trash_id: 158188097a58d7687dee647e04af0da3  # [Optional] Golden Rule HD
              select:
                - 47435ece6b99a0b477caf360e79ba0bb  # x265 (HD)
            # No `select:` here — both CFs in this group (HD Streaming Boost,
            # UHD Streaming Boost) are marked required=true upstream, so
            # recyclarr always includes them and listing them warned
            # "Selecting required CF ... is redundant" on every daily run.
            - trash_id: 85fae4a2294965b75710ef2989c850eb  # [Streaming Services] HD/UHD boost
            - trash_id: 59c3af66780d08332fdc64e68297098f  # [Unwanted] Unwanted Formats
              select:
                - 15a05bc7c1a36e2b57fd628f8977e2fc  # AV1
                - 32b367365729d530ca1c124a0b180c64  # Bad Dual Groups
                - 85c61753df5da1fb2aab6f2a47426b09  # BR-DISK
                - 6f808933a71bd9666531610cb8c059cc  # BR-DISK (BTN)
                - fbcb31d8dabd2a319072b84fc0b7249c  # Extras
                - 9c11cd3f07101cdba90a2d81cf0e56b4  # LQ
                - e2315f990da2e2cbfc9fa5b7a6fcfe48  # LQ (Release Title)
                - 23297a736ca77c0fc8e70f8edd7ee56c  # Upscaled

        # ── "Retro Animation" — prefer the private tracker over public ones,
        # even one quality notch down. ──────────────────────────────────────
        # Sonarr compares QUALITY TIER FIRST; custom-format score and indexer
        # priority only break ties WITHIN a tier. So a public 1080p always
        # beats a RetroToon 720p no matter how it's scored, and no custom
        # format can express "came from indexer X" — there is no such
        # condition. The only lever that crosses a notch is putting the tiers
        # in ONE quality group, which makes them equal; the Prowlarr indexer
        # priority (RetroToon = 1, publics = 25) then decides the winner.
        #
        # Deliberately a SEPARATE profile, assigned per-series in Sonarr, not
        # a change to WEB-1080p. Grouping 1080p with 720p globally would mean
        # Sonarr never upgrades 720p -> 1080p for ANYTHING, which is far too
        # blunt for a preference that only matters on retro cartoons (where
        # the quality ceiling is a 1970s TV master anyway).
        quality_profiles:
          # The stock WEB-1080p profile, unchanged (was the include's job).
          - trash_id: 72dae194fc92bf828f32cde7744e51a1  # WEB-1080p
            reset_unmatched_scores:
              enabled: true

          - name: Retro Animation
            upgrade:
              allowed: true
              until_quality: HD
              until_score: 10000
            min_format_score: 0
            quality_sort: top
            qualities:
              - name: HD
                qualities:
                  - Bluray-1080p
                  - WEBDL-1080p
                  - WEBRip-1080p
                  - HDTV-1080p
                  - Bluray-720p
                  - WEBDL-720p
                  - WEBRip-720p
                  - HDTV-720p

              # SD fallback, ranked BELOW the HD group so it is only ever taken
              # when no HD release exists — genuinely old animation often has no
              # HD master at all (RetroToon had a DVD copy of Jetsons S02 that was
              # refused outright with "DVD is not wanted in profile"). Upgrades
              # stay allowed up to HD, so an SD grab is replaced automatically if
              # an HD release turns up later.
              - name: SD
                qualities:
                  - Bluray-576p
                  - Bluray-480p
                  - DVD
                  - WEBDL-480p
                  - WEBRip-480p
                  - SDTV

    radarr:
      movies:
        base_url: http://localhost:7878
        api_key: !secret radarr_api_key

        delete_old_custom_formats: true

        # Inlined for the same reason as Sonarr above (includes registry is empty).
        # Same anime-floor reasoning as Sonarr above; animated FILMS hit the
        # identical wall (radarr movie floors: Bluray-1080p 50.8, WEB-1080p 12.5
        # MB/min vs anime 5), so the Retro Animation movie profile would be
        # defeated the same way.
        quality_definition:
          type: anime

        quality_profiles:
          - trash_id: d1d67249d3890e49bc12e275d989a7e9  # HD Bluray + WEB
            reset_unmatched_scores:
              enabled: true

          # Movie-side twin of the Sonarr profile of the same name, so animated
          # FILMS get the same "prefer RetroToon even one notch down" behaviour
          # that retro TV already has. Identical grouping on purpose: 1080p and
          # 720p in one tier makes them equal, which is the only thing that lets
          # the Prowlarr indexer priority (RetroToon=1, publics=25) outrank a
          # better public release. Radarr uses the same quality spellings as
          # Sonarr for these eight.
          #
          # Assign it per-movie in Radarr; the stock HD Bluray + WEB profile is
          # untouched and stays the default for everything else.
          - name: Retro Animation
            upgrade:
              allowed: true
              until_quality: HD
              until_score: 10000
            min_format_score: 0
            quality_sort: top
            qualities:
              - name: HD
                qualities:
                  - Bluray-1080p
                  - WEBDL-1080p
                  - WEBRip-1080p
                  - HDTV-1080p
                  - Bluray-720p
                  - WEBDL-720p
                  - WEBRip-720p
                  - HDTV-720p

              # SD fallback, ranked BELOW the HD group so it is only ever taken
              # when no HD release exists — genuinely old animation often has no
              # HD master at all (RetroToon had a DVD copy of Jetsons S02 that was
              # refused outright with "DVD is not wanted in profile"). Upgrades
              # stay allowed up to HD, so an SD grab is replaced automatically if
              # an HD release turns up later.
              # NB: Radarr also exposes CAM / TELESYNC / TELECINE / WORKPRINT /
              # DVDSCR / REGIONAL. Deliberately excluded — those are pre-release
              # cam rips, not a legitimate SD source for anything.
              - name: SD
                qualities:
                  - Bluray-576p
                  - Bluray-480p
                  - DVD
                  - DVD-R
                  - WEBDL-480p
                  - WEBRip-480p
                  - SDTV

        custom_format_groups:
          add:
            - trash_id: f8bf8eab4617f12dfdbd16303d8da245  # [Optional] Golden Rule HD
              select:
                - dc98083864ea246d05a42df0d05f81cc  # x265 (HD)
            - trash_id: a3ac6af01d78e4f21fcb75f601ac96df  # [Unwanted] Unwanted Formats
              select:
                - b8cd450cbfa689c0259a01d9e29ba3d6  # 3D
                - cae4ca30163749b891686f95532519bd  # AV1
                - b6832f586342ef70d9c128d40c07b872  # Bad Dual Groups
                - cc444569854e9de0b084ab2b8b1532b2  # Black and White Editions
                - ed38b889b31be83fda192888e2286d83  # BR-DISK
                - 0a3f082873eb454bde444150b70253cc  # Extras
                - e6886871085226c3da1830830146846c  # Generated Dynamic HDR
                - 90a6f9a284dff5103f6346090e6280c8  # LQ
                - e204b80c87be9497a8a6eaff48f72905  # LQ (Release Title)
                - 712d74cd88bceb883ee32f773656b1f5  # Sing-Along Versions
                - bfd8eb01832d646a0a89c4deb46f8564  # Upscaled
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
    # The guard is strict on purpose — the key must sit at column 0 as
    # `key: value`, because that is the only form recyclarr's YAML parser
    # accepts. But "empty" was a misleading thing to report: a key that IS
    # present and merely mis-indented, or missing the space after the colon,
    # produced the exact same "empty API keys" line and sent you hunting for a
    # value that was already there. Distinguish the two cases.
    missing=0
    for key in sonarr radarr; do
      if grep -qE "^''${key}_api_key: \S" ${appData}/secrets.yml; then
        continue
      fi
      missing=1
      if grep -qE "^[[:space:]]*''${key}_api_key:[[:space:]]*\S" ${appData}/secrets.yml; then
        echo "  ''${key}_api_key IS set but malformed — recyclarr will not read it."
        echo "  It must start at column 0 with exactly one space after the colon:"
        echo "    ''${key}_api_key: <value>"
        echo "  (check for leading whitespace, a tab, or no space after the colon)"
      else
        echo "  ''${key}_api_key is empty — fill it from the *arr UI (Settings > General > API Key)."
      fi
    done
    if [ "$missing" -ne 0 ]; then
      echo "  Skipping sync."
      exit 0
    fi

    # NOTE: `--app-data` was REMOVED in recyclarr 8.x and is not accepted in any
    # position ("Error: Unknown option 'app-data'"). Its successor env var
    # RECYCLARR_APP_DATA is *also* rejected, with a message pointing at
    # RECYCLARR_CONFIG_DIR. Because the empty-key guard above exits first, this
    # module has never actually reached the binary — filling in the API keys
    # would have failed the unit on the very first real run.
    export RECYCLARR_CONFIG_DIR=${appData}
    exec ${pkgs.recyclarr}/bin/recyclarr sync
  '';
in
{
  environment.systemPackages = [ pkgs.recyclarr ];

  systemd.services.recyclarr-sync = {
    description = "Recyclarr — sync TRaSH-Guides profiles into Sonarr & Radarr";
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
