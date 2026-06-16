# Litestream — continuous replication of the Vaultwarden SQLite vault to B2.
#
# STATUS: WIRED 2026-06-08 (imported in configuration.nix). It will not actually
# replicate until the B2 prerequisites below are done — the unit needs
# /var/lib/litestream/b2.env to start, and the bucket/endpoint must be real.
# Sketched 2026-06-07; see [[vaultwarden-selfhosted]].
#
# WHY: Vaultwarden's DB is already in the nightly restic→B2 backup, but that's a
# ~24h RPO and a file-level copy of a live SQLite DB (can be torn mid-write).
# Litestream streams the WAL to B2 continuously, giving a *consistent* replica
# seconds behind. Keep restic too — it covers attachments + versioned history;
# Litestream covers only db.sqlite3 (fast, consistent DB recovery).
#
# PREREQUISITES (do these first, on a keyboard):
#   1. Create a DEDICATED B2 bucket, e.g. "gromit-vaultwarden-litestream".
#   2. Create a B2 application key scoped to THAT bucket only (least privilege).
#   3. Note the bucket's S3 endpoint, e.g. s3.us-west-002.backblazeb2.com.
#   4. Store the key in secrets/litestream-b2.yaml via sops (root 0400 at runtime):
#        LITESTREAM_ACCESS_KEY_ID=<keyID>
#        LITESTREAM_SECRET_ACCESS_KEY=<applicationKey>
#      (migrated from the old plaintext /var/lib/litestream/b2.env on 2026-06-15)
#   5. Set the bucket/endpoint below, add `./modules/services/litestream.nix`
#      to configuration.nix, and `nixos-rebuild test` first.
#
# RECOVERY (on a fresh box, after restic restores the rest of /var/lib/bitwarden_rs):
#   litestream restore -o /var/lib/bitwarden_rs/db.sqlite3 \
#     s3://gromit-vaultwarden-litestream/vaultwarden
#   then start vaultwarden.
#
# NOTE TO VERIFY ON ACTIVATION: litestream must read AND write the DB dir (it
# creates a shadow WAL + .db-litestream temp), and the DB is 0600
# vaultwarden:vaultwarden. Run litestream as the vaultwarden user (override
# serviceConfig.User below) or it won't be able to open the file. Confirm with
# `journalctl -u litestream` after the first rebuild.
{ config, lib, pkgs, ... }:

{
  # B2 application key, now sourced from sops (migrated 2026-06-15 from the manual
  # root:0600 /var/lib/litestream/b2.env). The file holds the two LITESTREAM_*
  # vars; it decrypts to /run/secrets/litestream-b2 at activation. systemd reads
  # environmentFile as root before dropping to the vaultwarden User, so the secret
  # stays root-owned 0400 (vaultwarden never needs to read it). Chris owns the
  # value via `sops secrets/litestream-b2.yaml`; the agent only wires the path.
  sops.secrets."litestream-b2" = {
    sopsFile = ../../secrets/litestream-b2.yaml;
    key = "litestream-b2";
    # default owner root / mode 0400 is correct here.
  };

  services.litestream = {
    enable = true;
    environmentFile = config.sops.secrets."litestream-b2".path;   # holds LITESTREAM_ACCESS_KEY_ID / _SECRET_ACCESS_KEY
    settings = {
      dbs = [
        {
          path = "/var/lib/bitwarden_rs/db.sqlite3";
          replicas = [
            {
              type = "s3";
              bucket = "gromit-vaultwarden-litestream";          # <-- set to your bucket
              path = "vaultwarden";
              endpoint = "s3.us-west-002.backblazeb2.com";        # <-- set to your bucket's region endpoint
              # access-key-id / secret-access-key come from the env file above.
            }
          ];
        }
      ];
    };
  };

  # litestream needs to open the vaultwarden-owned (0600) DB and write its shadow
  # WAL alongside it — run it as the vaultwarden user. Verify after first rebuild.
  # (The B2 key in environmentFile is read by systemd as root before dropping to
  # this User, so b2.env stays root:root 0600 — vaultwarden need not read it.)
  systemd.services.litestream.serviceConfig.User = lib.mkForce "vaultwarden";

  # Start after vaultwarden so db.sqlite3 exists (vaultwarden runs migrations on
  # startup). Soft `after` only — litestream is a backup sidecar, so a vaultwarden
  # failure shouldn't cascade-stop it, and litestream retries if the DB is missing.
  systemd.services.litestream.after = [ "vaultwarden.service" ];
  systemd.services.litestream.wants = [ "vaultwarden.service" ];
}
