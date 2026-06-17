# Immich machine-learning, offloaded from gromit onto the 5900X.
#
# gromit runs immich-server + DB + the photo library; only the ML inference
# (face detection, CLIP smart-search, object tagging) runs here. gromit's server
# calls this over Tailscale — see modules/services/immich.nix, which disables
# local ML and points IMMICH_MACHINE_LEARNING_URL at 100.66.171.120:3003.
#
# Runs as a pinned OCI container rather than the NixOS immich module, because the
# module can't bring up ML without also standing up a server + DB here. The tag
# is derived from `pkgs.immich.version` so it ALWAYS matches gromit's server
# version (both hosts share one nixpkgs input) — a server/ML version mismatch
# breaks the ML API, and this makes the two move in lockstep on every bump.
#
# CPU inference on the 5900X (24 threads) — already a large jump over gromit.
# GPU (ROCm on the RX 580) is a deliberate non-goal for now: Polaris/gfx803 is
# not supported by current ROCm, so it's a future experiment, not this change.
{ config, lib, pkgs, ... }:
{
  virtualisation.podman.enable = true;
  virtualisation.oci-containers.backend = "podman";

  virtualisation.oci-containers.containers.immich-ml = {
    image = "ghcr.io/immich-app/immich-machine-learning:v${pkgs.immich.version}";
    autoStart = true;
    # Publish ONLY on the tailnet IP — never the LAN. gromit reaches :3003 here.
    ports = [ "100.66.171.120:3003:3003" ];
    volumes = [ "immich-ml-cache:/cache" ];   # model weights, downloaded on first use
    environment = {
      MACHINE_LEARNING_CACHE_FOLDER = "/cache";
      IMMICH_HOST = "0.0.0.0";   # inside the container; only ever published on the tailnet IP above
      IMMICH_PORT = "3003";
    };
  };

  # The published port binds the tailnet IP, which only exists once tailscale is
  # up — order the container after it and let systemd retry if the IP is late.
  systemd.services.podman-immich-ml = {
    after = [ "tailscaled.service" ];
    wants = [ "tailscaled.service" ];
    serviceConfig.RestartSec = lib.mkForce "10s";
  };

  # Belt-and-suspenders: only admit 3003 on the tailscale interface.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 3003 ];
}
