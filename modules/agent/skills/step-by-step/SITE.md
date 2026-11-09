# SITE.md — the homelab (gromit, wallace, lock3)

Facts for this site. `SKILL.md` beside this file is the shared method; this
layer wins on facts, the core wins on method.

## Phone or keyboard

**Default to PHONE.** Chris confirmed 2026-11-03: at home he is *mostly on a
phone*. This is the OPPOSITE of the Broadlinc pattern, where weekday working
hours mean a work PC — do not carry that rule across.

So unless he has said otherwise in this conversation:

- GUI steps, or **at most one short command**
- no SSH sessions, no multi-line pastes, no heredocs, no `umask`/brace groups
- a secret goes browser tab to browser tab, or waits for a keyboard
- PR review and merge are fine on a phone (see Merging)

Still confirm in one line when it matters: "I'm assuming phone — right?"

⚠️ Evidence this is not theoretical: on 2026-11-02 he was handed a four-line
block with `install -d`, `umask 077` and two brace groups redirecting to root
paths, to run on a VPS. On a phone that is a bad shape, and it also failed for
an unrelated reason (below).

## Terminals, and the `!` rule

**Never write a `!` command. Full stop.** Chris confirmed 2026-11-03 that he is
*never* inside the agent's own Claude session or a herdr pane — he always uses a
separate terminal. So the core rule's "ask first" collapses to "don't" here.
`!` would only ever run on gromit anyway, and in bash it negates exit status and
silently breaks `&&` chains.

Where he actually is:

| target | how he reaches it | notes |
|---|---|---|
| gromit | his own shell as `chris` | sudo prompts for a password |
| lock3 (VPS) | `ssh -p 22022` as `chris` or `root` | ⚠️ **port 22022, not 22** |
| wallace | ssh | |

## Consoles that cannot paste

- **lock3's Spaceship panel web console** — the break-glass path, deliberately
  kept working because the box became undiagnosable twice when it was key-only
  with no console password. It cannot paste: give short typeable lines, and get
  SSH working first.
- **gromit's VGA console** — see the `vga-console-ui-traps` memory: DarkGray is
  invisible and non-ASCII renders as `?`.

## Check the target: known gaps on these hosts

Do not assume a tool exists. Measured 2026-11-02:

| host | missing |
|---|---|
| lock3 | **no `openssl`**, **no `python3`**, no `jq` |
| gromit | **no `traceroute`** (use `tracepath`); see `no-python3-on-gromit` |

⚠️ This is the top failure category in `SKILL.md`, and it has already bitten
here twice in one evening:

- `openssl rand -hex 32` was handed to Chris for lock3, which has no openssl.
  It would have written an **empty** key and the file would have looked fine.
  Replacement that does work there: `head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n'`.
- `cd ~/lock3-website` was handed to him when that path exists only under
  `/home/claude` (mode 0700), so it is not readable by him at all.

Check reachability from *his* side, not from gromit: gromit reaches lock3 and
wallace, but **lock3 can only reach gromit on port 8789** (`tag:jumphost` ACL),
and lock3 and wallace cannot reach each other in either direction.

## Secrets

**`~/secrets-inbox/` on gromit is THE hand-off.** `0770 claude:users`, he is in
`users`, so he edits files there directly with no sudo and no paste into chat.
He tells me; I verify, place, and delete. Its `README.md` carries the contract
and a table that must be updated in the same breath as placing or deleting.

**The agent can ENCRYPT but never DECRYPT.** sops-nix on gromit and wallace,
each decrypting with its own SSH host key converted to age. I can add
`sops.secrets.<name>` plumbing and create a new encrypted file from public keys;
only Chris can read or edit a value.

Per-host reality:

| host | mechanism |
|---|---|
| gromit | sops-nix; secrets land in `/run/secrets/<name>` |
| wallace | sops-nix since 2026-11-02; recipient of **one** scoped secret only |
| lock3 | **no sops.** Hand-placed files: `/var/lib/lock3-form/env`, `/var/lib/ntfy-watch/env`, `/var/lib/homelab-watch/keys` |

`secret-put` is available on **gromit and wallace**. It is **not** on lock3, so
use the one-liner form there — and remember lock3 has no openssl for any length
or shape check.

When a secret must exist in two places (lock3 generated it, gromit and wallace
need it), generate where it is used, hand a copy through `secrets-inbox`, and
encrypt from there. Confirm the authoritative copy still exists before deleting
the inbox copy.

## Merging

**Forgejo web UI, often from his phone.** So:

- keep PR descriptions **scannable on a phone** — short paragraphs, narrow or
  no tables, the decision near the top
- ⚠️ I have been getting this wrong. The PRs in this session ran to dozens of
  lines with wide tables, written as if for a desktop reviewer.
- flakes and lock3-website PRs are **Chris-gated**; docs PRs in
  `nixos-homelab-improvements` are self-merged by the agent
- never type a bare `#N` in a Forgejo PR body — it autolinks to `/issues/` and
  404s. Use a full markdown link.

## Deploy paths, so a handoff names the right wait

| host | mechanism | latency |
|---|---|---|
| gromit, wallace, marcus | comin, against `main` | ~1–2 min after merge |
| lock3 | **pushed from gromit** (`lock3-deploy.timer`) | up to 5 min after merge |

⚠️ lock3 cannot pull from anywhere — not Forgejo (tailnet-only, ACL-denied) and
not the GitHub mirror (dead credential for 62 days). If a change must reach
lock3, it goes through gromit.
