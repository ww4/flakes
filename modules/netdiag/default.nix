# NETDIAG — network diagnostics, for client service calls AND the home LAN.
#
# Chris carries marcus to client sites; the working pattern is "put the laptop
# on their network and start investigating". This module makes that a real
# capability rather than improvised shell.
#
# ENABLED ON BOTH gromit AND marcus, deliberately — and gromit is arguably the
# more capable of the two, which inverts the obvious intuition:
#   - gromit is WIRED (enp3s0). marcus is usually on wifi, and most layer-2
#     diagnostics either do not work at all over wifi or return actively
#     misleading numbers (see the WIFI WARNING in netdiag.sh). For the home
#     LAN, run these on gromit.
#   - gromit is ALWAYS ON. Rogue DHCP and rogue IPv6 RA are intermittent by
#     nature and reward long observation; marcus is intermittently online.
#   - the agent lives on gromit, so diagnosing the home network no longer
#     depends on someone having booted the laptop.
# marcus keeps it because it is the machine that travels.
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
  # The derivations live in ./packages.nix so that netwatch.nix — a SIBLING
  # module, not a child — can reference the same ones. They used to be
  # `let`-bound right here, which made them unreachable from netwatch and left
  # its units with a `path` that could not contain them.
  inherit (import ./packages.nix { inherit pkgs; })
    netdiag netdiagPriv wsdiscover;
in
{
  # LLDP — RECEIVE-ONLY on BOTH hosts.
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
