# owen-usage: the house's daily electric usage, from Owen Electric's member
# portal, into Grafana.
#
# A small Python service (owen_usage.py) signs in once a day at 07:30 with
# Chris's portal login, calls the JSON endpoint behind the portal's Usage
# History chart, and keeps the whole daily history (2023-11-01 on; the portal
# has nothing older) in /var/lib/owen-usage/daily.json. On its FIRST run it
# fetches the full history itself, so no household data lives in this repo,
# which is mirrored to GitHub. After that it refetches the last 30 days each
# morning, which also picks up any corrections the co-op posts late.
#
# It serves, on 127.0.0.1:9208 only:
#   /daily.json    one row per day: kWh, meter reading, low/high/avg °F, HDD/CDD
#   /monthly.json  calendar-month totals
#   /summary.json  latest day, last 7/30 days, same 30 days last year, YTD
#   /metrics       fetch health for Prometheus (alert below)
#
# Older history from scanned paper bills goes in /var/lib/owen-usage/bills.json
# (format in owen_usage.py, bill_days). Each bill is spread evenly over its
# service days. Also state, not flake, for the same GitHub reason.
#
# The login is sops secret `owen-usage` (KEY=VALUE lines, read literally by the
# script via LoadCredential, never sourced). Endpoint details and the data's
# quirks (zero-padding before 2023-11-01, the 2025 flood days coming back
# empty) are in the owen-electric-usage-api agent memory and the script header.
{ config, pkgs, ... }:

let
  port = 9208;
  url = "http://127.0.0.1:${toString port}";

  owenUsage = pkgs.writers.writePython3Bin "owen-usage" {
    libraries = [ pkgs.python3Packages.httpx ];
    flakeIgnore = [ "E501" "W503" "W504" ];
  } (builtins.readFile ./owen_usage.py);
in
{
  sops.secrets."owen-usage" = {
    sopsFile = ../../../secrets/owen-usage.yaml;
    key = "owen-usage";
    # Read by systemd (root) for LoadCredential; the service itself runs as a
    # DynamicUser and only ever sees its private copy.
    owner = "root";
    mode = "0400";
  };

  systemd.services.owen-usage = {
    description = "owen-usage: daily household kWh from the Owen Electric portal";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    environment = {
      OWEN_USAGE_PORT = toString port;
      OWEN_USAGE_RUN_AT = "07:30";
      OWEN_USAGE_REFRESH_DAYS = "30";
      TZ = config.time.timeZone;
    };
    serviceConfig = {
      Type = "simple";
      ExecStart = "${owenUsage}/bin/owen-usage";
      DynamicUser = true;
      StateDirectory = "owen-usage";
      LoadCredential = [ "login:${config.sops.secrets."owen-usage".path}" ];
      Restart = "on-failure";
      RestartSec = 60;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      NoNewPrivileges = true;
      PrivateDevices = true;
      ProtectKernelTunables = true;
      ProtectControlGroups = true;
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
    };
  };

  services.prometheus.scrapeConfigs = [{
    job_name = "owen-usage";
    static_configs = [{ targets = [ "127.0.0.1:${toString port}" ]; }];
    scrape_interval = "5m";
  }];

  # Not urgent by nature: the data is a day behind anyway. Fires only after
  # three days with no successful fetch (password changed, portal redesigned),
  # info severity, held overnight.
  services.prometheus.rules = [
    (builtins.toJSON {
      groups = [{
        name = "owen-usage";
        rules = [{
          alert = "OwenUsageStale";
          expr = "time() - owen_usage_last_success_timestamp_seconds > 3 * 86400";
          for = "1h";
          labels.severity = "info";
          annotations = {
            summary = "Owen Electric usage has not updated in 3+ days";
            description = "owen-usage has had no successful portal fetch for {{ $value | humanizeDuration }}. Check journalctl -u owen-usage: a changed portal password or a portal redesign are the likely causes.";
            url = "https://grafana.rosemaryacres.com/d/owen-usage";
          };
        }];
      }];
    })
  ];

  homelab.monitoring.extraAlertmanagerRoutes = [{
    matchers = [ ''alertname="OwenUsageStale"'' ];
    receiver = "ntfy-noresolve";
    mute_time_intervals = [ "nights" ];
  }];

  homelab.monitoring.extraDatasources = [{
    name = "OwenUsage";
    uid = "owen-usage-infinity";
    type = "yesoreyeram-infinity-datasource";
    access = "proxy";
    # Same SSRF fence as the other Infinity sources: this one can reach the
    # owen-usage service and nothing else.
    jsonData = {
      allowedHosts = [ url ];
      timeoutInSeconds = 20;
    };
  }];

  services.grafana.provision.dashboards.settings.providers = [{
    name = "owen-usage";
    type = "file";
    folder = "Energy";
    disableDeletion = true;
    options.path = pkgs.runCommand "owen-usage-dashboards" { } ''
      mkdir -p $out
      ln -s ${./dashboard.json} $out/owen-usage.json
    '';
  }];
}
