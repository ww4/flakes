# Flakes — Gromit NixOS configuration

The NixOS flake for **Gromit**, a GNOME workstation that doubles as a homelab
server. Public repo — feel free to copy anything useful.

## Layout

```
flake.nix                  inputs (nixpkgs, vscode-server) + the gromit host
configuration.nix          module manifest — just the imports list
hardware-configuration.nix generated hardware scan
modules/
  boot, storage, networking, desktop, users, system,
  packages, virtualisation                         base system
  services/
    jellyfin, audiobookshelf, tandoor, pinchflat,
    bitcoind, immich, vscode-server, nextcloud,
    backup, notifications, media-mirror            per-service config
```

Each concern is one file. To try something out: add a module under `modules/`,
add one import to `configuration.nix`, `nixos-rebuild test`, and `git revert`
if it doesn't work out.

## Rebuild

```bash
cd ~/code/flakes
sudo nixos-rebuild switch --flake .#gromit
```

## Notes

- Backups: restic to a local pool repo + offsite Backblaze B2; a guarded
  weekly media mirror; status/alerts via self-hosted ntfy.
- Secrets are kept out of this repo — in root-only files on the host.
