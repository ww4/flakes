# gromit's music-library profiler, served from the 5900X.
#
# gromit's netradio-profile (modules/services/netradio) listens to every track
# once — YAMNet + a pitch tracker + the era measurements — and on its
# i5-4690K that is ~1.5 s a track, hours for the library. This box answers
# the same measurement over HTTP: gromit POSTs a file, gets the Facts JSON.
# gromit tries here first and measures locally when this box is off, so
# powering wallace down costs time, not correctness (the whisper/Kokoro shape,
# switchboard-inference.nix). Stateless: each file is a private temp file,
# measured, deleted. No auth — the firewall admits the port on tailscale0 only.
{ config, lib, pkgs, ... }:
let
  netradio = pkgs.callPackage ../../modules/services/netradio/package.nix { };
  yamnet = pkgs.callPackage ../../modules/services/netradio/yamnet.nix { };
  port = 8790;
in
{
  systemd.services.netradio-profile-server = {
    description = "netradio profile server (library audio analysis, for gromit)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    serviceConfig = {
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio profile-server"
        "--model ${yamnet}"
        "--listen 0.0.0.0" "--port ${toString port}"   # firewall scopes it to tailscale0
        "--workers 12"
      ];
      DynamicUser = true;
      Restart = "on-failure";
      RestartSec = 5;
      Nice = 10;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      PrivateDevices = true;
      NoNewPrivileges = true;
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
      MemoryMax = "8G";
    };
  };

  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ port ];
}
