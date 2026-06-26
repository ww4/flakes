# Riverwatch — Prometheus exporter for NOAA NWPS river gauges.
#
# Long-running Python service that polls NWPS every RIVERWATCH_INTERVAL
# seconds for each configured gauge and exposes the data as Prometheus
# metrics on :9201/metrics. Prometheus scrapes locally via 127.0.0.1.
#
# Metrics exposed (label: gauge):
#   riverwatch_stage_ft               current observed stage (ft)
#   riverwatch_flow_kcfs              current observed flow (kcfs)
#   riverwatch_forecast_crest_ft      forecast peak (ft); NWPS returns the
#                                     peak of the forecast series as
#                                     status.forecast.primary
#   riverwatch_observation_age_seconds  seconds since the observation
#   riverwatch_flood_threshold_ft     {category}   stage thresholds for
#                                                  action/minor/moderate/major
#   riverwatch_flood_category_active  {category}   1 if gauge currently in
#                                                  this category, 0 otherwise
#   riverwatch_fetch_success                       1 if last poll succeeded
#
# A -999 sentinel from NWPS is treated as missing and the metric is not set
# (so Prometheus shows a gap rather than a misleading -999 spike).
{ config, lib, pkgs, ... }:

let
  port = 9201;
  gauges = "LPTK2,GSTK2";

  exporter = pkgs.writers.writePython3Bin "riverwatch-exporter" {
    libraries = with pkgs.python3Packages; [ prometheus-client httpx ];
    flakeIgnore = [ "E501" ];
  } ''
    import os
    import time
    from datetime import datetime, timezone

    import httpx
    from prometheus_client import start_http_server, Gauge

    NWPS = "https://api.water.noaa.gov/nwps/v1/gauges"
    GAUGES = os.environ.get("RIVERWATCH_GAUGES", "LPTK2,GSTK2").split(",")
    PORT = int(os.environ.get("RIVERWATCH_PORT", "9201"))
    INTERVAL = int(os.environ.get("RIVERWATCH_INTERVAL_SECONDS", "600"))

    CATEGORIES = ["action", "minor", "moderate", "major", "no_flooding"]
    SENTINELS = (None, -999, -9999, "")

    stage_ft = Gauge("riverwatch_stage_ft", "Current observed stage (ft)", ["gauge"])
    flow_kcfs = Gauge("riverwatch_flow_kcfs", "Current observed flow (kcfs)", ["gauge"])
    forecast_crest_ft = Gauge("riverwatch_forecast_crest_ft", "Forecast peak stage (ft) — peak of forecast series", ["gauge"])
    observed_age = Gauge("riverwatch_observation_age_seconds", "Seconds since the observation was valid", ["gauge"])
    flood_threshold = Gauge("riverwatch_flood_threshold_ft", "Stage threshold for this flood category", ["gauge", "category"])
    flood_active = Gauge("riverwatch_flood_category_active", "1 if the gauge is currently in this category", ["gauge", "category"])
    fetch_success = Gauge("riverwatch_fetch_success", "1 if the last NWPS fetch for this gauge succeeded", ["gauge"])


    def fetch(gauge: str) -> None:
        try:
            with httpx.Client(timeout=20.0) as client:
                r = client.get(f"{NWPS}/{gauge}")
                r.raise_for_status()
                data = r.json()
            obs = (data.get("status") or {}).get("observed") or {}
            fcst = (data.get("status") or {}).get("forecast") or {}

            if obs.get("primary") not in SENTINELS:
                stage_ft.labels(gauge=gauge).set(obs["primary"])
            if obs.get("secondary") not in SENTINELS:
                flow_kcfs.labels(gauge=gauge).set(obs["secondary"])
            if fcst.get("primary") not in SENTINELS:
                forecast_crest_ft.labels(gauge=gauge).set(fcst["primary"])

            vt = obs.get("validTime")
            if vt:
                t = datetime.fromisoformat(vt.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - t).total_seconds()
                observed_age.labels(gauge=gauge).set(age)

            cats = ((data.get("flood") or {}).get("categories") or {})
            for cat in ("action", "minor", "moderate", "major"):
                th = (cats.get(cat) or {}).get("stage")
                if th not in SENTINELS:
                    flood_threshold.labels(gauge=gauge, category=cat).set(th)

            active = obs.get("floodCategory", "no_flooding")
            for cat in CATEGORIES:
                flood_active.labels(gauge=gauge, category=cat).set(
                    1 if cat == active else 0
                )

            fetch_success.labels(gauge=gauge).set(1)
            print(f"[{gauge}] stage={obs.get('primary')} flow={obs.get('secondary')} "
                  f"flood={active} forecast_crest={fcst.get('primary')}")
        except Exception as e:
            print(f"[{gauge}] fetch failed: {e!r}")
            fetch_success.labels(gauge=gauge).set(0)


    def main() -> None:
        start_http_server(PORT)
        print(f"riverwatch_exporter listening on :{PORT}, "
              f"gauges={GAUGES}, interval={INTERVAL}s")
        while True:
            for g in GAUGES:
                fetch(g)
            time.sleep(INTERVAL)


    if __name__ == "__main__":
        main()
  '';
in {
  systemd.services.riverwatch-exporter = {
    description = "Riverwatch — Prometheus exporter for NOAA NWPS river gauges";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    environment = {
      RIVERWATCH_PORT = toString port;
      RIVERWATCH_GAUGES = gauges;
      RIVERWATCH_INTERVAL_SECONDS = "600";
    };
    serviceConfig = {
      Type = "simple";
      ExecStart = "${exporter}/bin/riverwatch-exporter";
      DynamicUser = true;
      Restart = "on-failure";
      RestartSec = 30;
      # Hardening — read-only filesystem, no privileged caps, no network
      # except outbound.
      ProtectSystem = "strict";
      ProtectHome = true;
      NoNewPrivileges = true;
      PrivateTmp = true;
    };
  };

  # Add the exporter as a Prometheus scrape target. Merged with the
  # scrapeConfigs already declared in monitoring.nix.
  services.prometheus.scrapeConfigs = [{
    job_name = "riverwatch";
    static_configs = [{ targets = [ "127.0.0.1:${toString port}" ]; }];
    # Pull every 5 min — exporter only refreshes upstream every 10 min, no
    # need to hammer it. Prometheus will see the same values twice in a row
    # between refreshes; not a problem.
    scrape_interval = "5m";
  }];

  # Grafana dashboard for the river data, provisioned read-only from the
  # flake (so the dashboard is reproducible and version-controlled). Live
  # at https://grafana.rosemaryacres.com/d/riverwatch.
  services.grafana.provision.dashboards.settings.providers = [{
    name = "riverwatch";
    type = "file";
    folder = "Riverwatch";
    disableDeletion = true;
    options.path = pkgs.runCommand "riverwatch-dashboards" { } ''
      mkdir -p $out
      ln -s ${./riverwatch-dashboard.json} $out/riverwatch.json
    '';
  }];

  # Alert rules. Routed via Alertmanager → ntfy shim (see
  # alertmanager-ntfy.nix) onto the gromit-alerts topic.
  services.prometheus.rules = [
    (builtins.toJSON {
      groups = [{
        name = "riverwatch";
        interval = "1m";
        rules = [
          # Active flood — gauge currently sitting in action/minor/moderate/major.
          # Severity scales with category.
          {
            alert = "RiverFloodAction";
            expr = ''riverwatch_flood_category_active{category="action"} == 1'';
            for = "5m";
            labels.severity = "warning";
            annotations = {
              summary = "{{ $labels.gauge }} at ACTION stage";
              description = "Gauge {{ $labels.gauge }} has entered the ACTION flood category (NOAA-defined threshold). Stage is being monitored.";
            };
          }
          {
            alert = "RiverFloodMinor";
            expr = ''riverwatch_flood_category_active{category="minor"} == 1'';
            for = "5m";
            labels.severity = "warning";
            annotations = {
              summary = "{{ $labels.gauge }} at MINOR flood stage";
              description = "Gauge {{ $labels.gauge }} has reached MINOR flood category (NOAA threshold).";
            };
          }
          {
            alert = "RiverFloodModerate";
            expr = ''riverwatch_flood_category_active{category="moderate"} == 1'';
            for = "5m";
            labels.severity = "critical";
            annotations = {
              summary = "{{ $labels.gauge }} at MODERATE flood stage";
              description = "Gauge {{ $labels.gauge }} has reached MODERATE flood category. Property damage possible.";
            };
          }
          {
            alert = "RiverFloodMajor";
            expr = ''riverwatch_flood_category_active{category="major"} == 1'';
            for = "5m";
            labels.severity = "critical";
            annotations = {
              summary = "{{ $labels.gauge }} at MAJOR flood stage";
              description = "Gauge {{ $labels.gauge }} has reached MAJOR flood category. Significant impact expected.";
            };
          }

          # Forecast says we'll cross the action threshold within the forecast
          # horizon (NWPS forecasts run ~3 days out for these gauges).
          {
            alert = "RiverForecastCrestAboveAction";
            expr = ''riverwatch_forecast_crest_ft >= on(gauge) group_right riverwatch_flood_threshold_ft{category="action"}'';
            for = "10m";
            labels.severity = "warning";
            annotations = {
              summary = "{{ $labels.gauge }} forecast crest above ACTION stage";
              description = "NWPS forecast for {{ $labels.gauge }} predicts the river will reach at least the ACTION threshold within the forecast horizon.";
            };
          }

          # Rate of rise — stage climbed more than 1.5 ft in the last 6 hours.
          # Needs at least 6 h of collected data before it can fire.
          {
            alert = "RiverRapidRise";
            expr = ''(riverwatch_stage_ft - (riverwatch_stage_ft offset 6h)) > 1.5'';
            for = "15m";
            labels.severity = "warning";
            annotations = {
              summary = "{{ $labels.gauge }} rising rapidly";
              description = "Stage at {{ $labels.gauge }} has risen more than 1.5 ft in the last 6 hours — current value {{ $value }} ft above 6h-ago value.";
            };
          }

          # Operational: NWPS observation hasn't updated in 4+ hours.
          {
            alert = "RiverObservationStale";
            expr = "riverwatch_observation_age_seconds > 14400";
            for = "30m";
            labels.severity = "info";
            annotations = {
              summary = "{{ $labels.gauge }} observation stale (>4 h)";
              description = "Last NWPS observation for {{ $labels.gauge }} is {{ $value | humanizeDuration }} old. Possible NWPS / NOAA upstream issue.";
            };
          }

          # Operational: exporter can't reach NWPS for 4+ hours. Low-key by
          # design — info severity, and a long 4h `for` so a transient internet
          # blip (during which the notification wouldn't reach Chris anyway) is
          # ignored; only a sustained NWPS/connectivity problem fires. Routed in
          # monitoring.nix to a no-resolved receiver (no "RESOLVED" ping).
          {
            alert = "RiverwatchFetchFailing";
            expr = "riverwatch_fetch_success == 0";
            for = "4h";
            labels.severity = "info";
            annotations = {
              summary = "Riverwatch exporter can't reach NWPS for {{ $labels.gauge }}";
              description = "The riverwatch_exporter has been failing to fetch {{ $labels.gauge }} for 4+ hours. Check journalctl -u riverwatch-exporter (often just an NWPS/NOAA upstream or internet outage).";
            };
          }
        ];
      }];
    })
  ];
}
