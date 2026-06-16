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
      # OIDC (Phase 2) machine secrets — generated on-box like the others, so no
      # key material lives in git. hmac signs OIDC tokens; the RSA key is the
      # issuer JWKS private key (the module templates it into the oidc config).
      [ -s ${secDir}/oidc-hmac ]       || openssl rand -hex 32 > ${secDir}/oidc-hmac
      [ -s ${secDir}/oidc-issuer.pem ] || openssl genrsa -out ${secDir}/oidc-issuer.pem 4096
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
      # OIDC (Phase 2): the module env-injects the hmac and templates the issuer
      # key into identity_providers.oidc.jwks for us.
      oidcHmacSecretFile        = "${secDir}/oidc-hmac";
      oidcIssuerPrivateKeyFile  = "${secDir}/oidc-issuer.pem";
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

      # --- OIDC provider (Phase 2): true SSO web-login for OIDC-capable apps. ---
      # hmac_secret + jwks come from the secret files above (module-wired). Each
      # app is a client below. The client_secret here is a pbkdf2 HASH (safe in
      # the store — it's a one-way hash of a 256-bit random secret); the matching
      # plaintext lives in sops for the app to send (e.g. grafana-oidc-secret).
      identity_providers.oidc = {
        clients = [
          {
            client_id = "grafana";
            client_name = "Grafana";
            # hash of the secret in sops:grafana-oidc-secret (see monitoring.nix)
            client_secret = "$pbkdf2-sha512$310000$h.XQqknlgymykM29CKxJ1A$zD3BTX23n0WXZbjoHN4V9Pg/9ET6H2FIPMOejCmHMnbe.gdvaQ6bWUvkIhPNZxx5WQ6sLYbPmHT8tYIxMJGIQw";
            public = false;
            authorization_policy = "two_factor";
            redirect_uris = [ "https://grafana.${domain}/login/generic_oauth" ];
            scopes = [ "openid" "profile" "email" "groups" ];
            userinfo_signed_response_alg = "none";
            # Remember the consent grant instead of prompting on every login.
            # Stored in Authelia's DB per user+client+scopes (survives logout),
            # re-prompts once a year. (Default is "explicit" = prompt every time.)
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
            redirect_uris = [ "https://paperless.${domain}/accounts/oidc/authelia/login/callback/" ];
            scopes = [ "openid" "profile" "email" "groups" ];
            userinfo_signed_response_alg = "none";
            consent_mode = "pre-configured";
            pre_configured_consent_duration = "1y";
          }
        ];
      };

      session.cookies = [{
        domain = domain;
        authelia_url = "https://${authHost}";
        default_redirection_url = "https://${domain}";
        name = "authelia_session";
        # Relaxed for convenience — the real perimeter is the Tailscale source-gate
        # (only Chris can reach these vhosts), so long sessions are fine. A positive
        # remember_me also enables the "Remember me" checkbox on the login portal.
        expiration = "1M";      # hard session cap
        inactivity = "1w";      # idle timeout
        remember_me = "3M";     # "Remember me" extended lifetime + enables the checkbox
      }];

      # Runtime-writable files go in the service's StateDirectory (/var/lib/
      # authelia-main); ProtectSystem=strict makes everything else (incl. the
      # read-only secrets dir) non-writable, so the DB + notifier must live here.
      storage.local.path = "/var/lib/authelia-main/db.sqlite3";

      # filesystem notifier: 2FA-registration link is written to a file the admin
      # reads once (avoids an SMTP dependency for a single user).
      notifier.filesystem.filename = "/var/lib/authelia-main/notification.txt";

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
