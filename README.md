# Flakes — Gromit NixOS configuration

The NixOS flake for **Gromit**, a GNOME workstation that doubles as a homelab
server. Public repo — feel free to copy anything useful.

## Layout

```
flake.nix                  inputs (nixpkgs, vscode-server, home-manager) + the gromit host
configuration.nix          module manifest — just the imports list
hardware-configuration.nix generated hardware scan
modules/
  boot, storage, networking, desktop, users, system,
  packages, virtualisation                         base system
  home-manager                                     HM wiring (user config lives in ../home/)
  services/
    jellyfin, audiobookshelf, tandoor, pinchflat,
    bitcoind, immich, vscode-server, nextcloud,
    backup, notifications, media-mirror,
    gyb                                            per-service config
home/
  default.nix              composes the user-level modules below
  shell.nix                bash, aliases, history, prompt
  git.nix                  global git identity + config
  packages.nix             user-level packages (GUI apps, personal CLI tools)
  vscode.nix               programs.vscode (mutable extensions, declarative seam)
```

Each concern is one file. To try something out: add a module under `modules/`
or `home/`, add one import to `configuration.nix` or `home/default.nix`,
`nixos-rebuild test`, and `git revert` if it doesn't work out.

System packages (`modules/packages.nix`) are deliberately minimal — root-PATH
admin tools only. Personal apps live in `home/packages.nix` and only end up
in chris's user profile.

## Rebuild

```bash
cd ~/code/flakes
sudo nixos-rebuild switch --flake .#gromit
```

## Notes

- Backups: restic to a local pool repo + offsite Backblaze B2; a guarded
  weekly media mirror; status/alerts via self-hosted ntfy.
- Postgres backups are per-DB (`postgresqlBackup.databases = [...]`) rather
  than `pg_dumpall` — one DB failing doesn't take the chain down with it.
- Secrets are kept out of this repo — in root-only files on the host.
- Home Manager is integrated as a NixOS module (one `nixos-rebuild switch`
  handles system + user). `backupFileExtension = "hm-backup"` means the
  first switch that manages a pre-existing dotfile renames the original
  rather than clobbering it — inspect `*.hm-backup` files after activation
  and delete them once you're satisfied with HM's version.
