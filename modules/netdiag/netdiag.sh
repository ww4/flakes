#!/usr/bin/env bash
# netdiag — field network diagnostics for service calls.
#
# Built 2026-08-18 out of the Craigmyle Tractor camera outage, then broadened
# by a research pass into the problems that actually recur on service calls.
# Every subcommand is tested shell with stable output, so an empty result means
# empty, not "the parser broke".
#
# Privileged work goes through `sudo netdiag-priv` (a fixed, validated
# vocabulary — see netdiag-priv.sh). Everything else runs unprivileged.
#
# ⚠️ WIFI WARNING, because it is the most expensive mistake available here:
# most layer-2 diagnostics DO NOT WORK over wifi, and they fail silently rather
# than erroring. APs rate-limit broadcast, proxy ARP and buffer multicast, so a
# wire storming at 40,000 pps presents to a wifi client as a couple of hundred.
# STP and VLAN detection are not merely degraded but structurally impossible:
# BPDUs go to a reserved group address a conformant bridge must never forward,
# and the AP strips the 802.1Q tag before the frame reaches you. Commands that
# need a wire say so at runtime. Carry a USB-Ethernet adapter.
set -euo pipefail

PROG=netdiag

die() { echo "$PROG: $*" >&2; exit 2; }
hdr() { printf '\n=== %s ===\n' "$*"; }

usage() {
  cat <<'EOF'
netdiag <subcommand> [args]

  DISCOVER
    survey [iface]        FIRST LOOK — start here. Link, gateway, DNS, upstream
                          switch/port, layer-2 census, DHCP servers, and every
                          subnet in use on this segment.
    census [cidr]         who is really here — ARP + vendor + device class
    identify <ip>         deep fingerprint of one device
    oui <mac>             vendor lookup (offline)
    cameras               ONVIF WS-Discovery + mDNS + port signature
    snapshot <ip> [file]  pull ONE still frame — this is how you find out WHICH
                          physical camera an address is
    snapshot-all [dir]    a frame from every camera on the segment
    unifi                 Ubiquiti devices, incl. UNADOPTED access points

  DIAGNOSE
    storm [secs]          broadcast storm rate + definitive L2 loop test  [WIRE]
    rogue [secs]          rogue DHCP and rogue IPv6 RA, active + passive
    vlans [secs]          802.1Q tags visible — am I on a trunk?          [WIRE]
    listen [secs]         passive control-plane watch; foreign subnets
    whoami                which switch and port am I plugged into         [WIRE]
    switchport <mac> <switch-ip> [community]
                          WHICH SWITCH PORT is this MAC on (SNMP bridge table)

  AUDIT
    exposure              UPnP/NAT-PMP firewall holes, WAN IP, double-NAT/CGNAT
    audit <ip>            service exposure on one host

  REACH
    hop <cidr> [secs]     temporarily join a foreign subnet and scan it
    hop-clear             drop the temporary address early

[WIRE] = needs a wired port; the answer over wifi is absent or misleading.
EOF
}

priv() {
  sudo -n netdiag-priv "$@" 2>/dev/null || {
    echo "$PROG: 'sudo netdiag-priv $1' was refused." >&2
    echo "$PROG: the sudo allowlist entry is in modules/agent/sudo.nix." >&2
    return 1
  }
}

iface_default() { ip -j route show default 2>/dev/null | jq -r '.[0].dev // empty'; }

self_cidr() {
  ip -4 -j addr show dev "$1" 2>/dev/null |
    jq -r '.[0].addr_info[]? | select(.scope=="global") | "\(.local)/\(.prefixlen)"' | head -1
}

is_wireless() { [[ -d "/sys/class/net/$1/wireless" ]]; }

warn_if_wireless() {
  if is_wireless "$1"; then
    echo "!! ${1} is WIRELESS. This check needs a wired port — the result below"
    echo "!! is either absent or actively misleading. Get on a cable."
    echo
  fi
}

# ---------------------------------------------------------------- OUI lookup
# $NETDIAG_OUI is a build-time table: HEXPREFIX <tab> nibbles <tab> vendor.
# Longest-prefix match /36 -> /28 -> /24, because a /24-only lookup returns the
# shared IEEE registry owner rather than the real vendor for small allocations.
oui_lookup() {
  local mac=${1-} hex n
  hex=$(printf '%s' "$mac" | tr -d ':.-' | tr '[:lower:]' '[:upper:]')
  [[ ${#hex} -ge 6 ]] || { echo "?"; return; }
  # U/L bit: a locally-administered MAC is a randomising phone or laptop, never
  # infrastructure. Vendor lookup on one is meaningless.
  if (( 0x${hex:0:2} & 0x02 )); then echo "(randomised MAC)"; return; fi
  for n in 9 7 6; do
    awk -F'\t' -v p="${hex:0:$n}" -v n="$n" '$2==n && $1==p {print $3; exit}' "$NETDIAG_OUI" && :
  done | head -1 | grep . || echo "unknown"
}

# --------------------------------------------------- device classification
# Signatures that actually discriminate, from strongest to weakest. 554 is
# near-definitive for a camera; 9100/515 for a printer; 8291 for MikroTik.
classify() {
  local vendor=$1 ports=$2 v
  v=$(printf '%s' "$vendor" | tr '[:upper:]' '[:lower:]')
  case ",$ports," in
    *,554,*)                                        echo "IP camera"; return ;;
    *,9100,*|*,515,*)                               echo "printer/MFP"; return ;;
    *,8291,*)                                       echo "MikroTik router"; return ;;
  esac
  case ",$ports," in
    *,445,*)
      case ",$ports," in
        *,3389,*)                                   echo "Windows PC/server"; return ;;
        *,5000,*|*,5001,*)                          echo "NAS (Synology-like)"; return ;;
        *)                                          echo "Windows/SMB host"; return ;;
      esac ;;
  esac
  case "$v" in
    *ubiquiti*)                                     echo "Ubiquiti (AP/switch/radio)"; return ;;
    *routerboard*|*mikrotik*)                       echo "MikroTik"; return ;;
    *amcrest*|*dahua*|*hikvision*|*axis*|*hanwha*)  echo "IP camera (by vendor)"; return ;;
    *cambium*|*mimosa*|*tarana*|*telrad*)           echo "WISP radio/CPE"; return ;;
    *ricoh*|*brother*|*kyocera*|*lexmark*|*canon*)  echo "printer/MFP (by vendor)"; return ;;
    *yealink*|*polycom*|*poly*|*grandstream*|*snom*) echo "VoIP phone"; return ;;
    *synology*|*qnap*|*buffalo*)                    echo "NAS"; return ;;
    *meraki*|*cisco*|*juniper*|*netgear*|*aruba*)   echo "network infrastructure"; return ;;
  esac
  case ",$ports," in
    *,5060,*)                                       echo "VoIP phone"; return ;;
    *,161,*)                                        echo "network infrastructure"; return ;;
    *,443,*)                                        echo "appliance (https only)"; return ;;
  esac
  echo "unknown"
}

cmd_oui() {
  local mac=${1-}
  [[ -n $mac ]] || die "usage: netdiag oui <mac>"
  oui_lookup "$mac"
}

# ------------------------------------------------------------------ commands

cmd_whoami() {
  local iface out
  iface=$(iface_default) || true
  hdr "upstream neighbour (LLDP/CDP)"
  [[ -n ${iface:-} ]] && warn_if_wireless "$iface"
  out=$(priv lldp || true)
  if [[ -z ${out//[[:space:]]/} ]]; then
    echo "no LLDP/CDP neighbour seen."
    echo "causes: unmanaged switch; LLDP off upstream; you are on wifi; or the"
    echo "60-second advertise timer has not elapsed since lldpd started."
  else
    echo "$out" | grep -E 'Interface:|SysName:|SysDescr:|PortDescr:|PortID:|MgmtIP:|VLAN' || echo "$out"
  fi
}

cmd_census() {
  local iface target line ip mac vendor ports cls
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route; cannot pick an interface"
  target=${1-}
  hdr "layer-2 census (ARP — cannot be firewalled away)"
  printf '%-16s %-18s %-28s %s\n' IP MAC VENDOR "DEVICE (guess)"
  while read -r line; do
    ip=$(echo "$line" | awk '{print $1}')
    mac=$(echo "$line" | awk '{print $2}')
    [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    vendor=$(oui_lookup "$mac")
    ports=$(nmap -Pn -T4 --open -p 22,80,161,443,445,515,554,3389,5000,5001,5060,8291,9100,37777 \
              "$ip" -oG - 2>/dev/null | grep -oE '[0-9]+/open' | cut -d/ -f1 | paste -sd, || true)
    cls=$(classify "$vendor" "$ports")
    printf '%-16s %-18s %-28s %s\n' "$ip" "$mac" "${vendor:0:27}" "$cls"
  done < <(if [[ -n $target ]]; then priv arpscan "$iface" "$target"; else priv arpscan "$iface"; fi)
}

cmd_storm() {
  local secs=${1-15} iface cap total bpps
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"
  hdr "broadcast/multicast storm check (${secs}s on ${iface})"
  warn_if_wireless "$iface"

  echo "-- NIC hardware counters --"
  priv ifstats "$iface" || true

  cap=$(priv capture "$iface" storm "$secs" || true)
  total=$(printf '%s' "$cap" | grep -c . || true)
  bpps=$(( total / secs ))
  echo
  echo "-- rate --"
  echo "broadcast+multicast frames: ${total} in ${secs}s  =  ~${bpps} pps"
  echo "calibration: a quiet 200-host /24 sits at roughly 20-150 pps."
  echo "sustained >1000 pps warrants investigation; a real loop climbs toward"
  echo "line rate and the curve is EXPONENTIAL, not flat."
  echo
  echo "-- top sources --"
  printf '%s\n' "$cap" | grep -oE '[0-9a-f]{2}(:[0-9a-f]{2}){5} >' | tr -d ' >' |
    sort | uniq -c | sort -rn | head -10 |
    while read -r n m; do printf '  %6s  %s  %s\n' "$n" "$m" "$(oui_lookup "$m")"; done

  hdr "definitive L2 loop test"
  echo "any frame arriving INBOUND with our own MAC as source is a loop —"
  echo "there is no benign explanation for one."
  local loop n
  loop=$(priv capture "$iface" loop 10 || true)
  n=$(printf '%s' "$loop" | grep -c . || true)
  if (( n > 0 )); then
    echo "*** LOOP DETECTED: ${n} of our own frames came back in 10s ***"
    printf '%s\n' "$loop" | head -3
  else
    echo "no loop on this VLAN (0 frames returned)."
  fi
}

cmd_rogue() {
  local secs=${1-45} iface cap servers routers
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"

  hdr "ACTIVE: DHCP servers answering a DISCOVER"
  priv dhcp-probe "$iface" || echo "(probe unavailable)"
  echo "note: one DISCOVER, one transaction. A rogue that is intermittent, or"
  echo "filters by client-id, is missed here — that is what the passive pass is for."

  hdr "ACTIVE: IPv6 routers advertising"
  priv ra-probe "$iface" || echo "(probe unavailable)"
  echo "more than one distinct router above = rogue RA. This matters more than"
  echo "it looks: IPv6 is preferred over IPv4, so a rogue RA blackholes traffic"
  echo "while every IPv4 test you run still passes."

  hdr "PASSIVE: watching ${secs}s for intermittent offenders"
  cap=$(priv capture "$iface" dhcp "$secs" || true)
  servers=$(printf '%s' "$cap" | grep -oiE 'bootp|dhcp' | wc -l || true)
  echo "DHCP/BOOTP frames seen: ${servers}"
  printf '%s\n' "$cap" | grep -oE '^[0-9-]+ [0-9:.]+ [0-9a-f:]{17}' | awk '{print $3}' |
    sort -u | while read -r m; do
      [[ -n $m ]] && printf '  talker %s  %s\n' "$m" "$(oui_lookup "$m")"
    done
  routers=$(priv capture "$iface" all 5 2>/dev/null | grep -ci 'router.advertisement' || true)
  echo "router advertisements in a 5s sample: ${routers}"
  echo
  echo "for a real answer leave this running longer — both rogue DHCP and rogue"
  echo "RA are INTERMITTENT by nature and are routinely missed by one-shot probes."
}

cmd_vlans() {
  local secs=${1-60} iface tags
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"
  hdr "802.1Q tag enumeration (${secs}s on ${iface})"
  warn_if_wireless "$iface"
  # The NIC strips tags in hardware unless this is off — with it on you capture
  # a busy trunk and conclude "no VLANs here".
  priv vlan-offload "$iface" off || true
  tags=$(priv capture "$iface" vlan "$secs" 2>/dev/null | grep -oE 'vlan [0-9]+' | awk '{print $2}' | sort -un || true)
  priv vlan-offload "$iface" on || true
  if [[ -z ${tags//[[:space:]]/} ]]; then
    echo "no tagged frames — this is an ACCESS port (or you are on wifi)."
  else
    echo "tagged VLANs seen:"; printf '  %s\n' "$tags"
    echo "any output here means you are on a TRUNK."
  fi
  echo
  echo "note: the NATIVE VLAN is untagged by design and can never appear above."
  echo "get it from LLDP/CDP (netdiag whoami), not from tags."
}

cmd_switchport() {
  local mac=${1-} sw=${2-} comm=${3-public} hex dec port ifidx
  [[ -n $mac && -n $sw ]] || die "usage: netdiag switchport <mac> <switch-ip> [community]"
  hdr "locating ${mac} on switch ${sw}"
  hex=$(printf '%s' "$mac" | tr -d ':.-' | tr '[:lower:]' '[:upper:]')
  [[ ${#hex} -eq 12 ]] || die "need a full 12-hex-digit MAC"
  # The bridge forwarding table indexes entries BY the MAC in decimal octets.
  dec=$(echo "$hex" | fold -w2 | while read -r b; do printf '%d.' "0x$b"; done | sed 's/\.$//')
  echo "looking up bridge FDB index .${dec}"

  # dot1qTpFdbPort (VLAN-aware) first, then dot1dTpFdbPort (classic).
  port=$(snmpwalk -v2c -c "$comm" -On -t 3 -r 1 "$sw" 1.3.6.1.2.1.17.7.1.2.2 2>/dev/null |
           grep -F ".${dec} " | head -1 | awk -F'INTEGER: ' '{print $2}' | tr -d ' ')
  if [[ -z ${port:-} ]]; then
    port=$(snmpwalk -v2c -c "$comm" -On -t 3 -r 1 "$sw" 1.3.6.1.2.1.17.4.3.1.2 2>/dev/null |
             grep -F ".${dec} " | head -1 | awk -F'INTEGER: ' '{print $2}' | tr -d ' ')
  fi
  if [[ -z ${port:-} ]]; then
    echo "not found in the bridge table."
    echo "causes: wrong community; SNMP closed; the MAC is not on THIS switch;"
    echo "or the entry aged out (make the device talk, then retry)."
    return 1
  fi
  echo "bridge port: ${port}"
  ifidx=$(snmpget -v2c -c "$comm" -Ovq -t 3 "$sw" "1.3.6.1.2.1.17.1.4.1.2.${port}" 2>/dev/null | tr -d ' ')
  [[ -n ${ifidx:-} ]] || { echo "(could not map bridge port to ifIndex)"; return 0; }
  echo "ifIndex    : ${ifidx}"
  echo "port name  : $(snmpget -v2c -c "$comm" -Ovq -t 3 "$sw" "1.3.6.1.2.1.31.1.1.1.1.${ifidx}" 2>/dev/null | tr -d '"')"
  echo "description: $(snmpget -v2c -c "$comm" -Ovq -t 3 "$sw" "1.3.6.1.2.1.31.1.1.1.18.${ifidx}" 2>/dev/null | tr -d '"')"
  echo "oper status: $(snmpget -v2c -c "$comm" -Ovq -t 3 "$sw" "1.3.6.1.2.1.2.2.1.8.${ifidx}" 2>/dev/null)"
}

cmd_unifi() {
  local iface cidr base
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"
  cidr=$(self_cidr "$iface"); base=$(echo "$cidr" | cut -d. -f1-3)
  hdr "Ubiquiti discovery (UDP 10001) on ${base}.0/24"
  priv ubnt "${base}.0/24" || echo "(discovery unavailable)"
  cat <<'EOF'

READING ADOPTION STATE — the important caveat:
  config_status "default/unmanaged" = UNADOPTED
  config_status "managed/adopted"   = adopted
  config_status ABSENT              = TREAT AS POSSIBLY UNADOPTED.

nmap's ubiquiti-discovery parser reads the adoption TLV with a NATIVE-endian
unpack while every other multi-byte field uses big-endian. On x86 a device
sending the 4-byte form of "unadopted" parses as 16777216 and the field is
silently dropped. The bug is biased exactly against what you are looking for:
adopted devices almost always report, unadopted ones can vanish.

Corroborate with: hostname "UBNT" (default), ESSID "ubnt" (default), and the
factory-default address 192.168.1.20 when the segment has no DHCP.
EOF
}

cmd_exposure() {
  local gw wan_gw wan_real
  hdr "firewall holes and NAT position"
  gw=$(ip -j route show default 2>/dev/null | jq -r '.[0].gateway // "none"')
  echo "default gateway: ${gw}"

  hdr "UPnP port mappings (holes punched THROUGH the firewall)"
  upnpc -l 2>/dev/null | sed -n '/i protocol exPort/,$p' || echo "(no UPnP IGD responded)"
  echo "read the description column — it is client-supplied and usually names"
  echo "the culprit outright (a torrent client, a console, an NVR)."

  hdr "NAT-PMP / PCP"
  natpmpc 2>/dev/null | grep -iE 'public|external|readresp' || echo "(no NAT-PMP response)"
  echo "NAT-PMP cannot enumerate existing mappings — this only answers whether"
  echo "the door is unlocked, not who walked through it."

  hdr "WAN address, three ways (disagreement IS the finding)"
  wan_gw=$(external-ip 2>/dev/null | tail -1 || echo "n/a")
  wan_real=$(curl -sm 8 https://ifconfig.me 2>/dev/null || echo "n/a")
  echo "gateway believes : ${wan_gw}"
  echo "internet sees    : ${wan_real}"
  case "$wan_gw" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
      echo ">> DOUBLE NAT: the gateway's WAN address is RFC1918. Fixable on site —"
      echo ">> bridge the upstream CPE, or DMZ this router behind it." ;;
    100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*)
      echo ">> CGNAT (100.64/10). This is CARRIER-side and no customer-router"
      echo ">> change fixes it — the remedy is ordering a public/static IP." ;;
  esac
  echo
  echo "note: getting CGNAT and double-NAT backwards wastes a truck roll, which"
  echo "is why they are tested separately above."
}

cmd_audit() {
  local ip=${1-} ports
  [[ -n $ip ]] || die "usage: netdiag audit <ip>"
  hdr "service exposure on ${ip}"
  echo "NOTE: if you can SSH to this host, 'sudo ss -tulpn' beats every scan —"
  echo "it shows the BIND ADDRESS and the owning process. 127.0.0.1 is fine;"
  echo "0.0.0.0 and [::] are exposed. Without sudo the process column is blank"
  echo "with no error, which reads as a complete listing but is not."

  hdr "open TCP (full range)"
  nmap -Pn -T4 --open -p- --min-rate 2000 "$ip" 2>/dev/null | grep -E '^[0-9]+/tcp' || echo "(none)"
  ports=$(nmap -Pn -T4 --open -p- --min-rate 2000 "$ip" -oG - 2>/dev/null |
            grep -oE '[0-9]+/open' | cut -d/ -f1 | paste -sd, || true)

  if [[ -n ${ports:-} ]]; then
    hdr "service versions"
    nmap -Pn -sV -sC --version-intensity 5 -p "$ports" "$ip" 2>/dev/null |
      grep -E '^[0-9]+/tcp|Service Info' || true
  fi

  hdr "curated UDP (full-range UDP is impossible — see netdiag-priv)"
  priv udp-audit "$ip" || echo "(udp scan unavailable)"

  hdr "triage"
  for p in ${ports//,/ }; do
    case $p in
      23)   echo "  23    telnet          CRITICAL  cleartext credentials — disable, use SSH" ;;
      21)   echo "  21    FTP             CRITICAL  cleartext; check anonymous access" ;;
      2375) echo "  2375  docker API      CRITICAL  unauthenticated = root on the host" ;;
      3389) echo "  3389  RDP             CRITICAL if WAN-reachable — top ransomware vector" ;;
      445)  echo "  445   SMB             HIGH      check for SMBv1; never expose off-LAN" ;;
      3306|5432|27017|6379)
            echo "  ${p}  database        CRITICAL if not bound to loopback (Redis/Mongo often have NO auth)" ;;
      5900) echo "  5900  VNC             HIGH      frequently no password at all" ;;
      9100) echo "  9100  printer raw     HIGH      unauthenticated print; check the web admin too" ;;
      69)   echo "  69    TFTP            HIGH      no auth; often still serving device configs" ;;
      8291) echo "  8291  MikroTik Winbox HIGH if reachable outside the mgmt VLAN" ;;
      554)  echo "  554   RTSP            HIGH      default creds are common; never expose to WAN" ;;
      22)   echo "  22    SSH             OK        verify key-only + no root login" ;;
    esac
  done
  echo
  echo "bind address changes severity more than the port does: Postgres on"
  echo "127.0.0.1 is fine and on 0.0.0.0 is critical, and they are the same port."
}

cmd_listen() {
  local secs=${1-30} iface cap
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"
  hdr "passive control-plane watch (${secs}s on ${iface})"
  cap=$(priv capture "$iface" all "$secs" || true)
  if [[ -z ${cap//[[:space:]]/} ]]; then echo "captured nothing."; return 0; fi
  hdr "subnets observed on this segment"
  printf '%s\n' "$cap" | grep -oE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' |
    grep -vE '^(0\.|255\.|127\.|22[4-9]\.|23[0-9]\.)' |
    awk -F. '{print $1"."$2"."$3".0/24"}' | sort | uniq -c | sort -rn
  echo "(more than one populated /24 here = VLAN leakage or a leftover scope)"
  hdr "talkers by MAC (top 15)"
  printf '%s\n' "$cap" | grep -oE '^[0-9-]+ [0-9:.]+ [0-9a-f:]{17}' | awk '{print $3}' |
    sort | uniq -c | sort -rn | head -15 |
    while read -r n m; do printf '  %6s  %s  %s\n' "$n" "$m" "$(oui_lookup "$m")"; done
}

cmd_survey() {
  local iface cidr gw
  iface=${1-$(iface_default)}
  [[ -n ${iface:-} ]] || die "no default route; pass an interface explicitly"
  cidr=$(self_cidr "$iface")
  gw=$(ip -j route show default 2>/dev/null | jq -r '.[0].gateway // "none"')

  hdr "this host"
  echo "interface : ${iface}$(is_wireless "$iface" && echo '  [WIRELESS - L2 checks unreliable]')"
  echo "address   : ${cidr:-none}"
  echo "gateway   : ${gw}"
  echo "dns       : $(grep -E '^nameserver' /etc/resolv.conf 2>/dev/null | awk '{print $2}' | tr '\n' ' ')"

  cmd_whoami
  cmd_census
  hdr "DHCP servers answering"
  priv dhcp-probe "$iface" || echo "(unavailable)"
  cmd_listen 20

  hdr "next steps"
  echo "netdiag storm      - is the segment being flooded / is there a loop"
  echo "netdiag rogue      - rogue DHCP or IPv6 RA"
  echo "netdiag exposure   - what is punched through the firewall"
  echo "netdiag cameras    - camera census"
  echo "netdiag unifi      - Ubiquiti, incl. unadopted APs"
}

cmd_cameras() {
  local iface cidr base realm ip
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"
  cidr=$(self_cidr "$iface"); base=$(echo "$cidr" | cut -d. -f1-3)

  hdr "ONVIF WS-Discovery (vendor-neutral)"
  netdiag-wsdiscover || echo "(no ONVIF responders)"

  hdr "mDNS advertisements (Amcrest publish their full IP config here)"
  timeout 45 avahi-browse -art 2>/dev/null | grep -B4 'host=' |
    grep -oE '"(ip|mac|host)=[^"]*"' | tr -d '"' | paste - - - | sort -u || echo "(none)"

  hdr "port-signature sweep (80 + 554 RTSP + 37777 Dahua) on ${base}.0/24"
  # 37777/37810/37020 are ABSENT from nmap-services, so they must be listed
  # explicitly — nmap will not scan or name them otherwise.
  nmap -Pn -T4 --open -p 80,554,37777 "${base}.0/24" -oG - 2>/dev/null |
    awk '/554\/open/ {print $2}' |
    while read -r ip; do
      realm=$(curl -skm5 -i "http://${ip}/cgi-bin/magicBox.cgi?action=getSystemInfo" 2>/dev/null |
                grep -oE 'Login to [A-Za-z0-9]+' | head -1)
      printf '  %-16s %s\n' "$ip" "${realm:-<no serial in auth realm>}"
    done

  echo
  echo "note: mDNS coverage is partial — Dahua, Hikvision and the AMC103/AMC104"
  echo "Amcrest families do not advertise. Treat an mDNS count as a floor."
}

# ------------------------------------------------------------------ snapshot
# The point of this: a still frame identifies which PHYSICAL camera an address
# belongs to. At Craigmyle, 12 of 24 cameras had no known location and the only
# way to map them was to look at what they see.
#
# Credentials come from a FILE, never argv — an RTSP URL on the command line is
# visible in `ps` and shell history, and ffprobe echoes it back in its JSON
# `format.filename`. See netdiag-creds.env.example.
CREDS_FILE="${NETDIAG_CREDS:-$HOME/.config/netdiag/camera-creds.env}"

urlenc() { python3 -c 'import sys,urllib.parse as u;print(u.quote(sys.argv[1],safe=""))' "$1"; }

load_creds() {
  CAM_USER=""; CAM_PASS=""
  if [[ -r $CREDS_FILE ]]; then
    # shellcheck disable=SC1090
    . "$CREDS_FILE"
    CAM_USER=${CAM_USER:-${NETDIAG_CAM_USER:-}}
    CAM_PASS=${CAM_PASS:-${NETDIAG_CAM_PASS:-}}
  fi
}

# Vendor stream paths, most-likely first. A 404 means wrong vendor — try the next.
rtsp_paths() {
  cat <<'EOF'
/cam/realmonitor?channel=1&subtype=0
/Streaming/Channels/101
/axis-media/media.amp
/live
/onvif1
/11
EOF
}

snap_one() {
  local ip=$1 out=$2 u p path url rc
  load_creds
  [[ -n ${CAM_USER:-} ]] || { echo "  ${ip}: no credentials (see ${CREDS_FILE})"; return 1; }
  # URL-encode both halves. '#' is the real hazard, not '@': ffmpeg treats '#'
  # as a fragment delimiter FIRST, which moves the last-'@' userinfo boundary
  # and corrupts the hostname. A bare '@' alone is actually harmless.
  u=$(urlenc "$CAM_USER"); p=$(urlenc "$CAM_PASS")

  # 1) HTTP snapshot CGI — one request, no RTSP negotiation. Try it first.
  for path in /cgi-bin/snapshot.cgi /ISAPI/Streaming/channels/101/picture /axis-cgi/jpg/image.cgi; do
    if curl -sf --digest -u "${CAM_USER}:${CAM_PASS}" -m 8 -o "$out" "http://${ip}${path}" 2>/dev/null &&
       [[ -s $out ]]; then
      echo "  ${ip}: snapshot via http ${path}"; return 0
    fi
  done

  # 2) RTSP single frame. Plain -frames:v 1 is both correct and FASTEST — the
  # h264 decoder already discards until the first recovery point, so the old
  # "-ss 2 to avoid a green frame" idiom is obsolete and nearly doubles the
  # time. -timeout is the ONLY timeout that works for RTSP (-rw_timeout is
  # ignored by the demuxer; -listen_timeout flips ffmpeg into SERVER mode and
  # tries to bind). -an skips audio SETUP, one less round trip.
  while read -r path; do
    url="rtsp://${u}:${p}@${ip}:554${path}"
    set +e
    # -nostdin matters: without it ffmpeg consumes the enclosing `while read`
    # loop's stdin and snapshot-all silently stops after the first camera.
    ffmpeg -nostdin -hide_banner -loglevel error -rtsp_transport tcp -timeout 8000000 \
           -an -allowed_media_types video -i "$url" -frames:v 1 -update 1 -y "$out" 2>/dev/null
    rc=$?
    set -e
    if [[ $rc -eq 0 && -s $out ]]; then echo "  ${ip}: snapshot via rtsp ${path}"; return 0; fi
    # 8 = 401 Unauthorized. The camera is alive and answering; the credentials
    # are wrong. That is a finding, not a dead camera — stop trying paths.
    if [[ $rc -eq 8 ]]; then echo "  ${ip}: 401 UNAUTHORIZED — camera is up, credentials are wrong"; return 2; fi
  done < <(rtsp_paths)

  echo "  ${ip}: no frame (tried http cgi + $(rtsp_paths | wc -l) rtsp paths)"
  return 1
}

cmd_snapshot() {
  local ip=${1-} out=${2-}
  [[ -n $ip ]] || die "usage: netdiag snapshot <ip> [outfile]"
  out=${out:-./snap-${ip}.jpg}
  snap_one "$ip" "$out"
}

cmd_snapshot_all() {
  local dir=${1-./camera-snapshots} iface cidr base ip n=0
  iface=$(iface_default) || true
  [[ -n ${iface:-} ]] || die "no default route"
  cidr=$(self_cidr "$iface"); base=$(echo "$cidr" | cut -d. -f1-3)
  mkdir -p "$dir"
  hdr "pulling a still frame from every camera on ${base}.0/24"
  echo "output: ${dir}/"
  echo
  while read -r ip; do
    snap_one "$ip" "${dir}/cam-${ip//./-}.jpg" || true
    n=$((n+1))
  done < <(nmap -Pn -T4 --open -p 554 "${base}.0/24" -oG - 2>/dev/null | awk '/554\/open/ {print $2}')
  echo
  echo "${n} camera(s) attempted. Open ${dir}/ and look at them — the picture is"
  echo "what maps an address to a physical location, which no scan can do."
}

cmd_identify() {
  local ip=${1-} mac vendor ports
  [[ -n $ip ]] || die "usage: netdiag identify <ip>"
  hdr "host ${ip}"
  curl -sm2 -o /dev/null "http://${ip}/" 2>/dev/null || true
  # NOTE: with `dev` given, `ip neigh` OMITS the dev field, so the MAC is $3 not
  # $5. Parsing it as $5 silently yields the literal word "lladdr" — that bug
  # cost real time on 2026-08-18. Match the token instead of counting columns.
  mac=$(ip neigh show "$ip" 2>/dev/null | grep -oE 'lladdr [0-9a-f:]{17}' | awk '{print $2}')
  vendor=$(oui_lookup "${mac:-}")
  echo "mac       : ${mac:-<no ARP reply — not on this segment>}"
  echo "vendor    : ${vendor}"
  echo "reverse   : $(getent hosts "$ip" | awk '{print $2}' || echo none)"
  hdr "open ports"
  nmap -Pn -T4 --open -p 21-25,53,80,81,161,443,445,515,554,3389,5000,5001,5060,8000,8080,8291,8443,9100,37777 \
    "$ip" 2>/dev/null | grep -E '^[0-9]+/tcp' || echo "(none open in the common set)"
  ports=$(nmap -Pn -T4 --open -p 22,80,161,443,445,515,554,3389,5000,5001,5060,8291,9100,37777 \
            "$ip" -oG - 2>/dev/null | grep -oE '[0-9]+/open' | cut -d/ -f1 | paste -sd, || true)
  echo
  echo "device class (guess): $(classify "$vendor" "$ports")"
  hdr "http identity"
  curl -skm5 -I "http://${ip}/" 2>/dev/null | grep -iE '^(HTTP|server|www-authenticate)' || echo "(no http)"
  hdr "netbios"
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
    survey)     cmd_survey "$@" ;;
    census)     cmd_census "$@" ;;
    identify)   cmd_identify "$@" ;;
    oui)        cmd_oui "$@" ;;
    cameras)    cmd_cameras "$@" ;;
    snapshot)   cmd_snapshot "$@" ;;
    snapshot-all) cmd_snapshot_all "$@" ;;
    unifi)      cmd_unifi "$@" ;;
    storm)      cmd_storm "$@" ;;
    rogue)      cmd_rogue "$@" ;;
    vlans)      cmd_vlans "$@" ;;
    listen)     cmd_listen "$@" ;;
    whoami)     cmd_whoami "$@" ;;
    switchport) cmd_switchport "$@" ;;
    exposure)   cmd_exposure "$@" ;;
    audit)      cmd_audit "$@" ;;
    hop)        cmd_hop "$@" ;;
    hop-clear)  cmd_hop_clear "$@" ;;
    -h|--help|help) usage ;;
    *)          die "unknown subcommand: '${sub}' (try: netdiag --help)" ;;
  esac
}

main "$@"
