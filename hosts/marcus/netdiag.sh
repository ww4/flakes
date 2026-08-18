#!/usr/bin/env bash
# netdiag — field network diagnostics for service calls.
#
# Built 2026-08-18 out of the Craigmyle Tractor camera outage, where the slow
# parts were: no packet capture (a second subnet was found by luck, via mDNS),
# no LLDP (a whole afternoon of "which switch port is this on?"), and a pile of
# hand-rolled shell whose parsing bugs produced empty results that read as
# "nothing there". Every subcommand here is tested shell with stable output, so
# an empty result means empty, not "the parser broke".
#
# Privileged work goes through `sudo netdiag-priv` (a fixed, validated
# vocabulary — see netdiag-priv.sh). Everything else runs unprivileged.
set -euo pipefail

PROG=netdiag

die() { echo "$PROG: $*" >&2; exit 2; }
hdr() { printf '\n=== %s ===\n' "$*"; }

usage() {
  cat <<'EOF'
netdiag <subcommand> [args]

  survey [iface]        FIRST LOOK — link, gateway, DNS, upstream switch/port,
                        layer-2 census, DHCP servers, and every subnet in use
                        on this segment. Start here.
  census [cidr]         layer-2 census with vendor names (ARP = ground truth)
  listen [secs]         passive control-plane watch; finds foreign subnets,
                        rogue DHCP, VLAN leakage (default 30s)
  cameras               camera census: mDNS + ONVIF WS-Discovery + port signature
  host <ip>             deep single-host fingerprint
  hop <cidr> [secs]     add a temporary IP on a foreign subnet, scan it, report
                        (the address self-expires; `hop-clear` drops it early)
  hop-clear             remove every netdiag-added address
  whoami                which switch and port am I plugged into (LLDP/CDP)

Output is plain text, one record per line, safe to grep.
EOF
}

priv() {
  sudo -n netdiag-priv "$@" 2>/dev/null || {
    echo "$PROG: 'sudo netdiag-priv $1' was refused." >&2
    echo "$PROG: the sudo allowlist entry is in modules/agent/sudo.nix." >&2
    return 1
  }
}

iface_default() {
  ip -j route show default 2>/dev/null | jq -r '.[0].dev // empty'
}

self_cidr() {
  ip -4 -j addr show dev "$1" 2>/dev/null |
    jq -r '.[0].addr_info[]? | select(.scope=="global") | "\(.local)/\(.prefixlen)"' | head -1
}

# Group every IPv4 seen in a capture into /24s. A second populated /24 on one
# segment is the signature of VLAN leakage or a leftover DHCP scope — this is
# the check that would have found 192.168.128.0/24 in thirty seconds.
subnet_histogram() {
  grep -oE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' |
    grep -vE '^(0\.|255\.|127\.|22[4-9]\.|23[0-9]\.)' |
    awk -F. '{print $1"."$2"."$3".0/24"}' |
    sort | uniq -c | sort -rn
}

cmd_whoami() {
  hdr "upstream neighbour (LLDP/CDP)"
  local out
  out=$(priv lldp || true)
  if [[ -z ${out//[[:space:]]/} ]]; then
    echo "no LLDP/CDP neighbour seen."
    echo "unmanaged switch, LLDP disabled upstream, or lldpd needs a moment to hear a frame."
  else
    echo "$out" | grep -E 'Interface:|SysName:|PortDescr:|PortID:|MgmtIP:|VLAN' || echo "$out"
  fi
}

cmd_census() {
  local iface target
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route; pass an interface explicitly"
  target=${1-}
  hdr "layer-2 census (ARP — cannot be firewalled away)"
  if [[ -n $target ]]; then
    priv arpscan "$iface" "$target"
  else
    priv arpscan "$iface"
  fi
}

cmd_listen() {
  local secs=${1-30} iface cap
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route; cannot pick an interface"
  hdr "passive control-plane watch (${secs}s on ${iface})"
  echo "listening for ARP, DHCP, mDNS/WS-Discovery/SSDP/NetBIOS, LLDP/CDP, STP..."
  cap=$(priv capture "$iface" all "$secs" || true)
  if [[ -z ${cap//[[:space:]]/} ]]; then
    echo "captured nothing — quiet segment, or the capture was refused."
    return 0
  fi
  hdr "subnets observed on this segment"
  echo "$cap" | subnet_histogram
  echo "(more than one populated /24 here means VLAN leakage or a leftover DHCP scope)"
  hdr "DHCP servers heard"
  echo "$cap" | grep -iE 'bootp|dhcp' | grep -oiE 'from [0-9.]+|Reply|Offer|ACK' | sort | uniq -c | sort -rn || echo "none"
  hdr "LLDP/CDP frames"
  echo "$cap" | grep -icE 'lldp|cdp' | sed 's/^/frames seen: /'
  hdr "talkers by MAC (top 15)"
  echo "$cap" | grep -oE '^[0-9-]+ [0-9:.]+ [0-9a-f:]{17}' | awk '{print $3}' | sort | uniq -c | sort -rn | head -15
}

cmd_survey() {
  local iface cidr gw
  iface=${1-$(iface_default)}
  [[ -n ${iface:-} ]] || die "no default route; pass an interface explicitly"
  cidr=$(self_cidr "$iface")
  gw=$(ip -j route show default 2>/dev/null | jq -r '.[0].gateway // "none"')

  hdr "this host"
  echo "interface : ${iface}"
  echo "address   : ${cidr:-none}"
  echo "gateway   : ${gw}"
  echo "dns       : $(grep -E '^nameserver' /etc/resolv.conf 2>/dev/null | awk '{print $2}' | tr '\n' ' ')"

  cmd_whoami

  hdr "layer-2 census"
  priv arpscan "$iface" || echo "(ARP sweep unavailable)"

  hdr "DHCP servers answering"
  priv dhcp-probe "$iface" || echo "(DHCP probe unavailable)"

  cmd_listen 20

  hdr "next steps"
  echo "netdiag cameras          - enumerate cameras (incl. ones on the wrong subnet)"
  echo "netdiag host <ip>        - fingerprint a single device"
  echo "netdiag hop <cidr>       - reach a foreign subnet found above"
}

cmd_cameras() {
  local iface cidr base realm ip
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"
  cidr=$(self_cidr "$iface")
  base=$(echo "$cidr" | cut -d. -f1-3)

  hdr "ONVIF WS-Discovery (vendor-neutral — Hikvision/Dahua/Axis/Amcrest)"
  netdiag-wsdiscover || echo "(no ONVIF responders)"

  hdr "mDNS advertisements (Amcrest publish their full IP config here)"
  timeout 45 avahi-browse -art 2>/dev/null | grep -B4 'host=' |
    grep -oE '"(ip|mac|host)=[^"]*"' | tr -d '"' | paste - - - | sort -u ||
    echo "(none)"

  hdr "port-signature sweep (80 + 554 RTSP + 37777 Dahua) on ${base}.0/24"
  nmap -Pn -T4 --open -p 80,554,37777 "${base}.0/24" -oG - 2>/dev/null |
    awk '/554\/open/ && /37777\/open/ {print $2}' |
    while read -r ip; do
      realm=$(curl -skm5 -i "http://${ip}/cgi-bin/magicBox.cgi?action=getSystemInfo" 2>/dev/null |
                grep -oE 'Login to [A-Za-z0-9]+' | head -1)
      printf '  %-16s %s\n' "$ip" "${realm:-<older firmware: no serial in auth realm>}"
    done

  echo
  echo "note: mDNS coverage is partial — Dahua, Hikvision and the AMC103/AMC104"
  echo "Amcrest families do not advertise. Treat an mDNS count as a floor."
}

cmd_host() {
  local ip=${1-} mac
  [[ -n $ip ]] || die "usage: netdiag host <ip>"
  hdr "host ${ip}"
  curl -sm2 -o /dev/null "http://${ip}/" 2>/dev/null || true
  # NOTE: with `dev` given, `ip neigh` OMITS the dev field, so the MAC is $3 not
  # $5. Parsing it as $5 silently yields the literal word "lladdr" — that bug
  # cost real time on 2026-08-18. Match the token instead of counting columns.
  mac=$(ip neigh show "$ip" 2>/dev/null | grep -oE 'lladdr [0-9a-f:]{17}' | awk '{print $2}')
  echo "mac       : ${mac:-<no ARP reply — not on this segment>}"
  echo "reverse   : $(getent hosts "$ip" | awk '{print $2}' || echo none)"
  hdr "open ports"
  nmap -Pn -T4 --open -p 21-25,53,80,81,443,445,554,3389,5000,8000,8080,8443,9000,37777 "$ip" 2>/dev/null |
    grep -E '^[0-9]+/tcp' || echo "(none open in the common set)"
  hdr "http identity"
  curl -skm5 -I "http://${ip}/" 2>/dev/null | grep -iE '^(HTTP|server|www-authenticate)' || echo "(no http)"
  curl -skm5 "http://${ip}/" 2>/dev/null | grep -oiE '<title>[^<]*' | head -1 || true
  hdr "netbios / smb"
  nmap -Pn -sU -p137 --script nbstat "$ip" 2>/dev/null | grep -iE 'NetBIOS name' || echo "(none)"
}

cmd_hop() {
  local cidr=${1-} secs=${2-600} iface net
  [[ -n $cidr ]] || die "usage: netdiag hop <cidr> [seconds]"
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"
  net=$(echo "$cidr" | cut -d. -f1-3)
  hdr "adding a temporary address on ${cidr}"
  priv addr-add "$iface" "$cidr" "$secs"
  hdr "scanning ${net}.0/24"
  nmap -sn -T4 "${net}.0/24" 2>/dev/null | grep 'scan report' | sed 's/Nmap scan report for /  /'
  echo
  echo "address self-expires in ${secs}s; 'netdiag hop-clear' drops it now."
}

cmd_hop_clear() {
  local iface
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"
  priv addr-clear "$iface"
}

main() {
  local sub=${1-}
  [[ -n $sub ]] || { usage; exit 1; }
  shift
  case $sub in
    survey)    cmd_survey "$@" ;;
    census)    cmd_census "$@" ;;
    listen)    cmd_listen "$@" ;;
    cameras)   cmd_cameras "$@" ;;
    host)      cmd_host "$@" ;;
    hop)       cmd_hop "$@" ;;
    hop-clear) cmd_hop_clear "$@" ;;
    whoami)    cmd_whoami "$@" ;;
    -h|--help|help) usage ;;
    *)         die "unknown subcommand: '${sub}' (try: netdiag --help)" ;;
  esac
}

main "$@"
