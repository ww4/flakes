# MeshCentral server — self-hosted remote management (the web management site;
# endpoints run the separately-built MeshAgent, which is a FUTURE NixOS module).
#
# Uses the nixpkgs `services.meshcentral` module (which packages the server and
# already patches out its npm self-update). We run it behind nginx with
# TLS-offload: MeshCentral serves plain HTTP on a loopback port, nginx terminates
# TLS with the ACME (DNS-01) cert and inherits the global Tailscale/LAN
# source-gate (nginx-access.nix) — so the server is reachable only over the
# tailnet/LAN, which is exactly right for managing the NixOS fleet.
#
# FIRST-RUN: the first account created via the web UI becomes site admin. Visit
# https://mesh.rosemaryacres.com over Tailscale, create the admin, then we lock
# signups by setting settings.NewAccounts = false (follow-up). DB is the embedded
# NeDB (fine to start; MongoDB is a later option).
{ config, lib, pkgs, ... }:

let
  port = 4430;   # MeshCentral's internal HTTP listener; nginx proxies to it
in
{
  services.meshcentral = {
    enable = true;
    settings.settings = {
      Cert = "mesh.rosemaryacres.com";   # server identity / public name
      WANonly = true;                    # single external name (no LAN discovery)
      Port = port;                       # internal listener
      AliasPort = 443;                   # public port, so agent/URLs use 443
      RedirPort = 0;                     # no own HTTP-redirect listener (nginx forceSSL handles it)
      TlsOffload = "127.0.0.1";          # TLS is terminated by nginx on loopback
      SelfUpdate = false;                # never self-update (immutable store)
    };
    settings.domains."".certUrl = "https://mesh.rosemaryacres.com/";
  };

  # Reverse proxy — inherits the global source-gate + ACME DNS-01, like the other
  # vhosts. MeshCentral is websocket-heavy (agent + web UI) with long-lived
  # connections, hence proxyWebsockets + a long read timeout + unbounded uploads.
  services.nginx.virtualHosts."mesh.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString port}";
      proxyWebsockets = true;
      recommendedProxySettings = true;
      extraConfig = ''
        proxy_read_timeout 86400s;
        client_max_body_size 0;
      '';
    };
  };
}
