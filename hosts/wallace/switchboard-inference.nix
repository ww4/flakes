# Switchboard inference, offloaded from gromit onto the 5900X.
#
# gromit keeps Asterisk + the FastAGI loop (modules/services/switchboard.nix);
# only the two heavy steps run here: speech-to-text (whisper.cpp) and the
# conversational voice (Kokoro). gromit tries these over Tailscale FIRST and
# falls back to its own local copies if wallace is off — so powering wallace
# down costs latency, not the phone. See config.py `whisper_urls`/`kokoro_urls`.
#
# Measured on gromit's i5-4690K (4 threads): whisper base.en ~2 s per 4 s of
# speech, Kokoro ~0.75x realtime. The 5900X has 12 cores for whisper and 24
# threads for Kokoro's CPU build.
#
# Same shape as immich-ml.nix: the container publishes ONLY on the tailnet IP,
# ordered after tailscaled; the firewall admits the ports on tailscale0 alone.
{ config, lib, pkgs, ... }:

let
  tailnetIp = "100.66.171.120";
  # Models come from pkgs/switchboard/default.nix passthru. This box runs
  # small.en (4x base.en's cost, which the 5900X absorbs; it got the first real
  # notes right where base.en did not); gromit's local fallback stays base.en.
  switchboard = pkgs.callPackage ../../pkgs/switchboard { };
  whisperPort = 8778;
  kokoroPort = 8880;
in
{
  systemd.services.whisper-server = {
    description = "whisper.cpp server (switchboard speech-to-text, for gromit)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    serviceConfig = {
      ExecStart = lib.concatStringsSep " " [
        "${pkgs.whisper-cpp}/bin/whisper-server"
        "--host 0.0.0.0" "--port ${toString whisperPort}"   # firewall scopes it to tailscale0
        "-m ${switchboard.whisperModelSmall}"   # small.en here; gromit's fallback stays base.en
        "-t 12"
      ];
      DynamicUser = true;
      Restart = "on-failure";
      RestartSec = 5;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      PrivateDevices = true;
      NoNewPrivileges = true;
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" ];
      MemoryMax = "4G";
    };
  };

  virtualisation.oci-containers.containers.kokoro = {
    # Same pinned image as gromit's open-notebook Kokoro (services/open-notebook.nix).
    image = "ghcr.io/remsky/kokoro-fastapi-cpu:v0.7.2";
    autoStart = true;
    ports = [ "${tailnetIp}:${toString kokoroPort}:8880" ];
    environment = { PYTHONUNBUFFERED = "1"; };
  };

  systemd.services.podman-kokoro = {
    after = [ "tailscaled.service" ];
    wants = [ "tailscaled.service" ];
    serviceConfig.RestartSec = lib.mkForce "10s";
  };

  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ whisperPort kokoroPort ];
}
