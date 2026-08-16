# Open Notebook — self-hosted NotebookLM alternative at
# https://notebook.rosemaryacres.com (Tailscale source-gate + Authelia
# forward-auth; the auth wiring lives in authelia.nix).
#
# Feed it PDFs / web pages / YouTube / audio / office docs → chat with
# citations, generate multi-speaker podcast episodes from the sources.
#
# Stack (three containers on a private docker network):
#   - open-notebook-surrealdb : SurrealDB v2 (both relational + vector storage)
#   - open-notebook           : the app (Next.js + FastAPI), web UI on :8502
#   - open-notebook-kokoro    : Kokoro-FastAPI, local TTS with an OpenAI-compat
#                               API — used by the podcast generator so nothing
#                               ships to ElevenLabs/Deepgram/etc.
#
# Model provider is UI-configured post-deploy (Settings → Providers). Options:
#   - Fully-local:  add an OpenAI-compat provider pointing at Open WebUI on
#                   wallace (https://chat.rosemaryacres.com/api, user API key).
#                   Later follow-up: expose wallace's llama.cpp servers on the
#                   tailnet directly (hosts/wallace/llm.nix binds 127.0.0.1
#                   today) for a shorter path with no chat-app in the middle.
#   - Cloud:        Anthropic / OpenAI / Google — pick per notebook.
#   For TTS: add an OpenAI-compat provider "Kokoro" at http://kokoro:8880/v1
#           (any key; Kokoro ignores auth) — this is the point of the local
#           TTS container.
{ config, lib, pkgs, ... }:

let
  netName = "open-notebook-net";
  stateDir = "/var/lib/open-notebook";
in
{
  virtualisation.oci-containers.containers = {
    open-notebook-surrealdb = {
      image = "surrealdb/surrealdb:v2";
      # No CLI creds: the image is distroless (no `sh`, no coreutils) so a
      # shell-wrapper to inject the password fails with `exec: "sh": executable
      # file not found` — burned that path once. SurrealDB's clap args expose
      # SURREAL_USER / SURREAL_PASS as env-var equivalents to --user / --pass,
      # which is what the shared env file below supplies.
      cmd = [ "start" "--log" "info" "rocksdb:/mydata/mydatabase.db" ];
      environment = {
        SURREAL_EXPERIMENTAL_GRAPHQL = "true";
      };
      # Same env file as the app so a password rotation stays in sync.
      environmentFiles = [ "${stateDir}/app.env" ];
      volumes = [ "${stateDir}/surreal:/mydata" ];
      # No host port binding: the app reaches surrealdb over the private docker
      # network by name (`surrealdb:8000` via the network-alias below). Exposing
      # 8000 on the host would also collide with audiobookshelf.
      # --user root: bind-mount is owned root:root (StateDirectory mode 700);
      # the image's default non-root user can't create the RocksDB files
      # ("Failed to create RocksDB directory: PermissionDenied"). Upstream's
      # docker-compose does the same (`user: root  # Required for bind mounts
      # on Linux`).
      extraOptions = [ "--network=${netName}" "--network-alias=surrealdb" "--user=root" ];
    };

    open-notebook = {
      image = "lfnovo/open_notebook:v1-latest";
      dependsOn = [ "open-notebook-surrealdb" ];
      environment = {
        SURREAL_URL       = "ws://surrealdb:8000/rpc";
        SURREAL_USER      = "root";
        SURREAL_NAMESPACE = "open_notebook";
        SURREAL_DATABASE  = "open_notebook";
        # Local-LLM friendly default: one background task at a time so a small
        # local model isn't slammed with parallel embed/chat/podcast requests.
        # Harmless for cloud APIs — bump later if it becomes the bottleneck.
        OPEN_NOTEBOOK_WORKER_MAX_TASKS = "1";
      };
      # Encryption key + SurrealDB password come from a generated env file.
      # OPEN_NOTEBOOK_ENCRYPTION_KEY encrypts every API key stored in the DB;
      # rotating it would orphan them, so open-notebook-secrets preserves it
      # across restarts.
      environmentFiles = [ "${stateDir}/app.env" ];
      # 5055 (REST API) is deliberately NOT bound on the host — jellyseerr owns
      # that port. The web UI (8502) is what the nginx vhost proxies; the REST
      # API is reachable inside the docker network as open-notebook:5055 if
      # something else on the compose stack ever needs it. Re-expose on a free
      # host port if we grow programmatic-access use.
      ports = [ "127.0.0.1:8502:8502" ];
      volumes = [ "${stateDir}/data:/app/data" ];
      extraOptions = [ "--network=${netName}" ];
    };

    open-notebook-kokoro = {
      # Kokoro-82M FastAPI wrapper — OpenAI-compat /v1/audio/speech, CPU build.
      # Pinned exact: the project is young (v0.7.2 published 2026-08-07) and
      # moves fast enough that a floating `latest` could change the model file
      # or API without warning.
      image = "ghcr.io/remsky/kokoro-fastapi-cpu:v0.7.2";
      environment = {
        PYTHONUNBUFFERED = "1";
        # The model (~350 MB v1_0/kokoro-v1_0.pth) is NOT baked into the image.
        # Without this, startup crash-loops with `File not found:
        # v1_0/kokoro-v1_0.pth` (initial deploy hit restart counter 638). The
        # script downloads on first boot into the volume below, so subsequent
        # starts are instant. Idempotent — a re-download is skipped if present.
        DOWNLOAD_MODEL = "true";
      };
      # Persist the downloaded model + any voice tensors cached on first use,
      # so a container recreate doesn't re-fetch. Volume also hides the
      # image's /app/api/src/models entirely, so DOWNLOAD_MODEL is the only
      # path to a populated tree.
      volumes = [ "${stateDir}/kokoro:/app/api/src/models" ];
      # No host port: the app reaches Kokoro over the docker network at
      # http://kokoro:8880 (network-alias below). Not needed on the host.
      extraOptions = [ "--network=${netName}" "--network-alias=kokoro" ];
    };
  };

  # ---- prerequisite oneshots ---------------------------------------------

  # Generate the encryption key + SurrealDB password ONCE and preserve them
  # forever. Same pattern as mempool-db-secrets: keeps secrets out of the
  # Nix store; rotating them would orphan every API key encrypted with the
  # old key (they'd stay in surrealdb but the app couldn't decrypt them).
  systemd.services.open-notebook-secrets = {
    description = "Generate open-notebook encryption key + SurrealDB password (out of the Nix store)";
    wantedBy = [ "multi-user.target" ];
    before = [ "docker-open-notebook-surrealdb.service" "docker-open-notebook.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    path = [ pkgs.openssl pkgs.coreutils pkgs.gnugrep pkgs.gnused ];
    script = ''
      set -eu
      install -d -m 700 ${stateDir}
      envf=${stateDir}/app.env
      umask 077
      # Preserve existing values on redeploy — rotating the encryption key
      # would orphan every stored API key. Idempotently ADD any missing var
      # (so an older env from a partial first deploy heals into the current
      # schema without wiping the DB).
      touch "$envf"
      # Ensure a stable password we can reuse across any of the three names.
      pass=$(sed -n 's/^SURREAL_PASS=//p;s/^SURREAL_PASSWORD=//p' "$envf" | head -n1)
      if [ -z "$pass" ]; then pass=$(openssl rand -hex 24); fi
      grep -q '^OPEN_NOTEBOOK_ENCRYPTION_KEY=' "$envf" \
        || printf 'OPEN_NOTEBOOK_ENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)" >> "$envf"
      # SURREAL_USER + SURREAL_PASS are read by the SurrealDB container
      # (sh -c wrapper); SURREAL_PASSWORD is read by the app (upstream name).
      grep -q '^SURREAL_USER='     "$envf" || printf 'SURREAL_USER=root\n'    >> "$envf"
      grep -q '^SURREAL_PASS='     "$envf" || printf 'SURREAL_PASS=%s\n'     "$pass" >> "$envf"
      grep -q '^SURREAL_PASSWORD=' "$envf" || printf 'SURREAL_PASSWORD=%s\n' "$pass" >> "$envf"
      chmod 600 "$envf"
    '';
  };

  # User-defined docker network so the three containers resolve each other
  # by name (`surrealdb`, `kokoro`). oci-containers doesn't create networks.
  systemd.services.init-open-notebook-net = {
    description = "Create the open-notebook-net docker network";
    after = [ "docker.service" ];
    requires = [ "docker.service" ];
    before = [
      "docker-open-notebook-surrealdb.service"
      "docker-open-notebook.service"
      "docker-open-notebook-kokoro.service"
    ];
    wantedBy = [ "multi-user.target" ];
    path = [ pkgs.docker ];
    serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
    script = ''
      docker network inspect ${netName} >/dev/null 2>&1 \
        || docker network create ${netName}
    '';
  };

  # Ordering: containers wait on the network + the secrets file.
  systemd.services.docker-open-notebook-surrealdb = {
    after    = [ "init-open-notebook-net.service" "open-notebook-secrets.service" ];
    requires = [ "init-open-notebook-net.service" "open-notebook-secrets.service" ];
  };
  systemd.services.docker-open-notebook = {
    after    = [ "init-open-notebook-net.service" "open-notebook-secrets.service" ];
    requires = [ "init-open-notebook-net.service" "open-notebook-secrets.service" ];
  };
  systemd.services.docker-open-notebook-kokoro = {
    after    = [ "init-open-notebook-net.service" ];
    requires = [ "init-open-notebook-net.service" ];
  };

  systemd.tmpfiles.rules = [
    "d ${stateDir}          0700 root root - -"
    "d ${stateDir}/surreal  0700 root root - -"
    "d ${stateDir}/data     0700 root root - -"
    "d ${stateDir}/kokoro   0700 root root - -"
  ];

  # DNS: notebook.rosemaryacres.com -> 100.82.117.116 (proxy off), created
  # by the agent via the Cloudflare token. Inherits the global Tailscale/LAN
  # source-gate (nginx-access.nix); Authelia forward-auth is merged in from
  # authelia.nix (`services.nginx.virtualHosts."notebook.…" = protect;`).
  services.nginx.virtualHosts."notebook.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8502";
      recommendedProxySettings = true;
      # Next.js server-sent-events + websockets for streaming chat responses.
      proxyWebsockets = true;
      extraConfig = ''
        # PDFs, EPUBs, audio files can be large; the default 1M cap
        # would 413 on any real source upload.
        client_max_body_size 500M;
        # Streaming/podcast generation runs long — don't kill the request
        # at the default 60s.
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
      '';
    };
  };
}
