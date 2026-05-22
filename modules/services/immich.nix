# Immich — self-hosted photo & video management.
#
# Fresh setup (2026-05): the 2024 attempt was abandoned, its photo storage
# lost, and its orphaned DB renamed to immich_orphaned_2024. This is a clean
# start — the NixOS module creates a fresh DB (VectorChord) + Redis.
{ config, lib, pkgs, ... }:

{
  services.immich = {
    enable = true;
    host = "0.0.0.0";
    port = 2283;                       # already open in the firewall
    mediaLocation = "/mnt/fusion/immich";
  };
}
