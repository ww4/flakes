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
      # exec-form command so a password containing spaces stays one argument
      cmd = [
        "start"
        "--log" "info"
        "--user" "root"
        "--pass-file" "/run/creds/surreal-pass"
        "rocksdb:/mydata/mydatabase.db"
      ];
      environment = {
        SURREAL_EXPERIMENTAL_GRAPHQL = "true";
      };
      volumes = [
        "${stateDir}/surreal:/mydata"
        # Pass the generated password to SurrealDB via a file (kept out of
        # environment where `docker inspect` would leak it).
        "${stateDir}/surreal-pass:/run/creds/surreal-pass:ro"
      ];
      # No host port binding: the app reaches surrealdb over the private docker
      # network by name (`open-notebook-surrealdb:8000`). Exposing 8000 on the
      # host would also collide with audiobookshelf.
      extraOptions = [ "--network=${netName}" "--network-alias=surrealdb" ];
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
      ports = [
        "127.0.0.1:8502:8502"    # web UI (nginx fronts this)
        "127.0.0.1:5055:5055"    # REST API (also behind the same vhost)
      ];
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
        # Model + voice files are baked into the image; no HF download at boot.
        PYTHONUNBUFFERED = "1";
      };
      # Kokoro caches voice tensors on first use; volume keeps that between
      # restarts (also survives image pulls).
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
    path = [ pkgs.openssl pkgs.coreutils ];
    script = ''
      set -eu
      install -d -m 700 ${stateDir}
      envf=${stateDir}/app.env
      passf=${stateDir}/surreal-pass
      umask 077
      # Preserve existing values on redeploy; only generate on first boot.
      if [ ! -s "$envf" ]; then
        key=$(openssl rand -hex 32)
        pass=$(openssl rand -hex 24)
        printf 'OPEN_NOTEBOOK_ENCRYPTION_KEY=%s\nSURREAL_PASSWORD=%s\n' \
          "$key" "$pass" > "$envf"
        printf '%s' "$pass" > "$passf"
      fi
      # If someone stomped surreal-pass but the env still holds the password
      # (or vice-versa) re-derive from the env file, which is the source of
      # truth — mismatched creds silently fail the app's DB connect at boot.
      pass=$(sed -n 's/^SURREAL_PASSWORD=//p' "$envf")
      printf '%s' "$pass" > "$passf"
      chmod 600 "$envf" "$passf"
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
