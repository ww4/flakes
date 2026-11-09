# HMAC keys for the homelab-watch heartbeats — gromit AND wallace.
#
# Unlike the other secret modules beside it, this one is NOT gromit-only: both
# hosts send heartbeats, so both need their key. wallace is therefore a sops
# recipient — but a recipient of THIS FILE ALONE, via a scoped creation rule in
# .sops.yaml that sits above the catch-all. Adding wallace to the catch-all
# would have granted it decryption of every secret in this repo, which is a far
# larger grant than the feature needs and not one anyone would notice later.
#
# The same two values live on lock3 at /var/lib/homelab-watch/keys, where they
# were generated (lock3 has no sops, so that file is placed by hand). Rotating
# one end means rotating both.
#
# ⚠️ Mode 0400 and owned by root because the heartbeat unit runs as root. If
# that unit is ever given a dedicated user, this owner has to move with it or
# the heartbeat silently starts failing — and a heartbeat that stops reads as
# the HOST being down, so the symptom points at the wrong thing entirely.
{ config, ... }:
{
  sops.secrets."homelab-watch" = {
    sopsFile = ../../secrets/homelab-watch.yaml;

    # ⚠️ `key` selects ONE entry out of the file, named for this host. Without
    # it sops-nix looks for a YAML key matching the SECRET name
    # ("homelab-watch"), which does not exist in the file — the first build
    # failed exactly there, with "the key 'homelab-watch' cannot be found".
    #
    # Picking per-host is better than renaming the entry, because it means each
    # host decrypts only its OWN key: wallace never holds gromit's heartbeat key
    # in cleartext and vice versa. Least privilege inside a shared file.
    #
    # The deployed file therefore contains the BARE hex value with no name
    # prefix, which is why homelab-heartbeat.py accepts a bare value as well as
    # `name: hex` / `name=hex` lines.
    key = config.networking.hostName;

    owner = "root";
    mode = "0400";
  };
}
