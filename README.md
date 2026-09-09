# flakes — the NixOS fleet configuration

One flake, three hosts:

| Host | Role |
|---|---|
| **gromit** | Homelab server: storage pools, ~50 services, monitoring, the GitOps hub. Also a KDE Plasma desktop. |
| **wallace** | Compute node (Ryzen 9 5900X / RX 580): remote Nix builds, Immich machine learning. |
| **marcus** | ThinkPad T480 laptop, intermittently online. |

Most service and infrastructure modules live in a separate public library,
[homelab-modules](https://git.rosemaryacres.com/ww4/homelab-modules), consumed
as a flake input. This repo holds what is specific to these machines:

- **`modules/homelab-values.nix`** — the values the library modules read:
  domain, admin user, pool definitions, ntfy topic, per-service settings, and
  the sops secret declarations. If you are looking for "where is X
  configured", start here.
- **`configuration.nix`** — gromit's module manifest. Each import is one line:
  `hm.<name>` pulls a module from the library; `./modules/...` is local.
- **`hosts/`** — wallace and marcus.
- **`modules/`** — what stays local: hardware-specific modules, the scoped
  agent (`modules/agent/`), and services not in the library (the dashboard,
  the Bitcoin stack, backups, and personal one-offs).
- **`secrets/`** — sops-encrypted secrets. Safe to commit: ciphertext only,
  encrypted to gromit's SSH host key and the admin age key. See
  `secrets/README.md`.

## How changes ship (GitOps)

Nobody SSHes in to make changes. The flow is:

1. Edit, open a **pull request** on Forgejo.
2. **[comin](https://github.com/nlewo/comin)** runs on each host, polls `main`
   about every 60 seconds, and rebuilds when a PR is merged. Merging is the
   human gate — `main` is branch-protected.
3. For a live trial, push to the **`testing`** branch instead. comin applies
   it with `nixos-rebuild test`: real, but reverts on reboot. Promote to
   `main` when satisfied.

comin polls two remotes: Forgejo (the source of truth) and the GitHub
push-mirror. The mirror is normally passive; it exists so deploys survive a
Forgejo outage, and so that in a disaster a direct push to GitHub `main` can
still drive the fleet. A timer (`mirror-drift-watch`, from the library)
alerts if the mirror stops tracking Forgejo.

Manual rebuild (bootstrap or recovery):

```bash
sudo nixos-rebuild switch --flake .#gromit    # or #wallace, #marcus
```

## Conventions

- **One concern, one file, one import line.** The manifest reads top to
  bottom as a description of the machine.
- **Secrets: [sops-nix](https://github.com/Mic92/sops-nix).** Values are
  encrypted in this repo and decrypted at activation with the host's SSH key.
  The agent can wire a secret's plumbing but cannot read its value; editing
  needs the admin age key.
- **Two package lanes.** `modules/packages.nix` holds root-PATH admin tools
  only; personal and GUI apps live in `home/packages.nix`.
- **Network posture.** Every vhost sits behind the nginx source gate (library
  module `nginx-access`): reachable over Tailscale and the trusted LAN,
  denied from everywhere else. SSO (Authelia) in front of the apps that
  support it.
- **Backups.** restic to a local pool and offsite Backblaze B2, plus mirror
  jobs for media. Full design in `BACKUP-ARCHITECTURE.md`.

## The scoped agent

A Claude agent has its own Unix user on gromit and does routine operations
work through the same PR gate as everyone else. It cannot apply changes, hold
root, or read secrets. See `modules/agent/README.md`.

## For anyone reading this from outside

The reusable parts are in
[homelab-modules](https://git.rosemaryacres.com/ww4/homelab-modules)
(mirrored to [GitHub](https://github.com/ww4/homelab-modules)) — an
option-driven module library you can consume directly. This repo is one
consumer of it: a values file, hardware scans, and local modules. It is not a
template; it is an example of the pattern.
