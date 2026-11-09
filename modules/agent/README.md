# The scoped agent — live since June 2026

A Claude agent (Claude Code) runs permanently on gromit as the `claude` user
and does routine operations work: wiring services, extending monitoring,
migrating secrets' plumbing, keeping documentation current. This directory is
its access model.

## Three kinds of action, gated differently

| What the agent does | Gate |
|---|---|
| Read and diagnose (most of the time) | Standing, read-only: journal group + scoped read commands |
| Propose changes (edit the flake) | Standing: produces a branch and a PR, applies nothing |
| Apply (rebuild, restart, delete) | Chris's approval — a PR merge, or a short sudo allowlist for safe imperative ops |

The middle row is the important one: the agent's normal output is a diff, not
an effect.

## Files here

- **`claude-user.nix`** — the dedicated `claude` system user. It does not
  inherit chris's keys, wallets, or desktop session. Read access comes from
  the `systemd-journal` group.
- **`sudo.nix`** — the sudo allowlist: exact commands only, no wildcards on
  dangerous verbs, no `rm`, no `nixos-rebuild` (comin owns rebuilds). Where
  the agent needs a privileged capability, it gets a fixed-purpose wrapper
  with a closed vocabulary (`smart-dump`, `netdiag-priv`,
  `agent-restic-ro.sh`) — never the underlying tool. Each entry's comment
  states what it allows and why.
- **`comin.nix`** — the GitOps applier, shared by all three hosts. It polls
  Forgejo `main` (plus the GitHub mirror as a fallback remote) and rebuilds
  on merge. Chris merging the PR is the human-in-the-loop.
- **`claude-agent-profile.nix`** — the Claude Code harness (command guard,
  hooks, managed settings), consumed from the shared `agent-modules` flake so
  this host and the work-side agent host run one definition. The guard is
  root-owned at mode 0555 in `/etc/claude-code/`; the agent cannot edit it.
- **`digest.nix`, `daybook.nix`, `claude-config-sync.nix`** — scheduled
  headless runs: a weekly status digest, twice-daily planning notes, and an
  hourly pull of the shared global config.
- **`*-secret.nix`** — sops declarations for API keys the agent uses
  (Sonarr/Radarr/Prowlarr, Jellyfin, Cloudflare for lock3, Discourse,
  DigitalOcean, Open WebUI). The agent reads these; it cannot read any other
  secret.

## How a change flows

1. The agent edits its own clone, pushes a branch, opens a PR on Forgejo.
2. Optionally it pushes to `testing` first; comin applies that with
   `nixos-rebuild test` so the change can be verified live and reverts on
   reboot.
3. Chris reviews the diff and merges to `main`.
4. comin sees `main` advance and runs `nixos-rebuild switch`. Both git and
   the NixOS generation list can roll it back.

The agent never holds root at any point in that flow. Docs-repo PRs are the
exception to the review gate: Chris opted out of reviewing documentation, so
the agent self-merges those via the ww4-bot API.

## Boundaries worth restating

- The agent cannot grant itself new powers: the harness is root-owned and the
  managed-settings tier rejects hooks and permissions from anywhere else.
  Widening its access takes a PR like any other change.
- It is not a sops recipient. It wires `sops.secrets.<name>` plumbing but
  cannot decrypt values.
- Every sudo invocation is logged to `/var/log/sudo-claude.log`.
