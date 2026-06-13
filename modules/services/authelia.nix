# Authelia — centralized SSO / forward-auth gateway (Phase 1).
#
# Puts a real login + 2FA in front of the services that currently have NONE:
# Prometheus, Glances, MeTube. nginx forward-auth (auth_request) sends each
# request to Authelia; unauthenticated users get 302'd to the portal at
# auth.rosemaryacres.com. The cookie domain is rosemaryacres.com, shared across
# every subdomain, so one login covers all protected vhosts.
#
# DELIBERATELY NOT protected here (would break them — see the SSO analysis):
#   Grafana (anon embed), ntfy (webhooks/app), Alby Hub (NWC), Vaultwarden
#   (Bitwarden clients), mempool (homepage widget), and every app with a native
#   client. Those come later via OIDC (Phase 2), not forward-auth.
#
# Machine secrets (jwt/session/storage-encryption) are auto-generated random on
# first boot. The ONLY human step (see PR notes): replace the seeded temp user in
# /var/lib/authelia-secrets/users.yml with your own (authelia crypto hash
# generate argon2 --password '…'), then enrol TOTP/passkey at first login.
{ config, lib, pkgs, ... }:
let
  domain   = "rosemaryacres.com";
  authHost = "auth.${domain}";
  port     = 9091;
  secDir   = "/var/lib/authelia-secrets";

  # Reusable forward-auth wiring merged into each protected vhost. Server-level
  # auth_request (covers every location); the internal subrequest location turns
  # auth_request OFF on itself to avoid a loop.
  protect = {
    extraConfig = ''
      auth_request /internal/authelia/authz;
      auth_request_set $user   $upstream_http_remote_user;
      auth_request_set $groups $upstream_http_remote_groups;
      auth_request_set $name   $upstream_http_remote_name;
      auth_request_set $email  $upstream_http_remote_email;
      proxy_set_header Remote-User   $user;
      proxy_set_header Remote-Groups $groups;
      proxy_set_header Remote-Name   $name;
      proxy_set_header Remote-Email  $email;
      error_page 401 =302 https://${authHost}/?rd=$scheme://$http_host$request_uri;
    '';
    locations."/internal/authelia/authz" = {
      proxyPass = "http://127.0.0.1:${toString port}/api/authz/auth-request";
      extraConfig = ''
        internal;
        auth_request off;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-Method $request_method;
        proxy_set_header X-Original-URL $scheme://$http_host$request_uri;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header X-Forwarded-Uri $request_uri;
      '';
    };
  };
in
{
  # --- random machine secrets + a seeded temp user (so the service can start) ---
  systemd.services.authelia-secrets = {
    description = "Generate Authelia machine secrets + seed user db";
    wantedBy = [ "multi-user.target" ];
    before = [ "authelia-main.service" ];
    after = [ "systemd-sysusers.service" ];
    serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
    path = [ pkgs.openssl pkgs.coreutils pkgs.authelia ];
    script = ''
      set -eu
      install -d -m 700 ${secDir}
      for s in jwt session storage; do
        [ -s ${secDir}/$s ] || openssl rand -hex 48 > ${secDir}/$s
      done
      if [ ! -s ${secDir}/users.yml ]; then
        # Seed a temp user with a RANDOM password (nobody can log in — Chris
        # replaces this file with his own). Lets Authelia start + lets us verify
        # the forward-auth redirect.
        tmp=$(openssl rand -hex 16)
        hash=$(authelia crypto hash generate argon2 --password "$tmp" 2>/dev/null \
                 | sed -n 's/^Digest: //p')
        cat > ${secDir}/users.yml <<EOF
users:
  chris:
    disabled: false
    displayname: "Chris"
    password: "$hash"
    email: chris@${domain}
    groups: [admins]
EOF
      fi
      chmod 600 ${secDir}/* || true
      chown -R authelia-main:authelia-main ${secDir} || true
    '';
  };

  services.authelia.instances.main = {
    enable = true;
    secrets = {
      jwtSecretFile            = "${secDir}/jwt";
      sessionSecretFile        = "${secDir}/session";
      storageEncryptionKeyFile = "${secDir}/storage";
    };
    settings = {
      theme = "dark";
      server.address = "tcp://127.0.0.1:${toString port}/";
      log.level = "info";

      authentication_backend.file.path = "${secDir}/users.yml";
      # single-user file backend: no self-service password reset (needs SMTP)
      authentication_backend.password_reset.disable = true;

      totp.issuer = domain;
      webauthn.display_name = "Gromit";

      session.cookies = [{
        domain = domain;
        authelia_url = "https://${authHost}";
        default_redirection_url = "https://${domain}";
        name = "authelia_session";
        expiration = "8h";
        inactivity = "1h";
      }];

      storage.local.path = "${secDir}/db.sqlite3";

      # filesystem notifier: 2FA-registration link is written to a file the admin
      # reads once (avoids an SMTP dependency for a single user).
      notifier.filesystem.filename = "${secDir}/notification.txt";

      access_control = {
        default_policy = "deny";
        rules = [{
          domain = [
            "prometheus.${domain}"
            "glances.${domain}"
            "metube.${domain}"
          ];
          policy = "two_factor";
        }];
      };
    };
  };

  # --- the portal vhost ---
  services.nginx.virtualHosts."${authHost}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString port}";
      extraConfig = ''
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header X-Forwarded-Uri $request_uri;
        proxy_set_header X-Forwarded-For $remote_addr;
      '';
    };
  };

  # --- merge forward-auth into the three protected vhosts (no service-module edits) ---
  services.nginx.virtualHosts."prometheus.${domain}" = protect;
  services.nginx.virtualHosts."glances.${domain}"    = protect;
  services.nginx.virtualHosts."metube.${domain}"     = protect;
}
