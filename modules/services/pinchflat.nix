# PinchFlat — YouTube archiver.
{ config, lib, pkgs, ... }:

{
  services.pinchflat = {
    enable = true;
    selfhosted = true;
    mediaDir = "/mnt/fusion/pinchflat";
  };

  # Not great, but needed (per maintainer): run as a fixed system user
  # rather than a DynamicUser.
  users.users.pinchflat = {
    isSystemUser = true;
    group = "pinchflat";
  };
  systemd.services.pinchflat.serviceConfig.User = "pinchflat";
  systemd.services.pinchflat.serviceConfig.DynamicUser = lib.mkForce false;

  # A vhost like every other service. The Homepage tile used to link the raw
  # http://<tailscale-ip>:8945 — the only tile bypassing the nginx/TLS
  # posture, and a dead link from any real client anyway: pinchflat binds
  # 0.0.0.0 but no firewall rule ever opened 8945 on tailscale0/enp3s0
  # (2026-08 audit M6). nginx proxies over loopback; the port stays closed.
  # DNS: pinchflat.rosemaryacres.com -> 100.82.117.116 (created 2026-08-17,
  # proxy off, same as every homelab vhost).
  services.nginx.virtualHosts."pinchflat.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8945";
      proxyWebsockets = true;
      extraConfig = ''
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
      '';
    };
  };
}
