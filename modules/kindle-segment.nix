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
# The first attempt blocked Amazon's OTA domains in blocky, scoped to the
# Kindle's IP on the house LAN. Chris rejected it, correctly: that design is
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
#   1. iptables: default DROP for anything crossing this interface, with
#      narrowly scoped pinholes.
#   2. blocky (see services/blocky.nix): this CIDR gets an allowlist-only group
#      — a /.*/ denylist plus the permitted names. Verified behaviourally, not
#      assumed: a live blocky answered sanctuary.skyvincent.com and returned
#      0.0.0.0 for google.com, amazon.com and all three OTA hosts.
#
# Break either one and nothing on this segment reaches anything.
#
# ── OBSERVE FIRST, THEN ALLOW ─────────────────────────────────────────────────
# Nothing here is based on a guess about what a device needs, because the
# segment can be watched. Two complementary logs, and BOTH are required:
#
#   * blocky's queryLog names what was ASKED FOR, per client IP — this is what
#     reveals a controller hostname like unifi.broadlinc.com.
#   * the kernel's `kindle-fw-drop:` lines name what was SENT, with real
#     addresses and ports — the only thing that catches a client dialling a
#     HARDCODED IP, which never appears in DNS at all.
#
# `kindle-watch` prints both, plus the leases and the per-rule counters.
#
# The intended sequence:
#   1. Bring the segment up with the pinhole list as it ships, plug ONLY the AP
#      in, and leave it to sit. Read `kindle-watch`. It reaches nothing; that
#      is the point. Note its MAC from the lease file and what it wanted.
#   2. Fill in `apMAC` and add its pinholes below, scoped to its reservation.
#   3. Dry-run the jailbreak page from a LAPTOP on this segment and watch again
#      — Sanctuary pulls three files and it is undocumented whether they share
#      the page's host.
#   4. Only then connect the Kindle.
#
# ── After the jailbreak ───────────────────────────────────────────────────────
# Repoint the Kindle's pinhole at gromit's own 192.168.1.65:443 and it lives
# here permanently: Calibre-Web over OPDS and NOTHING else, no internet at all,
# so it can never OTA again. That retires the risk outright rather than
# maintaining a denylist against it — and gives the isolated untrusted-device
# network that was wanted anyway.
#
# ⚠️ COST: enp6s0u1 is the designated standby LAN NIC (networking.nix mirrors
# enp3s0's rules onto it so a failed onboard port is a cable move). Using it
# here spends that failover. enp3s0 was re-checked at 1000 Mb/s on 2026-10-31,
# so the August 100 Mb problem is not currently in play — but if the onboard
# port dies while this is deployed, moving the cable also means disabling this.
#
# ── The AP side (Chris's plan, 2026-10-31) ───────────────────────────────────
# Provision the UniFi AP HD on the 16-port switch with the controller reachable,
# give it a DEDICATED Kindle-only SSID, then move its switch port onto an
# isolated VLAN and feed gromit's spare port.
#
# The dedicated SSID is what removes the association hazard: sharing the house
# SSID would let the Kindle pick a house AP on signal strength and reach the
# internet unfiltered, with nothing on gromit able to see it had happened.
#
# ⚠️ GROMIT'S PORT MUST DELIVER THE VLAN UNTAGGED. This module puts the address
# straight on enp6s0u1, so set that switch port's Native VLAN / Primary Network
# to the Kindle VLAN and allow no other tagged networks on it. If it arrives
# tagged, nothing reaches this interface and the segment is simply dead
# (fail-closed, but confusing). To run it tagged instead, add
#   networking.vlans."kindle" = { id = <vid>; interface = "enp6s0u1"; };
# and point `iface` below at that name.
#
# An adopted UniFi AP keeps serving its provisioned SSIDs indefinitely with no
# controller reachable, so step 1 costs nothing operationally — the AP keeps
# working while being watched. It takes a DHCP lease here, which is why the
# Kindle has a reservation: the lease file shows BOTH, and the Kindle is
# identified by MAC, never by being "the only one".
{ config, lib, pkgs, ... }:

let
  iface = "enp6s0u1";
  gateway = "192.168.50.1";
  prefixLength = 24;
  subnet = "192.168.50.0/24";
  poolFrom = "192.168.50.100";
  poolTo = "192.168.50.150";

  # The Kindle's own Wi-Fi MAC, read off its Device Info screen.
  kindleMAC = "34:af:b3:27:e8:7a";
  kindleIP = "192.168.50.10";

  # The UniFi AP, learned from its DHCP DISCOVERs on 2026-11-01 — it is the only
  # client on this wire. Reserved so it is never confused with the Kindle. Its
  # PINHOLES are still deliberately empty: knowing its MAC is not knowing what
  # it needs, and that is still the observation pass's job.
  # (was: left null on purpose — step 1 is to plug it in with no pinholes
  #  at all and WATCH, which is also how its MAC is learned.)
  apMAC = "68:d7:9a:d6:bd:10";
  apIP = "192.168.50.11";

  # unifi.broadlinc.com, resolved 2026-11-01 — identical from 1.1.1.1 and
  # 8.8.8.8. Pinned as an address like the Sanctuary host: if the controller
  # ever moves, the AP loses it, which is the safe direction.
  controllerIP = "137.184.194.91";

  uplink = "enp3s0";
  houseNet = "192.168.1.0/24";

  # ── Pinholes ────────────────────────────────────────────────────────────────
  # Every entry is scoped to a SOURCE address, so one device's permitted flow can
  # never become another's. Anything not listed is dropped and logged.
  pinholes = [
    # sanctuary.skyvincent.com — one A record, identical from 1.1.1.1 / 9.9.9.9 /
    # 8.8.8.8, no CNAME, TTL 1799: a single-homed host rather than a CDN, so
    # pinning the address is sound. If it moves, the failure is "no internet",
    # which is the safe direction; re-resolve and update here.
    { src = kindleIP; dst = "78.24.205.144"; proto = "tcp"; port = 80;
      what = "sanctuary.skyvincent.com — the jailbreak"; }

    # ── AP → UniFi controller ────────────────────────────────────────────────
    # The observation pass on 2026-11-01 found the AP reaches its controller
    # TWO different ways, which is why both logs were needed:
    #
    #   * by NAME for inform — `unifi.broadlinc.com` asked for 4×, answered
    #     0.0.0.0, so no packet was ever sent. DNS log only.
    #   * by HARDCODED IP for syslog — 44 packets to 137.184.194.91 udp/5514,
    #     which never appears in DNS at all. Packet log only.
    #
    # Either log alone would have produced an incomplete allowlist.
    #
    # The inform PORT is 8080 on Chris's word — he runs this controller, so
    # that is first-hand rather than the convention being assumed. The drop log
    # stays the check: if 8080 is not the whole story, it will say so.
    { src = apIP; dst = controllerIP; proto = "tcp"; port = 8080;
      what = "unifi.broadlinc.com — inform/adoption"; }
    { src = apIP; dst = controllerIP; proto = "udp"; port = 5514;
      what = "same host — remote syslog (observed, 44 pkts)"; }

    # NOT allowed, on purpose: {0,1,2,3}.ubnt.pool.ntp.org. Pool records rotate
    # across many addresses, so an IP pinhole for them would be fragile and
    # broad. A UniFi AP runs fine with drifting time, and inform on 8080 is
    # plain HTTP so there is no certificate clock-skew problem. If AP
    # timestamps matter later, the clean fix is to map those names to the
    # gateway in blocky's customDNS and serve NTP locally — zero egress —
    # rather than open a pinhole per pool address.
  ];

  mkPinhole = p:
    "iptables -A kindle-fwd -i ${iface} -s ${p.src} -d ${p.dst} -p ${p.proto}"
    + " --dport ${toString p.port} -j ACCEPT  # ${p.what}";
  acceptRules = lib.concatMapStringsSep "\n    " mkPinhole pinholes;

  reservations =
    [{ hw-address = kindleMAC; ip-address = kindleIP; }]
    ++ lib.optional (apMAC != null) { hw-address = apMAC; ip-address = apIP; };

  # One place to see everything this segment did: what it asked for, what it
  # sent, who holds a lease, and which rules fired.
  watch = pkgs.writeShellScriptBin "kindle-watch" ''
    set -uo pipefail
    since="''${1:--30 min}"
    echo "=== SEGMENT HEALTH ==="
    # This block exists because of 2026-11-01: every counter below read zero and
    # every service read active while the segment was structurally dead. Zero
    # counters mean "nothing happened" OR "nothing CAN happen" — these four
    # checks tell the two apart.
    addr=$(${pkgs.iproute2}/bin/ip -4 -o addr show ${iface} 2>/dev/null | ${pkgs.gawk}/bin/awk '{print $4}' | tr '\n' ' ')
    if echo "$addr" | ${pkgs.gnugrep}/bin/grep -q '${gateway}/'; then
      echo "  [ ok ] ${iface} has ${gateway} — kea can select its subnet"
    else
      echo "  [FAIL] ${iface} address is '$addr', expected ${gateway}/${toString prefixLength}"
      echo "         kea cannot match its subnet and will answer NOTHING, silently."
      echo "         check: systemctl status network-addresses-${iface}.service"
    fi
    case "$addr" in
      *192.168.1.*) echo "  [WARN] a HOUSE-LAN address is present — is this wire really isolated?" ;;
    esac
    for u in kea-dhcp4-server blocky; do
      printf "  [%s] %s\n" "$(${pkgs.systemd}/bin/systemctl is-active $u >/dev/null 2>&1 && echo ' ok ' || echo FAIL)" "$u"
    done
    if ${pkgs.iptables}/bin/iptables -L kindle-fwd -n >/dev/null 2>&1; then
      echo "  [ ok ] kindle-fwd chain present"
    elif [ "$(id -u)" -ne 0 ]; then
      echo "  [ -- ] kindle-fwd chain: needs root to check"
    else
      echo "  [FAIL] kindle-fwd chain MISSING — nothing is being filtered"
    fi
    echo
    echo "=== DNS: what the segment ASKED FOR (since $since) ==="
    echo "    (names only show here; a hardcoded IP never appears in DNS)"
    ${pkgs.systemd}/bin/journalctl -u blocky --since "$since" --no-pager -o cat \
      | ${pkgs.gnugrep}/bin/grep -F 'queryLog' \
      | ${pkgs.gnugrep}/bin/grep -E 'client_ip=192\.168\.50\.' \
      | ${pkgs.gnused}/bin/sed -E 's/.*client_ip=([0-9.]+).*question_name=([^ ]+).*response_type=([A-Z]+).*/\1  \3  \2/' \
      | sort | uniq -c | sort -rn | head -40
    echo
    echo "=== PACKETS: what it actually SENT and got dropped (since $since) ==="
    ${pkgs.systemd}/bin/journalctl -k --since "$since" --no-pager -o cat \
      | ${pkgs.gnugrep}/bin/grep -F 'kindle-fw-drop:' \
      | ${pkgs.gnused}/bin/sed -E 's/.*SRC=([0-9.]+).*DST=([0-9.]+).*PROTO=([A-Z]+).*(DPT=[0-9]+).*/\1 -> \2 \3 \4/' \
      | sort | uniq -c | sort -rn | head -40
    echo
    echo "=== LEASES (identify by MAC — the AP leases here too) ==="
    if [ -r /var/lib/kea/dhcp4.leases ]; then
      ${pkgs.coreutils}/bin/cat /var/lib/kea/dhcp4.leases
    else
      echo "    (no lease file yet at /var/lib/kea/dhcp4.leases)"
    fi
    echo
    echo "=== RULE COUNTERS ==="
    if [ "$(id -u)" -ne 0 ]; then
      echo "    (needs root — re-run as: sudo kindle-watch)"
    else
    ${pkgs.iptables}/bin/iptables -L kindle-fwd -v -n --line-numbers
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

  # No IPv6 on this segment, deliberately. The forward rules are v4, so a
  # working v6 path would route straight around them — and gromit's uplink does
  # have public v6. Disabling it at the interface removes that entire bypass
  # class rather than mirroring every rule into ip6tables.
  boot.kernel.sysctl = {
    "net.ipv6.conf.${iface}.disable_ipv6" = 1;
    "net.ipv6.conf.${iface}.accept_ra" = 0;
  };

  # ⚠️ THE UNIT THAT NEVER RAN (first deploy, 2026-11-01).
  # NixOS generates network-addresses-<if>.service with WantedBy pointing ONLY
  # at the interface's .device unit. enp6s0u1 was ALREADY up when this module
  # first deployed, so no udev event ever fired, the unit sat inactive(dead),
  # and the interface kept a stale DHCP address from before the switch port was
  # moved to the isolated VLAN. Nothing complained.
  #
  # The damage was entirely silent: with no 192.168.50.x address on the
  # interface kea could not select its configured subnet, so it logged
  # "listening on interface enp6s0u1", accepted every DHCP query the AP sent,
  # and answered NONE of them. A healthy-looking service that was structurally
  # incapable of its job — the AP broadcast DISCOVERs into nothing for ten
  # minutes. Safe, but by accident rather than by design, which is why
  # kindle-watch now leads with a health block instead of raw counters.
  systemd.services."network-addresses-${iface}" = {
    wantedBy = [ "multi-user.target" ];
    # NetworkManager is unmanaged for this interface by now, so a stale address
    # left over from the house-LAN era is never cleaned up by anything. Flush
    # before assigning, or the old address just sits alongside the new one.
    preStart = "${pkgs.iproute2}/bin/ip addr flush dev ${iface} || true";
  };

  # Ordering, so kea cannot repeat the trick of starting subnet-less. `wants`
  # rather than `requires`: the address unit is BindsTo= its .device, so a hard
  # requirement would stop kea on a cable pull and never bring it back.
  systemd.services.kea-dhcp4-server = {
    after = [ "network-addresses-${iface}.service" ];
    wants = [ "network-addresses-${iface}.service" ];
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
        inherit subnet reservations;
        pools = [{ pool = "${poolFrom} - ${poolTo}"; }];
        option-data = [
          { name = "routers"; data = gateway; }
          { name = "domain-name-servers"; data = gateway; }
        ];
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
  # to it — no SSH, no 443, no Jellyfin.
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
  # Order: conntrack first (so replies to permitted flows return), an explicit
  # DROP toward the house LAN, the pinholes, then LOG-and-DROP.
  networking.firewall.extraCommands = ''
    iptables -N kindle-fwd 2>/dev/null || true
    iptables -F kindle-fwd

    iptables -A kindle-fwd -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A kindle-fwd -i ${iface} -d ${houseNet} -j DROP  # no reaching the house LAN
    ${acceptRules}

    # Log before dropping — this is what makes "observe, then allow" possible,
    # and the ONLY place a client dialling a hardcoded IP shows up.
    # Rate-limited so a chatty AP retrying its controller cannot flood journald.
    iptables -A kindle-fwd -m limit --limit 20/min --limit-burst 40 \
      -j LOG --log-prefix "kindle-fw-drop: " --log-level 6
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

  environment.systemPackages = [ watch ];
}
