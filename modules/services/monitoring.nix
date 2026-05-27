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

    # Generic systemd-unit-failure alert. node_exporter's systemd collector
    # publishes node_systemd_unit_state{name=...,state=...} with value 1 for
    # the current state. We fire when any unit has state="failed" for 5 min.
    # This is the unified replacement for per-service onFailure wiring —
    # any new systemd job that goes red gets surfaced without bespoke
    # plumbing in each module.
    #
    # Lives in its own file via ruleFiles instead of services.prometheus.rules
    # because the rules option concatenates list entries into a single file as
    # separate JSON documents, which Prometheus parses as only the first doc —
    # silently dropping later entries (like this one if riverwatch comes first).
    ruleFiles = [
      (pkgs.writeText "systemd-rules.yml" (builtins.toJSON {
        groups = [{
          name = "systemd";
          interval = "1m";
          rules = [
            {
              alert = "SystemdUnitFailed";
              expr = ''node_systemd_unit_state{state="failed"} == 1'';
              "for" = "5m";
              labels = { severity = "warning"; };
              annotations = {
                summary = "{{ $labels.name }} in failed state";
                description = "Systemd unit {{ $labels.name }} on {{ $labels.instance }} has been in 'failed' state for 5 minutes. Check: journalctl -u {{ $labels.name }} --no-pager | tail";
              };
            }
          ];
        }];
      }))
    ];

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
        };
        receivers = [{
          name = "ntfy";
          # Placeholder webhook — replaced when the shim service lands.
          # For now Alertmanager will fire but the POST won't go anywhere
          # meaningful until the shim is wired up. That's OK; we have no
          # alert rules defined yet.
          webhook_configs = [{
            url = "http://127.0.0.1:9095/alert";
            send_resolved = true;
          }];
        }];
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
      KEY=/var/lib/grafana/secret_key
      if [ ! -s "$KEY" ]; then
        install -d -o grafana -g grafana -m 0750 /var/lib/grafana
        ${pkgs.openssl}/bin/openssl rand -base64 32 > "$KEY"
        chown grafana:grafana "$KEY"
        chmod 0640 "$KEY"
      fi
    '';
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
    };
    provision = {
      enable = true;
      datasources.settings.datasources = [
        {
          name = "Prometheus";
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
