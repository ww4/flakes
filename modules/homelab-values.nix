# Personal values for the homelab-modules library (the PUBLIC repo), plus the
# sops declarations its modules expect. This file IS the private half of the
# split: implementations live in the library; everything gromit-specific they
# read lives here. When a module migrates to the library, its values and
# secret declarations land here.
{ ... }:

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
    };
    backup = {   # Backup Bucket - 22 TB (4× 6TB drives)
      mountpoint = "/mnt/backup/all";
      branches = "/mnt/backup/D*";
      # epmfs: media-mirror writes /mnt/backup/all/Movies/X and bub-mirror the
      # parallel copy under rick-offsite/; epmfs keeps them on one branch so
      # rsync --link-dest can hardlink between them (see the library module).
      createPolicy = "epmfs";
      # Must exceed the largest single file — the 4 GiB default let bub-mirror
      # fill D1 until a movie temp file no longer fit.
      minFreeSpace = "100G";
    };
  };

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
