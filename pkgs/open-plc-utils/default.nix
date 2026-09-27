# open-plc-utils — Qualcomm Atheros HomePlug AV management tools.
#
# NOT IN NIXPKGS (checked 2026-09-27: no open-plc-utils, no faifa, nothing under
# plc/homeplug/powerline except vendor HTTP shims). Packaged here because a
# powerline segment is otherwise completely undiagnosable — the adapters are
# transparent L2 bridges, so they do not appear in an IP scan, answer no ping,
# and hold no address. They are invisible to every other tool in netdiag.
#
# HOW IT TALKS TO THEM. HomePlug AV management frames, ethertype 0x88E1, sent
# raw on the local segment. Consequences worth knowing before reaching for it:
#   * it needs CAP_NET_RAW and the -i <iface> of the segment the adapters are on
#     -> so it lives behind netdiag-priv, like tcpdump and arp-scan.
#   * IT IS LAYER 2 ONLY. The frames do not route, so this cannot be run from
#     gromit against a customer site. It needs marcus plugged into that LAN.
#   * WIRED ONLY. Sending 0x88E1 out a wifi interface finds nothing, which looks
#     identical to "there are no adapters here" — the same trap the L2 checks in
#     NETDIAG.md already carry a warning about.
#
# Chipset coverage is the reason this is the right tool rather than a vendor
# utility: QCA INT6x00/AR7420/QCA7420 silicon is inside most HomePlug AV gear
# regardless of the badge on it (TP-Link, Netgear, Zyxel, Trendnet, Devolo), and
# the management frames are the same. A vendor API would cover one brand.
{ lib, stdenv, fetchFromGitHub }:

stdenv.mkDerivation {
  pname = "open-plc-utils";
  version = "0-unstable-2026-09-27";

  src = fetchFromGitHub {
    owner = "qca";
    repo = "open-plc-utils";
    rev = "5e0295719d70058221273ac0e780fcd82e28ea0f";
    hash = "sha256-pLy+LQWFVdQjkFMy3FV1adlZ/Y7+EJFy2CXTh8tZLjY=";
  };

  # Upstream's install target writes to /usr/local and also tries to install
  # manpages and symlinks with hardcoded paths. Point everything at $out.
  makeFlags = [ "ROOT=" "BIN=${placeholder "out"}/bin" "MAN=${placeholder "out"}/share/man" ];

  enableParallelBuilding = true;

  preInstall = ''
    mkdir -p $out/bin $out/share/man/man1 $out/share/man/man2
  '';

  meta = with lib; {
    description = "Qualcomm Atheros HomePlug AV powerline management utilities";
    longDescription = ''
      Reads and configures HomePlug AV powerline adapters over raw ethernet
      management frames. The diagnostic that matters for a flaky powerline leg is
      the negotiated PHY rate per link: healthy HomePlug AV negotiates roughly
      80-200 Mbit/s, and a leg that has degraded to single-digit Mbit/s will drop
      streams while still passing a ping.
    '';
    homepage = "https://github.com/qca/open-plc-utils";
    license = licenses.bsd3;
    platforms = platforms.linux;
  };
}
