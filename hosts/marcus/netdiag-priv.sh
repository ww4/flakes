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
#   - captures are snaplength-limited to headers, so a service call cannot
#     hoover up a client's payload traffic
#   - temporary addresses carry a kernel lifetime and expire on their own
#
# Called by `netdiag`; run directly only for debugging. See netdiag.nix.
set -euo pipefail

PROG=netdiag-priv
LABEL_SUFFIX=nd

die() { echo "$PROG: $*" >&2; exit 2; }

usage() {
  cat <<'EOF'
netdiag-priv <subcommand> [args]      (invoked through sudo by `netdiag`)

  capture <iface> <profile> <secs>    control-plane capture, headers only
                                      profiles: arp dhcp discovery neighbour stp all
  arpscan <iface> [cidr]              ARP sweep — layer-2 ground truth + vendors
  dhcp-probe <iface>                  DHCP DISCOVER: who answers, with what scope
  lldp                                LLDP/CDP neighbours — which switch and port
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

# Fixed capture vocabulary. The caller picks a NAME; the filter text is ours.
filter_for() {
  case ${1-} in
    arp)       echo 'arp or rarp' ;;
    dhcp)      echo 'udp port 67 or udp port 68' ;;
    discovery) echo 'udp port 5353 or udp port 3702 or udp port 1900 or udp port 137 or udp port 138' ;;
    neighbour) echo 'ether proto 0x88cc or ether host 01:00:0c:cc:cc:cc' ;;
    stp)       echo 'stp' ;;
    all)       echo 'arp or rarp or udp port 67 or udp port 68 or udp port 5353 or udp port 3702 or udp port 1900 or udp port 137 or udp port 138 or ether proto 0x88cc or ether host 01:00:0c:cc:cc:cc or stp' ;;
    *)         die "unknown capture profile: '${1-}' (arp dhcp discovery neighbour stp all)" ;;
  esac
}

cmd_capture() {
  local iface=${1-} profile=${2-} secs=${3-30} filter rc
  valid_iface "$iface"
  filter=$(filter_for "$profile")
  valid_secs "$secs" 300
  # -s 512 keeps headers and control-plane options but truncates payload.
  # No -w/-W/-z: this writes to stdout only and cannot execute anything.
  set +e
  timeout "$secs" tcpdump -i "$iface" -nn -e -tttt -s 512 -l "$filter"
  rc=$?
  set -e
  # 124 = timeout fired (expected), 143 = SIGTERM during shutdown.
  case $rc in
    0|124|143) : ;;
    *) die "tcpdump exited $rc" ;;
  esac
}

cmd_arpscan() {
  local iface=${1-} target=${2-}
  valid_iface "$iface"
  if [[ -z $target ]]; then
    arp-scan --interface="$iface" --localnet --plain
  else
    valid_cidr "$target"
    arp-scan --interface="$iface" --plain "$target"
  fi
}

cmd_dhcp_probe() {
  local iface=${1-}
  valid_iface "$iface"
  # DISCOVER only — nmap's script does not accept the offer, so no lease is taken.
  nmap --script broadcast-dhcp-discover -e "$iface" 2>/dev/null |
    sed -n '/broadcast-dhcp-discover/,$p'
}

cmd_lldp() {
  lldpcli show neighbors details 2>/dev/null ||
    die "lldpcli failed — is services.lldpd enabled and running?"
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
    capture)    cmd_capture "$@" ;;
    arpscan)    cmd_arpscan "$@" ;;
    dhcp-probe) cmd_dhcp_probe "$@" ;;
    lldp)       cmd_lldp "$@" ;;
    addr-add)   cmd_addr_add "$@" ;;
    addr-clear) cmd_addr_clear "$@" ;;
    -h|--help|help) usage ;;
    *)          die "unknown subcommand: '${sub}'" ;;
  esac
}

main "$@"
