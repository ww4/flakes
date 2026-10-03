"""Finding a Roku on the LAN, so nobody has to type an address that DHCP owns.

Roku answer SSDP for the search target `roku:ecp`. That is the whole mechanism
and it is reliable when the box is awake and on the same broadcast domain.

It is NOT reliable in general, and the fallbacks matter more than the discovery:

  * multicast does not cross VLANs or most wifi isolation, so a box on a guest
    network is invisible however long you wait;
  * a SUSPENDED Roku may not answer SSDP while still answering ECP perfectly
    well on 8060 — which is exactly the state one was found in on 2026-10-03,
    silent to a search and happy to report its own model number;
  * some access points drop multicast under load.

So the module takes a configured `host` first and only searches when it has
none, and a search that finds nothing is a reason to say so rather than to
fail: ECP is still there to be reached by address.
"""

from __future__ import annotations

import logging
import re
import socket
import urllib.parse

log = logging.getLogger("roku.discover")

GROUP = ("239.255.255.250", 1900)
TARGET = "roku:ecp"


def _message() -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {GROUP[0]}:{GROUP[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"ST: {TARGET}\r\n"
        "MX: 3\r\n\r\n"
    ).encode()


def find(timeout: float = 4.0, tries: int = 3, bind: str = "") -> list[dict]:
    """Every Roku that answers, as {host, port, location, usn}.

    UDP, so the search is sent more than once on purpose: a single lost
    datagram is an ordinary event on wifi and would otherwise read as "no Roku
    on this network".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.bind((bind, 0))
    except OSError as e:
        log.warning("cannot bind %s for discovery: %s", bind or "*", e)
        return []

    found: dict[str, dict] = {}
    try:
        for _ in range(max(1, tries)):
            try:
                sock.sendto(_message(), GROUP)
            except OSError as e:
                log.warning("discovery send failed: %s", e)
                return []
        while True:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            text = data.decode("utf-8", "replace")
            loc = re.search(r"(?im)^LOCATION:\s*(\S+)", text)
            usn = re.search(r"(?im)^USN:\s*(\S+)", text)
            location = loc.group(1) if loc else ""
            port = 8060
            if location:
                parsed = urllib.parse.urlparse(location)
                port = parsed.port or 8060
            found[addr[0]] = {"host": addr[0], "port": port,
                              "location": location, "usn": usn.group(1) if usn else ""}
    finally:
        sock.close()
    return sorted(found.values(), key=lambda d: d["host"])
