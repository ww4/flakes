# NETWATCH — the guard dog. Scheduled netdiag runs with a persistent baseline.
#
# Chris asked for "some sense of how the network is doing and if there has been
# any intrusion or anything suspicious". This is that, built on netdiag and the
# house alerting conventions (ntfy on 127.0.0.1:8090/gromit-alerts, quiet hours
# 22:00-07:00, notify on state CHANGE only) rather than on anything new.
#
# gromit-only by design: netdiag itself is on both hosts, but the guard dog wants
# a WIRED, ALWAYS-ON vantage point and marcus is neither (it travels, and most
# L2 checks are useless-to-misleading over wifi).
#
# CADENCE — each tier costs roughly what it is worth:
#   scan   15 min   ARP census diff -> NEW DEVICE, MAC<->IP rebinding, duplicate
#                   IP, gateway MAC change. ~10 s. This is the intrusion signal.
#   drift  hourly   per-known-host open-port diff -> a device that started
#                   listening on something new.
#   report 07:10    the health report: rogue DHCP/RA, storm/loop, UPnP+NAT-PMP
#                   exposure, subnet histogram. After quiet hours end, so its
#                   ping lands at a civil time.
#   audit  Sun 08:00  camera census, unadopted APs, gateway exposure audit.
#
# ⚠️ ALERTING: NOTHING HERE MAY EVER WAKE ANYONE. Chris's rule, 2026-08-19,
# after a spoofing-signature design woke him: "I don't care about man in the
# middle or arp spoofing at 3:00 a.m. ... I don't want to be awoken unless
# there's a physical threat to my family such as fire, a flood or an electrical
# problem. That goes for all classes of network traffic."
#
# Every event netwatch raises is a network event, so the `critical` escape hatch
# was REMOVED from notify() outright — not defaulted off, removed — so no future
# edit can reintroduce a 3 a.m. page. Findings raised during quiet hours are
# HELD and delivered as one consolidated summary after 07:00 ("diagnose it,
# make a note and flag me in the morning").
#
# ⚠️ THE POINT OF THE WATCHDOG BELOW: a guard dog that silently fails looks
# exactly like a quiet network. netwatch itself refuses to report an empty or
# implausibly-small scan as an all-clear, and this INDEPENDENT timer alerts if
# netwatch stops stamping state.json at all — so the death of the watcher is
# itself an alert. Modelled on services/sentinel.nix's sentinel-watchdog.
{ config, lib, pkgs, ... }:

let
  # The tools netwatch.py actually shells out to (`netdiag-priv arpscan`, and
  # `netdiag rogue|storm|exposure|listen|cameras|unifi|audit`). These are NOT in
  # `pkgs` — they are this repo's derivations, shared with default.nix via
  # packages.nix. environment.systemPackages putting them on the INTERACTIVE
  # PATH is exactly what made the omission invisible: every hand-run worked
  # while every timer-driven scan died with FileNotFoundError.
  inherit (import ./packages.nix { inherit pkgs; }) netdiag netdiagPriv;

  # The endpoint behind the ntfy notification buttons.
  netwatchActions = pkgs.writers.writePython3Bin "netwatch-actions" {
    flakeIgnore = [ "E501" "E203" "W503" "W504" ];
  } (builtins.readFile ./netwatch-actions.py);

  netwatch = pkgs.writers.writePython3Bin "netwatch" {
    flakeIgnore = [ "E501" "E203" "W503" "W504" ];
  } (builtins.readFile ./netwatch.py);

  # Every tier runs as root: netdiag-priv needs raw sockets, and running as root
  # skips the sudo hop entirely. Type=oneshot, no lingering process.
  job = description: args: {
    inherit description;
    path = [ netwatch netdiag netdiagPriv ]
      ++ (with pkgs; [ iproute2 nmap curl coreutils gnugrep gawk ]);
    serviceConfig = {
      Type = "oneshot";
      StateDirectory = "netwatch";
      StateDirectoryMode = "0770";
      Group = "netwatch";
      # It reads the network and writes one state dir; it needs nothing else.
      ProtectHome = true;
      PrivateTmp = true;
      NoNewPrivileges = false;   # netdiag-priv legitimately needs capabilities
    };
    script = "netwatch ${args}";
  };

  timer = desc: cal: {
    description = desc;
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = cal;
      Persistent = true;         # catch up after downtime rather than skipping
      RandomizedDelaySec = "45s";
    };
  };
in
{
  environment.systemPackages = [ netwatch netwatchActions ];

  # --- The notification action buttons ------------------------------------
  # A NEW DEVICE alert used to end in a shell command, which is unusable on a
  # phone: readable but not runnable, so effectively no call to action at all.
  # It now carries Accept / Investigate buttons that call this service.
  #
  # Loopback-only, proxied under the EXISTING ntfy vhost — which already has
  # DNS and a certificate, and inherits the LAN/Tailscale source gate from
  # nginx-access.nix, so no new subdomain, cert or Authelia rule is needed.
  # Hosting it there is also coherent: these are the notification's own
  # callbacks.
  systemd.services.netwatch-actions = {
    description = "netwatch notification action endpoint (ntfy buttons)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    path = with pkgs; [ netwatch nmap curl iproute2 coreutils ];
    serviceConfig = {
      ExecStart = lib.getExe netwatchActions;
      Restart = "on-failure";
      # Writes the netwatch baseline, so it shares the state dir. Runs as its
      # own user rather than root: it is the only network-facing piece here.
      User = "netwatch-act";
      Group = "netwatch";
      StateDirectory = "netwatch";
      StateDirectoryMode = "0770";
      ProtectHome = true;
      PrivateTmp = true;
      NoNewPrivileges = true;
    };
  };

  users.users.netwatch-act = {
    isSystemUser = true;
    group = "netwatch";
    description = "netwatch notification action endpoint";
  };
  users.groups.netwatch = { };

  services.nginx.virtualHosts."ntfy.rosemaryacres.com".locations."/netwatch-action/" = {
    proxyPass = "http://127.0.0.1:8799";
  };

  systemd.services.netwatch-scan   = job "netwatch: presence diff (new devices)" "scan";
  systemd.services.netwatch-drift  = job "netwatch: open-port drift on known hosts" "drift";
  systemd.services.netwatch-report = job "netwatch: daily network health report" "report";
  systemd.services.netwatch-audit  = job "netwatch: weekly exposure + camera audit" "audit";

  systemd.timers.netwatch-scan   = timer "netwatch presence diff every 15 min" "*:0/15";
  systemd.timers.netwatch-drift  = timer "netwatch port drift hourly" "hourly";
  # 07:10 — AFTER the 07:00 quiet-hours boundary, not before it. Scheduling a
  # notifying job at 06:45 put it inside quiet hours, which was wrong even at
  # low priority: a morning-facing job belongs in the morning.
  systemd.timers.netwatch-report = timer "netwatch daily report" "*-*-* 07:10:00";
  systemd.timers.netwatch-audit  = timer "netwatch weekly audit" "Sun *-*-* 08:00:00";

  # --- Watchdog for the watchdog -------------------------------------------
  # Independent of netwatch: posts to ntfy directly so it still works when
  # netwatch is the thing that is broken. netwatch stamps state.json on every
  # successful run, so a stale mtime means the guard dog is dead — which is
  # precisely the failure that would otherwise be indistinguishable from a
  # quiet, healthy network.
  systemd.services.netwatch-watchdog = {
    description = "Alert if netwatch has stopped running";
    path = with pkgs; [ curl coreutils ];
    serviceConfig.Type = "oneshot";
    script = ''
      f=/var/lib/netwatch/state.json
      [ -e "$f" ] || exit 0            # never run yet — nothing to judge
      age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || echo 0) ))
      # 15-min cadence; 50 min is comfortably past two missed runs.
      if [ "$age" -gt 3000 ]; then
        curl -s --max-time 10 \
          -H "Title: netwatch STALLED" -H "Priority: 4" -H "Tags: warning" \
          -d "netwatch has not completed a scan for $((age / 60)) min (state.json stale). The network is NOT being watched. Check: systemctl status netwatch-scan.timer netwatch-scan.service" \
          http://127.0.0.1:8090/gromit-alerts >/dev/null || true
      fi
    '';
  };
  systemd.timers.netwatch-watchdog = {
    description = "netwatch liveness check";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "20min";
      OnUnitActiveSec = "20min";
      Persistent = true;
    };
  };
}
