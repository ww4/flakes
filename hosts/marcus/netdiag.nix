# NETDIAG — field network diagnostics for client service calls.
#
# Chris carries marcus to client sites; the working pattern is "put the laptop
# on their network and start investigating". This module makes that a real
# capability rather than improvised shell.
#
# Built 2026-08-18 out of the Craigmyle Tractor camera outage, then broadened by
# a research pass into the problems that actually recur on service calls:
# broadcast storms and L2 loops, rogue DHCP and rogue IPv6 RA, unknown devices
# on static addresses, unadopted UniFi APs, camera/stream enumeration, and
# service-exposure auditing.
#
# THE THREE THINGS THAT COST THE MOST TIME AT CRAIGMYLE, AND THEIR ANSWERS:
#   1. NO PACKET CAPTURE. A stale VLAN was handing out a parallel
#      192.168.128.0/24 on the same wire; it was found by luck, because Amcrest
#      happen to broadcast their IP config over mDNS. The site's Dahua and
#      Hikvision cameras advertise nothing, so the luck does not generalise.
#      `netdiag listen` buckets every address seen into /24s.
#   2. NO LLDP/CDP, and no way to answer "which switch port is this on". The
#      port history died with the switch the storm destroyed. `netdiag whoami`
#      answers it for our own port; `netdiag switchport <mac> <switch-ip>` walks
#      the SNMP bridge forwarding table and answers it for ANY device — which is
#      the question that cost an entire afternoon, and it needs no vendor API.
#   3. FRAGILE SHELL. Four parsing bugs, each producing a plausible EMPTY RESULT
#      rather than an error. Everything here is tested shell in its own file,
#      gated by shellcheck at build time (the ethswitch.sh precedent).
#
# PRIVILEGE MODEL (Chris's call, 2026-08-18): a NARROW allowlist, not blanket
# sudo. `netdiag-priv` is the single privileged entry point and takes a closed
# vocabulary — validated interfaces and CIDRs, capture filters selected from a
# fixed profile list rather than passed in, nmap only ever with a FIXED script
# and FIXED port list, and tcpdump never handed -w/-W/-z (file writes and its
# postrotate exec hook). Two deliberate client-network properties: captures are
# snaplength-limited to headers, so a service call cannot collect a customer's
# payload traffic; and temporary addresses carry a kernel lifetime, so a
# forgotten address expires instead of lingering on someone else's network.
{ config, lib, pkgs, ... }:

let
  # Offline MAC-vendor table, baked at build time from wireshark's `manuf`.
  #
  # Deliberately NOT a hardcoded OUI list and NOT a runtime fetch of the IEEE
  # CSV. Hardcoding is wrong because it silently rots: a compiled MikroTik list
  # omitted 00:0C:42, the original RouterBOARD block and the most common one on
  # older gear at a rural site — every such device would have gone unidentified.
  # Fetching at runtime is wrong because a service call is exactly when the
  # uplink may be down, and oui.csv covers only MA-L, missing every /28 and /36
  # assignment. `manuf` is offline, covers all four registries, and is already
  # compiled into tshark.
  #
  # Output: HEXPREFIX <tab> nibble-count <tab> vendor. The nibble count drives
  # longest-prefix matching (/36 -> /28 -> /24) — a /24-only lookup returns the
  # shared IEEE registry owner rather than the real vendor for small blocks.
  ouiTable = pkgs.runCommand "netdiag-oui.tsv"
    { nativeBuildInputs = [ pkgs.wireshark-cli pkgs.gawk ]; } ''
      tshark -G manuf | grep -v '^#' | awk -F'\t' '
        {
          p = $1; sub(/[ \t]+$/, "", p)
          n = 24
          if (index(p, "/") > 0) { split(p, a, "/"); p = a[1]; n = a[2] + 0 }
          gsub(/:/, "", p)
          print toupper(p) "\t" (n / 4) "\t" $3
        }' > $out
      test -s $out
    '';

  # ONVIF WS-Discovery. Vendor-neutral, so it sees the cameras mDNS misses.
  wsdiscover = pkgs.writers.writePython3Bin "netdiag-wsdiscover" {
    flakeIgnore = [ "E501" "E203" "W503" ];
  } (builtins.readFile ./netdiag-wsdiscover.py);

  # The privileged half. Small on purpose.
  netdiagPriv = pkgs.writeShellApplication {
    name = "netdiag-priv";
    runtimeInputs = with pkgs; [
      coreutils gnused gnugrep gawk
      iproute2 jq tcpdump arp-scan nmap lldpd ndisc6 ethtool
    ];
    text = builtins.readFile ./netdiag-priv.sh;
  };

  # The unprivileged half — the command surface driven day to day.
  netdiag = pkgs.writeShellApplication {
    name = "netdiag";
    runtimeInputs = (with pkgs; [
      coreutils gnused gnugrep gawk findutils
      iproute2 jq nmap curl avahi sudo net-snmp miniupnpc libnatpmp
      ffmpeg python3
    ]) ++ [ wsdiscover ];
    text = ''
      export NETDIAG_OUI=${ouiTable}
    '' + builtins.readFile ./netdiag.sh;
  };
in
{
  # LLDP — RECEIVE-ONLY.
  #
  # The default (advertising) mode would broadcast LLDP frames into a client's
  # layer-2 domain and insert this laptop into their switch's neighbour table.
  # That is fine on our own kit and inappropriate on someone else's network: a
  # consultant's laptop should not announce itself as network infrastructure at
  # a client site. `-r` listens without transmitting, which is all the
  # diagnostics need.
  services.lldpd = {
    enable = true;
    extraArgs = [ "-r" ];
  };

  environment.systemPackages = [ netdiag netdiagPriv wsdiscover ] ++ (with pkgs; [
    # --- discovery / layer 2 ---
    arp-scan          # ARP sweep + OUI vendors; flags duplicate IPs natively
    fping             # fast parallel ICMP sweeps
    masscan           # very fast sweeps when the range is large
    nbtscan           # NetBIOS name sweep — names Windows hosts fast
    avahi             # avahi-browse: mDNS/Bonjour enumeration
    arping            # -d exits 1 on a duplicate IP: scriptable conflict check

    # --- capture / protocol analysis ---
    wireshark-cli     # tshark: the workhorse. STP, VLAN, MNDP, DHCP, RA dissectors
    termshark         # TUI over the same captures, for Chris's own eyes
    netsniff-ng       # ifpps live pps meter; mausezahn frame crafting for loop tests
    dhcpdump          # human-readable passive DHCP watch

    # --- IPv6 ---
    ndisc6            # rdisc6 -m: enumerate ALL routers. Rogue-RA detection

    # --- topology / addressing ---
    lldpd             # lldpcli: which switch, which port, which native VLAN
    ipcalc sipcalc    # subnet maths

    # --- traffic attribution ---
    # NOTE: on a switched LAN a laptop on an access port CANNOT see other hosts'
    # unicast traffic, and promiscuous mode does not change that — the switch
    # never delivers those frames. So these answer "what is MY link doing", and
    # real per-host attribution comes from SNMP counters on the switch, not from
    # capture. ntopng was evaluated and deliberately rejected: it hard-requires
    # redis, ships default admin/admin credentials, and its value only accrues
    # over days of watching a link it can actually see.
    iftop             # by host-pair, non-interactive with -t
    nethogs           # per-process, local box only
    vnstat            # interface baselining over time
    darkstat          # long-running capture with a web UI; works offline on a pcap

    # --- services / exposure ---
    dhcping           # confirm one suspected DHCP server is live
    net-snmp          # snmpwalk: the bridge table, PoE draw, port status, models
    onesixtyone       # SNMP community sweep (BYO wordlist — nmap ships one)
    miniupnpc         # upnpc -l: firewall holes punched via UPnP. Under-used
    libnatpmp         # natpmpc: the NAT-PMP half of the same question
    lsof              # fallback when ss -p is unhelpful
    testssl sslscan   # TLS triage on internal services
    whatweb           # fingerprint the unlabelled thing on port 8080
    nmap-formatter    # nmap XML -> markdown for a client report
    whois socat netcat-gnu samba tcptraceroute speedtest-cli

    # --- wireless (WISP work) ---
    iw wavemon

    # --- camera / video ---
    # ffmpeg pulls a single still frame off an RTSP camera. This is the only
    # thing that answers "which PHYSICAL camera is this address" — at Craigmyle
    # 12 of 24 cameras had no known location and no scan could map them.
    ffmpeg

    # --- console access ---
    picocom           # serial console into a switch or radio
  ]);
}
