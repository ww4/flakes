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
    # Usage: gromit-notify <title> <message> [priority] [tags] [click-url]
    #   priority:  min | low | default | high | urgent
    #   tags:      comma-separated ntfy tags/emoji (e.g. warning,floppy_disk)
    #   click-url: makes the whole notification tappable (ntfy "Click:" header)
    title=''${1:?usage: gromit-notify <title> <message> [priority] [tags] [click-url]}
    message=''${2:?usage: gromit-notify <title> <message> [priority] [tags] [click-url]}
    priority=''${3:-default}
    tags=''${4:-}
    click=''${5:-}

    args=( -fsS --max-time 15
           -H "Title: $title"
           -H "Priority: $priority" )
    if [ -n "$tags" ]; then
      args+=( -H "Tags: $tags" )
    fi
    if [ -n "$click" ]; then
      args+=( -H "Click: $click" )
    fi
    curl "''${args[@]}" -d "$message" \
      "http://localhost:8090/gromit-alerts" > /dev/null
  '';
}
