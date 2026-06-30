# Alertmanager → ntfy webhook shim.
#
# Alertmanager fires JSON to http://127.0.0.1:9094/alert; this service
# unpacks each alert in the batch, formats title/priority/tags, and posts
# to the existing self-hosted ntfy server on the gromit-alerts topic
# (same channel the gromit-notify helper + restic jobs already use).
#
# Severity → ntfy priority:  critical=5, warning=3, info=2, default=3.
# A 'resolved' status sends a "RESOLVED:" notification at low priority
# with a checkmark tag.
{ config, lib, pkgs, ... }:

let
  port = 9095;  # 9094 is alertmanager's cluster (gossip) port; 9095 is free
  ntfyServer = "http://127.0.0.1:8090";
  ntfyTopic = "gromit-alerts";

  shim = pkgs.writers.writePython3Bin "alertmanager-ntfy-shim" {
    libraries = with pkgs.python3Packages; [ fastapi uvicorn httpx ];
    flakeIgnore = [ "E501" ];
  } ''
    import os
    import sys

    import httpx
    import uvicorn
    from fastapi import FastAPI, Request

    NTFY = os.environ.get("NTFY_SERVER", "http://127.0.0.1:8090")
    TOPIC = os.environ.get("NTFY_TOPIC", "gromit-alerts")
    PORT = int(os.environ.get("SHIM_PORT", "9094"))
    # Tapping the notification opens this DNS page (ntfy "Click" action). An
    # alert can override it with a `url` annotation; otherwise this default.
    DEFAULT_CLICK = os.environ.get("DEFAULT_CLICK_URL", "https://grafana.rosemaryacres.com")

    app = FastAPI()

    SEVERITY_PRIORITY = {"critical": 5, "warning": 3, "info": 2}
    SEVERITY_TAG = {"critical": "rotating_light", "warning": "warning", "info": "information_source"}
    # Internal scrape-target labels — never useful in a notification (this is the
    # "127.0.0.1:9201" noise). Hidden from the body. The drive-temp metric's
    # identifying labels (device/model/bus/rotational) are surfaced in the per
    # alert summary instead, so they're hidden here to avoid duplication.
    HIDDEN_LABELS = ("alertname", "severity", "instance", "job",
                     "device", "model", "bus", "rotational")


    def _single_line(a):
        """One alert -> its summary (the per-instance detail, e.g. 'sdj WDC… — 59 °C')."""
        ann = a.get("annotations") or {}
        labels = a.get("labels") or {}
        return (ann.get("summary") or ann.get("description")
                or ", ".join(f"{k}={v}" for k, v in labels.items() if k not in HIDDEN_LABELS)
                or labels.get("alertname", "Alert"))


    @app.post("/alert")
    async def alert(req: Request):
        payload = await req.json()
        alerts = payload.get("alerts", []) or []
        # Group by (status, alertname) so multiple instances of one rule — e.g.
        # several drives over the temp limit at once — collapse into ONE
        # notification listing them all, instead of one ding per drive.
        groups = {}
        for a in alerts:
            labels = a.get("labels") or {}
            key = (a.get("status", "firing"), labels.get("alertname", "Alert"))
            groups.setdefault(key, []).append(a)

        sent = 0
        async with httpx.AsyncClient(timeout=10.0) as client:
            for (status, alertname), group in groups.items():
                labels0 = group[0].get("labels") or {}
                ann0 = group[0].get("annotations") or {}
                severity = labels0.get("severity", "warning")

                if status == "resolved":
                    title = f"RESOLVED: {alertname}"
                    priority = 2
                    tag = "white_check_mark"
                else:
                    title = f"{severity.upper()}: {alertname}"
                    priority = SEVERITY_PRIORITY.get(severity, 3)
                    tag = SEVERITY_TAG.get(severity, "warning")

                if len(group) == 1:
                    a = group[0]
                    annotations = a.get("annotations") or {}
                    summary = _single_line(a)
                    description = annotations.get("description", "")
                    body = summary
                    if description and description != summary:
                        body = f"{summary}\n\n{description}"
                    extra = {k: v for k, v in (a.get("labels") or {}).items() if k not in HIDDEN_LABELS}
                    if extra:
                        body += "\n\n" + ", ".join(f"{k}={v}" for k, v in extra.items())
                else:
                    # Multiple instances: header + one bullet per instance.
                    title = f"{title} ({len(group)})"
                    body = f"{len(group)} firing:\n" + "\n".join(f"• {_single_line(a)}" for a in group)

                click = ann0.get("url") or DEFAULT_CLICK
                # An alert can route itself to a different ntfy topic via a
                # `topic` annotation (e.g. riverwatch → its own topic); default
                # is the shared topic.
                topic = ann0.get("topic") or TOPIC

                try:
                    r = await client.post(
                        f"{NTFY}/{topic}",
                        content=body,
                        headers={"Title": title, "Priority": str(priority), "Tags": tag, "Click": click},
                    )
                    r.raise_for_status()
                    sent += 1
                except Exception as e:
                    print(f"ntfy POST failed for {alertname}: {e!r}", file=sys.stderr)

        return {"ok": True, "received": len(alerts), "forwarded": sent}


    @app.get("/health")
    async def health():
        return {"ok": True}


    if __name__ == "__main__":
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
  '';
in {
  systemd.services.alertmanager-ntfy = {
    description = "Alertmanager → ntfy webhook shim";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    environment = {
      NTFY_SERVER = ntfyServer;
      NTFY_TOPIC = ntfyTopic;
      SHIM_PORT = toString port;
    };
    serviceConfig = {
      Type = "simple";
      ExecStart = "${shim}/bin/alertmanager-ntfy-shim";
      DynamicUser = true;
      Restart = "on-failure";
      RestartSec = 10;
      ProtectSystem = "strict";
      ProtectHome = true;
      NoNewPrivileges = true;
      PrivateTmp = true;
    };
  };
}
