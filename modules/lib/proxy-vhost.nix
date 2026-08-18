# The house proxy vhost, in one place.
#
# Nine services carried this exact block copy-pasted — forceSSL + ACME
# (DNS-01, so acmeRoot = null) fronting a loopback backend with the four
# standard forwarding headers (June + 2026-08 audits: the repo's largest
# single duplication). One definition now; a call site passes its port and
# appends whatever extra nginx directives it genuinely needs.
#
# DELIBERATELY NOT UNIFIED: the other vhost family uses
# recommendedProxySettings (a different, larger nginx directive set) —
# folding the two families together would change every service's rendered
# config. Byte-identity of the refactor was proven by diffing the rendered
# nginx.conf before/after; keep it that way when touching this.
#
# Usage:
#   services.nginx.virtualHosts."thing.rosemaryacres.com" =
#     import ../lib/proxy-vhost.nix { port = 1234; };
# Optional: websockets = false; extraConfig = ''...appended directives...'';
{ port, websockets ? true, extraConfig ? "" }:

{
  forceSSL = true;
  enableACME = true;
  acmeRoot = null;
  locations."/" = {
    proxyPass = "http://127.0.0.1:${toString port}";
    proxyWebsockets = websockets;
    extraConfig = ''
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
    '' + extraConfig;
  };
}
