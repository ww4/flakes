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

    topic=gromit-alerts
    server=http://localhost:8090

    # `-sS`, not `-fsS`: -f makes curl exit non-zero and swallow the response,
    # which loses the actual HTTP status. We want the status in the log, so the
    # check is explicit below.
    args=( -sS --max-time 15
           -H "Title: $title"
           -H "Priority: $priority" )
    if [ -n "$tags" ]; then
      args+=( -H "Tags: $tags" )
    fi
    if [ -n "$click" ]; then
      args+=( -H "Click: $click" )
    fi

    # The fallback is OUTSIDE the substitution on purpose: curl's own -w already
    # prints "000" when it cannot connect, so `|| echo 000` inside would append a
    # second one and log http=000000.
    code="$(curl "''${args[@]}" -d "$message" -o /dev/null \
              -w '%{http_code}' "$server/$topic" 2>/dev/null)" || true
    code="''${code:-000}"

    # EVERY send leaves a trace, success included. Until 2026-09-06 only the
    # FAILURE paths logged, so a delivered notification and a silently dropped
    # one were indistinguishable after the fact — which is exactly the question
    # that came up when several newsdesk editions appeared to go missing and
    # there was no way to tell whether they had ever been sent.
    #
    # The TITLE is logged, deliberately not the body. Bodies carry sentinel
    # diagnoses, device names and addresses; the journal is a wider audience than
    # the phone. Title + priority + tags + size + status is enough to answer
    # "was it sent, when, and did ntfy take it?" without copying every alert.
    if [ "$code" = "200" ]; then
      echo "gromit-notify: sent topic=$topic priority=$priority tags=''${tags:--} bytes=''${#message} http=$code title=\"$title\""
    else
      echo "gromit-notify: FAILED topic=$topic priority=$priority http=$code title=\"$title\"" >&2
      exit 1
    fi
  '';
}
