# An isolated, FAIL-CLOSED network segment on the standby USB NIC.
#
# WHY THIS EXISTS: the Kindle Paperwhite 4 is on firmware 5.18.1. The Sanctuary
# jailbreak covers 5.16.4–5.18.3 inclusive and shipping firmware is already
# 5.19.x, so one OTA past 5.18.3 closes that window permanently — there is no
# downgrade path, and there is only one of this Kindle. But Sanctuary is not
# self-hostable (no published server; the Kindle browses to the real host over
# plain HTTP), so the device MUST be online while it is most vulnerable.
#
# ── Why DNS filtering was the wrong tool ──────────────────────────────────────
# The first attempt at this blocked Amazon's OTA domains in blocky, scoped to
# the Kindle's IP on the house LAN. Chris rejected it, correctly: that design is
# FAIL-OPEN. On the flat LAN the Kindle's gateway is the router, not gromit, so
# gromit only ever sees the queries the Kindle chooses to send it. Every way it
# can go wrong — static DNS not applied, a hardcoded resolver, a stale lease, a
# typo in the Advanced screen — ends with the Kindle on full unfiltered
# internet, and from gromit's side that is indistinguishable from working.
#
# A filter outside the data path cannot fail closed. So this puts gromit IN the
# path: it is the segment's only gateway, and forwarding is deny-by-default.
# Every failure mode now ends in "no internet", which costs an evening instead
# of the device.
#
# ── Two independent layers, both failing the same direction ───────────────────
#   1. iptables: default DROP for anything crossing this interface, with a
#      single pinhole to the Sanctuary host on tcp/80.
#   2. blocky (see services/blocky.nix): this CIDR gets an allowlist-only group
#      — a /.*/ denylist plus one permitted name. Verified behaviourally, not
#      assumed: a live blocky answered sanctuary.skyvincent.com and returned
#      0.0.0.0 for google.com, amazon.com and all three OTA hosts.
#
# Break either one and the Kindle simply has no internet.
#
# ── The point of being in the path: measure, don't guess ──────────────────────
# The jailbreak downloads three files and it is NOT known whether they come from
# the same host as the page. Rather than guess the allowlist and find out with
# the Kindle, run the DRY RUN FIRST: put a laptop on this segment, load the page,
# then read the drop counters (`kindle-fw-counters`) to see exactly what else it
# reached for, and widen `allowedHosts` to measured reality. Only then connect
# the Kindle.
#
# ── After the jailbreak ───────────────────────────────────────────────────────
# Point `allowedHosts` at gromit's own 192.168.1.65:443 instead of Sanctuary and
# the Kindle lives here permanently: Calibre-Web over OPDS and NOTHING else, no
# internet at all, so it can never OTA again. That retires the risk outright
# rather than maintaining a denylist against it — and gives the isolated
# untrusted-device network that was wanted anyway.
#
# ⚠️ COST: enp6s0u1 is the designated standby LAN NIC (networking.nix mirrors
# enp3s0's rules onto it so a failed onboard port is a cable move). Using it
# here spends that failover. enp3s0 was re-checked at 1000 Mb/s on 2026-10-31,
# so the August 100 Mb problem is not currently in play — but if the onboard
# port dies while this is deployed, moving the cable also means disabling this.
#
# ── The AP side (Chris's plan, 2026-10-31) ───────────────────────────────────
# Provision the UniFi AP HD on the 16-port switch with full internet and the
# controller reachable, give it a DEDICATED Kindle-only SSID, then move its
# switch port onto an isolated VLAN and trunk that to gromit's spare port.
#
# Doing it in that order matters: a dedicated SSID removes the association
# hazard entirely. Sharing the house SSID would let the Kindle pick a house AP
# on signal strength and reach the internet unfiltered, with nothing on gromit
# able to see that it had happened.
#
# ⚠️ GROMIT'S PORT MUST DELIVER THE VLAN UNTAGGED. This module puts the address
# straight on ${iface}, so set that switch port's Native VLAN / Primary Network
# to the Kindle VLAN and allow no other tagged networks on it. If it arrives
# tagged instead, nothing reaches this interface and the segment is simply dead
# (fail-closed, but confusing). To run it tagged instead, add
#   networking.vlans."${iface}.<vid>" = { id = <vid>; interface = "${iface}"; };
# and move the address, firewall interface and kea interface onto that name.
#
# An adopted UniFi AP keeps serving its provisioned SSIDs indefinitely with no
# controller reachable, so losing the controller after the move is expected and
# fine. The AP itself will take a DHCP lease here too — which is why the Kindle
# has a reservation above: the lease file will show both, and the Kindle is
# identified by MAC, never by being "the only one".
{ config, lib, pkgs, ... }:

let
  iface = "enp6s0u1";
  gateway = "192.168.50.1";
  prefixLength = 24;
  subnet = "192.168.50.0/24";
  poolFrom = "192.168.50.100";
  poolTo = "192.168.50.150";

  # The Kindle's own Wi-Fi MAC, read off its Device Info screen. Reserved so
  # that the AP — which also takes a lease on this segment once its switch port
  # moves to the isolated VLAN — can never be mistaken for the Kindle, and so
  # the pinhole below can be scoped to the Kindle ALONE.
  kindleMAC = "34:af:b3:27:e8:7a";
  kindleIP = "192.168.50.10";
  uplink = "enp3s0";
  houseNet = "192.168.1.0/24";

  # The single pinhole. sanctuary.skyvincent.com resolved to one A record,
  # identical from 1.1.1.1 / 9.9.9.9 / 8.8.8.8, no CNAME, TTL 1799 — a
  # single-homed host rather than a CDN, so pinning the address is sound.
  # If it ever moves, the failure is "no internet", which is the safe
  # direction; re-resolve and update here.
  allowedHosts = [
    { host = "78.24.205.144"; port = 80; what = "sanctuary.skyvincent.com (jailbreak)"; }
  ];

  acceptRules = lib.concatMapStringsSep "\n" (h:
    "iptables -A kindle-fwd -i ${iface} -s ${kindleIP} -d ${h.host} -p tcp --dport ${toString h.port} -j ACCEPT  # ${h.what}"
  ) allowedHosts;

  # Reads the DROP counters so the dry run can be measured rather than guessed.
  counters = pkgs.writeShellScriptBin "kindle-fw-counters" ''
    set -euo pipefail
    echo "== kindle-fwd chain (packets/bytes per rule) =="
    ${pkgs.iptables}/bin/iptables -L kindle-fwd -v -n --line-numbers
    echo
    echo "== current DHCP leases on ${iface} =="
    if [ -r /var/lib/kea/dhcp4.leases ]; then
      ${pkgs.coreutils}/bin/cat /var/lib/kea/dhcp4.leases
    else
      echo "(no lease file yet at /var/lib/kea/dhcp4.leases)"
    fi
  '';
in
{
  # NetworkManager must keep its hands off, or it will fight the static address.
  # The interface currently has no NM profile and sits in state `unavailable`
  # (no carrier), so nothing is being taken away from it.
  networking.networkmanager.unmanaged = [ "interface-name:${iface}" ];

  networking.interfaces.${iface}.ipv4.addresses = [
    { address = gateway; inherit prefixLength; }
  ];

  # No IPv6 on this segment, deliberately. The forward rules below are v4, so a
  # working v6 path would route straight around them — and gromit's uplink does
  # have public v6. Disabling it at the interface removes that entire bypass
  # class rather than trying to mirror every rule into ip6tables.
  boot.kernel.sysctl = {
    "net.ipv6.conf.${iface}.disable_ipv6" = 1;
    "net.ipv6.conf.${iface}.accept_ra" = 0;
  };

  services.kea.dhcp4 = {
    enable = true;
    settings = {
      interfaces-config.interfaces = [ iface ];
      lease-database = {
        type = "memfile";
        persist = true;
        name = "/var/lib/kea/dhcp4.leases";
      };
      valid-lifetime = 3600;
      subnet4 = [{
        id = 50;
        inherit subnet;
        pools = [{ pool = "${poolFrom} - ${poolTo}"; }];
        option-data = [
          { name = "routers"; data = gateway; }
          { name = "domain-name-servers"; data = gateway; }
        ];
        reservations = [{
          hw-address = kindleMAC;
          ip-address = kindleIP;
        }];
      }];
    };
  };

  # Masquerade the segment out the uplink. Scoped to this interface, so Docker's
  # own POSTROUTING rules are untouched.
  networking.nat = {
    enable = true;
    externalInterface = uplink;
    internalInterfaces = [ iface ];
  };

  # Gromit itself answers DHCP and DNS for the segment. Nothing else is exposed
  # to it — no SSH, no 443, no Jellyfin. (blocky.nix already opens 53 on this
  # interface for its standby-NIC role; repeating it here keeps this module
  # self-contained, and the lists merge.)
  networking.firewall.interfaces.${iface} = {
    allowedUDPPorts = [ 53 67 ];
    allowedTCPPorts = [ 53 ];
  };

  # ── The default-deny forward policy ─────────────────────────────────────────
  # Deliberately a DEDICATED CHAIN jumped to from FORWARD, rather than changing
  # the FORWARD policy itself: Docker writes extensively into FORWARD on this
  # box, and flipping the global policy would break container networking and
  # could be bypassed by Docker's own ACCEPTs. Scoping to this interface leaves
  # every other forwarding path exactly as it was.
  #
  # Rule order matters: conntrack first (so replies to permitted flows return),
  # then an explicit DROP toward the house LAN, then the pinholes, then DROP.
  networking.firewall.extraCommands = ''
    iptables -N kindle-fwd 2>/dev/null || true
    iptables -F kindle-fwd

    iptables -A kindle-fwd -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A kindle-fwd -i ${iface} -d ${houseNet} -j DROP  # no reaching the house LAN
    ${acceptRules}
    iptables -A kindle-fwd -j DROP  # everything else, both directions

    # Jump in from FORWARD for both directions, at the top, idempotently.
    iptables -C FORWARD -i ${iface} -j kindle-fwd 2>/dev/null || iptables -I FORWARD 1 -i ${iface} -j kindle-fwd
    iptables -C FORWARD -o ${iface} -j kindle-fwd 2>/dev/null || iptables -I FORWARD 2 -o ${iface} -j kindle-fwd

    # v6 is disabled on the interface above; this is the belt to that braces, so
    # a future sysctl change cannot silently open a v6 bypass.
    ip6tables -C FORWARD -i ${iface} -j DROP 2>/dev/null || ip6tables -I FORWARD 1 -i ${iface} -j DROP
    ip6tables -C FORWARD -o ${iface} -j DROP 2>/dev/null || ip6tables -I FORWARD 1 -o ${iface} -j DROP
  '';

  networking.firewall.extraStopCommands = ''
    iptables -D FORWARD -i ${iface} -j kindle-fwd 2>/dev/null || true
    iptables -D FORWARD -o ${iface} -j kindle-fwd 2>/dev/null || true
    iptables -F kindle-fwd 2>/dev/null || true
    iptables -X kindle-fwd 2>/dev/null || true
    ip6tables -D FORWARD -i ${iface} -j DROP 2>/dev/null || true
    ip6tables -D FORWARD -o ${iface} -j DROP 2>/dev/null || true
  '';

  environment.systemPackages = [ counters ];
}
