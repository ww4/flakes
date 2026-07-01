# MeshCentral MeshAgent — the endpoint agent that self-manages this NixOS host
# by connecting to our MeshCentral server (modules/services/meshcentral.nix).
#
# There is no meshagent package/module in nixpkgs; this is the gap this project
# fills. The prebuilt Linux binary is patchelf'd in ./package.nix. The agent
# expects its `.msh` identity file NEXT TO the executable and needs a WRITABLE
# datapath (it creates meshagent.db + a DAIPC socket), so the service stages the
# store binary + the sops-encrypted .msh into /var/lib/meshagent and runs there.
{ config, lib, pkgs, ... }:
let
  meshagent = pkgs.callPackage ./package.nix { };
  datapath = "/var/lib/meshagent";
in
{
  # The .msh identity (server URL + MeshID + server cert hash) as a sops secret —
  # keeps the enrollment-capable MeshID out of git. StartupType is appended at
  # start (systemd = 1); the other fields come from the server-generated .msh.
  sops.secrets."meshagent-msh".sopsFile = ../../../secrets/meshagent-msh.yaml;
  sops.secrets."meshagent-msh".key = "msh";

  systemd.services.meshagent = {
    description = "MeshCentral agent (self-manage this host via MeshCentral)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "simple";
      StateDirectory = "meshagent";
      WorkingDirectory = datapath;
      # Stage binary + .msh into the writable datapath each start.
      ExecStartPre = pkgs.writeShellScript "meshagent-stage" ''
        set -eu
        install -m0555 ${meshagent}/bin/meshagent ${datapath}/meshagent
        umask 077
        cat ${config.sops.secrets."meshagent-msh".path} > ${datapath}/meshagent.msh
        echo "StartupType=1" >> ${datapath}/meshagent.msh
      '';
      ExecStart = "${datapath}/meshagent connect";
      Restart = "always";
      RestartSec = 10;
      # Runs as root — MeshCentral's model is full device management (the remote
      # terminal manages the host). Env gives the spawned remote-terminal shell a
      # working PATH + bash: THIS is the "confined shell" fix (the known NixOS
      # failure was a bare env with no bash/commands). Update prevention is by
      # version-match: the agent (1.1.59) == the server's bundled agent, so the
      # server never pushes a self-update.
      Environment = [
        "PATH=/run/current-system/sw/bin:/usr/bin:/bin"
        "SHELL=/run/current-system/sw/bin/bash"
      ];
    };
  };
}
