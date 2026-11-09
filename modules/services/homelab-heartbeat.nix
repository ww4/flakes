# homelab-heartbeat — the inside half of the external watcher.
#
# gromit cannot report gromit being unreachable, so the verdict is formed OUTSIDE
# the house, by the correlator on the lock3 VPS. This is what feeds it: each host
# says "I am alive" once a minute, and "here is whether I can see my peer".
#
# The pair is what makes the verdicts separable. lock3 can probe gromit directly
# but CANNOT reach wallace at all (measured 2026-11-01: blocked in both
# directions by the ACL on lock3's tagged-devices tag), so gromit's view from
# inside the house is the only second opinion wallace will ever get. Equally,
# wallace's view is what distinguishes "gromit is down" from "gromit is up and
# its heartbeat is not arriving".
#
# ⚠️ Posted over the PUBLIC endpoint, not the tailnet, and for a concrete reason
# rather than convenience: wallace cannot reach lock3 over the tailnet either.
# Signing is what makes that safe — HMAC-SHA256 with a per-host key, a timestamp
# and a nonce, all verified on arrival.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.heartbeat;
in
{
  options.homelab.heartbeat = {
    enable = lib.mkEnableOption "the homelab-watch heartbeat sender";

    host = lib.mkOption {
      type = lib.types.str;
      description = "This host's name as the watcher knows it (gromit|wallace).";
    };
    peer = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = ''
        The peer this host reports on. Produces a `sees_<peer>` field, which is
        the corroborating signal the correlator uses before calling a host down.
      '';
    };
    peerAddr = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Peer address to probe. A LAN address is preferred: it tests the thing that matters without depending on Tailscale being healthy.";
    };
    peerPort = lib.mkOption {
      type = lib.types.port;
      default = 22;
      description = "Peer TCP port to probe. TCP rather than ICMP so the sender needs no CAP_NET_RAW.";
    };
    keyFile = lib.mkOption {
      type = lib.types.path;
      description = "File holding this host's HMAC key (sops-decrypted at activation).";
    };
    url = lib.mkOption {
      type = lib.types.str;
      default = "https://watch.rosemaryacres.com/hb";
      description = "The correlator's heartbeat intake.";
    };
    intervalSec = lib.mkOption {
      type = lib.types.int;
      default = 60;
      description = ''
        Must stay comfortably under the watcher's HEARTBEAT_STALE_S (180), which
        is three missed beats. Raising this without raising that there would turn
        ordinary jitter into a false outage.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.homelab-heartbeat = {
      description = "Send a signed heartbeat to the external watcher on lock3";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = [ pkgs.python3 ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${pkgs.python3}/bin/python3 ${./homelab-heartbeat.py}";

        # Hardening: it needs the network, the key file, and nothing else. No
        # capabilities at all — the peer probe is a TCP connect precisely so
        # CAP_NET_RAW is not required.
        CapabilityBoundingSet = [ "" ];
        AmbientCapabilities = [ "" ];
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
      };
      environment = {
        WATCH_URL = cfg.url;
        WATCH_HOST = cfg.host;
        WATCH_KEY_FILE = toString cfg.keyFile;
        WATCH_KEY_NAME = cfg.host;
        WATCH_PEER = cfg.peer;
        WATCH_PEER_ADDR = cfg.peerAddr;
        WATCH_PEER_PORT = toString cfg.peerPort;
      };
    };

    systemd.timers.homelab-heartbeat = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "90s";
        OnUnitActiveSec = "${toString cfg.intervalSec}s";

        # ⚠️ NO Persistent = true, unlike most timers here. Persistent replays a
        # missed run after downtime, and replaying a heartbeat is exactly the
        # wrong behaviour: the heartbeat's whole meaning is "I am alive NOW". A
        # catch-up beat on boot would backdate liveness the host did not have.
        # The watcher would reject it anyway — the timestamp would be outside
        # the replay window — but relying on the far end to reject something we
        # should never send is the wrong place to put the safeguard.
        AccuracySec = "5s";
      };
    };
  };
}
