# netdiag — field network diagnostics

Marcus travels to client sites. Put it on their network, then hand the agent a
symptom and let it work. This is the toolkit behind that.

## The one command to start with

```
netdiag survey
```

Prints, in order: this host's link/address/gateway/DNS · the upstream switch and
port via LLDP · a layer-2 ARP census with vendor names · which DHCP servers
answer · a 20-second passive control-plane capture summarised into **every
subnet in use on this segment** · suggested next steps.

That last item is the one that matters most. More than one populated /24 on a
single segment means VLAN leakage or a leftover DHCP scope, and it is exactly
the fault that cost an afternoon at Craigmyle Tractor on 2026-08-18.

## The rest

| Command | What it answers |
|---|---|
| `netdiag whoami` | which switch and port am I plugged into (LLDP/CDP) |
| `netdiag census [cidr]` | who is really on this segment (ARP cannot be firewalled away) |
| `netdiag listen [secs]` | what is on the wire that shouldn't be — foreign subnets, rogue DHCP |
| `netdiag cameras` | camera census: ONVIF WS-Discovery + mDNS + port signature |
| `netdiag host <ip>` | deep fingerprint of one device |
| `netdiag hop <cidr>` | temporarily join a foreign subnet and scan it |
| `netdiag hop-clear` | drop the temporary address early |

## Why these tools

Three things were slow during the Craigmyle outage, and each maps to something
here.

**No packet capture.** A replacement switch had been provisioned onto a stale
VLAN 88 carrying a parallel `192.168.128.0/24`. Five healthy cameras sat in it,
invisible to Blue Iris because nothing routed there. It was found by luck —
Amcrest broadcast their full IP config over mDNS, and the site's Dahua and
Hikvision cameras advertise nothing at all. `netdiag listen` now finds this
class of fault directly, by bucketing every address seen in a capture into /24s.

**No LLDP/CDP.** "Which switch port is that camera on" was unanswerable all
afternoon. `services.lldpd` fixes both directions: we can see the upstream
switch, and the switch's neighbour table names this laptop instead of a MAC.

**Fragile improvised shell.** Four parsing bugs that day each produced a
plausible *empty result* rather than an error — the worst failure mode, because
"nothing found" and "my parser broke" look identical. One example worth
remembering: given `dev`, `ip neigh` **omits** the dev field, so the MAC is `$3`
and not `$5`; reading `$5` silently yields the literal word `lladdr` and a
device count of zero. Everything here is tested shell, gated by shellcheck at
build time.

## Privilege model

`netdiag-priv` is the **single** privileged entry point, reached through a
narrow sudo allowlist in `modules/agent/sudo.nix` — not blanket sudo. It takes
a closed vocabulary:

- interface names and CIDRs are regex- and range-validated
- capture filters are chosen from a fixed profile list (`arp dhcp discovery
  neighbour stp all`), never passed in
- tcpdump never receives `-w`/`-W`/`-z` — no file writes, no postrotate exec hook
- captures are snaplength-limited to headers, so **a service call cannot collect
  a client's payload traffic**
- temporary addresses carry a kernel lifetime and expire on their own, so a
  forgotten address never lingers on someone else's network

Everything else in the toolkit runs unprivileged.

## Working on a client's network

Two habits worth keeping. Capture profiles are deliberately control-plane only —
prefer them to raw tcpdump, both because they are more useful and because you
are on someone else's network. And `netdiag hop` addresses self-expire, but run
`netdiag hop-clear` before you leave anyway.
