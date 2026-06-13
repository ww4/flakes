# secrets/

Encrypted secret files live here, one per logical secret, managed with `sops`
(see `../.sops.yaml` for recipients and `../modules/sops.nix` for the wiring).

These files are **safe to commit** — they're encrypted to gromit's host key + the
admin age key. They decrypt at activation into `/run/secrets/<name>` (root-owned
by default; set `owner`/`mode` per secret).

To add or edit a secret (needs the admin age key):

```sh
sops secrets/<name>.yaml      # opens $EDITOR on the decrypted content
```

Then declare it in `../modules/sops.nix` and point the consuming service at
`config.sops.secrets."<name>".path`.

Phase 0 (current): infrastructure only — no secrets migrated yet.
