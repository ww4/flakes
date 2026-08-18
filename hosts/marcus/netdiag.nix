# NETDIAG — field network diagnostics for service calls.
#
# Chris carries marcus to client sites; the working pattern is "put the laptop
# on their network and start investigating". This module makes that a real
# capability rather than improvised shell.
#
# Built 2026-08-18, straight out of the Craigmyle Tractor camera outage. Three
# things cost the most time that day and each one is answered here:
#
#   1. NO PACKET CAPTURE. A stale VLAN was handing out a parallel
#      192.168.128.0/24 on the same wire; it was found by luck, because Amcrest
#      happen to broadcast their IP config over mDNS. The Dahua and Hikvision
#      cameras on that site advertise nothing, so the luck does not generalise.
#      `netdiag listen` now groups every IPv4 seen in a control-plane capture
#      into /24s — a second populated subnet is then obvious in seconds.
#   2. NO LLDP/CDP. "Which switch port is this camera on?" was a dead end for
#      an entire afternoon. `netdiag whoami` answers it for our own port, and
#      services.lldpd makes us visible to the switch in return.
#   3. FRAGILE SHELL. Four separate parsing bugs that day each produced a
#      plausible EMPTY RESULT rather than an error — the worst possible failure
#      mode, because "nothing found" and "my parser broke" look identical.
#      Everything here is a tested script that shellcheck gates at build time.
#
# Privilege model (Chris's call, 2026-08-18): a NARROW sudo allowlist, not
# blanket sudo. `netdiag-priv` is the single privileged entry point and accepts
# only a fixed vocabulary — validated interface names and CIDRs, capture
# filters chosen from a named profile list rather than passed in, and never
# tcpdump's -w/-W/-z (file writes and its postrotate exec hook). Captures are
# snaplength-limited to headers, so a service call cannot hoover up a client's
# payload traffic. The sudoers entry lives in modules/agent/sudo.nix.
{ config, lib, pkgs, ... }:

let
  # ONVIF WS-Discovery. Vendor-neutral, so it sees the cameras mDNS misses.
  wsdiscover = pkgs.writers.writePython3Bin "netdiag-wsdiscover" {
    flakeIgnore = [ "E501" "E203" "W503" ];
  } (builtins.readFile ./netdiag-wsdiscover.py);

  # The privileged half. Small on purpose — everything it does needs raw
  # sockets or an interface change, and nothing else belongs here.
  netdiagPriv = pkgs.writeShellApplication {
    name = "netdiag-priv";
    runtimeInputs = with pkgs; [
      coreutils gnused gnugrep gawk
      iproute2 jq tcpdump arp-scan nmap lldpd
    ];
    text = builtins.readFile ./netdiag-priv.sh;
  };

  # The unprivileged half — the command surface actually driven day to day.
  netdiag = pkgs.writeShellApplication {
    name = "netdiag";
    runtimeInputs = (with pkgs; [
      coreutils gnused gnugrep gawk findutils
      iproute2 jq nmap curl avahi sudo
    ]) ++ [ wsdiscover ];
    text = builtins.readFile ./netdiag.sh;
  };
in
{
  # LLDP: learn the upstream switch and port, and advertise ourselves back so
  # the switch's neighbour table names this laptop instead of a bare MAC.
  services.lldpd.enable = true;

  environment.systemPackages = [ netdiag netdiagPriv wsdiscover ] ++ (with pkgs; [
    # --- discovery / layer 2 ---
    arp-scan          # ARP sweep + built-in OUI vendor database (ground truth)
    fping             # fast parallel ICMP sweeps
    masscan           # very fast sweeps when the range is large
    nbtscan           # NetBIOS name sweep — names Windows hosts fast
    avahi             # avahi-browse: mDNS/Bonjour enumeration

    # --- capture / protocol analysis ---
    wireshark-cli     # tshark: dissect DHCP/ARP/CDP/LLDP/STP properly
    termshark         # TUI over the same captures, for Chris's own eyes

    # --- topology / addressing ---
    lldpd             # lldpcli: which switch, which port, which VLAN
    ipcalc            # subnet maths
    sipcalc           # subnet maths, richer output

    # --- services / transport ---
    dhcping           # is a DHCP server actually answering?
    net-snmp          # snmpwalk/snmpget: switches, radios, UPSes
    whois             # ownership when a strange public IP turns up
    socat             # arbitrary socket plumbing
    netcat-gnu        # port poking
    samba             # smbclient: shares and auth on Windows hosts
    tcptraceroute     # traceroute that survives ICMP-filtering firewalls
    arping            # single-target layer-2 liveness
    speedtest-cli     # throughput complaints

    # --- wireless (WISP work) ---
    iw                # station/survey data
    wavemon           # live signal monitor

    # --- console access ---
    picocom           # serial console into a switch or radio
  ]);
}
