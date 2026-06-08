# Agent access structure — Claude on gromit (STAGED / INERT)

Design for letting an AI agent (Claude Code) live permanently on gromit with
*enough* access to be useful, but with consequential changes **gated on Chris's
say-so** — not standing root. Drafted 2026-06-07. See memory `[[gromit-security-review]]`.

**Nothing here is active until you import the modules in `configuration.nix` and
add the comin flake input.** These files have zero effect as-is.

## Principle: three buckets, gated differently

| What the agent does | Gate |
|---|---|
| Read / diagnose (most of the time) | standing, **read-only** (journal group + a few scoped read cmds) |
| Propose changes (edit the flake) | standing — produces a git branch/PR, applies nothing |
| **Apply** (rebuild, restart, delete) | **your approval** — via PR merge (declarative) or a tiny sudo allowlist (safe imperative ops) |

The old `NOPASSWD: ALL` collapsed all three into one. This splits them.

## The pieces (this directory)

- **`claude-user.nix`** — dedicated `claude` system user. Bounds blast radius;
  does NOT inherit chris's keys/wallets/GUI. Read access via the `systemd-journal`
  group. Carries the agent's SSH key (the agent connects as `claude@`, not root/chris).
- **`sudo.nix`** — scoped sudoers: passwordless for a SHORT explicit allowlist of
  safe ops only (restart specific services, run media-mirror). No `rm`, no
  `nixos-rebuild` (comin owns that), no wildcards.
- **`comin.nix`** — the GitOps applier. Polls the flake repo; `nixos-rebuild test`
  on the `testing` branch (so the agent can self-validate ephemerally), full
  `switch` only when a commit reaches **`main`**. **You merging the PR is the gate.**
- **`claude-settings.proposed.json`** + **`pretooluse-guard.sh`** — Claude Code
  harness config: allowlist read-only tools, route writes to git, deny destructive
  ops at the harness layer (belt-and-suspenders above the OS).

## How a change flows once active

1. Agent (as `claude`) edits the flake, pushes a **branch**, opens a PR.
2. Optionally pushes to `testing` → comin runs `nixos-rebuild test` (ephemeral) so
   the agent can verify it builds/works without persisting.
3. **You review the PR diff on the GitHub/Forgejo mobile app and merge to `main`.**
4. comin sees `main` advanced → `nixos-rebuild switch`. Revertible via git +
   NixOS generation. The agent never held root for any of it.

## Activation checklist (at a keyboard)

1. Add comin as a flake input (`flake.nix`): `comin.url = "github:nlewo/comin";`
   and import its module; verify option names with `nixos-option services.comin`.
2. Set the repo URL in `comin.nix`. If the flake repo is private, add a read token
   file for comin.
3. **Protect `main`** on the flake remote (require PR + your review). THIS is the
   approval gate — without branch protection the model is just a suggestion.
4. Generate an SSH key for `claude` for git push; add as a deploy key (write to
   branches, NOT a path around main protection).
5. Move the agent's authorized key from root/chris onto `claude` (in
   `claude-user.nix`); point the agent's connection at `claude@100.82.117.116`.
6. Import the three `.nix` modules in `configuration.nix`; `nixos-rebuild test`.
7. Install `claude-settings.proposed.json` into the agent's Claude Code config and
   wire the hook; confirm reads are allowed and writes/destructive are gated.
8. Tighten the `sudo.nix` allowlist to the exact ops you want me to do unattended.
9. Once verified, you can drop chris's `NOPASSWD: ALL` (security review Tier 2).

## What to verify (don't trust this blindly)
- Exact `services.comin` option schema (versions differ) — `nixos-option`.
- sudoers command paths (`/run/current-system/sw/bin/...`) resolve on this system.
- The Claude Code permission-rule + hook JSON format against current docs.
