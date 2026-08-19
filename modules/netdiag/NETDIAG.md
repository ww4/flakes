# netdiag — field network diagnostics

Marcus travels to client sites. Put it on their network, hand the agent a
symptom, let it work. This is the toolkit behind that.

## Read this before you trust any layer-2 answer

**Most L2 diagnostics do not work over wifi, and they fail silently.** APs
rate-limit broadcast, proxy ARP and buffer multicast, so a wire storming at
40,000 pps presents to an associated client as a couple of hundred — you would
measure "normal" and clear a segment that is on fire. STP and VLAN detection are
not merely degraded but structurally impossible: BPDUs go to a reserved group
address a conformant bridge must never forward, and the AP strips the 802.1Q tag
before the frame reaches you.

**Carry a USB-Ethernet adapter.** Commands marked `[WIRE]` warn at runtime.

The second structural limit: **a switched LAN hides other hosts' unicast traffic
from you**, and promiscuous mode does not change that — the switch never
delivers those frames. "Who is eating the bandwidth" is therefore not a capture
question; it is an SNMP-counters-on-the-switch question.

## Start here

```
netdiag survey
```

This host, the upstream switch and port, a layer-2 census with vendors and
device-type guesses, which DHCP servers answer, and every subnet in use on the
segment. More than one populated /24 means VLAN leakage or a leftover scope —
the fault that cost an afternoon at Craigmyle on 2026-08-18.

## Command surface

| Command | Answers |
|---|---|
| `survey [iface]` | first look — start here |
| `census [cidr]` | who is really here: ARP + vendor + device class |
| `identify <ip>` | deep fingerprint of one device |
| `oui <mac>` | vendor lookup, offline |
| `cameras` | ONVIF WS-Discovery + mDNS + port signature |
| `snapshot <ip> [file]` | **which physical camera is this?** — pulls a still frame |
| `snapshot-all [dir]` | a frame from every camera on the segment |
| `unifi` | Ubiquiti devices, including **unadopted** APs |
| `storm [secs]` `[WIRE]` | broadcast rate + definitive L2 loop test |
| `rogue [secs]` | rogue DHCP and rogue IPv6 RA, active + passive |
| `vlans [secs]` `[WIRE]` | 802.1Q tags — am I on a trunk? |
| `listen [secs]` | passive watch; foreign subnets, rogue services |
| `whoami` `[WIRE]` | which switch and port am I on |
| `switchport <mac> <switch-ip> [community]` | **which switch port is any device on** |
| `exposure` | UPnP/NAT-PMP holes, WAN IP, double-NAT vs CGNAT |
| `audit <ip>` | service exposure on one host |
| `hop <cidr>` / `hop-clear` | temporarily join a foreign subnet |

## The things worth knowing

**`switchport` is the one that closes the old wound.** "Which switch port is
this camera on" was unanswerable all afternoon at Craigmyle, because the switch
holding the port history was the one the storm destroyed. The SNMP bridge
forwarding table (`1.3.6.1.2.1.17.7.1.2.2`) answers it for any device, with no
vendor API and no dashboard — just a readable community string. It also gives
PoE draw per port, link state and port descriptions.

**The loop test is definitive.** Capture inbound-only and filter on our own MAC
as *source*. Any frame arriving that way was returned by the switch fabric;
there is no benign explanation. Zero frames means no loop on this VLAN.

**`snapshot` is how you map an address to a location.** No scan can tell you
which physical camera `192.168.1.114` is. A still frame can. Credentials come
from `~/.config/netdiag/camera-creds.env` (see `netdiag-creds.env.example`),
never from argv — an RTSP URL on the command line is visible in `ps`, in shell
history, and in ffprobe's own JSON output.

**Unadopted UniFi APs have a caveat.** nmap's `ubiquiti-discovery` reads the
adoption TLV with a native-endian unpack while every other multi-byte field uses
big-endian. On x86 a device sending the 4-byte form of "unadopted" parses as
16777216 and the field is silently dropped — biased exactly against what you are
looking for. **Treat an absent `config_status` as possibly unadopted**, and
corroborate with hostname `UBNT`, ESSID `ubnt`, or the factory address
192.168.1.20.

**Rogue IPv6 RA is the most-missed fault here.** A consumer router plugged in
LAN-to-LAN starts advertising itself as the IPv6 default route. IPv6 is
preferred over IPv4, so traffic blackholes while every IPv4 test you run passes.
It presents as "some websites are slow", never as "IPv6 is broken".

**Rogue DHCP and rogue RA are intermittent by nature.** A one-shot probe misses
them. `netdiag rogue` does both an active probe and a passive watch, and the
passive half is the one that catches the travel router someone plugs in twice a
day.

**Absence of BPDUs proves nothing.** Access ports commonly run BPDU guard or
filter, so a healthy STP network shows zero BPDUs on your port. Never report
"STP is not running".

**Vendor identification is not a hardcoded list.** The OUI table is baked at
build time from wireshark's `manuf`, covering all four IEEE registries with
longest-prefix matching (/36 → /28 → /24). A hardcoded list silently rots: one
compiled MikroTik list omitted `00:0C:42`, the original RouterBOARD block and
the most common one on older rural gear. Locally-administered MACs are reported
as randomised rather than looked up — those are phones and laptops, never
infrastructure.

## Vendor discovery, by vendor

| Vendor | How you find it |
|---|---|
| **MikroTik** | MNDP (UDP 5678) + CDP + LLDP, **all on by default**. Passive capture finds it. Winbox on 8291 is a definitive port signature. |
| **Ubiquiti** | UDP 10001 discovery, with adoption state (see caveat above) |
| **Cambium** | DHCP Option 60 vendor class — `cambium`, `Cambium-WiFi-AP`, `Cambium PMP 450 AP`. Fingerprint from router logs without touching the radio. |
| **Mimosa** | **No discovery protocol exists** — the vendor's own manual says so. OUI matching is the correct answer, not a fallback. |
| **Tarana** | Nothing to probe; radios auto-register outbound. Default MGMT `192.168.10.2/24`. ⚠️ The BN MGMT port is a Harting push-pull connector — a bare RJ45 can jam or damage it. |
| **Cameras** | ONVIF WS-Discovery (vendor-neutral, but multicast so it will not cross VLANs), plus mDNS for Amcrest only |

Lost radio, no known address: ePMP AP `192.168.0.1` / SM `192.168.0.2`;
PMP/PTP 450 answers on `169.254.1.1` **even after** a static address is set;
PTP 820 `192.168.1.1`.

## Privilege model

`netdiag-priv` is the **single** privileged entry point, on a narrow sudo
allowlist in `modules/agent/sudo.nix` — not blanket sudo. Closed vocabulary:
interfaces and CIDRs regex- and range-validated; capture filters chosen from a
fixed profile list; nmap only ever with a fixed script and fixed port list;
tcpdump never handed `-w`/`-W`/`-z`.

Two properties that exist specifically because this runs on **other people's**
networks:

- **captures are snaplength-limited to headers** — a service call cannot collect
  a client's payload traffic
- **temporary addresses carry a kernel lifetime** — a forgotten address expires
  instead of lingering on a customer's LAN after you drive away

`services.lldpd` runs **receive-only** (`-r`) for the same reason: a
consultant's laptop should not advertise itself as network infrastructure in a
client's L2 domain.

## Before you leave a site

`netdiag hop-clear`, and confirm any fix with a fresh sweep rather than from the
dashboard's own claim.
