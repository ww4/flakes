# guide-preview — the homelab guide site (ww4/homelab-guide, Zola), served
# over the tailnet at guide.<domain> while the public edge does not exist yet.
#
# The content is NOT built by this flake: the guide repo is private until
# launch, and comin builds anonymously, so it cannot be a flake input. The
# claude agent's user service builds it (`zola build --base-url`) into
# /var/lib/guide-preview on every merge to the guide's main; nginx serves the
# directory. Content updates are therefore decoupled from deploys — the same
# shape as a Pages build, which is what replaces this at launch.
{ config, ... }:

let
  dir = "/var/lib/guide-preview";
in
{
  systemd.tmpfiles.rules = [
    "d ${dir} 0755 claude users - -"
  ];

  services.nginx.virtualHosts."guide.${config.homelab.domain}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    root = dir;
    locations."/" = {
      tryFiles = "$uri $uri/index.html =404";
      extraConfig = ''
        add_header Cache-Control "no-cache";
      '';
    };
  };
}
