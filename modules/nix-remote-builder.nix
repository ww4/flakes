# Remote Nix builder — gromit offloads builds to wallace (Ryzen 9 5900X, 24t).
# gromit's root nix-daemon SSHes to wallace's `nixremote` user over Tailscale;
# wallace builds and returns the results. If wallace is unreachable, Nix falls
# back to building locally — so this is an accelerator, never a hard dependency.
# Wallace's side (the nixremote trusted-user) is in hosts/wallace/configuration.nix.
{ config, lib, pkgs, ... }:
{
  # Private key for gromit-root -> wallace:nixremote (read by the root nix-daemon).
  sops.secrets."wallace-builder-key" = {
    sopsFile = ../secrets/wallace-builder-key.yaml;
    key = "wallace-builder-key";
  };

  nix.distributedBuilds = true;
  nix.settings.builders-use-substitutes = true;   # wallace pulls its own deps from caches
  # Make `claude` a trusted nix user so its builds go THROUGH the daemon (which
  # holds `builders`) and thus offload to wallace — root's `nix build` runs
  # in-process and bypasses the daemon's builders, which is why root couldn't
  # offload. This also lets the agent's own `nixos-rebuild build` validations use
  # the farm. NOTE: trusted nix users can influence the store (substituters,
  # build-hook) — a real privilege bump on the otherwise-untrusted agent.
  nix.settings.trusted-users = [ "root" "claude" ];
  # nix 2.34's `builders` defaults EMPTY and nix.buildMachines only writes the
  # /etc/nix/machines file — without this the daemon never reads it, so offload
  # silently no-ops. Point it at the machines file explicitly. (Verified 2026-06-17:
  # the SSH/key/host-key/transport all work; this was the missing piece.)
  nix.settings.builders = "@/etc/nix/machines";
  nix.buildMachines = [{
    hostName = "100.66.171.120";          # wallace on the tailnet (stable)
    sshUser = "nixremote";
    sshKey = config.sops.secrets."wallace-builder-key".path;
    protocol = "ssh-ng";
    system = "x86_64-linux";
    maxJobs = 12;                          # 5900X: 12 cores / 24 threads
    speedFactor = 4;                       # much faster than gromit -> prefer it
    supportedFeatures = [ "nixos-test" "benchmark" "big-parallel" "kvm" ];
    # base64 of wallace's /etc/ssh/ssh_host_ed25519_key.pub — pins the host so
    # the root nix-daemon never prompts / can't be MITM'd.
    publicHostKey = "c3NoLWVkMjU1MTkgQUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSVA3d3pHczFKcXZrQ0lPSFVyZW0xK3puS21Wbk41NEFzS3lTbW5FN01tVzIgcm9vdEB3YWxsYWNlCg==";
  }];
}
