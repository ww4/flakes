# nginx source-access gate (security review 2026-06-04).
#
# Every *.rosemaryacres.com vhost is meant to be reachable only over Tailscale
# or the trusted local/LAN network -- never the public IPv6 GUA this box holds
# (2603:6013:340:104::/64) or any other WAN source. Previously nothing enforced
# that: nginx listened on 0.0.0.0/[::]:443 with no allow/deny, so the
# "Tailscale-only" posture was DNS-illusory (a forged Host: header from the LAN
# or public IPv6 reached the backend, with only per-app login as the gate).
#
# allow/deny here lives in the http{} block and is inherited by every server{}
# block (ngx_http_access_module), so this one place gates all current and
# future vhosts. ACME is DNS-01 (Cloudflare), so there is no inbound HTTP-01
# challenge to carve out. bitcoind P2P (8333) is not proxied by nginx and is
# unaffected.
#
# To reach a service from a new network, add its source range below and rebuild.
{ ... }:

{
  services.nginx.commonHttpConfig = ''
    # --- Allowed sources: loopback, RFC1918/LAN, docker, Tailscale ---
    allow 127.0.0.0/8;          # loopback (host-local service fetches)
    allow 10.0.0.0/8;           # RFC1918
    allow 172.16.0.0/12;        # RFC1918 (docker bridges; server-side widget fetches)
    allow 192.168.0.0/16;       # RFC1918 (the trusted LAN, 192.168.1.0/24)
    allow 100.64.0.0/10;        # Tailscale CGNAT (IPv4)
    allow ::1/128;              # loopback (IPv6)
    allow fd7a:115c:a1e0::/48;  # Tailscale (IPv6)
    allow fc00::/7;             # unique-local (IPv6)
    allow fe80::/10;            # link-local (IPv6)
    # Everything else -- notably the public IPv6 GUA and any WAN -- is denied.
    deny all;
  '';
}
