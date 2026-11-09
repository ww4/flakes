# Control a Roku from this machine, over its External Control Protocol.
#
# ECP is plain HTTP on port 8060 with no authentication, no pairing and no
# cloud account. It is what Roku's own phone app speaks, which is why that app
# keeps working when the internet does not, and Roku document it publicly.
#
# The smallest useful configuration is nothing at all — the address is found by
# SSDP:
#
#   services.roku.enable = true;
#
# What this adds over `curl http://<address>:8060/keypress/Home`:
#
#   * one origin a browser will accept, so a page can drive it;
#   * JSON instead of XML;
#   * a key allow-list, because ECP answers 200 to a misspelled button and
#     then does nothing;
#   * a gate on the power keys, which are one request from a dark television
#     while somebody is watching it;
#   * discovery, and re-discovery after DHCP moves the box.
#
# It binds to localhost. Anything that can reach it can already reach the Roku
# directly, so this is a translation layer with one opinion, not a security
# boundary — and it does not pretend otherwise.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.roku;
  roku = pkgs.callPackage ./package.nix { };
in
{
  options.services.roku = {
    enable = lib.mkEnableOption "the Roku control API";

    host = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "192.168.1.89";
      description = ''
        The Roku's address. Left empty it is discovered by SSDP and
        re-discovered when it stops answering, which is the right default on a
        DHCP network.

        Set it when discovery cannot work: multicast does not cross VLANs or
        most wifi client isolation. Note also that a SUSPENDED Roku can be
        silent to SSDP while answering ECP perfectly well — so "nothing found"
        is not the same as "nothing there", and `roku-find` says so.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8060;
      description = "The device's ECP port. 8060 on every Roku shipped so far.";
    };

    name = lib.mkOption {
      type = lib.types.str;
      default = "Roku";
      description = ''
        What to call it in a user interface. The device reports its own name
        and that is usually better, so this is a fallback rather than an
        override.
      '';
    };

    apiPort = lib.mkOption {
      type = lib.types.port;
      default = 8793;
      description = "Where this service listens, on 127.0.0.1.";
    };

    timeout = lib.mkOption {
      type = lib.types.float;
      default = 5.0;
      description = ''
        Seconds to wait on the device. Generous on purpose: a Roku waking from
        suspend answers slowly, and a remote that gives up before the box does
        feels broken when it is merely early.
      '';
    };

    allowPower = lib.mkOption {
      type = lib.types.bool;
      default = false;
      example = true;
      description = ''
        Permit the PowerOn, PowerOff and Power keys.

        Off by default, and deliberately: every other ECP key moves a cursor or
        starts something, and the worst case is somebody pressing Back. Power
        is one request away from a dark television while a person is watching
        it, and a remote in a browser is easy to leave open on a tablet. Turn
        it on when the Roku drives a screen whose state nobody minds.

        Nothing about this stops the physical remote, and nothing stops anyone
        on the LAN reaching the device directly — see the note at the top. It
        stops THIS service doing it by accident.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ roku ];   # roku-find, for when nothing answers

    systemd.services.roku-api = {
      description = "Roku control API (ECP)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        ExecStart = lib.concatStringsSep " " ([
          "${roku}/bin/roku-api"
          "--listen 127.0.0.1"
          "--api-port ${toString cfg.apiPort}"
          "--port ${toString cfg.port}"
          "--timeout ${toString cfg.timeout}"
        ] ++ lib.optional (cfg.host != "") "--host ${cfg.host}"
          ++ lib.optional cfg.allowPower "--allow-power");
        Restart = "always";
        RestartSec = 5;
        DynamicUser = true;
        # Discovery is multicast UDP and control is TCP, both on the LAN; it
        # needs no filesystem, no privileges and no other address family.
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" ];
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
        CapabilityBoundingSet = [ "" ];
      };
    };
  };
}
