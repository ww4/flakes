# Local LLM stack on wallace — the "generation" half of the talk-to-my-books RAG
# (retrieval lives on gromit: archive-semantic + Open WebUI's own document RAG).
#
# Two llama.cpp servers + Open WebUI as the chat front-end:
#   - llama-gpu  : Qwen2.5-3B (Q4) FULLY on the RX 580 via VULKAN. Polaris/gfx803
#                  is dropped by modern ROCm, so Vulkan (Mesa RADV) is the only
#                  GPU path. ~1.93 GB weights fit the card's 4 GB VRAM with room
#                  for an 8K context. Fast (~25-40 tok/s).
#   - llama-cpu  : Qwen2.5-7B (Q4) on the 5900X (CPU, -ngl 0). Slower but smarter;
#                  pick it in the UI's model dropdown when quality > speed.
#   - open-webui : ChatGPT-style UI, talks to both as OpenAI-compatible backends.
#                  Exposed via gromit's nginx at chat.rosemaryacres.com (tailnet).
#
# Model weights are NOT in the Nix store (multi-GB blobs) — each service fetches
# its GGUF once into its StateDirectory on first start (idempotent).
{ config, lib, pkgs, ... }:
let
  llamaVulkan = pkgs.llama-cpp.override { vulkanSupport = true; };

  # gromit's immich-ml proves the pattern; here the Vulkan ICD must be visible to
  # llama-server so RADV is found. hardware.graphics populates /run/opengl-driver.
  # HOME → the StateDirectory so RADV can persist its shader cache (otherwise it
  # tries //.cache on the read-only root and recompiles shaders every start).
  vkEnv = {
    VK_ICD_FILENAMES = "/run/opengl-driver/share/vulkan/icd.d/radeon_icd.x86_64.json";
    HOME = "/var/lib/llama-gpu";
  };

  mkFetch = name: url: pkgs.writeShellScript "fetch-${name}" ''
    set -eu
    dest="$STATE_DIRECTORY/${name}.gguf"
    if [ ! -s "$dest" ]; then
      echo "downloading ${name} model (first run)..."
      ${pkgs.curl}/bin/curl -fL --retry 4 --retry-delay 5 -o "$dest.part" "${url}"
      mv "$dest.part" "$dest"
    fi
  '';
in
{
  # Mesa RADV Vulkan userspace for the RX 580 (headless — no X pulled in).
  hardware.graphics.enable = true;

  # ---- GPU instance: Qwen2.5-3B on the RX 580 (Vulkan) ----
  systemd.services.llama-gpu = {
    description = "llama.cpp — Qwen2.5-3B on RX 580 (Vulkan)";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];
    environment = vkEnv;
    serviceConfig = {
      DynamicUser = true;
      StateDirectory = "llama-gpu";
      SupplementaryGroups = [ "render" "video" ];     # /dev/dri/renderD128 access
      TimeoutStartSec = "3600";                        # first-run model download
      ExecStartPre = mkFetch "Qwen2.5-3B-Instruct-Q4_K_M"
        "https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf";
      ExecStart = ''
        ${llamaVulkan}/bin/llama-server \
          --model ''${STATE_DIRECTORY}/Qwen2.5-3B-Instruct-Q4_K_M.gguf \
          --alias qwen2.5-3b-gpu \
          --host 127.0.0.1 --port 8080 \
          --n-gpu-layers 99 --ctx-size 8192
      '';
      Restart = "on-failure";
      RestartSec = "10s";
    };
  };

  # ---- CPU instance: Qwen2.5-7B on the 5900X ----
  systemd.services.llama-cpu = {
    description = "llama.cpp — Qwen2.5-7B on the 5900X (CPU)";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      DynamicUser = true;
      StateDirectory = "llama-cpu";
      TimeoutStartSec = "3600";
      ExecStartPre = mkFetch "Qwen2.5-7B-Instruct-Q4_K_M"
        "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf";
      ExecStart = ''
        ${llamaVulkan}/bin/llama-server \
          --model ''${STATE_DIRECTORY}/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
          --alias qwen2.5-7b-cpu \
          --host 127.0.0.1 --port 8081 \
          --n-gpu-layers 0 --threads 12 --ctx-size 8192
      '';
      Restart = "on-failure";
      RestartSec = "10s";
    };
  };

  # ---- Open WebUI: chat front-end for both backends ----
  # Run as the upstream container, NOT the nixpkgs package: open-webui 0.9.5 in
  # our nixpkgs fails to build (frontend: Rollup can't resolve @internationalized/date).
  # The image is :latest pinned by digest (current + reproducible; bump the digest
  # to update). Host networking so it reaches both llama-servers on 127.0.0.1 and
  # listens on :3000 (gromit fronts it; firewalled to tailscale0 below).
  virtualisation.oci-containers.containers.open-webui = {
    image = "ghcr.io/open-webui/open-webui@sha256:7f1b0a1a50cfbac23da3b16f96bc968fd757b26dc9e54e93813d61768ea9184e";
    autoStart = true;
    extraOptions = [ "--network=host" ];
    volumes = [ "open-webui-data:/app/backend/data" ];
    environment = {
      PORT = "3000";
      ENABLE_OLLAMA_API = "False";
      # Two OpenAI-compatible backends (semicolon-separated, keys positionally matched).
      OPENAI_API_BASE_URLS = "http://127.0.0.1:8080/v1;http://127.0.0.1:8081/v1";
      OPENAI_API_KEYS = "sk-local;sk-local";
      WEBUI_URL = "https://chat.rosemaryacres.com";
      WEBUI_AUTH = "True";       # first account created becomes admin
      # No phoning home.
      ANONYMIZED_TELEMETRY = "False";
      DO_NOT_TRACK = "True";
      SCARF_NO_ANALYTICS = "True";
    };
  };

  # Open WebUI reachable only over the tailnet (gromit proxies it publicly).
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 3000 ];

  environment.systemPackages = [ pkgs.vulkan-tools ];   # vulkaninfo for diagnostics
}
