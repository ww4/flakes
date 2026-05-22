# gromit-notify — thin wrapper around curl -> the local ntfy instance.
#
# Imported by notifications.nix (which runs the ntfy server itself) and by any
# other module whose scripts need to send alerts. Keeping it standalone avoids
# a module-to-module dependency.
{ pkgs }:

pkgs.writeShellApplication {
  name = "gromit-notify";
  runtimeInputs = [ pkgs.curl ];
  text = ''
    # Usage: gromit-notify <title> <message> [priority] [tags]
    #   priority: min | low | default | high | urgent
    #   tags:     comma-separated ntfy tags/emoji (e.g. warning,floppy_disk)
    title=''${1:?usage: gromit-notify <title> <message> [priority] [tags]}
    message=''${2:?usage: gromit-notify <title> <message> [priority] [tags]}
    priority=''${3:-default}
    tags=''${4:-}

    args=( -fsS --max-time 15
           -H "Title: $title"
           -H "Priority: $priority" )
    if [ -n "$tags" ]; then
      args+=( -H "Tags: $tags" )
    fi
    curl "''${args[@]}" -d "$message" \
      "http://localhost:8090/gromit-alerts" > /dev/null
  '';
}
