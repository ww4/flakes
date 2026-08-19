# The netdiag DERIVATIONS, split out from default.nix so that netwatch.nix can
# reference them too.
#
# WHY THIS FILE EXISTS (2026-08-19): these packages used to be `let`-bound
# inside default.nix, which made them invisible to netwatch.nix — a sibling
# module, not a child. netwatch's units therefore had a `path` that could not
# contain `netdiag` or `netdiag-priv`, and every scan died with
# `FileNotFoundError` from the first run after deploy. The binaries WERE on the
# interactive PATH via environment.systemPackages, which is exactly what made it
# look fine when checked by hand.
#
# Same inputs on both call sites means the same store path, so importing this
# twice does not duplicate anything in the closure.
{ pkgs }:

rec {
  # Offline MAC-vendor table, baked at build time from wireshark's `manuf`.
  #
  # Deliberately NOT a hardcoded OUI list and NOT a runtime fetch of the IEEE
  # CSV. Hardcoding is wrong because it silently rots: a compiled MikroTik list
  # omitted 00:0C:42, the original RouterBOARD block and the most common one on
  # older gear at a rural site — every such device would have gone unidentified.
  # Fetching at runtime is wrong because a service call is exactly when the
  # uplink may be down, and oui.csv covers only MA-L, missing every /28 and /36
  # assignment. `manuf` is offline, covers all four registries, and is already
  # compiled into tshark.
  #
  # Output: HEXPREFIX <tab> nibble-count <tab> vendor. The nibble count drives
  # longest-prefix matching (/36 -> /28 -> /24) — a /24-only lookup returns the
  # shared IEEE registry owner rather than the real vendor for small blocks.
  ouiTable = pkgs.runCommand "netdiag-oui.tsv"
    { nativeBuildInputs = [ pkgs.wireshark-cli pkgs.gawk ]; } ''
      tshark -G manuf | grep -v '^#' | awk -F'\t' '
        {
          p = $1; sub(/[ \t]+$/, "", p)
          n = 24
          if (index(p, "/") > 0) { split(p, a, "/"); p = a[1]; n = a[2] + 0 }
          gsub(/:/, "", p)
          print toupper(p) "\t" (n / 4) "\t" $3
        }' > $out
      test -s $out
    '';

  # ONVIF WS-Discovery. Vendor-neutral, so it sees the cameras mDNS misses.
  wsdiscover = pkgs.writers.writePython3Bin "netdiag-wsdiscover" {
    flakeIgnore = [ "E501" "E203" "W503" ];
  } (builtins.readFile ./netdiag-wsdiscover.py);

  # The privileged half. Small on purpose.
  netdiagPriv = pkgs.writeShellApplication {
    name = "netdiag-priv";
    runtimeInputs = with pkgs; [
      coreutils gnused gnugrep gawk
      iproute2 jq tcpdump arp-scan nmap lldpd ndisc6 ethtool
    ];
    text = builtins.readFile ./netdiag-priv.sh;
  };

  # The unprivileged half — the command surface driven day to day.
  netdiag = pkgs.writeShellApplication {
    name = "netdiag";
    runtimeInputs = (with pkgs; [
      coreutils gnused gnugrep gawk findutils
      iproute2 jq nmap curl avahi sudo net-snmp miniupnpc libnatpmp
      ffmpeg python3
    ]) ++ [ wsdiscover ];
    text = ''
      export NETDIAG_OUI=${ouiTable}
    '' + builtins.readFile ./netdiag.sh;
  };
}
