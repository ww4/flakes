# Monitoring stack — Prometheus + Grafana + Alertmanager + node_exporter.
#
# Each service binds 127.0.0.1; nginx fronts the user-facing two
# (grafana, prometheus) at HTTPS. node_exporter is local-only.
#
# Alertmanager routes to a tiny webhook→ntfy shim (see ./monitoring-ntfy.nix
# in a follow-up) so notifications continue to flow through the existing
# gromit-alerts topic.
#
# Prometheus retention: 365d at native 1m resolution. Storage is on the
# nvme (/var/lib/prometheus2), uses ~50–200 MB/year per active target —
# trivial for this scale.
{ config, lib, pkgs, ... }:

let
  grafanaHost      = "grafana.rosemaryacres.com";
  prometheusHost   = "prometheus.rosemaryacres.com";
  grafanaPort      = 3001;   # avoid 3000 clash with Homepage container
  prometheusPort   = 9090;   # default, already in firewall.allowedTCPPorts
  alertmanagerPort = 9093;
  nodeExporterPort = 9100;
in {
  # Prometheus — TSDB + scrape engine.
  services.prometheus = {
    enable = true;
    port = prometheusPort;
    listenAddress = "127.0.0.1";
    # Default 5m stales out our 15-min-cadence USGS backfill samples when
    # Grafana picks a >5m step. Bump to 16m so a backfill sample remains
    # queryable until the next one would have arrived.
    extraFlags = [ "--query.lookback-delta=16m" ];
    # 110y — covers the riverwatch USGS backfill all the way back to LPTK2's
    # gauge install in 1925 (we don't realistically expect to keep Prometheus
    # itself for 110y, but this stops time-based retention from pruning the
    # historical blocks we'll import).
    retentionTime = "40150d";
    globalConfig = {
      scrape_interval = "1m";
      evaluation_interval = "1m";
    };
    scrapeConfigs = [
      { job_name = "prometheus";
        static_configs = [{ targets = [ "127.0.0.1:${toString prometheusPort}" ]; }];
      }
      { job_name = "node";
        static_configs = [{ targets = [ "127.0.0.1:${toString nodeExporterPort}" ]; }];
      }
    ];
    alertmanagers = [{
      static_configs = [{ targets = [ "127.0.0.1:${toString alertmanagerPort}" ]; }];
    }];

    # The systemd-unit-failure alert (and all other alerting) is owned by
    # Grafana Alerting now, not Prometheus rules + Alertmanager. node_exporter
    # still publishes node_systemd_unit_state{name=...,state=...}; a
    # Grafana-managed rule queries it, and Grafana's notification policy adds
    # the 22:00–07:00 mute timing + routes to the ntfy contact point. Keeping
    # the rule in Grafana (rather than here) is what makes the schedule and
    # severity editable from the web UI. Alertmanager stays installed but
    # idle (no Prometheus rules feed it).

    # node_exporter — host metrics (CPU, RAM, disk, network).
    exporters.node = {
      enable = true;
      port = nodeExporterPort;
      listenAddress = "127.0.0.1";
      enabledCollectors = [ "systemd" "processes" ];
      # node_exporter's default systemd collector excludes mount/device/
      # automount/scope/slice. Override to include mount units so the
      # SystemdUnitFailed alert catches mount failures too (e.g. the WD My
      # Book on /mnt/primary/D6 that drops out on reboot). Keep
      # device/automount/scope/slice excluded — those are noisy and not
      # actionable.
      extraFlags = [
        ''--collector.systemd.unit-exclude=.+\.(automount|device|scope|slice)''
      ];
    };

    # Alertmanager — routes alerts to receivers. Receiver wiring is in a
    # follow-up commit (needs the ntfy webhook shim service).
    alertmanager = {
      enable = true;
      port = alertmanagerPort;
      listenAddress = "127.0.0.1";
      configuration = {
        route = {
          group_by = [ "alertname" ];
          group_wait = "30s";
          group_interval = "5m";
          repeat_interval = "12h";
          receiver = "ntfy";
          # RiverwatchFetchFailing is informational and self-resolving — route it
          # to a receiver that does NOT send "RESOLVED" pings (Chris doesn't want
          # the all-clear). Everything else stays on the default receiver.
          routes = [{
            matchers = [ ''alertname="RiverwatchFetchFailing"'' ];
            receiver = "ntfy-noresolve";
          }];
        };
        receivers = [
          {
            name = "ntfy";
            webhook_configs = [{
              url = "http://127.0.0.1:9095/alert";
              send_resolved = true;
            }];
          }
          {
            name = "ntfy-noresolve";
            webhook_configs = [{
              url = "http://127.0.0.1:9095/alert";
              send_resolved = false;
            }];
          }
        ];
      };
    };
  };

  # Grafana — visualization. Provisioned with Prometheus as the default
  # datasource so dashboards "just work" on first login.
  #
  # NixOS 26.05 dropped Grafana's hard-coded default secret_key, so we
  # generate a random one on first deploy and reference it via $__file.
  systemd.services.grafana-secret-key = {
    description = "Generate Grafana secret_key on first boot";
    wantedBy = [ "multi-user.target" ];
    before = [ "grafana.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      install -d -o grafana -g grafana -m 0750 /var/lib/grafana
      KEY=/var/lib/grafana/secret_key
      if [ ! -s "$KEY" ]; then
        ${pkgs.openssl}/bin/openssl rand -base64 32 > "$KEY"
        chown grafana:grafana "$KEY"
        chmod 0640 "$KEY"
      fi
      # Admin password — needed to log in and edit alert rules / policies /
      # mute timings in the GUI (anonymous access is Viewer-only). Generated
      # once; referenced via $__file below. Change it by editing this file
      # (Grafana re-applies it on restart) or rotate in the UI.
      PW=/var/lib/grafana/admin_password
      if [ ! -s "$PW" ]; then
        # No trailing newline — Grafana's $__file{} uses the bytes verbatim as
        # the admin password, so a stray newline makes the password un-typeable.
        ${pkgs.openssl}/bin/openssl rand -base64 18 | tr -d '\n' > "$PW"
        chown grafana:grafana "$PW"
        chmod 0640 "$PW"
      fi
    '';
  };

  # OIDC client secret for Grafana's generic_oauth (Phase 2 SSO). Grafana reads
  # it via $__file as the grafana user → owner=grafana. The matching pbkdf2 hash
  # is in the Authelia client config (authelia.nix).
  sops.secrets."grafana-oidc-secret" = {
    sopsFile = ../../secrets/grafana-oidc-secret.yaml;
    key = "grafana-oidc-secret";
    owner = "grafana";
  };

  services.grafana = {
    enable = true;
    # Infinity datasource — lets us pull arbitrary JSON (NWPS forecast)
    # directly into a panel alongside Prometheus observed data.
    declarativePlugins = with pkgs.grafanaPlugins; [
      yesoreyeram-infinity-datasource
    ];
    settings = {
      server = {
        http_addr = "127.0.0.1";
        http_port = grafanaPort;
        domain = grafanaHost;
        root_url = "https://${grafanaHost}/";
      };
      security = {
        secret_key = "$__file{/var/lib/grafana/secret_key}";
        admin_user = "admin";
        admin_password = "$__file{/var/lib/grafana/admin_password}";
        # Allow iframe embedding so the Homepage tile can render a panel.
        # Grafana is already Tailscale-only; nginx isn't adding X-Frame-Options.
        allow_embedding = true;
      };
      analytics.reporting_enabled = false;
      # Anonymous Viewer so the Homepage iframe (no login) can fetch the panel.
      # Same Tailscale-only perimeter as the rest of Grafana — anyone who can
      # reach the URL was already trusted.
      "auth.anonymous" = {
        enabled = true;
        org_role = "Viewer";
        org_name = "Main Org.";
      };
      # OIDC SSO via Authelia (Phase 2). Adds a "Sign in with Authelia" button
      # ALONGSIDE anon-viewer (the Homepage embed) and the admin login form —
      # none are removed. Authelia group `admins` → Grafana Admin, else Viewer.
      "auth.generic_oauth" = {
        enabled = true;
        name = "Authelia";
        icon = "signin";
        client_id = "grafana";
        client_secret = "$__file{${config.sops.secrets."grafana-oidc-secret".path}}";
        scopes = "openid profile email groups";
        auth_url = "https://auth.rosemaryacres.com/api/oidc/authorization";
        token_url = "https://auth.rosemaryacres.com/api/oidc/token";
        api_url = "https://auth.rosemaryacres.com/api/oidc/userinfo";
        login_attribute_path = "preferred_username";
        groups_attribute_path = "groups";
        role_attribute_path = "contains(groups[*], 'admins') && 'Admin' || 'Viewer'";
        allow_sign_up = true;
        use_pkce = true;
      };
    };
    provision = {
      enable = true;
      datasources.settings.datasources = [
        {
          name = "Prometheus";
          # Pinned so the codified alert rules + dashboards (which reference
          # this uid) always resolve, regardless of provisioning order.
          uid = "PBFA97CFB590B2093";
          type = "prometheus";
          access = "proxy";
          url = "http://127.0.0.1:${toString prometheusPort}";
          isDefault = true;
        }
        {
          # Used by panel 7 to overlay the NWPS stage forecast.
          # No base URL — the panel target supplies the full URL per-query.
          name = "NWPS";
          uid = "nwps-infinity";
          type = "yesoreyeram-infinity-datasource";
          access = "proxy";
          jsonData = {
            # Restrict outbound URLs so this datasource can't be misused as
            # a generic SSRF tool. NWPS only.
            allowedHosts = [ "https://api.water.noaa.gov" ];
            timeoutInSeconds = 30;
          };
        }
      ];

      # NOTE: dashboards are intentionally NOT provisioned. Grafana 13's
      # apiserver does not grant the anonymous Viewer read access to
      # provisioned dashboards in the root/General folder (the Homepage iframe
      # 403s), and its resource manager wedges provisioned dashboards so they
      # can't be moved or deleted. Imperative dashboards (live in
      # /var/lib/grafana) work fine for anonymous embeds — that's why the
      # Riverwatch graph works. gromit-temps lives in the "Temperatures" folder
      # (which grants Viewer:View). JSON snapshots are kept under
      # ./grafana/dashboards/ as re-import references. Alerting (above) IS
      # provisioned — it has no such limitation.

      # Alerting codified from the live DB so it's no longer set up ad hoc.
      # Exported in provisioning format and embedded via fromJSON so the
      # working config is reproduced verbatim. Provisioned alerting is
      # file-managed (read-only in the GUI), which is the point.
      #   contact-points : ntfy webhook -> the ntfy shim
      #   mute-timings   : "nights" (22:00-07:00 America/New_York)
      #   policies       : two-tier — severity=critical bypasses the mute,
      #                    everything else routes through the muted catch-all
      #   alert-rules    : systemd unit failures + temperature warn/critical
      alerting = {
        contactPoints.settings = builtins.fromJSON (builtins.readFile ./grafana/contact-points.json);
        muteTimings.settings   = builtins.fromJSON (builtins.readFile ./grafana/mute-timings.json);
        policies.settings      = builtins.fromJSON (builtins.readFile ./grafana/policies.json);
        rules.settings         = builtins.fromJSON (builtins.readFile ./grafana/alert-rules.json);
      };
    };
  };

  # nginx vhosts: grafana + prometheus, Tailscale-only via Cloudflare DNS-01.
  services.nginx.virtualHosts."${grafanaHost}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString grafanaPort}";
      proxyWebsockets = true;
      extraConfig = ''
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
      '';
    };
  };

  services.nginx.virtualHosts."${prometheusHost}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString prometheusPort}";
      extraConfig = ''
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
      '';
    };
  };
}
