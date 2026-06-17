# TLS front door for wallace's Open WebUI (the local-LLM chat at chat.rosemaryacres.com).
# Open WebUI + the llama.cpp backends run on wallace (hosts/wallace/llm.nix); gromit
# is the homelab's reverse proxy, so it terminates HTTPS here and proxies to wallace
# over Tailscale. Auto source-gated to LAN+Tailscale by modules/services/nginx-access.nix.
{ config, lib, pkgs, ... }:
{
  services.nginx.virtualHosts."chat.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;                               # Cloudflare DNS-01 (defaults in nextcloud.nix)
    locations."/" = {
      proxyPass = "http://100.66.171.120:3000";    # wallace Open WebUI on the tailnet
      proxyWebsockets = true;                        # streaming responses + live updates
      extraConfig = ''
        client_max_body_size 1024M;                  # document uploads for RAG
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
      '';
    };
  };
}
