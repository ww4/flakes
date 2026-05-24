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

    app = FastAPI()

    SEVERITY_PRIORITY = {"critical": 5, "warning": 3, "info": 2}
    SEVERITY_TAG = {"critical": "rotating_light", "warning": "warning", "info": "information_source"}


    @app.post("/alert")
    async def alert(req: Request):
        payload = await req.json()
        alerts = payload.get("alerts", []) or []
        sent = 0
        async with httpx.AsyncClient(timeout=10.0) as client:
            for a in alerts:
                labels = a.get("labels") or {}
                annotations = a.get("annotations") or {}
                status = a.get("status", "firing")
                severity = labels.get("severity", "warning")
                alertname = labels.get("alertname", "Alert")
                summary = annotations.get("summary") or annotations.get("description") or alertname
                description = annotations.get("description", "")

                if status == "resolved":
                    title = f"RESOLVED: {alertname}"
                    priority = 2
                    tag = "white_check_mark"
                else:
                    title = f"{severity.upper()}: {alertname}"
                    priority = SEVERITY_PRIORITY.get(severity, 3)
                    tag = SEVERITY_TAG.get(severity, "warning")

                body = summary
                if description and description != summary:
                    body = f"{summary}\n\n{description}"
                # Annotate which labels triggered (helpful for multi-gauge alerts)
                extra = {k: v for k, v in labels.items() if k not in ("alertname", "severity")}
                if extra:
                    body += "\n\n" + ", ".join(f"{k}={v}" for k, v in extra.items())

                try:
                    r = await client.post(
                        f"{NTFY}/{TOPIC}",
                        content=body,
                        headers={"Title": title, "Priority": str(priority), "Tags": tag},
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
