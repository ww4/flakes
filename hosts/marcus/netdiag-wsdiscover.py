"""ONVIF WS-Discovery probe — vendor-neutral camera enumeration.

mDNS only finds cameras whose firmware bothers to advertise; on the Craigmyle
site that was the Amcrest AMC039/AMC065 families and nothing else, while the
Dahua and Hikvision units stayed invisible. Every ONVIF camera, regardless of
vendor, answers a WS-Discovery Probe on 239.255.255.250:3702.

Prints one line per responder: IP, then the service URLs and scopes it reports
(model, hardware and location often appear in the scopes).
"""
from __future__ import annotations

import re
import socket
import struct
import sys
import uuid

MCAST_GROUP = "239.255.255.250"
MCAST_PORT = 3702
WAIT_SECONDS = 4.0

PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{msgid}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""


def local_addresses() -> list[str]:
    """Every IPv4 address we could send the probe from (one per subnet)."""
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except socket.gaierror:
        pass
    # Also whatever address reaches the default route, which getaddrinfo misses
    # when the hostname does not resolve to it.
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe_sock.connect(("8.8.8.8", 53))
        addrs.add(probe_sock.getsockname()[0])
    except OSError:
        pass
    finally:
        probe_sock.close()
    return sorted(a for a in addrs if not a.startswith("127."))


def probe_from(source: str) -> dict[str, str]:
    """Send one Probe from `source` and collect replies until the timeout."""
    found: dict[str, str] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 2))
    try:
        sock.bind((source, 0))
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source)
        )
        sock.settimeout(WAIT_SECONDS)
        message = PROBE.format(msgid=uuid.uuid4())
        sock.sendto(message.encode(), (MCAST_GROUP, MCAST_PORT))
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            found[addr[0]] = data.decode("utf-8", "replace")
    except OSError as exc:
        print(f"  (probe from {source} failed: {exc})", file=sys.stderr)
    finally:
        sock.close()
    return found


def summarise(body: str) -> tuple[list[str], list[str]]:
    urls = re.findall(r"https?://[^\s<>\"]+", body)
    scopes: list[str] = []
    match = re.search(r"<[^>]*Scopes[^>]*>(.*?)</[^>]*Scopes>", body, re.S)
    if match:
        scopes = [
            s.rsplit("/", 1)[-1]
            for s in match.group(1).split()
            if "onvif://www.onvif.org/" in s
        ]
    return sorted(set(urls)), [s for s in scopes if s]


def main() -> int:
    sources = local_addresses()
    if not sources:
        print("no usable local IPv4 address", file=sys.stderr)
        return 1

    responders: dict[str, str] = {}
    for source in sources:
        responders.update(probe_from(source))

    if not responders:
        print("no ONVIF responders")
        return 0

    for ip in sorted(responders, key=lambda a: [int(o) for o in a.split(".")]):
        urls, scopes = summarise(responders[ip])
        print(f"  {ip}")
        if scopes:
            print(f"      scopes : {' '.join(scopes)}")
        for url in urls:
            print(f"      service: {url}")
    print(f"\n{len(responders)} ONVIF responder(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
