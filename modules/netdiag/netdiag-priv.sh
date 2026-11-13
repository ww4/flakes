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
  plc <iface> [samples]               HomePlug/powerline adapters: membership + PHY rates
  ifstats <iface>                     NIC hardware counters (broadcast/mcast/errors)
  vlan-offload <iface> on|off         NIC VLAN-tag stripping (must be OFF to see tags)
  addr-add <iface> <cidr> [secs]      temporary secondary IP (default 1800s, self-expiring)
  addr-clear <iface>                  drop every netdiag-added address
  dhcp-serve <iface> [cidr] [secs]    temporary DHCP server (default 10.54.0.1/24, 1800s)
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

# Dotted-quad <-> 32-bit integer, for carving a DHCP pool out of a server CIDR.
ip2int() {
  local IFS=. a b c d
  read -r a b c d <<<"${1}"
  echo "$(( (a << 24) | (b << 16) | (c << 8) | d ))"
}
int2ip() {
  local n=${1}
  echo "$(( (n >> 24) & 255 )).$(( (n >> 16) & 255 )).$(( (n >> 8) & 255 )).$(( n & 255 ))"
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
  # --plain drops the banner. Duplicate replies are left UNSUPPRESSED (no
  # --ignoredups) so a human reading raw output sees exactly what came back.
  #
  # ⚠️ A DUPLICATE REPLY IS NOT AN IP CONFLICT. This comment previously claimed
  # it was, and netwatch was built on that claim — with --retry=3 a host simply
  # answers more than one probe, so the same IP+MAC prints repeatedly. On
  # 2026-08-19 that produced alerts naming a single MAC 2-3 times as "possible
  # ARP spoofing". A real conflict is one IP with two DIFFERENT MACs. Any
  # automated consumer of this output must deduplicate on (ip, mac) first.
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

# HomePlug AV management frames (ethertype 0x88E1) on the local segment.
#
# A powerline adapter is a transparent L2 bridge: no IP, no ARP entry of its
# own, answers no ping. It is invisible to every other probe in this toolkit,
# which is exactly why a flaky powerline leg presents as "the cameras behind it
# drop together for no reason". These frames are the only way to see it.
#
# The number that matters is the negotiated PHY rate. HomePlug AV healthy is
# roughly 80-200 Mbit/s; a degraded leg falls to single digits and will drop
# camera streams while still answering a ping perfectly.
cmd_plc() {
  local iface=${1-} samples=${2-1}
  valid_iface "$iface"
  if [[ ! $samples =~ ^[0-9]{1,2}$ ]] || ((samples < 1)); then
    die "samples must be 1-99"
  fi

  # A wireless interface finds nothing and looks identical to "no adapters
  # here". Refuse rather than report a false negative — the same trap the other
  # layer-2 checks carry a warning about.
  if [[ -d /sys/class/net/${iface}/wireless ]]; then
    die "${iface} is wireless; HomePlug management frames need a WIRED port on
      the same segment as the adapters. A wifi run returns nothing and that is
      indistinguishable from 'there are no powerline adapters'."
  fi

  echo "== powerline: network membership (${iface}) =="
  # No device argument = local broadcast, so every adapter on the segment
  # answers. That is the discovery step.
  plcstat -i "$iface" -m 2>&1 || echo "  (no reply — see the note below)"

  echo
  echo "== powerline: topology + per-link PHY rate =="
  plcstat -i "$iface" -t -l "$samples" -w 1 2>&1 ||
    echo "  (no reply — see the note below)"

  echo
  echo "== powerline: local device identity =="
  plctool -i "$iface" -I 2>&1 || true

  cat <<'NOTE'

NO REPLY AT ALL means one of: there are genuinely no HomePlug adapters on this
segment; the port is not on the same L2 segment as them; or the adapters use
HomePlug AV2/G.hn silicon that ignores these frames (G.hn is a different
standard and these tools cannot see it). It does NOT prove the absence of a
powerline leg on its own.
NOTE
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

# Reverts everything dhcp-serve put in place. Values come from globals (not the
# function's locals) because the EXIT trap fires AFTER cmd_dhcp_serve has
# returned on the normal path, when its locals are already out of scope — a
# cleanup reading locals would silently leave the address and the firewall hole
# behind. Each step is guarded so a partial setup still tears down cleanly.
DHCP_IFACE="" DHCP_CIDR="" DHCP_LEASEFILE="" DHCP_PIDFILE="" DHCP_FW_ADDED=0 DHCP_ADDR_ADDED=0
dhcp_cleanup() {
  if (( DHCP_FW_ADDED )); then
    iptables -D nixos-fw -i "$DHCP_IFACE" -p udp --dport 67 -j ACCEPT 2>/dev/null || true
  fi
  if (( DHCP_ADDR_ADDED )); then
    ip addr del "$DHCP_CIDR" dev "$DHCP_IFACE" 2>/dev/null || true
  fi
  if [[ -n $DHCP_LEASEFILE ]]; then
    rm -f "$DHCP_LEASEFILE" "$DHCP_PIDFILE" 2>/dev/null || true
  fi
}

# dhcp-serve — stand up a bounded, self-cleaning DHCP server on ONE interface.
#
# For bringing a DHCP-only device up on a direct cable where nothing hands out
# leases: a factory-reset NAS, a PXE box, a locked appliance. dnsmasq runs in
# DHCP-ONLY mode (DNS disabled with --port=0), hands out a small pool carved
# from the server CIDR, and both the address and the firewall hole are reverted
# on exit. The serve is time-boxed; the lease outlives it enough to keep the
# client usable while you log in.
#
# Safety, in the same spirit as the rest of this file:
#   - refuses a wireless iface (you cannot bridge a DHCP server onto wifi)
#   - refuses an iface that carries the default route (never serve toward an
#     uplink — that is a rogue DHCP server on someone's real LAN)
#   - DNS is off; this cannot become a resolver
#   - the UDP/67 accept is scoped to this one iface and removed on exit
cmd_dhcp_serve() {
  local iface=${1-} cidr=${2-10.54.0.1/24} secs=${3-1800}
  valid_iface "$iface"
  valid_cidr "$cidr"
  valid_secs "$secs" 7200

  [[ -d /sys/class/net/${iface}/wireless ]] &&
    die "${iface} is wireless; serve DHCP on a WIRED port only"
  if ip route show default dev "$iface" | grep -q .; then
    die "${iface} carries the default route; refusing to serve DHCP toward an uplink"
  fi
  if [[ $(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo 0) != 1 ]]; then
    echo "$PROG: warning: ${iface} has no carrier — no client can reach the server until it links" >&2
  fi

  local addr pfx mask net bcast server_int ps pe netmask lease=3600
  addr=${cidr%/*}; pfx=${cidr#*/}
  server_int=$(ip2int "$addr")
  mask=$(( (0xFFFFFFFF << (32 - pfx)) & 0xFFFFFFFF ))
  net=$(( server_int & mask ))
  bcast=$(( net | (~mask & 0xFFFFFFFF) ))
  netmask=$(int2ip "$mask")
  # Pool: start 10 into the subnet, up to 100 addresses, clamped to the usable
  # range and never overlapping the server's own address.
  ps=$(( net + 10 )); pe=$(( ps + 99 ))
  (( ps < net + 1 )) && ps=$(( net + 1 ))
  (( ps > bcast - 1 )) && ps=$(( net + 1 ))
  (( pe > bcast - 1 )) && pe=$(( bcast - 1 ))
  (( server_int >= ps && server_int <= pe )) && ps=$(( server_int + 1 ))
  (( pe >= ps )) || die "${cidr} is too small to carve a DHCP pool"

  DHCP_IFACE=$iface
  DHCP_CIDR=$cidr
  DHCP_LEASEFILE="/run/${PROG}-dhcp-${iface}.leases"
  DHCP_PIDFILE="/run/${PROG}-dhcp-${iface}.pid"
  trap dhcp_cleanup EXIT

  # The server needs an address in the pool's subnet to source replies from.
  # Only tear down what we actually added.
  if ! ip -4 -j addr show dev "$iface" |
       jq -e --arg c "$cidr" '[.[0].addr_info[]? | "\(.local)/\(.prefixlen)"] | index($c)' >/dev/null; then
    ip addr add "$cidr" dev "$iface"
    DHCP_ADDR_ADDED=1
  fi

  # Open UDP/67 inbound on THIS iface only. The default NixOS firewall drops the
  # broadcast DISCOVER (nixos-fw-log-refuse catches non-unicast), so without
  # this dnsmasq runs but never hears a client — a silent failure. Fail loud if
  # the chain we expect is not there rather than serve into a black hole.
  if iptables -n -L nixos-fw >/dev/null 2>&1; then
    iptables -I nixos-fw 1 -i "$iface" -p udp --dport 67 -j ACCEPT
    DHCP_FW_ADDED=1
  else
    die "no nixos-fw chain — refusing to serve DHCP behind an unrecognised firewall"
  fi

  echo "$PROG: serving DHCP on ${iface} for ${secs}s" >&2
  echo "$PROG:   server ${addr}  pool $(int2ip "$ps")-$(int2ip "$pe")  mask ${netmask}  lease ${lease}s" >&2

  local rc
  set +e
  timeout "$secs" dnsmasq \
    --keep-in-foreground \
    --conf-file=/dev/null \
    --port=0 \
    --interface="$iface" --bind-interfaces --except-interface=lo \
    --no-resolv --no-hosts --no-ping \
    --dhcp-authoritative \
    --dhcp-leasefile="$DHCP_LEASEFILE" \
    --pid-file="$DHCP_PIDFILE" \
    --dhcp-range="$(int2ip "$ps"),$(int2ip "$pe"),${netmask},${lease}" \
    --log-dhcp --log-facility=-
  rc=$?
  set -e
  case $rc in
    0|124|143) : ;;
    *) die "dnsmasq exited $rc" ;;
  esac
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
    plc)       cmd_plc "$@" ;;
    ifstats)      cmd_ifstats "$@" ;;
    vlan-offload) cmd_vlan_offload "$@" ;;
    addr-add)     cmd_addr_add "$@" ;;
    addr-clear)   cmd_addr_clear "$@" ;;
    dhcp-serve)   cmd_dhcp_serve "$@" ;;
    -h|--help|help) usage ;;
    *)            die "unknown subcommand: '${sub}'" ;;
  esac
}

main "$@"
