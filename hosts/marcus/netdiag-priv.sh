#!/usr/bin/env bash
# netdiag-priv — the ONLY privileged entry point of the netdiag toolkit.
#
# Everything here needs raw sockets or an interface change; nothing here is raw
# tool access. Each subcommand builds its own arguments from a validated, fixed
# vocabulary, so this cannot be turned into a general root shell:
#
#   - interface names are regex-checked AND must already exist
#   - CIDRs are regex-checked, octet-range-checked, /8../30 only
#   - capture filters are chosen from a fixed profile list, never passed in
#   - tcpdump NEVER gets -w/-W/-z (file writes and its postrotate-exec hook)
#   - nmap is only ever invoked with a FIXED script and a FIXED port list;
#     no user-supplied --script, port spec, or -oN path
#   - captures are snaplength-limited to headers, so a service call cannot
#     hoover up a client's payload traffic
#   - temporary addresses carry a kernel lifetime and expire on their own
#
# Called by `netdiag`; run directly only for debugging. See netdiag.nix.
set -euo pipefail

PROG=netdiag-priv
LABEL_SUFFIX=nd

# The curated UDP set. Full-range UDP is impossible (targets rate-limit ICMP
# port-unreachable to ~1/sec, so 65535 ports floors at ~18h). These are the
# ports that actually carry findings. 623 = IPMI/BMC, which sits at rank 87 in
# nmap's frequency table — inside --top-ports 100 but outside 50, so it is
# missed by the obvious shortcut.
UDP_AUDIT_PORTS=53,67,68,69,111,123,137,138,161,162,445,500,514,520,623,1434,1900,4500,5060,5353

die() { echo "$PROG: $*" >&2; exit 2; }

usage() {
  cat <<'EOF'
netdiag-priv <subcommand> [args]      (invoked through sudo by `netdiag`)

  capture <iface> <profile> <secs>    control-plane capture, headers only
        profiles: arp dhcp discovery neighbour stp vlan storm loop all
  arpscan <iface> [cidr]              ARP sweep — layer-2 ground truth + vendors
  dhcp-probe <iface>                  DHCP DISCOVER: who answers, with what scope
  ra-probe <iface>                    IPv6 router advertisements — ALL responders
  lldp                                LLDP/CDP neighbours (receive-only daemon)
  ubnt <cidr>                         Ubiquiti UDP-10001 discovery (adoption state)
  udp-audit <ip>                      curated 20-port UDP service scan
  ifstats <iface>                     NIC hardware counters (broadcast/mcast/errors)
  vlan-offload <iface> on|off         NIC VLAN-tag stripping (must be OFF to see tags)
  addr-add <iface> <cidr> [secs]      temporary secondary IP (default 1800s, self-expiring)
  addr-clear <iface>                  drop every netdiag-added address
EOF
}

valid_iface() {
  local i=${1-}
  [[ $i =~ ^[a-z][a-z0-9_-]{0,10}$ ]] || die "bad interface name: '${i}'"
  ip link show dev "$i" >/dev/null 2>&1 || die "no such interface: '${i}'"
}

valid_secs() {
  local s=${1-} max=${2-300}
  [[ $s =~ ^[0-9]+$ ]] || die "seconds must be a number: '${s}'"
  (( s >= 1 && s <= max )) || die "seconds must be 1..${max}: '${s}'"
}

valid_cidr() {
  local c=${1-} addr pfx octet
  [[ $c =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] || die "bad CIDR: '${c}'"
  addr=$(echo "$c" | cut -d/ -f1)
  pfx=$(echo "$c" | cut -d/ -f2)
  (( pfx >= 8 && pfx <= 30 )) || die "prefix must be /8../30: '${c}'"
  for octet in $(echo "$addr" | tr '.' ' '); do
    (( octet <= 255 )) || die "octet out of range in '${c}'"
  done
}

valid_ip() {
  local a=${1-} octet
  [[ $a =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "bad IPv4 address: '${a}'"
  for octet in $(echo "$a" | tr '.' ' '); do
    (( octet <= 255 )) || die "octet out of range in '${a}'"
  done
}

# Fixed capture vocabulary. The caller picks a NAME; the filter text is ours.
# 'loop' is special-cased in cmd_capture because it needs the interface's MAC.
filter_for() {
  case ${1-} in
    arp)       echo 'arp or rarp' ;;
    dhcp)      echo 'udp port 67 or udp port 68 or udp port 546 or udp port 547' ;;
    discovery) echo 'udp port 5353 or udp port 3702 or udp port 1900 or udp port 137 or udp port 138 or udp port 5678 or udp port 10001' ;;
    neighbour) echo 'ether proto 0x88cc or ether host 01:00:0c:cc:cc:cc' ;;
    stp)       echo 'stp' ;;
    vlan)      echo 'vlan' ;;
    # Storm analysis wants ALL broadcast/multicast, not a protocol subset —
    # the whole point is measuring the rate of everything flooding the segment.
    storm)     echo 'ether broadcast or ether multicast' ;;
    all)       echo 'arp or rarp or udp port 67 or udp port 68 or udp port 546 or udp port 547 or udp port 5353 or udp port 3702 or udp port 1900 or udp port 137 or udp port 138 or udp port 5678 or udp port 10001 or ether proto 0x88cc or ether host 01:00:0c:cc:cc:cc or stp or icmp6' ;;
    *)         die "unknown capture profile: '${1-}' (arp dhcp discovery neighbour stp vlan storm loop all)" ;;
  esac
}

cmd_capture() {
  local iface=${1-} profile=${2-} secs=${3-30} filter mymac rc
  valid_iface "$iface"
  valid_secs "$secs" 300
  if [[ $profile == loop ]]; then
    # THE definitive L2 loop test. With -Q in, any frame arriving with our own
    # MAC as source was returned to us by the switch fabric. There is no benign
    # explanation. The filter is built from the kernel, never from user input.
    mymac=$(cat "/sys/class/net/${iface}/address")
    filter="ether src ${mymac}"
  else
    filter=$(filter_for "$profile")
  fi
  # -Q in is NOT optional: libpcap otherwise delivers our OWN outbound frames,
  # which inflates every rate and makes us appear as a source in the loop test.
  # -s 512 keeps headers and control-plane options but truncates payload.
  # No -w/-W/-z: stdout only, and nothing can be executed.
  set +e
  timeout "$secs" tcpdump -i "$iface" -Q in -nn -e -tttt -s 512 -l "$filter"
  rc=$?
  set -e
  case $rc in
    0|124|143) : ;;
    *) die "tcpdump exited $rc" ;;
  esac
}

cmd_arpscan() {
  local iface=${1-} target=${2-}
  valid_iface "$iface"
  # --plain drops the banner; duplicates are NOT suppressed (no --ignoredups)
  # because a duplicate reply is exactly the IP-conflict signal we want.
  if [[ -z $target ]]; then
    arp-scan --interface="$iface" --localnet --retry=3 --plain
  else
    valid_cidr "$target"
    arp-scan --interface="$iface" --retry=3 --plain "$target"
  fi
}

cmd_dhcp_probe() {
  local iface=${1-}
  valid_iface "$iface"
  # DISCOVER only — the script never accepts an offer, so no lease is taken.
  # A longer timeout catches slow/secondary responders; mac=random avoids
  # colliding with a reservation for this laptop's real MAC.
  nmap --script broadcast-dhcp-discover \
       --script-args 'broadcast-dhcp-discover.timeout=20,broadcast-dhcp-discover.mac=random' \
       -e "$iface" 2>/dev/null | sed -n '/broadcast-dhcp-discover/,$p'
}

cmd_ra_probe() {
  local iface=${1-}
  valid_iface "$iface"
  # -m = wait for and display ALL responses, not just the first. That is what
  # turns "find my router" into "find every router", which is the rogue check.
  rdisc6 -m -w 5000 -n "$iface" 2>&1 || true
}

cmd_lldp() {
  lldpcli show neighbors details 2>/dev/null ||
    die "lldpcli failed — is lldpd running?"
}

cmd_ubnt() {
  local target=${1-}
  valid_cidr "$target"
  # Fixed script, fixed port. The portrule matches open|filtered, so UDP's
  # usual ambiguity does not suppress results.
  nmap -sU -p 10001 --script ubiquiti-discovery "$target" 2>/dev/null |
    sed -n '/Nmap scan report/,$p'
}

cmd_udp_audit() {
  local target=${1-}
  valid_ip "$target"
  nmap -sU -sV --version-intensity 0 -p "$UDP_AUDIT_PORTS" --open "$target" 2>/dev/null |
    sed -n '/Nmap scan report/,$p'
}

cmd_ifstats() {
  local iface=${1-}
  valid_iface "$iface"
  # Counter NAMES are driver-defined (igb says rx_broadcast, others just
  # broadcast, some expose neither) — always grep, never assume a name.
  ethtool -S "$iface" 2>/dev/null | grep -iE 'broadcast|multicast|error|crc|drop|missed' ||
    echo "(this driver exposes no broadcast/multicast counters)"
}

cmd_vlan_offload() {
  local iface=${1-} state=${2-}
  valid_iface "$iface"
  case $state in
    on|off) : ;;
    *) die "vlan-offload state must be on|off: '${state}'" ;;
  esac
  # With rx-vlan-offload ON the NIC strips the 802.1Q tag in hardware before
  # the frame reaches libpcap. You then capture a busy trunk and conclude
  # "no VLANs here". Must be OFF to enumerate tags.
  ethtool -K "$iface" rxvlan "$state"
  echo "$PROG: rx-vlan-offload on ${iface} -> ${state}"
}

cmd_addr_add() {
  local iface=${1-} cidr=${2-} ttl=${3-1800} label
  valid_iface "$iface"
  valid_cidr "$cidr"
  valid_secs "$ttl" 7200
  label="${iface}:${LABEL_SUFFIX}"
  (( ${#label} <= 15 )) || die "interface name too long to label: '${iface}'"
  if ip -4 -j addr show dev "$iface" |
       jq -e --arg c "$cidr" '[.[0].addr_info[]? | "\(.local)/\(.prefixlen)"] | index($c)' >/dev/null; then
    echo "$PROG: ${cidr} already present on ${iface}"
    return 0
  fi
  # valid_lft makes the kernel reclaim this by itself, so a forgotten address
  # never lingers on a client's network after we leave.
  ip addr add "$cidr" dev "$iface" label "$label" valid_lft "$ttl" preferred_lft "$ttl"
  echo "$PROG: added ${cidr} on ${iface} (expires in ${ttl}s)"
}

cmd_addr_clear() {
  local iface=${1-} label found=0 a
  valid_iface "$iface"
  label="${iface}:${LABEL_SUFFIX}"
  while read -r a; do
    [[ -n $a ]] || continue
    ip addr del "$a" dev "$iface" && echo "$PROG: removed ${a} from ${iface}"
    found=1
  done < <(ip -4 -j addr show dev "$iface" |
             jq -r --arg L "$label" '.[0].addr_info[]? | select(.label==$L) | "\(.local)/\(.prefixlen)"')
  (( found )) || echo "$PROG: no netdiag-added addresses on ${iface}"
}

main() {
  local sub=${1-}
  [[ -n $sub ]] || { usage; exit 1; }
  shift
  case $sub in
    capture)      cmd_capture "$@" ;;
    arpscan)      cmd_arpscan "$@" ;;
    dhcp-probe)   cmd_dhcp_probe "$@" ;;
    ra-probe)     cmd_ra_probe "$@" ;;
    lldp)         cmd_lldp "$@" ;;
    ubnt)         cmd_ubnt "$@" ;;
    udp-audit)    cmd_udp_audit "$@" ;;
    ifstats)      cmd_ifstats "$@" ;;
    vlan-offload) cmd_vlan_offload "$@" ;;
    addr-add)     cmd_addr_add "$@" ;;
    addr-clear)   cmd_addr_clear "$@" ;;
    -h|--help|help) usage ;;
    *)            die "unknown subcommand: '${sub}'" ;;
  esac
}

main "$@"
