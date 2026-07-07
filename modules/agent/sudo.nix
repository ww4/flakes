# Scoped sudoers for the `claude` agent (LIVE — imported by configuration.nix
# since the agent-activation PRs; this header used to say "STAGED/INERT").
#
# A SHORT, EXPLICIT allowlist of safe, routine, reversible operations the
# agent may do unattended. Everything else (rebuilds,
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

        # DNS validation: non-disruptive DHCP DISCOVER (takes no lease) to see
        # what the router hands clients as DNS. A fixed-purpose wrapper, NOT raw
        # nmap. Provided by services/blocky.nix.
        { command = "${sw}/dhcp-probe";                             options = nopw; }

        # --- Read-only container + firewall diagnostics (added 2026-07-06) ---
        # Evidence-driven (Chris asked the agent to request capabilities rather
        # than work around their absence): the jellyseerr ETIMEDOUT diagnosis
        # (#73) and the mempool bring-up both needed container state + firewall
        # rules; the agent got there via config-reading — slower, weaker
        # evidence. `docker inspect`/`exec` are deliberately NOT here: inspect
        # prints container env vars, which include secrets (gluetun WG key,
        # mempool DB creds); exec is arbitrary execution.
        # (sudoers gotcha: "cmd *" does NOT match a bare "cmd" — list both.)
        { command = "${sw}/docker ps";                      options = nopw; }
        { command = "${sw}/docker ps *";                    options = nopw; }
        { command = "${sw}/docker logs *";                  options = nopw; }
        { command = "${sw}/iptables-save";                  options = nopw; }

        # Backup verification via a read-only purpose wrapper (the pattern the
        # NOTE below recommends) — never raw `sudo restic` (could forget/prune).
        # Lets the agent confirm "did last night's snapshot include X?" itself
        # (first use: verifying the PR #74 paths). See ./agent-restic-ro.sh.
        { command = "/etc/claude-code/agent-restic-ro.sh";   options = nopw; }
        { command = "/etc/claude-code/agent-restic-ro.sh *"; options = nopw; }

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
