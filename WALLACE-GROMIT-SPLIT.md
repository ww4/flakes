# Wallace & Gromit — the two-host split

Two NixOS hosts, one flake (`nixosConfigurations.{gromit,wallace}`). Named for
the claymation duo: **Gromit** quietly holds everything together (the data);
**Wallace** runs the heavy contraptions (the compute). Wallace does **not**
replace Gromit — it offloads CPU/GPU-bound work.

## The hardware reality (what drives the split)

| | Gromit | Wallace |
|---|---|---|
| CPU | aging | **Ryzen 9 5900X** (12c/24t) |
| RAM | — | 31 GB |
| GPU | none | **AMD RX 580 8 GB** (amdgpu) |
| Storage | **29 TB mergerfs** (fusion) + backup pool — *all the data* | SSD/NVMe only (no media pool **yet**) |
| Role | storage + stateful services | compute / offload |

**Hard constraint:** the media drives can't move right now, and Wallace has only
**3× 3.5" bays**. So the split is staged: compute first (no data move), storage
later (when a few drives migrate).

## Principle

> Data-resident services stay on **Gromit** (the disks live there). CPU/GPU/RAM
> heavy work moves to **Wallace**, reaching Gromit's data over the LAN (NFS/Tailscale)
> when it needs to.

## Phase 1 — now (no drives moved): Wallace = compute offload

Highest-value, lowest-risk first:

1. **Remote Nix build farm.** Add Wallace to Gromit's `nix.buildMachines` (or as a
   `--build-host`). The 5900X dwarfs Gromit's CPU, so `nixos-rebuild`/comin builds
   run on Wallace and copy back. No data needed, big wall-clock win. ⭐ *do first*
2. **Immich machine-learning.** Keep the Immich server + DB + photos on Gromit;
   point `IMMICH_MACHINE_LEARNING_URL` at a GPU-accelerated ML container on
   Wallace. Face/CLIP/object detection (the slow part) runs on the RX 580. ⭐
3. **Local LLM / AI.** The long-planned "talk to my books" RAG generation + the
   archive semantic-search embedding/indexing, on the RX 580 (llama.cpp Vulkan/
   ROCm) with 31 GB RAM. Retrieval is already built; Wallace is the missing GPU. ⭐
4. **Batch + hardware transcoding.** The WALLACE-ISO→Jellyfin HandBrake jobs, and
   Jellyfin transcode offload via the RX 580 (VAAPI/VCN H.264/H.265). Reads media
   from Gromit over NFS; writes back. Classic heavy load, ideal for the GPU.
5. **Heavy one-offs / Stable Diffusion** (optional, GPU) as wanted.

Everything else **stays on Gromit**: the mergerfs pools + all backups, the *arr
stack (needs the library + downloads), bitcoind/Fulcrum/mempool (chain data),
Nextcloud/Paperless/Vaultwarden/Forgejo/Jellyfin servers, Homepage, monitoring,
Authelia/SSO, the scoped agent + comin hub.

## Phase 2 — when drives move into Wallace's 3× 3.5" bays

The big win: **move the unstable USB-enclosure backup drives into Wallace's
internal SATA bays.** That kills the USB drop-off/over-current problem (the
`pool-autoremount` self-heal and the enclosure over-current incidents) by making
them reliable internal disks — and gives true hardware separation for 3-2-1:

- **Wallace hosts the backup pool** (restic target + media-mirror destination);
  Gromit replicates to it over the LAN. Primary data on Gromit, local backup on
  *different hardware* (Wallace) — a real second copy, not same-box.
- With only 3 bays, prioritize the backup-pool drives. (A future HBA/more bays
  could later let Wallace take a media or bitcoind role too.)

## Cross-cutting

- **Network:** both on Tailscale; NFS over the LAN (or Tailscale) for Wallace→Gromit
  media/data access. Same nginx source-gate posture.
- **Observability:** Wallace runs node_exporter, scraped by Gromit's Prometheus;
  same Grafana, same ntfy alerts, same quiet-hours.
- **GitOps:** Wallace runs **comin** too (pulls this repo, builds `.#wallace`) so
  both hosts are managed from one flake via merged PRs — same model as Gromit.
- **SSO + agent:** Wallace behind Authelia where it exposes web UIs; the scoped
  agent manages it the same way (SSH + nixos-rebuild through the PR flow).

## Status

- ✅ Wallace installed (NixOS, dual-boot with Windows), folded into this flake as
  the second host (bootstrap config).
- ⏭️ Next, in order: (1) remote Nix builder, (2) comin on Wallace, (3) Immich ML
  offload, (4) local LLM, (5) transcode offload. Then Phase 2 when drives move.
