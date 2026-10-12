"""nginx-config-check — refuse to build a config nginx cannot serve.

WHY THIS EXISTS, and why it is not `nginx -t`. On 2026-10-12 two nginx changes
reached production on this box and each broke a live service:

  12:18  `recommendedProxySettings` was enabled, injecting the recommended
         proxy_set_header include into all 37 proxy locations. One of them —
         the MCP connector — already set `Host` by hand, so nginx emitted the
         header TWICE and h11 rejects a request with two Host headers per
         RFC 7230. The connector returned 400 for two hours.
  13:55  A new vhost used `ssl = true` inside its `listen` entry without
         `onlySSL`, so `listen ... ssl` was emitted with no `ssl_certificate`.
         nginx then refuses to START, which took down ALL 75 vhosts — including
         the Forgejo that comin pulls from, so the deploy could not be undone
         without a manual switch-to-configuration.

Both built green. `services.nginx.validateConfigFile` was already true, but
nixpkgs' `writeNginxConfig` runs **gixy** — a SECURITY linter — and never
`nginx -t`, so it has no opinion on whether the config can start.

⚠️ AND `nginx -t` CANNOT BE THE GATE HERE. `-t` runs the full init cycle and
BINDS every listen socket. In a nix sandbox the specific addresses (100.82.x)
do not exist and privileged ports cannot be bound; on the host the running
nginx already holds them. Verified both ways before giving up on it. So this
checks the two failure SHAPES structurally instead, which is deterministic and
needs no network at all.
"""
from __future__ import annotations

import re
import sys

RECOMMENDED_INCLUDE = "recommended-proxy_set_header-headers.conf"
# The headers nixpkgs' recommended include sets. Setting any of these by hand in
# a location that ALSO pulls the include emits the header twice.
INCLUDE_SETS = ("Host", "X-Real-IP", "X-Forwarded-For", "X-Forwarded-Proto",
                "X-Forwarded-Host", "X-Forwarded-Server")
# Only a duplicate Host is fatal — proven by replay: duplicate Host gives 400
# "Invalid HTTP request received.", while duplicate X-Real-IP /
# X-Forwarded-For / X-Forwarded-Proto all return 401 normally.
FATAL_DUPES = ("Host",)


def split_blocks(text: str, keyword: str) -> list:
    """Yield the bodies of top-level `keyword { ... }` blocks, brace-counted."""
    out, i = [], 0
    # ⚠️ `location` carries a path and optional modifier before the brace
    # (`location = /x {`, `location ~ ^/y {`), so a `keyword\s*\{` pattern
    # matches NONE of them. That silently found 0 of 120 location blocks and
    # made the duplicate-header check a no-op that reported success.
    pat = re.compile(r"\b" + keyword + r"\b[^{;}]*\{")
    while True:
        m = pat.search(text, i)
        if not m:
            return out
        depth, j = 1, m.end()
        while depth and j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        out.append(text[m.end():j - 1])
        i = j


def check_ssl_certificates(conf: str) -> list:
    """Every `listen ... ssl` server must carry a certificate, or nginx won't start."""
    problems = []
    for body in split_blocks(conf, "server"):
        if not re.search(r"listen[^;]*\bssl\b", body):
            continue
        if "ssl_certificate" in body:
            continue
        name = (re.search(r"server_name\s+([^;]+);", body) or [None, "<unnamed>"])[1]
        listen = (re.search(r"(listen[^;]+);", body) or [None, "?"])[1]
        problems.append(
            f'server "{name.strip()}" has `{listen.strip()}` but no '
            f"ssl_certificate — nginx will REFUSE TO START, taking every "
            f"vhost down with it. Set onlySSL/addSSL/forceSSL (or "
            f"useACMEHost with one of them); `ssl = true` in a listen entry "
            f"alone does NOT emit the certificate directives.")
    return problems


def check_duplicate_proxy_headers(conf: str) -> list:
    """A location must not hand-set a header the recommended include also sets."""
    problems = []
    for sbody in split_blocks(conf, "server"):
        name = (re.search(r"server_name\s+([^;]+);", sbody) or [None, "<unnamed>"])[1]
        for lbody in split_blocks(sbody, "location"):
            if RECOMMENDED_INCLUDE not in lbody:
                continue
            for hdr in INCLUDE_SETS:
                if re.search(r"proxy_set_header\s+" + re.escape(hdr) + r"\s", lbody):
                    sev = "FATAL" if hdr in FATAL_DUPES else "warning"
                    problems.append(
                        f'{sev}: server "{name.strip()}" sets '
                        f"`proxy_set_header {hdr}` by hand AND includes the "
                        f"recommended proxy headers, so nginx sends it TWICE."
                        + (" Two Host headers violate RFC 7230 and the upstream "
                           "will answer 400 for every request."
                           if hdr in FATAL_DUPES else
                           " Harmless today, but ambiguous — drop one."))
    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: nginx-config-check <nginx.conf>", file=sys.stderr)
        return 2
    conf = open(sys.argv[1], encoding="utf-8", errors="replace").read()

    fatal, warn = [], []
    for p in check_ssl_certificates(conf) + check_duplicate_proxy_headers(conf):
        (warn if p.startswith("warning") else fatal).append(p)

    servers = len(split_blocks(conf, "server"))
    for w in warn:
        print(f"  [warn]  {w}")
    for f in fatal:
        print(f"  [FAIL]  {f}", file=sys.stderr)
    if fatal:
        print(f"\nnginx-config-check: {len(fatal)} fatal problem(s) in "
              f"{servers} server blocks — refusing to build.", file=sys.stderr)
        return 1
    print(f"  nginx-config-check: {servers} server blocks OK"
          + (f" ({len(warn)} warning(s))" if warn else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
