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
# Phase 0 (this commit): infrastructure only — NO real secrets migrated yet.
# Migrate one at a time:
#   1. Chris: `sops secrets/<name>.yaml`  -> add the value (decrypts with his key)
#   2. declare it here:
#        sops.secrets."<name>" = {
#          sopsFile = ./secrets/<name>.yaml;
#          owner = "<service-user>"; mode = "0400";
#        };
#   3. point the service at `config.sops.secrets."<name>".path`; drop the old
#      /var/lib/... file and its manual-drop note.
#
# Migration backlog (today's hand-dropped secrets):
#   restic/{password,b2-env}, vaultwarden/env, homepage/secrets.env,
#   grafana/admin_password, paperless/admin-password, litestream/b2.env,
#   gluetun/wg.env, mempool/{db,rpc}.env, aurral/decluttarr/media-curate env,
#   the Cloudflare ACME token, and (optionally) the Authelia secrets.
{ ... }:
{
  # Use gromit's SSH host key as the age identity for decryption at activation.
  sops.age.sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
}
