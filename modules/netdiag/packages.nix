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

  # Unprivileged on purpose: TCP connect needs no raw socket, so the leg soak
  # runs as an ordinary user the moment marcus is plugged in.
  legsUnchecked = pkgs.writers.writePython3Bin "netdiag-legs" {
    flakeIgnore = [ "E501" "E203" "W503" ];
  } (builtins.readFile ./netdiag-legs.py);

  # The clustering is GATED BY ITS TESTS, because it is the one piece of this
  # toolkit that produces a CONCLUSION rather than a reading — "these five share
  # an upstream" sends someone to a switch. Two real bugs (a sorted-key lookup
  # and a min() denominator) both under/over-grouped while looking perfectly
  # plausible, and both were caught only by asserting a known answer.
  legs = pkgs.runCommand "netdiag-legs" { } ''
    ${pkgs.python3}/bin/python3 ${./netdiag-legs-test.py}       ${legsUnchecked}/bin/netdiag-legs
    mkdir -p $out/bin
    ln -s ${legsUnchecked}/bin/netdiag-legs $out/bin/netdiag-legs
  '';

  # The privileged half. Small on purpose.
  netdiagPriv = pkgs.writeShellApplication {
    name = "netdiag-priv";
    runtimeInputs = with pkgs; [
      coreutils gnused gnugrep gawk
      iproute2 jq tcpdump arp-scan nmap lldpd ndisc6 ethtool
    ] ++ [
      # HomePlug AV management tools. NOT in nixpkgs — packaged in pkgs/.
      (pkgs.callPackage ../../pkgs/open-plc-utils { })
    ];
    text = builtins.readFile ./netdiag-priv.sh;
  };

  # The unprivileged half — the command surface driven day to day.
  #
  # ⚠️ DO NOT PUT `sudo` IN runtimeInputs. It was here until 2026-10-27 and it
  # broke EVERY privileged subcommand on both hosts.
  #
  # writeShellApplication prepends runtimeInputs to PATH, so a `sudo` listed here
  # SHADOWS /run/wrappers/bin/sudo — and the Nix store copy is not setuid:
  #
  #   store   /nix/store/…-sudo-1.9.17p2/bin/sudo   -r-xr-xr-x   exit 1
  #   wrapper /run/wrappers/bin/sudo                -r-s--x--x   exit 0
  #   "sudo: …/bin/sudo must be owned by uid 0 and have the setuid bit set"
  #
  # On NixOS the setuid wrapper is the ONLY sudo that can elevate, so netdiag.sh
  # now calls it by absolute path and sudo is deliberately absent from this list.
  # Measured, not assumed: inside netdiag every `priv` call failed while the same
  # command from an interactive shell returned exit 0 — which is what made it look
  # like a sudo-allowlist problem for two months.
  netdiag = pkgs.writeShellApplication {
    name = "netdiag";
    runtimeInputs = (with pkgs; [
      coreutils gnused gnugrep gawk findutils
      iproute2 jq nmap curl avahi net-snmp miniupnpc libnatpmp
      ffmpeg python3
    ]) ++ [ wsdiscover legs ];
    text = ''
      export NETDIAG_OUI=${ouiTable}
    '' + builtins.readFile ./netdiag.sh;
  };
}
