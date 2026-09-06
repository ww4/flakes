# Scoped `claude` agent access on WALLACE.
#
# Deliberately NOT an import of ../../modules/agent/claude-user.nix: that module
# puts the agent in the `media` group, and `users.groups.media` is declared in
# modules/users.nix, which wallace does not import. Importing it wholesale fails
# to build. It also installs the claude-code package, which wallace has no use
# for — the agent RUNS on gromit and only reaches wallace over SSH.
#
# Why this exists: on 2026-09-06 wallace had been sitting in Windows since
# 2026-09-03 with no way for the agent to correct it. The EFI boot order can only
# be made durable FROM LINUX (Windows rewrites it on every boot), and the agent
# had no login on wallace's NixOS side — the old `id_dadpc_tmp` recon key was
# deliberately revoked on 2026-08-17 and is on the standing revoke list. So the
# one machine that could fix the problem was the one nobody could reach.
{ config, lib, pkgs, ... }:

{
  users.users.claude = {
    isNormalUser = true;
    description = "Claude Code agent (scoped, non-root) — remote ops from gromit";
    home = "/home/claude";
    shell = pkgs.bashInteractive;

    # Read-only diagnostics without sudo. Deliberately NOT `wheel`, NOT `docker`
    # — both are root-equivalent. This key grants an unprivileged shell plus the
    # journal, and nothing else beyond the one sudo rule below.
    extraGroups = [ "systemd-journal" ];

    # Generated on gromit 2026-09-06 for this purpose alone; the private half
    # lives in the agent's ~/.ssh on gromit and is not in this repo. A public key
    # is safe in the world-readable nix store.
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH+GmYPPfCNxXgVdxgaqutwpkWVom4iFN5tr5033MXJp claude-gromit->wallace-20260906"
    ];
  };

  # ONE privileged capability, matching the house rules in modules/agent/sudo.nix:
  # exact commands, no wildcards on dangerous verbs.
  #
  #   `efibootmgr`         bare — read the current entries and order
  #   `efibootmgr -o *`    REORDER only
  #
  # Notably absent: `-b`/`-B` (delete an entry) and `-c` (create one). Reordering
  # is fully reversible and cannot orphan the Windows entry; deletion is not, and
  # a mistake there is a machine that boots nothing.
  security.sudo.extraRules = [
    {
      users = [ "claude" ];
      commands = [
        { command = "${pkgs.efibootmgr}/bin/efibootmgr";      options = [ "NOPASSWD" ]; }
        { command = "${pkgs.efibootmgr}/bin/efibootmgr -o *"; options = [ "NOPASSWD" ]; }
      ];
    }
  ];
}
