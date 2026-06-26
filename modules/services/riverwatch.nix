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

  # Stateful rapid-rise notifier. Replaces the old Prometheus RiverRapidRise
  # alert (which fired per-gauge and couldn't compare against the *last
  # notification*). Runs on a timer; for each gauge it asks Prometheus for the
  # stage change over RISE_WINDOW, and decides whether to notify based on state
  # persisted in StateDirectory:
  #   - notify when a gauge first crosses RISE_BASE_FT (a new rapid rise), OR
  #   - when an already-rising gauge's rise climbs > RISE_ESCALATE_FRACTION
  #     (default 25%) ABOVE its rise at the last notification — a relative
  #     "significant change" with no band edges to flap across, OR
  #   - at most once per RISE_MIN_INTERVAL_SECONDS (12 h) as a "still rising"
  #     reminder while any gauge stays active.
  # All currently-rising gauges are reported in ONE combined message. When a
  # gauge drops back below RISE_BASE_FT its baseline is cleared (event over).
  notifier = pkgs.writers.writePython3Bin "riverwatch-notifier" {
    libraries = [ pkgs.python3Packages.httpx ];
    flakeIgnore = [ "E501" ];
  } ''
    import json
    import os
    import sys
    import time

    import httpx

    PROM = os.environ.get("PROM_URL", "http://127.0.0.1:9090")
    NTFY = os.environ.get("NTFY_SERVER", "http://127.0.0.1:8090")
    TOPIC = os.environ.get("NTFY_TOPIC", "gromit-alerts")
    STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/riverwatch-notifier/state.json")
    WINDOW = os.environ.get("RISE_WINDOW", "6h")
    BASE_FT = float(os.environ.get("RISE_BASE_FT", "1.5"))
    ESCALATE = float(os.environ.get("RISE_ESCALATE_FRACTION", "0.25"))
    MIN_INTERVAL = int(os.environ.get("RISE_MIN_INTERVAL_SECONDS", "43200"))


    def query_rises():
        expr = f"(riverwatch_stage_ft - riverwatch_stage_ft offset {WINDOW})"
        r = httpx.get(f"{PROM}/api/v1/query", params={"query": expr}, timeout=15.0)
        r.raise_for_status()
        rises = {}
        for series in r.json().get("data", {}).get("result", []):
            gauge = series.get("metric", {}).get("gauge")
            if not gauge:
                continue
            try:
                rises[gauge] = float(series["value"][1])
            except (KeyError, ValueError, TypeError):
                continue
        return rises


    def load_state():
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            return {"last_notify": float(s.get("last_notify", 0)),
                    "gauges": {k: float(v) for k, v in (s.get("gauges") or {}).items()}}
        except (OSError, ValueError):
            return {"last_notify": 0.0, "gauges": {}}


    def save_state(state):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)


    def post(title, body, priority, tags):
        httpx.post(f"{NTFY}/{TOPIC}", content=body.encode(),
                   headers={"Title": title, "Priority": str(priority), "Tags": tags},
                   timeout=10.0).raise_for_status()


    def main():
        now = time.time()
        try:
            rises = query_rises()
        except Exception as e:
            print(f"prometheus query failed: {e!r}", file=sys.stderr)
            return

        state = load_state()
        last_notify = state["last_notify"]
        last = state["gauges"]  # gauge -> rise (ft) at the last notification

        active = {g: v for g, v in rises.items() if v > BASE_FT}

        accel = {g for g, v in active.items() if g in last and v > (1 + ESCALATE) * last[g]}
        fresh = {g for g in active if g not in last}
        periodic = bool(active) and (now - last_notify) >= MIN_INTERVAL

        # Event over for any gauge that fell back below the base — reset baseline.
        for g in list(last):
            if g not in active:
                del last[g]

        if active and (accel or fresh or periodic):
            lines = []
            for g in sorted(active):
                if g in accel:
                    note = "   ↑ accelerating"
                elif g in fresh:
                    note = "   ⚠ rising"
                else:
                    note = ""
                lines.append(f"• {g}: +{active[g]:.1f} ft / {WINDOW}{note}")
            if accel:
                why = "rate of rise jumped >25% since the last alert"
            elif fresh:
                why = "rapid rise started"
            else:
                why = "still rising (12h update)"
            body = "River rising rapidly:\n" + "\n".join(lines) + f"\n\n({why})"
            try:
                post("River rising rapidly", body, 4, "ocean,chart_with_upwards_trend")
            except Exception as e:
                print(f"ntfy post failed: {e!r}", file=sys.stderr)
                return  # don't advance state if the notification didn't go out
            for g, v in active.items():
                last[g] = v
            last_notify = now
            print(f"notified ({why}): {active}")
        else:
            print(f"no notify. active={active} since_last={int(now - last_notify)}s")

        save_state({"last_notify": last_notify, "gauges": last})


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

  # Rapid-rise notifier (stateful; see the `notifier` script above). Runs on a
  # timer, keeps its state in /var/lib/riverwatch-notifier.
  systemd.services.riverwatch-notifier = {
    description = "Riverwatch — rapid-rise notifier (relative threshold + 12h floor)";
    after = [ "network-online.target" "prometheus.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${notifier}/bin/riverwatch-notifier";
      DynamicUser = true;
      StateDirectory = "riverwatch-notifier";
      Environment = [
        "STATE_FILE=/var/lib/riverwatch-notifier/state.json"
        "RISE_WINDOW=6h"                      # window the "rise" is measured over
        "RISE_BASE_FT=1.5"                    # ft over the window to count as "rising rapidly"
        "RISE_ESCALATE_FRACTION=0.25"         # re-alert if the rise climbs >25% above last alert
        "RISE_MIN_INTERVAL_SECONDS=43200"     # otherwise at most one alert per 12h
      ];
      ProtectSystem = "strict";
      ProtectHome = true;
      NoNewPrivileges = true;
      PrivateTmp = true;
    };
  };
  systemd.timers.riverwatch-notifier = {
    description = "Run the riverwatch rapid-rise notifier periodically";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "10m";
      OnUnitActiveSec = "15m";
      Persistent = true;
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

          # NOTE: rate-of-rise alerting is handled by the stateful
          # riverwatch-notifier service (above), not a Prometheus rule — it needs
          # to compare against the *last notification* (relative 25% escalation +
          # 12h floor), which Alertmanager can't express.

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

          # Operational: exporter can't reach NWPS for 30+ minutes.
          {
            alert = "RiverwatchFetchFailing";
            expr = "riverwatch_fetch_success == 0";
            for = "30m";
            labels.severity = "warning";
            annotations = {
              summary = "Riverwatch exporter can't reach NWPS for {{ $labels.gauge }}";
              description = "The riverwatch_exporter has been failing to fetch {{ $labels.gauge }} for more than 30 minutes. Check journalctl -u riverwatch-exporter.";
            };
          }
        ];
      }];
    })
  ];
}
