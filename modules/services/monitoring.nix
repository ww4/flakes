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
    retentionTime = "365d";
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

    # node_exporter — host metrics (CPU, RAM, disk, network).
    exporters.node = {
      enable = true;
      port = nodeExporterPort;
      listenAddress = "127.0.0.1";
      enabledCollectors = [ "systemd" "processes" ];
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
            url = "http://127.0.0.1:9094/alert";
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
    settings = {
      server = {
        http_addr = "127.0.0.1";
        http_port = grafanaPort;
        domain = grafanaHost;
        root_url = "https://${grafanaHost}/";
      };
      security.secret_key = "$__file{/var/lib/grafana/secret_key}";
      analytics.reporting_enabled = false;
    };
    provision = {
      enable = true;
      datasources.settings.datasources = [{
        name = "Prometheus";
        type = "prometheus";
        access = "proxy";
        url = "http://127.0.0.1:${toString prometheusPort}";
        isDefault = true;
      }];
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
