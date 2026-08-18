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
        # ...plus the CONTAINERIZED services as a class (added 2026-07-27 after
        # THREE same-day diagnoses each stalled on a container restart the
        # agent couldn't perform: qbittorrent s6 init-hang ×2, jellyseerr
        # wedged-DNS discriminating test). Scoped to the docker-* unit prefix
        # — restart-only (no stop/start: a restart can't leave something
        # down), containers only (every one is supervised + reversible), no
        # new read powers. The bare-name rule can't be abused for non-docker
        # units since the prefix is fixed.
        { command = "${sw}/systemctl restart docker-*";             options = nopw; }
        # Restart the two bitcoind CREDENTIAL-STAGING units (added 2026-08-09
        # after the mempool cookie-race diagnosis: the agent found the root
        # cause — both units staged a stale bitcoind cookie and had been
        # 401ing for 17 days — but could not apply the one-line fix, and Chris
        # was away from a terminal. Restart-only, two exact unit names, no
        # wildcard. Both are stateless credential re-stagers: they re-read
        # bitcoind's .cookie and re-probe until it authenticates, so a restart
        # can only ever refresh a credential, never destroy data or leave
        # something down. Deliberately NOT bitcoind itself — restarting the
        # node risks a multi-hour reindex (see the 2026-06-11 incident).
        { command = "${sw}/systemctl restart fulcrum";              options = nopw; }
        { command = "${sw}/systemctl restart mempool-cookie-sync";  options = nopw; }

        # Hardlink completed downloads into the Jellyfin library. Needs root only
        # because fs.protected_hardlinks=1 forbids linking a file the agent
        # neither owns nor can write (the download tree is chris:users 0644). A
        # fixed-purpose wrapper, NOT raw ln: it only ever CREATES hardlinks under
        # /mnt/fusion/{Movies,TV Shows}, never deletes or overwrites, and
        # validates the entire plan before acting. See services/media-link.nix.
        { command = "${sw}/media-link";                             options = nopw; }
        { command = "${sw}/media-link *";                           options = nopw; }

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

        # --- Field network diagnostics on marcus (added 2026-08-18) ---
        # Evidence: the Craigmyle Tractor camera outage. The agent diagnosed a
        # stale VLAN handing out a parallel 192.168.128.0/24 on the same wire,
        # but could not do three things unaided — add a temporary address to
        # reach that subnet, run a DHCP DISCOVER, or capture a single packet —
        # so Chris had to be its hands for each. Finding the rogue subnet at
        # all came down to luck (Amcrest broadcast their IP config over mDNS;
        # the site's Dahua and Hikvision cameras advertise nothing).
        #
        # NOT raw tcpdump/nmap/ip. `netdiag-priv` is a fixed-purpose wrapper
        # with a closed vocabulary: interface names and CIDRs are regex- and
        # range-validated, capture filters are selected from a named profile
        # list rather than passed in, tcpdump never receives -w/-W/-z (file
        # writes and its postrotate exec hook), captures are snaplength-limited
        # to headers so a client's payload traffic is never collected, and
        # temporary addresses carry a kernel lifetime so they expire on their
        # own instead of lingering on someone else's network. See
        # hosts/marcus/netdiag-priv.sh — it is the only privileged entry point
        # of the toolkit, which keeps the audit surface to one file.
        # Inert on gromit (netdiag is a marcus-only module).
        { command = "${sw}/netdiag-priv";                    options = nopw; }
        { command = "${sw}/netdiag-priv *";                  options = nopw; }

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
