# media-link — hardlink completed downloads into the Jellyfin library.
#
# For seeded content, the library copy must be a HARDLINK, never a move: moving
# the file out from under qBittorrent kills the seed, which on a private tracker
# costs ratio and eventually the account. One copy of the bytes, two names —
# seeds from /mnt/fusion/arr, plays from /mnt/fusion/{Movies,TV Shows}, and
# media-mirror (rsync -aH, /arr excluded) backs it up exactly once.
#
#   media-link "<src-dir>" --show "Tom Terrific (1957)" [--season N] [--extras]
#   media-link "<src-dir>" --movie "Some Film (1974)"
#
# Dry-run by default; --apply to act. Nothing is deleted or overwritten, and the
# whole plan is validated before a single link is made.
#
# ⚠️ MUST run as root or chris. `fs.protected_hardlinks=1` forbids creating a
# hardlink to a file you neither own nor can write, and the download tree is
# chris:users 0644 — so the agent (uid claude) gets EPERM. Hence the scoped sudo
# entry in modules/agent/sudo.nix; the agent invokes `sudo media-link …`.
{ config, lib, pkgs, ... }:

let
  media-link = pkgs.writeShellApplication {
    name = "media-link";
    runtimeInputs = [ pkgs.python3 ];
    text = ''
      export FUSION=/mnt/fusion
      export MOVIES_DIR="/mnt/fusion/Movies"
      export TV_DIR="/mnt/fusion/TV Shows"
      export FUSION_BRANCHES="/mnt/primary/D*"
      exec ${pkgs.python3}/bin/python3 ${./media-link.py} "$@"
    '';
  };
in
{
  environment.systemPackages = [ media-link ];
}
