# Calibre-Web — web library + OPDS feed over the Calibre library that has been
# sitting on the pool since 2016 (/mnt/fusion/books/Calibre: 327 books, verified
# consistent — the metadata.db lists exactly the 327 book directories on disk).
#
# WHY THIS EXISTS: Chris turned up a Kindle Paperwhite 4 (10th gen, firmware
# 5.18.1) in October 2026. The plan is KOReader on a jailbroken device pulling
# over OPDS, not Amazon's Send-to-Kindle — so what this has to serve is an OPDS
# catalogue on the LAN. KOReader reads EPUB, PDF, MOBI, AZW3 and TXT natively,
# so nothing here converts anything on the way out; the library's 99 MOBI and 15
# AZW files go to the device as-is.
#
# WHY NOT Calibre-Web-Automated: CWA's selling points are ingest-folder
# auto-conversion, an EPUB fixer for files Amazon's Send-to-Kindle rejects, and
# KOReader progress sync. The first two only matter on the Amazon path, which
# this deliberately is not. Progress sync is the one real loss, and it is not
# worth a pinned container image plus a PUID/PGID dance for a first cut. If the
# sync turns out to matter, CWA is a drop-in swap at that point.
#
# ── The Kindle cannot join Tailscale ──────────────────────────────────────────
# Same problem as the Roku and the TVs. The path that makes this reachable from
# a plain LAN device was verified on gromit 2026-10-23 before this was written,
# and every leg of it already existed:
#
#   * nginx listens on 0.0.0.0:443 (not just the Tailscale address).
#   * networking.nix opens 80/443 globally, i.e. on enp3s0 too — unlike 22/8096,
#     which are scoped per interface.
#   * The nginx-access source gate allows 192.168.0.0/16, so a LAN client is
#     permitted rather than denied with the public internet.
#   * blocky's customDNS maps the whole rosemaryacres.com zone to 192.168.1.65,
#     so a new subdomain needs no new DNS entry to resolve on the LAN.
#
# So no new listener, firewall hole or split-horizon entry is needed here.
#
# ⚠️ THE ONE THING THAT IS NOT ALREADY TRUE: the Kindle must query blocky
# DIRECTLY. `dig @192.168.1.1 abs.rosemaryacres.com` returns 100.82.117.116 —
# the router is NOT forwarding to blocky, so the LAN gets the public (Tailscale)
# answer and a non-Tailscale device cannot reach it. Set a static DNS of
# 192.168.1.65 in the Kindle's Wi-Fi settings (Settings → Wi-Fi → the network →
# Advanced). Using a bare IP in KOReader instead would defeat TLS, since the
# Let's Encrypt cert only matches the hostname.
#
# NOT behind Authelia, on purpose: OPDS clients authenticate with HTTP Basic and
# cannot complete an SSO redirect, so protecting this vhost would break the only
# thing it exists to do. Calibre-Web's own login guards the web UI, and the
# source gate above keeps the whole thing off the public internet.
#
# The hostname is `ebooks`, not `books` — books.rosemaryacres.com is stacks, the
# catalogue of the PHYSICAL shelves. Different library, easy to confuse.
{ config, lib, pkgs, ... }:

let
  port = 8083;
  library = "/mnt/fusion/books/Calibre";
  # Deliberately beside the library rather than inside it, so Calibre never
  # sees a stray database file at its library root.
  dbSnapshot = "/mnt/fusion/books/metadata.db.pre-calibre-web";
in
{
  services.calibre-web = {
    enable = true;

    # The module's default is ::1, but lib/proxy-vhost.nix dials
    # http://127.0.0.1:<port>. Left at the default, nginx gets connection
    # refused on every request while the service itself looks healthy.
    listen.ip = "127.0.0.1";
    listen.port = port;

    # Runs as the calibre-web system user (created by the upstream module) but
    # in `media`, which is the group that owns the library — /mnt/fusion/books
    # is chris:media with group write, so this is what lets it open the
    # database read-write. Audiobookshelf is in the same group for the same
    # reason.
    group = "media";

    options = {
      calibreLibrary = library;
      # Puts calibre's ebook-convert behind the UI's convert button. Worth the
      # ~1 GB closure here specifically because 159 of the 327 books are PDFs,
      # which are miserable on a 6" screen and are the one part of this library
      # that may genuinely need converting.
      enableBookConversion = true;
      enableBookUploading = true;
    };
  };

  # Calibre-Web opens the library READ-WRITE (the upstream module puts
  # calibreLibrary in the unit's ReadWritePaths), and this database is on
  # Calibre schema version 22 from December 2016 against a current Calibre that
  # expects far newer. The first open migrates it, one way.
  #
  # /mnt/fusion/books is NOT in the restic set (backup.nix covers Bitcoin,
  # nextcloud and immich off the pool, not this), and SnapRAID parity is no help
  # against a software migration — the next sync just follows it. So take the
  # pre-migration copy ourselves, once.
  #
  # The `! -e` guard is the point: it keeps the ORIGINAL schema-22 database. A
  # plain overwrite would, on the second start, replace the only pre-migration
  # copy with an already-migrated one and quietly throw away the thing being
  # protected.
  systemd.services.calibre-web-db-snapshot = {
    description = "One-time copy of the 2016 Calibre metadata.db before Calibre-Web migrates it";
    before = [ "calibre-web.service" ];
    requiredBy = [ "calibre-web.service" ];
    serviceConfig = {
      Type = "oneshot";
      User = "calibre-web";
      Group = "media";
    };
    script = ''
      set -euo pipefail
      if [ -f ${lib.escapeShellArg library}/metadata.db ] && [ ! -e ${lib.escapeShellArg dbSnapshot} ]; then
        ${pkgs.coreutils}/bin/cp -p ${lib.escapeShellArg library}/metadata.db ${lib.escapeShellArg dbSnapshot}
        echo "snapshotted pre-migration metadata.db to ${dbSnapshot}"
      else
        echo "snapshot already present or no library database; nothing to do"
      fi
    '';
  };

  services.nginx.virtualHosts."ebooks.rosemaryacres.com" =
    import ../lib/proxy-vhost.nix {
      port = port;
      extraConfig = ''
        # Book downloads over OPDS; the default 1m body limit is about uploads
        # via the UI, which is enabled above.
        client_max_body_size 0;
        proxy_read_timeout 300s;
      '';
    };
}
