# Agent context — ww4/flakes (Gromit NixOS config)

You are the scoped **`claude` agent** on Gromit (NOT Chris). This repo is Gromit's
NixOS configuration, deployed by **comin** when a commit reaches `main`.

**Change flow (the only path):** NEVER push to `main` (PreToolUse guard blocks it +
branch-protected). Branch off `origin/main` → validate with
`nixos-rebuild build --flake .#gromit` → commit (use `git commit -F <file>` so a
message containing `rm -rf`/`main` can't trip the guard) → push (keep the literal
strings `" main"` and `--force` OUT of the shell command — the guard substring-
matches the whole command) → open a PR via the **ww4-bot API**
(`~/.config/ww4-bot/token.env`). **Chris merges** — that's the approval gate. Do
NOT self-merge flake PRs. Add any new module files with `git add` (flakes only see
git-tracked files).

**Iterate live:** push to the `testing` branch → comin runs an ephemeral
`nixos-rebuild test`. `testing` must be a **descendant of `main`** or comin
silently fetches-but-won't-deploy it. Roll back / reset `testing` via the API
ref-PATCH (`git/refs/heads/testing`, `force:true`) — NOT a git force-push (guarded).

**Playbooks:** `/flake-pr`, `/flake-test`, `/new-service`, `/sops-add`.
**Full guide + every gotcha:** agent memory `claude-agent-guide` and `open-loops`
(task board). The rich homelab memory is keyed to the `nixos-homelab-improvements`
project dir — recall it if it didn't auto-load.
