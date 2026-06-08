# Backup Architecture

Last updated: 2026-05-27

This document describes how data flows between **Gromit** (NixOS, primary
homelab at home) and **Bub** (Ubuntu 22.04, secondary Plex server at Rick's
house). It is the source of truth for "where does X live, and how do I get
it back?" Update it whenever a flow, retention policy, or credential
location changes.

## Hosts at a glance

| Host   | Role                                   | OS              | Tailscale IP    |
|--------|----------------------------------------|-----------------|-----------------|
| Gromit | Primary homelab, media, backups        | NixOS           | 100.82.117.116  |
| Bub    | Offsite Plex for Rick, offsite backup  | Ubuntu 22.04.5  | 100.112.10.93   |

SSH from mcp-server to bub uses ProxyJump through gromit. Gromit has `Host bub`
aliased in `~/.ssh/config`. See `reference_bub_access` in memory.

## Data classes and tiers

| Tier | What                                   | Where it lives                              |
|------|----------------------------------------|---------------------------------------------|
| 1    | Irreplaceable small state              | restic repos (encrypted, dedup'd)           |
| 2    | Media library — high-trust             | `/mnt/fusion` + mirrored to `/mnt/backup/all/media-mirror` + mirrored from bub to `/mnt/backup/all/rick-offsite` |
| 3    | Media — low-trust / regenerable        | `/mnt/fusion` only (no replica) — pinchflat archives, low-value movies |

Bub's local media (`/mnt/fusion` on bub) is treated as tier 2 from Gromit's
perspective: it is the **offsite copy of Rick's media library**, pulled into
`/mnt/backup/all/rick-offsite/` once a week.

## Flows

### Flow 1 — Gromit tier-1 → restic (local + B2)

Source: see `criticalPaths` in `modules/services/backup.nix` — includes
all small irreplaceable application state (Nextcloud, Audiobookshelf,
Jellyfin, Grafana, Tandoor, Homepage, **Forgejo** with its sqlite DB +
bare repos + LFS), plus the `/mnt/fusion/{Bitcoin,nextcloud,immich}`
external-storage trees and the irreplaceable bits of `/home/chris`.
Configured via NixOS `services.restic.backups.{critical-local, critical-b2}`.

```
gromit critical paths ──(02:30)──▶ /mnt/backup/all/restic        (local repo)
                       └─(03:00)──▶ b2:gromit-restic              (B2 repo)
```

- Both repos use the same passphrase at `/var/lib/restic/password`.
- B2 credentials: `/var/lib/restic/b2-env`.
- Retention: 7 daily / 4 weekly / 6 monthly.
- Snapshots tagged with `--host=gromit` (restic default).

### Flow 2 — Bub tier-1 → restic (gromit-local + B2)

Source: bub's `/etc`, `/home/chris`, `/root`, `/var/lib/plexmediaserver`,
`/var/spool/cron`. Configured via systemd timer + shell script on bub (see
`/tmp/bub-restic-setup/`).

```
bub critical paths ──(04:00)──▶ sftp://restic-push@gromit:/mnt/backup/all/restic   (same repo as Flow 1)
                    └─(04:00)──▶ b2:gromit-restic                                   (same repo as Flow 1)
```

- Both pushes go into the **same restic repos** Gromit uses. Snapshots are
  tagged `--host=bub --tag=bub-tier1` so they're easy to list separately:
    `restic -r /mnt/backup/all/restic snapshots --host bub`
- Same passphrase as Gromit's restic. Bub stores a copy at
  `/etc/bub-restic/password`.
- B2 credentials on bub: `/etc/bub-restic/b2-env` (same B2 bucket, same keys
  as gromit).
- Bub's SSH key for the SFTP push: `/etc/bub-restic/ssh-key` (dedicated
  ed25519, authorized for the dedicated `restic-push` user on gromit —
  *not* chris, see below).
- 04:00 nightly — runs **after** gromit's 02:30 local run so they don't
  race on the repo lock. Retention same as gromit.
- bub runs **restic 0.18.1** from the upstream GitHub release at
  `/usr/local/bin/restic`. Ubuntu 22.04's apt restic is 0.12.1, too old
  to read repo format v2 (compression-enabled) that gromit's 0.18.1
  created. The apt package is uninstalled on bub to avoid version
  confusion.

**Why a dedicated `restic-push` user, not chris** — the gromit-side
`/mnt/backup/all/restic` repo is `2770 root:restic` with default ACLs so
new files inherit the group. Adding chris to the supplementary `restic`
group would normally be enough, but mergerfs + FUSE `default_permissions`
on kernel 6.x doesn't honor supplementary groups for callers — only the
caller's *primary* group is checked. So a dedicated `restic-push` system
user (primary group=`restic`, shell=`bash`, SSH key forced to
`internal-sftp` via `restrict,command=...` in authorized_keys) is the
only thing that works.

### Flow 3 — Gromit tier-2 → media-mirror (rsync)

Configured via `modules/services/media-mirror.nix`.

```
/mnt/fusion ──(weekly Sun 04:00)──▶ /mnt/backup/all/media-mirror
                                    (rsync -a --delete, excludes restic repo)
```

- Same host, different pool: `fusion` (primary, mfs placement) →
  `backup/all` (epmfs placement).
- `--delete` is on. Anything removed from `/mnt/fusion` is removed from the
  mirror on the next run, so the mirror tracks tier-2 + tier-3 *promotions*
  but does not retain deleted content. (Tier 1's restic is the snapshot
  history.)
- The restic local repo at `/mnt/backup/all/restic` is **excluded** from
  this rsync — critical: a `--delete` mirror would otherwise wipe the
  encrypted repo if a path collision happened.

### Flow 4 — Bub tier-2 → rick-offsite (two-pass: link pass + copy pass)

Configured via `modules/services/bub-mirror.nix` on Gromit. Gromit pulls
from Bub, not the other way around (keeps SSH initiated from the trusted
side).

```
bub:/mnt/fusion ──(weekly Sun 06:00)──▶ /mnt/backup/all/rick-offsite
  Phase 1  bub-link-pass  — hardlink overlap, co-located on master's branch
  Phase 2  rsync --compare-dest=/mnt/backup/all — copy ONLY Rick-unique files
           (both phases under flock /run/lock/backup-pool.lock)
```

**Why two passes (and not the old `rsync --link-dest`):** the original
single `rsync --link-dest=/mnt/backup/all` *silently fell back to a full
copy* whenever mergerfs placed the rick-offsite copy on a **different
branch** than the master. Hardlinks cannot cross mergerfs branches
(`func.link=epall` + `link-exdev=passthrough` → the kernel returns `EXDEV`
→ rsync copies). A 2026-06-01 audit found ~16% of the overlap had been
duplicated this way (≈475 GB reclaimed by the fix). `category.create=epmfs`
does **not** prevent this — it's the *create* policy and has no effect on
where `link()` lands.

The fix splits the work so a wasteful copy is structurally impossible:

- **Phase 1 — link pass (`bub-link-pass.sh`).** `media-mirror.sh` dumps
  gromit's `/mnt/fusion` directly at the root of `/mnt/backup/all`, so
  `/mnt/backup/all/Movies/X.mkv` *is* gromit's mirror of that movie. The
  link pass fetches Rick's inventory and, for every Rick file matching a
  gromit master (path + size), creates the rick-offsite hardlink **directly
  on the master's physical branch** (`/mnt/backup/D?/...`, bypassing
  mergerfs `link()` entirely) — so `EXDEV` can never happen. Stray
  cross-branch copies from earlier runs are deleted and re-linked. This is
  metadata-only (no data moves), so it cannot stress the USB hub.
- **Phase 2 — copy pass.** `rsync --compare-dest=/mnt/backup/all` **skips**
  anything already in gromit's media-mirror (the overlap Phase 1 just
  hardlinked) and copies **only Rick-unique content** — it can no longer
  duplicate the overlap because it never considers those files. Tolerates
  rsync exit 23/24 (some of Rick's files are unreadable on his box).
- Practical effect unchanged & now reliable: any title in both libraries is
  a single inode shared between `/mnt/backup/all/Movies/X` and
  `/mnt/backup/all/rick-offsite/Movies/X`. Storage = max(library) +
  uniques(rick), not sum. Matching is **size-only** (immutable media).
- **Deletion semantics:** pruning gromit's media-mirror copy of a file just
  drops that inode's link count by one; if rick-offsite still links it, the
  data persists as rick-offsite's *sole, normal-file* link and Rick's backup
  stays intact. A library restore pulls from `/mnt/backup/all/Movies/` (not
  `rick-offsite/`), so a file pruned from the media-mirror won't reappear in
  the library.
- `bub-link-pass` is also a manual command:
  `sudo DRYRUN=1 bub-link-pass "TV Shows/Some Show"` previews the dedup.

### Flow 5 — Bub local Plex serves files

Bub continues running its own Plex against `/mnt/fusion` (bub-local).
Nothing in this architecture changes that — bub's Plex is independent of
the backup flows. Bub's `/mnt/fusion` is its own canonical store for
Rick's library.

## Storage math (approximate, 2026-05-27)

Gromit `/mnt/backup/all` is a 22 TB mergerfs pool over 4× 6 TB drives
(D1-D4). After tier-2 mirror it currently holds ~12 TB. Once bub-mirror
runs:

- gromit media-mirror: ~12 TB
- rick-offsite uniques (Rick's TV/photos/etc not in gromit's library): est.
  1-3 TB based on the 964-path drift audit (2026-05-27).
- shared via hardlinks: ~0 extra bytes (each shared file is one inode).
- restic repos (gromit + bub tier-1): <500 GB.
- Headroom for parity overhead and growth: 6-8 TB.

## Credentials inventory

All credential files are **root-only, mode 0600** except where noted.

| Path on host        | Host    | Purpose                                |
|---------------------|---------|----------------------------------------|
| `/var/lib/restic/password` | Gromit | restic repo passphrase (shared by both gromit repos) |
| `/var/lib/restic/b2-env`   | Gromit | B2_ACCOUNT_ID / B2_ACCOUNT_KEY         |
| `/root/.ssh/id_ed25519`    | Gromit | SSH key authorized on bub for bub-mirror pulls |
| `/var/lib/restic-push/.ssh/authorized_keys` | Gromit | SFTP-only key entry for bub's tier-1 push (locked with `restrict,command="internal-sftp"`) |
| `/etc/bub-restic/password` | Bub    | Same passphrase as gromit (offsite copy of the same key material) |
| `/etc/bub-restic/b2-env`   | Bub    | Same B2 creds as gromit                |
| `/etc/bub-restic/ssh-key`  | Bub    | Dedicated key authorized for restic-push@gromit |

The restic passphrase exists in three places (gromit, bub, plus the user's
password manager). Loss of all three = data is encrypted bricks. Verify
the passphrase is in the password manager before relying on this.

## Recovery procedures

### Restore tier 1 from gromit-local restic
```
sudo restic -r /mnt/backup/all/restic \
  --password-file /var/lib/restic/password \
  snapshots
sudo restic -r /mnt/backup/all/restic \
  --password-file /var/lib/restic/password \
  restore latest --host gromit --target /tmp/restore
```

### Restore tier 1 from B2 (gromit-side fire scenario)
Same commands, point at `b2:gromit-restic` and source the
`b2-env`:
```
sudo bash -c 'set -a; . /var/lib/restic/b2-env; restic -r b2:gromit-restic \
  --password-file /var/lib/restic/password snapshots'
```

### Restore tier 1 from bub's side (gromit dead)
On bub:
```
sudo bash -c 'set -a; . /etc/bub-restic/b2-env; restic -r b2:gromit-restic \
  --password-file /etc/bub-restic/password snapshots --host gromit'
```
Then `restore latest --host gromit --target /mnt/recovery`.

### Restore bub's tier-1 (bub failure scenario)
On gromit (bub's snapshots live in the gromit-local repo, tagged
`bub-tier1`):
```
sudo restic -r /mnt/backup/all/restic \
  --password-file /var/lib/restic/password \
  snapshots --host bub --tag bub-tier1
sudo restic -r /mnt/backup/all/restic \
  --password-file /var/lib/restic/password \
  restore latest --host bub --tag bub-tier1 --target /tmp/bub-restore
```

### Tier 2 (media) restore
Just `rsync` or `cp` from `/mnt/backup/all/media-mirror/` back to
`/mnt/fusion/`. No tooling beyond rsync needed.

### Tier 2 (Rick's media) restore to bub
From gromit, push back to bub:
```
sudo rsync -aH -e "ssh -i /root/.ssh/id_ed25519 -p 4089" \
  /mnt/backup/all/rick-offsite/ chris@100.112.10.93:/mnt/fusion/
```

## Operational notes

- **Verify hardlink dedup is actually working** after the first bub-mirror
  run: `stat` a file in both `/mnt/backup/all/media-mirror/...` and
  `/mnt/backup/all/rick-offsite/...` and confirm `Inode` matches. If not,
  the file landed on a different mergerfs branch and hardlinking silently
  failed — investigate epmfs placement.
- **Restic repo lock contention**: Gromit's local + B2 push runs at
  02:30/03:00, Bub's runs at 04:00. The gap is intentional. If a job
  overruns and locks the repo, the next will fail loudly via the
  `notify-failure@` template — that's the signal to investigate.
- **Bandwidth**: bub-mirror moves real bytes only for files not already in
  `/mnt/backup/all`. After steady state, weekly transfer should be modest
  (= Rick's new acquisitions). The first run will be large — schedule it
  when Rick's residential link is least loaded, or rate-limit with
  `--bwlimit`.
- **The `.pool-member` sentinel**: every backup pool drive holds a
  `.pool-member` file at its root. media-mirror and bub-mirror both
  preflight-check this. If a drive failed to mount, the bare mountpoint
  has no `.pool-member`, the job aborts, and a notification fires —
  preventing rsync from writing to the bare mountpoint and slowly filling
  the root filesystem.

## Pending flake changes (to apply in step 3)

The deployment for Flow 2 (bub tier-1) currently has some imperative
state on gromit that needs to be encoded in the flake so it survives
`nixos-rebuild`:

1. **`backup.nix` — declare the restic group and push user.** Roughly:
   ```nix
   users.groups.restic = {};
   users.users.restic-push = {
     isSystemUser = true;
     group = "restic";
     home = "/var/lib/restic-push";
     createHome = true;
     shell = pkgs.bashInteractive;
     openssh.authorizedKeys.keys = [
       "restrict,command=\"internal-sftp\" ssh-ed25519 AAAA...bub-restic@bub"
     ];
   };
   # Optional: add chris to restic group for manual `sg restic -c ...`
   # access. Not strictly required since restic-push handles automated push.
   users.users.chris.extraGroups = [ "restic" ];
   ```
2. **`backup.nix` — own the repo group + setgid + default ACL.** Currently
   set imperatively (`chgrp -R restic`, `chmod 2770`, `setfacl -R -d -m
   g:restic:rwX`). Encode via `systemd.tmpfiles.rules` or a one-shot
   `systemd.services.restic-repo-perms.script` that idempotently applies
   these on boot.
3. **`storage.nix`** — already updated: `category.create=epmfs` +
   `func.getattr=newest` on `/mnt/backup/all`. No more pending storage
   changes.
4. **`bub-mirror.nix`** — already drafted, needs to be added to
   `configuration.nix` imports.

Until step 3 lands, the imperative gromit setup persists because
`users.mutableUsers = true` is the NixOS default — but a future tightening
to `mutableUsers = false` would wipe the `restic-push` account.

## What's NOT in this architecture

- **Database hot-state captures**: PostgreSQL is dumped to
  `/var/backup/postgresql` nightly (separate from restic); that directory
  IS picked up by the restic critical paths.
- **Cloud-only services** (Bitwarden, GitHub, etc.): out of scope.
- **VM images / Proxmox state**: Gromit is not a Proxmox host. Broadlinc
  Proxmox is a separate environment with its own backup story (PBS).
- **Bub's pinchflat/arr/restic dirs**: deliberately excluded from
  bub-mirror — pinchflat is tier 3, arr is regenerable, bub's restic dir
  is empty (bub doesn't run restic locally, it pushes elsewhere).
