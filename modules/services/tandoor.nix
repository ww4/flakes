# Tandoor Recipes.
{ config, lib, pkgs, ... }:

{
  services.tandoor-recipes = {
    enable = true;
    address = "0.0.0.0";
    # Default ALLOWED_HOSTS is restrictive; permit any Host header (the
    # Tailscale-only firewall is the real perimeter).
    extraConfig.ALLOWED_HOSTS = "*";
  };

  # Reachable only over the Tailscale interface.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 8080 ];
}
