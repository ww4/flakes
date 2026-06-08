# Scoped sudoers for the `claude` agent (STAGED / INERT — not imported).
#
# Replaces "NOPASSWD: ALL" with a SHORT, EXPLICIT allowlist of safe, routine,
# reversible operations the agent may do unattended. Everything else (rebuilds,
# rm, disk ops, user/secret changes) is NOT here → needs Chris (rebuilds flow
# through comin + a PR merge instead). See ./README.md.
#
# Rules:
#   - List exact commands. NO wildcards on dangerous verbs.
#   - No `nixos-rebuild` (comin owns applying config).
#   - No `rm`, no `dd`, no `mkfs`, no `userdel`, no editing /var/lib/*/secrets.
#   - Prefer read + restart of known-good services + project CLIs.
{ config, lib, pkgs, ... }:

let
  sw = "/run/current-system/sw/bin";
  nopw = [ "NOPASSWD" ];
in
{
  security.sudo.extraRules = [
    {
      users = [ "claude" ];
      commands = [
        # --- Read-only inspection of service-owned state ---
        { command = "${sw}/systemctl status *";            options = nopw; }
        { command = "${sw}/systemctl is-active *";          options = nopw; }
        { command = "${sw}/systemctl is-failed *";          options = nopw; }

        # --- Routine, reversible ops the agent may do alone ---
        { command = "${sw}/media-mirror";                   options = nopw ++ [ "SETENV" ]; }
        { command = "${sw}/systemctl reset-failed *";       options = nopw; }
        # Restart ONLY these known services (extend explicitly; never wildcard):
        { command = "${sw}/systemctl restart vaultwarden";          options = nopw; }
        { command = "${sw}/systemctl restart media-mirror-sync";    options = nopw; }
        { command = "${sw}/systemctl start media-mirror-sync";      options = nopw; }

        # NOTE: for reading specific root/service-owned files (e.g. the
        # vaultwarden sqlite DB), prefer a tiny purpose wrapper in /etc that the
        # agent may run, rather than `sudo cat *` (which leaks every secret).
      ];
    }
  ];

  # Audit: keep a record of what the agent ran as root.
  security.sudo.extraConfig = ''
    Defaults:claude log_output, logfile="/var/log/sudo-claude.log", !syslog
  '';
}
