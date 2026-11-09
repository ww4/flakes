# secrets/

Encrypted secret files, one per logical secret, managed with `sops`. See
`../.sops.yaml` for recipients and `../modules/sops.nix` for the activation
wiring.

These files are **safe to commit**: they are ciphertext, encrypted to
gromit's SSH host key and the admin age key. At activation they decrypt to
`/run/secrets/<name>` (root-owned 0400 by default; `owner`/`mode` set per
secret where a service needs to read directly).

To edit a value (needs the admin age key):

```sh
sops secrets/<name>.yaml
```

Declarations — the `sops.secrets."<name>"` blocks that give each file an
owner and a runtime path — live next to their consumers:
`modules/homelab-values.nix` for secrets used by library modules,
`modules/agent/*-secret.nix` for the agent's API keys, and the local service
modules for the rest. To add a new secret, use the `/sops-add` flow: the
agent writes the plumbing and a template in `~claude/secrets-inbox/`; you
fill in the value.

Everything that should be in sops is; the exceptions are deliberate.
Runtime-generated credentials (mempool's DB and RPC files, Grafana's
generated admin password and secret key, ntfy's subscriber password) manage
themselves on the box, and putting them here would create a second source of
truth.
