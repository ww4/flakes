# rebooter-receiver — the home end of the Craigmyle rebooter's push channel.
#
# The ConnectSense Rebooter Pro in the Craigmyle office attic power-cycles the
# office end of the 60 GHz AF60 link to the new shop when it loses the far
# side. On 2026-11-04, on site, it was subscribed to push notifications:
#
#   POST https://104.207.80.108:443/notify
#
# THE CHAIN (deliberately the same shape as homelab-mcp.nix, which is proven):
#   Rebooter 192.168.1.137 (Craigmyle LAN)
#     -> https://104.207.80.108:443/notify   lock3 VPS
#     -> GATE 1 there: allow 24.106.142.194 (Craigmyle egress); deny all
#     -> Tailscale -> gromit 100.82.117.116:8794
#     -> GATE 2 here: allow the VPS's tailnet IP; deny all
#     -> this service, 127.0.0.1:8795
#
# WHY PUSH AND NOT POLL. Craigmyle's LAN is 192.168.1.0/24 and so is gromit's
# own home LAN, so a plain subnet route cannot reach the device; 4via6 plus a
# tailnet grant would be needed, and the NVR there advertises no routes at all.
# A device-initiated push needs no route into that LAN and sidesteps the
# overlap entirely. That is the only reason this direction was chosen.
#
# ⚠️ THE SUBSCRIPTION CANNOT BE CHANGED FROM HERE. The device serves POST
# /notify only -- GET returns 405 -- so it cannot even be read back, let alone
# repointed. Changing the URL, the port or the receiving host means someone
# standing on the Craigmyle LAN. Treat the address above as fixed.
#
# ⚠️ AND IT IS NOT YET PROVEN TO WORK. The device was handed a 10-year
# self-signed certificate at registration, but it connects to a bare IP and so
# sends no SNI, which means lock3 serves it the DEFAULT vhost's Let's Encrypt
# certificate instead (measured 2026-11-06: CN=lock3.net). If the device
# validates what it is served against what it was given, nothing will ever
# arrive. The tell is a TLS handshake failure from 24.106.142.194 in lock3's
# nginx log; the fallback is a dedicated default_server there presenting the
# self-signed cert. Until one real notification lands, this is unverified.
#
# WHAT IT IS FOR, once it works: a LONG-WINDOW TREND of reboots per week. An
# auto-rebooter HIDES DECAY -- a once-daily reboot is ~2 flap-counter units
# against a threshold of 24, and the 65-second outage (measured 2026-11-04)
# falls between two ten-minute camera polls. So the link could degrade from
# monthly to daily reboots with nothing reporting it. Lowering the camera
# threshold would only re-create the noise problem; counting reboots over a
# long window is the right instrument.
{ config, lib, pkgs, ... }:

let
  # The loopback service. Never published.
  port = 8795;

  # nginx's tailnet listener -- THIS NUMBER IS LOAD-BEARING. lock3's vhost
  # proxies to it by literal address (ww4/lock3-website, nixos/configuration.nix),
  # so changing it here silently breaks delivery with no error on this side.
  jumpPort = 8794;

  # Both verified free before being chosen, against gromit's listeners AND
  # every proxy_pass target in the live nginx config -- after an incident where
  # a port was picked that nginx already proxied to, and nginx then could not
  # restart. A proxy TARGET is as taken as a listener.
  tailnetIP = "100.82.117.116";

  # lock3.sheep-trout.ts.net, the VPS at 104.207.80.108. A tailnet IP and not a
  # name: nginx resolves names once at startup, so a name here would bake in
  # whatever it resolved to at boot. If the VPS is rebuilt and rejoins with a
  # different address, THIS LINE must change or the endpoint 403s with no other
  # symptom.
  jumpHostIP = "100.125.31.125";

  stateDir = "/var/lib/rebooter-receiver";

  receiverUnchecked = pkgs.writers.writePython3Bin "rebooter-receiver" {
    flakeIgnore = [ "E501" "E203" "W503" "W504" ];
  } (builtins.readFile ./rebooter-receiver.py);

  # Tests gate the build. Every assertion is about NOT LOSING AN EVENT: this
  # service exists to capture a payload nobody has seen, and if the first one
  # arrives malformed, non-UTF-8 or oversized, a crash or a silent drop
  # destroys the single observation the whole chain was built to make. There is
  # no second chance until the link next fails.
  receiver = pkgs.runCommand "rebooter-receiver" { } ''
    ${pkgs.python3}/bin/python3 ${./rebooter-receiver-test.py} ${receiverUnchecked}/bin/rebooter-receiver
    mkdir -p $out/bin
    ln -s ${receiverUnchecked}/bin/rebooter-receiver $out/bin/rebooter-receiver
  '';
in
{
  systemd.services.rebooter-receiver = {
    description = "Receive push notifications from the Craigmyle link rebooter";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];

    environment = {
      REBOOTER_RECEIVER_PORT = toString port;
      REBOOTER_RECEIVER_STATE = stateDir;
    };

    serviceConfig = {
      ExecStart = "${receiver}/bin/rebooter-receiver";
      Restart = "always";
      RestartSec = 5;

      # A dedicated static user rather than DynamicUser, deliberately: with
      # DynamicUser the state lands under /var/lib/private, which is 0700
      # root-only, and the agent could then not read the events it is supposed
      # to be building a trend from. The directory holds device notifications,
      # no secrets, so it is world-readable.
      User = "rebooter-receiver";
      Group = "rebooter-receiver";
      StateDirectory = "rebooter-receiver";
      StateDirectoryMode = "0755";

      # It talks to nothing. It only ever answers a request that nginx on this
      # box has already accepted from one tailnet peer.
      PrivateNetwork = false;
      IPAddressAllow = [ "localhost" ];
      IPAddressDeny = [ "any" ];

      NoNewPrivileges = true;
      PrivateTmp = true;
      PrivateDevices = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" ];
      RestrictNamespaces = true;
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      SystemCallArchitectures = "native";
      SystemCallFilter = [ "@system-service" ];
    };
  };

  users.users.rebooter-receiver = {
    isSystemUser = true;
    group = "rebooter-receiver";
    description = "Craigmyle link rebooter callback receiver";
  };
  users.groups.rebooter-receiver = { };

  # GATE 2. A listener bound to the TAILNET address specifically, not the
  # wildcard, so the port cannot appear on the LAN or the WAN even if the
  # firewall rule below were ever loosened.
  services.nginx.virtualHosts."rebooter-receiver-jump" = {
    serverName = "_";
    listen = [{
      addr = tailnetIP;
      port = jumpPort;
    }];

    extraConfig = ''
      allow ${jumpHostIP};
      deny all;
    '';

    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString port}";
      extraConfig = ''
        # Headers are set BY HAND because services.nginx.recommendedProxySettings
        # is globally false on this box (see modules/agent/queue-replies.nix --
        # an outage fix, not a preference: the include plus a hand-set Host sent
        # the header TWICE, which is an RFC 7230 violation that four separate
        # upstreams answered with 400). If that global is ever turned back on,
        # these lines must go or this endpoint breaks the same way.
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
      '';
    };
  };

  # The tailnet interface otherwise permits only a short list of ports (see
  # modules/networking.nix). Without this the VPS's connections are dropped
  # before nginx ever sees them, which looks identical to the service being
  # down.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ jumpPort ];
}
