# Forgejo — self-hosted Git forge at https://git.rosemaryacres.com
#
# Friendly fork of Gitea, governed by Codeberg e.V. Lightweight Go single
# binary; perfect for personal + Broadlinc tooling.
#
# DNS setup (Cloudflare, manual one-time step):
#   A   git.rosemaryacres.com   100.82.117.116    (Tailscale IP, proxy off)
#
# Reverse proxy: nginx → 127.0.0.1:3000 (Forgejo HTTP), forceSSL + DNS-01
# wildcard ACME via the same Cloudflare token the other vhosts use.
#
# First-user bootstrap: the first account created via the web UI is
# auto-promoted to admin (Forgejo default behavior). Disable open
# registration after that via the UI (Site Admin → Configuration) or by
# uncommenting DISABLE_REGISTRATION below and rebuilding.
#
# GitHub mirror: once the admin account exists, repos can be migrated en
# masse via /api/v1/repos/migrate with a GitHub PAT — see the companion
# script /tmp/mirror-github-to-forgejo.sh.
{ config, lib, pkgs, ... }:

let
  # Idempotently register the Authelia OIDC source (Phase 2c). Runs as an
  # ExecStartPost in the forgejo unit so it inherits the service's env +
  # credentials (SECRET_KEY etc., needed to encrypt the stored client secret).
  # Non-fatal (the unit prefixes it with '-' and the script always exits 0) so a
  # hiccup never takes Forgejo down. Reads the client secret from sops at runtime.
  oauthSetup = pkgs.writeShellScript "forgejo-oauth-authelia" ''
    set -u
    fj=${config.services.forgejo.package}/bin/forgejo
    secret=$(cat /run/secrets/forgejo-oidc-secret 2>/dev/null) || exit 0
    [ -n "$secret" ] || exit 0
    # already present? (match the source name in `admin auth list`)
    if "$fj" admin auth list 2>/dev/null | grep -qiw 'authelia'; then exit 0; fi
    "$fj" admin auth add-oauth \
      --name authelia \
      --provider openidConnect \
      --key forgejo \
      --secret "$secret" \
      --auto-discover-url https://auth.rosemaryacres.com/.well-known/openid-configuration \
      --scopes "openid email profile groups" || true
    exit 0
  '';
in
{
  # OIDC client secret (Phase 2c). owner=forgejo so the ExecStartPost can read it.
  sops.secrets."forgejo-oidc-secret" = {
    sopsFile = ../../secrets/forgejo-oidc-secret.yaml;
    key = "forgejo-oidc-secret";
    owner = "forgejo";
  };

  systemd.services.forgejo.serviceConfig.ExecStartPost = [ "-${oauthSetup}" ];

  services.forgejo = {
    enable = true;
    # SQLite is fine for personal scale (single user, dozens of repos).
    # Postgres becomes worthwhile only above ~50 active users or heavy CI.
    database.type = "sqlite3";
    # Repos + LFS objects live under /var/lib/forgejo by default; that's on
    # the nvme root (336 GB free). For big LFS volumes we'd move this to
    # /mnt/fusion later, but for now nvme is fast and has headroom.
    lfs.enable = true;
    settings = {
      server = {
        DOMAIN = "git.rosemaryacres.com";
        ROOT_URL = "https://git.rosemaryacres.com/";
        HTTP_ADDR = "127.0.0.1";
        HTTP_PORT = 3002;       # 3000=homepage, 3001=grafana, 3003=already used
        # SSH is exposed via the host's sshd on a dedicated port so we don't
        # collide with port 22. Forgejo embeds the right git URL for clones.
        SSH_DOMAIN = "git.rosemaryacres.com";
        START_SSH_SERVER = false;       # use host sshd, not built-in
        SSH_PORT = 22;                  # what Forgejo advertises in clone URLs
      };
      service = {
        # Open registration off — invite/admin-only. Flip true if you want
        # to onboard collaborators via signup, then back off.
        DISABLE_REGISTRATION = true;
        REQUIRE_SIGNIN_VIEW = false;    # public repos remain anonymous-cloneable
      };
      session.COOKIE_SECURE = true;
      log.LEVEL = "Info";
      # Mailer left unset — no SMTP wired yet. Password resets will need
      # CLI 'forgejo admin user change-password' until then.
      "ui.meta" = {
        AUTHOR = "gromit Forgejo";
        DESCRIPTION = "Self-hosted Git on gromit";
      };
    };
  };

  # nginx reverse proxy. Same pattern as cloud.rosemaryacres.com.
  # NB: recommendedProxySettings is set at the location level only, NOT
  # globally — setting it at the services.nginx level pushes proxy_*
  # directives into the http {} block, which expands every vhost's
  # proxy_headers_hash beyond nginx's default bucket size and causes
  # those vhosts to return 400 on every request.
  services.nginx = {
    enable = true;
    virtualHosts."git.rosemaryacres.com" = {
      forceSSL = true;
      enableACME = true;
      acmeRoot = null;                  # DNS-01 (inherited defaults)
      locations."/" = {
        proxyPass = "http://127.0.0.1:3002";
        recommendedProxySettings = true;
        # Forgejo's git smart-HTTP can push large objects; lift the cap.
        extraConfig = ''
          client_max_body_size 4G;
          proxy_request_buffering off;
        '';
      };
    };
  };

  # Acme defaults (email + Cloudflare DNS provider) are already set by
  # nextcloud.nix — we inherit them. If that module is ever removed, move
  # the security.acme block here.
}
