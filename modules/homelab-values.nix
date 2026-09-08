# Personal values for the homelab-modules library (the PUBLIC repo), plus the
# sops declarations its modules expect. This file IS the private half of the
# split: implementations live in the library; everything gromit-specific they
# read lives here. When a module migrates to the library, its values and
# secret declarations land here.
{ config, pkgs, ... }:

{
  # ── homelab.* option values ────────────────────────────────────────────────
  homelab.domain = "rosemaryacres.com";
  homelab.adminUser = "chris";
  homelab.adminDisplayName = "Chris";
  homelab.ntfy.url = "http://localhost:8090/gromit-alerts";

  # ── mergerfs pools (wave 1; assembled by the library's mergerfs-pools) ─────
  # The physical member mounts live in ./storage.nix (hardware).
  homelab.pools = {
    fusion = {   # Primary Bucket - 28.3 TB (sdf 7.3T + sdg 9.1T + 3× 2.7T Hitachis + sdk 3.6T)
      mountpoint = "/mnt/fusion";
      branches = "/mnt/primary/D*";
      createPolicy = "mfs";
      fsname = "mergerfs";
      # For the auto-remounter (wave 2).
      memberDir = "/mnt/primary";
      members = [ "D1" "D2" "D3" "D4" "D5" "D6" ];
    };
    backup = {   # Backup Bucket - 22 TB (4× 6TB drives)
      mountpoint = "/mnt/backup/all";
      branches = "/mnt/backup/D*";
      memberDir = "/mnt/backup";
      members = [ "D1" "D2" "D3" "D4" ];
      # epmfs: media-mirror writes /mnt/backup/all/Movies/X and bub-mirror the
      # parallel copy under rick-offsite/; epmfs keeps them on one branch so
      # rsync --link-dest can hardlink between them (see the library module).
      createPolicy = "epmfs";
      # Must exceed the largest single file — the 4 GiB default let bub-mirror
      # fill D1 until a movie temp file no longer fit.
      minFreeSpace = "100G";
    };
  };

  # ── pinchflat (wave 3a) ────────────────────────────────────────────────────
  homelab.pinchflat.mediaDir = "/mnt/fusion/pinchflat";

  # ── download-stack shared values + wave 3d ─────────────────────────────────
  homelab.arrStack = {
    root = "/mnt/fusion/arr";
    owner = "chris";
    group = "users";
    # puid/pgid defaults (1000/100) match chris:users.
  };
  homelab.recyclarr.configFile = ./services/recyclarr-config.yml;

  # ── wave 3c service values ─────────────────────────────────────────────────
  homelab.acme = {
    email = "chris@saenzmail.com";
    credentialsFile = config.sops.secrets."cloudflare-dns-api".path;
  };
  homelab.nextcloud = {
    adminPasswordFile = config.sops.secrets."nextcloud-admin-pass".path;
    oidcSecretFile = config.sops.secrets."nextcloud-oidc-secret".path;
  };
  homelab.forgejo.oidcSecretFile = config.sops.secrets."forgejo-oidc-secret".path;
  homelab.ntfy = {
    baseUrl = "http://100.82.117.116:8090";   # what the phone app subscribes to
    topic = "gromit-alerts";
  };

  # Nightly database backups. Dump named databases individually (pg_dump per
  # DB) rather than pg_dumpall — pg_dumpall aborts entirely if any one
  # database fails, which silently broke ALL backups for 16 months when the
  # orphaned immich DB became undumpable.
  services.postgresqlBackup = {
    enable = true;
    startAt = "*-*-* 01:15:00";
    databases = [ "nextcloud" "immich" "stacks" ];
  };

  # ── wave 3b service values ─────────────────────────────────────────────────
  homelab.paperless = {
    adminPasswordFile = config.sops.secrets."paperless-admin".path;
    oidcEnvFile = config.sops.secrets."paperless-oidc-env".path;
  };
  homelab.vaultwarden = {
    subdomain = "keys";   # renamed from vault.* — Chrome Safe Browsing kept flagging it
    envFile = config.sops.secrets."vaultwarden-env".path;
    extraConfig = {
      # Outbound email via Postmark: invites, new-device alerts, email 2FA.
      # Postmark uses its Server API token as BOTH SMTP_USERNAME and
      # SMTP_PASSWORD — both live in the env secret. SMTP_FROM must be a
      # Postmark-verified sender.
      SMTP_HOST = "smtp.postmarkapp.com";
      SMTP_PORT = 587;
      SMTP_SECURITY = "starttls";        # 587 = STARTTLS
      SMTP_FROM = "vault@rosemaryacres.com";
      SMTP_FROM_NAME = "Rosemary Acres Vault";
    };
  };
  homelab.immich = {
    mediaLocation = "/mnt/fusion/immich";
    # ML offloaded to wallace (Ryzen 9 5900X) over the tailnet; version kept
    # in lockstep via hosts/wallace/immich-ml.nix (pkgs.immich.version).
    mlUrl = "http://100.66.171.120:3003";
  };
  homelab.metube = {
    downloadDir = "/mnt/fusion/youtube/metube";
    # uid pinned to what was ACTUALLY auto-allocated before pinning (NixOS
    # refuses to change an existing user's uid); 984 = the media group's gid.
    uid = 971;
    mediaGid = 984;
  };

  # ── drive-temps (wave 2) ───────────────────────────────────────────────────
  homelab.driveTemps = {
    # Historical metric name — dashboards, the drive-temperature alert group
    # and months of TSDB history use it; keep it.
    metricPrefix = "gromit_drive_";
    # The backup-pool WD Elements bridges misreport power state, so these are
    # SMART-read only while actively doing I/O (same by-id set as
    # drive-spindown.nix — keep the two lists in step).
    spindownDriveIds = [
      "usb-WD_Elements_25A3_575832324439303132343737-0:0"
      "usb-WD_Elements_25A3_57583532444330364C304B32-0:0"
      "usb-WD_Elements_25A3_575835314438394844563632-0:0"
      "usb-WD_Elements_25A3_575832324443303254584B36-0:0"
    ];
  };

  # ── deploy-drift watch (wave 2) ────────────────────────────────────────────
  # Compares Forgejo main against comin's deployed commit. Born from the
  # Sep 5 outage: the GitHub mirror froze and every comin gauge stayed green
  # while merges piled up undeployed for two days.
  homelab.deployDriftWatch = {
    enable = true;
    repoUrl = "https://git.rosemaryacres.com/ww4/flakes.git";
  };

  # ── Monitoring (wave 1b; the stack machinery lives in the library) ─────────
  homelab.monitoring = {
    enable = true;
    # Site-specific exporters the library doesn't know about.
    extraScrapeConfigs = [
      # comin's own metrics — last_build/eval/deployment/fetch_failed gauges,
      # so a broken merge (comin running but NOT deploying) pages instead of
      # silently sitting on the old generation.
      { job_name = "comin";
        static_configs = [{ targets = [ "127.0.0.1:4243" ]; }];
      }
      # Blocky (local split-horizon DNS) — query counts, cache hit ratio,
      # upstream failures. See services/blocky.nix (metrics on loopback :4000).
      { job_name = "blocky";
        static_configs = [{ targets = [ "127.0.0.1:4000" ]; }];
      }
    ];
    # Riverwatch operational/health alerts are info-level AND muted overnight —
    # they hold until morning. NOT actual river conditions; flood/forecast/
    # rapid-rise alerts are deliberately not routed here.
    extraAlertmanagerRoutes = [
      {
        matchers = [ ''alertname="RiverwatchFetchFailing"'' ];
        receiver = "ntfy-noresolve";          # also no "RESOLVED" ping
        mute_time_intervals = [ "nights" ];
      }
      {
        matchers = [ ''alertname="RiverObservationStale"'' ];
        receiver = "ntfy";
        mute_time_intervals = [ "nights" ];
      }
    ];
    extraAlertRuleFiles = [ ./services/grafana/alert-rules-local.json ];
    extraDatasources = [
      {
        # Used by the riverwatch dashboard to overlay the NWPS stage forecast.
        # No base URL — the panel target supplies the full URL per-query.
        name = "NWPS";
        uid = "nwps-infinity";
        type = "yesoreyeram-infinity-datasource";
        access = "proxy";
        jsonData = {
          # Restrict outbound URLs so this datasource can't be misused as a
          # generic SSRF tool. NWPS only.
          allowedHosts = [ "https://api.water.noaa.gov" ];
          timeoutInSeconds = 30;
        };
      }
    ];
    extraPlugins = with pkgs.grafanaPlugins; [ yesoreyeram-infinity-datasource ];
    grafanaOidcSecretFile = config.sops.secrets."grafana-oidc-secret".path;
  };

  # Prometheus site overrides: 110y retention covers the riverwatch USGS
  # backfill to the gauge's 1925 install; the 16m lookback keeps a
  # 15-min-cadence backfill sample queryable until the next would arrive.
  services.prometheus.retentionTime = "40150d";
  services.prometheus.extraFlags = [ "--query.lookback-delta=16m" ];

  # ── Authelia SSO (wave 1) ──────────────────────────────────────────────────
  homelab.authelia = {
    enable = true;
    displayName = "Gromit";   # WebAuthn enrolment name
    # ONE list drives forward-auth AND the access rule (the old two-edit trap).
    # notes.* (SilverBullet) deliberately NOT here: Chris wants notetaking
    # frictionless (2026-07-09) — the shared grocery list especially (Mary, at
    # the store, via Tailscale). The Tailscale/LAN source-gate is the perimeter.
    protectedVhosts = [ "prometheus" "glances" "metube" "notebook" ];
    # OIDC clients: secrets here are pbkdf2 HASHES (safe in the store); each
    # matching plaintext lives in that app's sops secret.
    oidcClients = [
      {
        client_id = "grafana";
        client_name = "Grafana";
        # hash of the secret in sops:grafana-oidc-secret (see monitoring.nix)
        client_secret = "$pbkdf2-sha512$310000$h.XQqknlgymykM29CKxJ1A$zD3BTX23n0WXZbjoHN4V9Pg/9ET6H2FIPMOejCmHMnbe.gdvaQ6bWUvkIhPNZxx5WQ6sLYbPmHT8tYIxMJGIQw";
        public = false;
        authorization_policy = "two_factor";
        redirect_uris = [ "https://grafana.rosemaryacres.com/login/generic_oauth" ];
        scopes = [ "openid" "profile" "email" "groups" ];
        userinfo_signed_response_alg = "none";
        # Remember the consent grant instead of prompting on every login.
        consent_mode = "pre-configured";
        pre_configured_consent_duration = "1y";
      }
      {
        client_id = "paperless";
        client_name = "Paperless";
        # hash of the secret in sops:paperless-oidc-env (see paperless.nix)
        client_secret = "$pbkdf2-sha512$310000$LgpCpOKDW/t4QM8eUXeonA$hfWVb9HDQczx6WgLlhAMIasYanUcGD6f48S0bcJBjZ7HuOByN3Qxyy6b/qkCj8x/IXNZ2XEzyDKIv6tspOtqzw";
        public = false;
        authorization_policy = "two_factor";
        # allauth openid_connect callback: /accounts/oidc/<provider_id>/login/callback/
        redirect_uris = [ "https://paperless.rosemaryacres.com/accounts/oidc/authelia/login/callback/" ];
        scopes = [ "openid" "profile" "email" "groups" ];
        userinfo_signed_response_alg = "none";
        consent_mode = "pre-configured";
        pre_configured_consent_duration = "1y";
      }
      {
        client_id = "forgejo";
        client_name = "Forgejo";
        # hash of sops:forgejo-oidc-secret (added to Forgejo by a oneshot)
        client_secret = "$pbkdf2-sha512$310000$WZbMWTU2heGL4wBXuVyZuw$ntAra1S22bx7x1db22aY/DlEz3kDYgNoJCN2dlHPQhE6ZDaYPIh3D6OXIq7nFbVOSssM/ObQgBhhx2oLkLRuQw";
        public = false;
        authorization_policy = "two_factor";
        redirect_uris = [ "https://git.rosemaryacres.com/user/oauth2/authelia/callback" ];
        scopes = [ "openid" "profile" "email" "groups" ];
        userinfo_signed_response_alg = "none";
        consent_mode = "pre-configured";
        pre_configured_consent_duration = "1y";
      }
      {
        client_id = "nextcloud";
        client_name = "Nextcloud";
        # hash of sops:nextcloud-oidc-secret (added via occ user_oidc oneshot)
        client_secret = "$pbkdf2-sha512$310000$nyWAbCd3XLd6/m2aw6rCsQ$X3jJPvIOjw7ZjT4jlUedbOy3.7BTcmJyY7mQMKIfE4uPpfQKlsWaJPCNQ5AM.3ufMqu41PTLj6tAqjTWQ5JUSg";
        public = false;
        authorization_policy = "two_factor";
        redirect_uris = [ "https://cloud.rosemaryacres.com/apps/user_oidc/code" ];
        scopes = [ "openid" "profile" "email" "groups" ];
        userinfo_signed_response_alg = "none";
        consent_mode = "pre-configured";
        pre_configured_consent_duration = "1y";
      }
      {
        client_id = "immich";
        client_name = "Immich";
        # hash of sops:immich-oidc-secret (enabled in Immich admin UI)
        client_secret = "$pbkdf2-sha512$310000$27oes9TlfWsZP.q/Mi3RUQ$t0g3rYXOoqz5FKLiYp/4Lez/rLxIKK0kcusmaEbxMaynDcX6KOYntQKhjZZ8P9MeXCz2eq4VUW//.HOKtgjywA";
        public = false;
        authorization_policy = "two_factor";
        redirect_uris = [
          "https://photos.rosemaryacres.com/auth/login"
          "https://photos.rosemaryacres.com/user-settings"
          "app.immich:///oauth-callback"
        ];
        scopes = [ "openid" "profile" "email" "groups" ];
        userinfo_signed_response_alg = "none";
        consent_mode = "pre-configured";
        pre_configured_consent_duration = "1y";
      }
    ];
  };
  homelab.quietHours = { start = 22; end = 7; };   # no non-critical pages overnight
  homelab.arrMissingSweep.user = "claude";         # owns the arr-api sops secret

  # ── values that lived in the moved base modules ────────────────────────────
  # (was modules/system.nix)
  time.timeZone = "America/New_York";

  # (was modules/boot.nix) Headless virtual display. gromit runs with no
  # monitor attached, so every i915 display connector probes "disconnected" →
  # no CRTC/output exists → KDE Plasma has no screen to place a desktop on and
  # renders nothing, so MeshCentral's remote desktop captures only a black
  # framebuffer. Force the HDMI-A-1 connector on at 1920x1080 ("e" =
  # force-enabled even with nothing plugged in) so a CRTC/output exists; Plasma
  # then draws a desktop that XGetImage (the MeshAgent KVM) can capture.
  # Confirmed at runtime via /sys/class/drm/card1-HDMI-A-1/status=on.
  boot.kernelParams = [ "video=HDMI-A-1:1920x1080e" ];

  # ── sops declarations for library modules ──────────────────────────────────
  # Cloudflare DNS-01 token for ACME (was 0644 plaintext once — closing that
  # hole was the point of the sops move). owner=claude/0400 because the token
  # is DUAL-USE: ACME (systemd reads the environmentFile as root) AND the
  # agent's own DNS automation, which reads the file directly.
  sops.secrets."cloudflare-dns-api" = {
    sopsFile = ../secrets/cloudflare-dns-api.yaml;
    key = "cloudflare-dns-api";
    owner = "claude";
    mode = "0400";
  };
  # Only read at first install (long since set up), kept wired to satisfy the
  # module's required adminpassFile and to retire the old plaintext.
  sops.secrets."nextcloud-admin-pass" = {
    sopsFile = ../secrets/nextcloud-admin-pass.yaml;
    key = "nextcloud-admin-pass";
    owner = "nextcloud";
    mode = "0400";
  };
  sops.secrets."nextcloud-oidc-secret" = {
    sopsFile = ../secrets/nextcloud-oidc-secret.yaml;
    key = "nextcloud-oidc-secret";
    owner = "nextcloud";
    mode = "0400";
  };
  sops.secrets."aurral-env" = {
    sopsFile = ../secrets/aurral-env.yaml;
    key = "aurral-env";
  };
  sops.secrets."unpackerr-env" = {
    sopsFile = ../secrets/unpackerr-env.yaml;
    key = "unpackerr-env";
  };
  sops.secrets."forgejo-oidc-secret" = {
    sopsFile = ../secrets/forgejo-oidc-secret.yaml;
    key = "forgejo-oidc-secret";
    owner = "forgejo";
  };
  sops.secrets."paperless-admin" = {
    sopsFile = ../secrets/paperless-admin.yaml;
    key = "paperless-admin";
    owner = "paperless";
  };
  sops.secrets."paperless-oidc-env" = {
    sopsFile = ../secrets/paperless-oidc-env.yaml;
    key = "paperless-oidc-env";
    owner = "paperless";
  };
  sops.secrets."vaultwarden-env" = {
    sopsFile = ../secrets/vaultwarden-env.yaml;
    key = "vaultwarden-env";
  };
  # Immich OIDC secret: not wired declaratively (see the library module's
  # header); root-owned, read with sudo for the one-time admin-UI step.
  sops.secrets."immich-oidc-secret" = {
    sopsFile = ../secrets/immich-oidc-secret.yaml;
    key = "immich-oidc-secret";
    mode = "0400";
  };
  # Grafana's OIDC client secret (generic_oauth reads it via $__file as the
  # grafana user). The matching pbkdf2 hash is in homelab.authelia.oidcClients.
  sops.secrets."grafana-oidc-secret" = {
    sopsFile = ../secrets/grafana-oidc-secret.yaml;
    key = "grafana-oidc-secret";
    owner = "grafana";
  };

  # Library modules only ever reference config.sops.secrets.<name>.path; the
  # declarations (and the encrypted files) stay in this repo.
  sops.secrets."decluttarr-env" = {
    sopsFile = ../secrets/decluttarr-env.yaml;
    key = "decluttarr-env";
  };
  sops.secrets."meshagent-msh" = {
    sopsFile = ../secrets/meshagent-msh.yaml;
    key = "msh";
  };
}
