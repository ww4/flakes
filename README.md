# gromit — NixOS homelab configuration

The NixOS flake for **gromit**: a single mini-tower in rural Kentucky running
~50 services — media, photos, documents, a Git forge, a Bitcoin full node,
single-sign-on, monitoring, and 3-2-1 backups — entirely declaratively. It also
runs GNOME as a desktop. Public repo; lift anything useful.

> A longer, narrative tour (and the unusual part — an AI agent that does real ops
> work here without being able to break anything) lives in the companion docs
> repo: `showcase.md` and `code-audit-2026-06.md`.

## How changes ship (GitOps)

Nobody SSHes in to "just tweak something." The flow is:

1. Edit a module, open a **pull request**.
2. **[comin](https://github.com/nlewo/comin)** (a GitOps applier running on the
   box) polls `main` every ~60s and rebuilds when a PR is merged. **Merging is
   the human gate.**
3. For a live trial, push to the **`testing`** branch instead — comin applies it
   with `nixos-rebuild test` (ephemeral, auto-reverts on reboot). Promote to
   `main` when happy.

The repo is hosted on a self-hosted **Forgejo**, mirrored to GitHub, which comin
pulls — so the whole loop closes on gromit's own hardware.

Manual rebuild (fresh box / bootstrap):

```bash
sudo nixos-rebuild switch --flake .#gromit
```

## Layout

Each concern is exactly one file — one import line, one greppable name.

```
flake.nix                  inputs (nixpkgs, home-manager, comin, sops-nix, vscode-server) + the gromit host
configuration.nix          the module manifest — just the imports list
hardware-configuration.nix generated hardware scan (machine-specific)
.sops.yaml                 sops recipients (gromit host key + admin age key)
secrets/                   sops-encrypted secrets — safe to commit (ciphertext; keys aren't in the repo)
modules/
  boot, storage, networking, desktop, users, system,
  packages, virtualisation, home-manager, sops          base system
  agent/                                                 the scoped, non-root Claude agent (see agent/README.md)
    claude-user, sudo, comin, claude-harness, digest
  services/                                              ~40 per-service modules (catalog below)
home/                                                    Home-Manager user config (shell, git, packages, vscode)
```

## Service catalog

| Area | Services |
|------|----------|
| **Media** | Jellyfin, Audiobookshelf, Immich (photos), the *arr stack (Prowlarr/Sonarr/Radarr/Lidarr/LazyLibrarian), Jellyseerr, Aurral, Recyclarr, Decluttarr, qBittorrent (via Gluetun VPN), MeTube, Pinchflat, Tandoor |
| **Cloud & productivity** | Nextcloud, Paperless-ngx, Vaultwarden, Forgejo |
| **Bitcoin** | bitcoind (full node), Fulcrum (Electrum server), mempool.space, Alby Hub (Lightning) |
| **Platform** | nginx (Tailscale/LAN source-gate), Authelia (SSO: forward-auth + OIDC), Homepage dashboard, Prometheus + Grafana + Alertmanager, Glances, Uptime-Kuma, ntfy, Riverwatch (a creek-gauge exporter) |
| **Storage & backup** | mergerfs (two pools), pool-autoremount (self-healing), SnapRAID, restic (local + B2), Litestream, bub-mirror |
| **Remote / misc** | VS Code remote server, RDP remote desktop, GYB (Gmail backup) |

## Conventions worth knowing

- **Secrets: [sops-nix](https://github.com/Mic92/sops-nix).** Every credential is
  encrypted *in this repo* under `secrets/` and decrypted at activation with
  gromit's SSH **host** key — so the config is self-contained and nothing
  sensitive is ever in plaintext on disk. Edit a value with `sops
  secrets/<name>.yaml` (needs the admin age key); the agent can wire the plumbing
  but can't read the values. See `modules/sops.nix`.
- **Two package lanes.** `modules/packages.nix` is deliberately minimal —
  root-PATH admin tools only. Personal/GUI apps live in `home/packages.nix` and
  land only in chris's user profile.
- **Backups.** restic to a local mergerfs pool repo **and** offsite Backblaze B2,
  plus a guarded weekly media mirror; status/alerts via self-hosted ntfy. Postgres
  dumps are per-DB (`postgresqlBackup.databases = [...]`) not `pg_dumpall`, so one
  bad DB can't break the whole chain. Full design in `BACKUP-ARCHITECTURE.md`.
- **Home Manager** is integrated as a NixOS module (one `nixos-rebuild switch`
  does system + user). `backupFileExtension = "hm-backup"` means the first switch
  to manage a pre-existing dotfile renames the original rather than clobbering it
  — inspect and delete the `*.hm-backup` files once you're happy with HM's version.
- **Network posture.** All vhosts are reachable over Tailscale only; an nginx
  source-gate (`modules/services/nginx-access.nix`) is the perimeter, with SSO in
  front of the apps that support it.

## A note for anyone forking this

It's a living homelab, not a turnkey template. Disk layout, the domain
(`rosemaryacres.com`), the Tailscale IP, and the `chris` user are gromit-specific
and currently hardcoded; a few services need a documented manual bootstrap (admin
users, API keys, the B2 bucket); and `secrets/` only decrypts on a host whose key
is a recipient in `.sops.yaml`. The `code-audit-2026-06.md` in the docs repo is
candid about all of this and tracks the path to a cleaner, more liftable layout.
