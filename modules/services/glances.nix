# Glances — htop-style system monitor with a REST/web API.
#
# Used by the Homepage dashboard to surface CPU / RAM / disk / network /
# load / temps as live charts on rosemaryacres.com.
#
# https://github.com/nicolargo/glances
{ config, lib, pkgs, ... }:

{
  services.glances = {
    enable = true;
    port = 61208;
    openFirewall = false;             # we open it scoped to bridges below
    # --webserver is mandatory: the unit's default ExecStart launches the
    # curses TUI which immediately exits without a terminal. With this
    # flag, the same process serves the HTML UI at http://gromit:61208/
    # and the REST API at /api/4/<endpoint>.
    extraArgs = [
      "--webserver"
      "--disable-plugin" "raid"        # raid plugin needs mdadm devices
    ];
  };

  # Homepage runs on arr-net (docker bridge br-<hash>) and reaches Glances
  # at the bridge gateway (172.21.0.1). Tailscale clients reach it on
  # 100.82.117.116:61208 — keep that accessible too.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 61208 ];
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 61208 -j nixos-fw-accept
  '';
}
