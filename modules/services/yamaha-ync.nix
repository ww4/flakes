# The living-room receiver (a Yamaha R-N301) as two loopback services from
# the public yamaha-ync package: a JSON API for the radio page / phone app
# (proxied under radio.<domain>/receiver/ by the netradio vhost) and an MCP
# server for a local Claude. The host address is the one setting that is
# ours; everything else is the package's defaults.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.yamaha-ync;
  yamaha-ync = pkgs.callPackage ../../pkgs/yamaha-ync { };
  hardening = {
    DynamicUser = true;
    NoNewPrivileges = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateTmp = true;
    RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
    Restart = "always";
    RestartSec = 5;
  };
in
{
  options.services.yamaha-ync = {
    enable = lib.mkEnableOption "the Yamaha receiver's JSON API and MCP server";
    host = lib.mkOption { type = lib.types.str; description = "the receiver's IP or name"; };
    name = lib.mkOption { type = lib.types.str; default = "Living room"; };
    apiPort = lib.mkOption { type = lib.types.port; default = 8791; };
    mcpPort = lib.mkOption { type = lib.types.port; default = 8790; };
    allowNetworkChanges = lib.mkOption { type = lib.types.bool; default = false; };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ yamaha-ync ];   # `ync <host> status` for a quick look

    systemd.services.yamaha-ync-api = {
      description = "Yamaha receiver JSON API (yamaha-ync)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = {
        YNC_HOST = cfg.host; YNC_NAME = cfg.name;
        YNC_BIND = "127.0.0.1"; YNC_PORT = toString cfg.apiPort;
      };
      serviceConfig = hardening // { ExecStart = "${yamaha-ync}/bin/ync-api"; };
    };

    systemd.services.yamaha-ync-mcp = {
      description = "Yamaha receiver MCP server (yamaha-ync)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = {
        YNC_HOST = cfg.host; YNC_NAME = cfg.name;
        YNC_TRANSPORT = "streamable-http"; YNC_BIND = "127.0.0.1"; YNC_PORT = toString cfg.mcpPort;
        YNC_ALLOW_NETWORK_CHANGES = lib.boolToString cfg.allowNetworkChanges;
      };
      serviceConfig = hardening // { ExecStart = "${yamaha-ync}/bin/yamaha-ync-mcp"; };
    };
  };
}
