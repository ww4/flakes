# sops-nix — secrets encrypted in this repo, decrypted at activation.
#
# Decryption (the box): gromit decrypts with its SSH ed25519 HOST key, converted
# to age — so no extra decryption key lives on the box (and it survives reboots).
# Editing (the human): Chris's admin age key, registered in ./.sops.yaml.
#
# Security boundary: the `claude` agent has NO decryption key. It can add the
# plumbing — `sops.secrets.<name>` declarations + wiring a service's *File option
# to `config.sops.secrets.<name>.path` — but it CANNOT read or edit secret
# values. Chris edits values with `sops` using his key. (Same split we used for
# the ww4-bot token: agent adds the reference, Chris adds the value.)
#
# Migration: COMPLETE (2026-06-16). All static secrets live in secrets/*.yaml
# (restic, vaultwarden, homepage, paperless, litestream, gluetun, aurral,
# decluttarr, media-curate, nextcloud-admin, the Cloudflare token, the OIDC
# client secrets). Deliberately NOT migrated — they're runtime-generated/self-
# managing, so sops would be a fragile second source of truth: mempool db/rpc
# env (regenerated each boot / rotating bitcoind cookie) and grafana
# admin_password + secret_key (generated once on the box).
#
# To add a NEW secret, declared next to its consumer module:
#   1. encrypt the value: `sops secrets/<name>.yaml` (needs the admin age key)
#   2. declare it: sops.secrets."<name>" = {
#        sopsFile = ../../secrets/<name>.yaml; owner = "<svc-user>"; mode = "0400";
#      };
#   3. point the service's *File option at `config.sops.secrets."<name>".path`.
{ ... }:
{
  # Use gromit's SSH host key as the age identity for decryption at activation.
  sops.age.sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
}
