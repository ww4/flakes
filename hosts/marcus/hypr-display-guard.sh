# hypr-display-guard — the laptop panel is NEVER dark with nothing plugged in.
#
# THE RULE (Chris, 2026-10-27, and it is absolute):
#   "Is there any way that a panel will be dark with no monitor attached?
#    There's no case in which I want that to be true. I don't care if there's a
#    setting somewhere, we should ignore it. Those settings should only be
#    possible when external displays are available."
#
# So this is an INVARIANT, continuously enforced — not a crash recovery:
#
#   no external output connected  =>  eDP-1 is enabled, whatever any file says
#   an external IS connected      =>  the saved layout is honoured, including
#                                     monitor=eDP-1,disable
#
# WHY IT EXISTS. nwg-displays (Super+Shift+M) writes ~/.config/hypr/monitors.conf,
# which hyprland.conf `source`s LAST, so it overrides the declarative defaults.
# On 2026-10-21 08:42 it wrote, correctly for a docked laptop:
#
#     monitor=eDP-1,disable
#     monitor=HDMI-A-2,1360x768@60.02,1536x0,1.0
#
# Six days later Hyprland started with nothing on HDMI. eDP-1 off, HDMI-A-2
# absent => ZERO enabled outputs. Hyprland was perfectly healthy: it started,
# logged nothing, accepted input, ran five minutes, and drew to nowhere. Plasma
# was fine because Plasma never reads that file — which is exactly what made it
# look like a hardware fault and sent the first diagnosis to the BIOS.
#
# ⚠️ AN EARLIER VERSION OF THIS SCRIPT ONLY CHECKED AT SESSION START, AND ONLY
# FOR ZERO MONITORS. That was not enough, and the gaps are worth naming so they
# do not come back: unplugging a dock mid-session left the panel dark with the
# guard already exited; running nwg-displays while undocked blacked it out
# instantly; and even in the caught case there was a six-second dark window.
# Enforcing an invariant on every monitor event closes all three.
#
# MODES
#   --daemon   exec-once. Enforce now, then on every monitor/config event.
#   --enforce  One-shot enforcement.
#   --reset    Move monitors.conf aside and restore defaults. The manual escape
#              hatch on Super+Shift+D — must work with nothing on screen.

CONF="$HOME/.config/hypr/monitors.conf"
# Injected by hyprland.nix so there is exactly ONE definition of the defaults;
# the fallbacks only matter when running the script by hand.
EDP="${HYPR_EDP_DEFAULT:-eDP-1,preferred,auto,1.25}"
CATCHALL="${HYPR_EXT_DEFAULT:-,preferred,auto,1}"

# Absolute hyprctl, injected by hyprland.nix; the fallback keeps the script
# runnable by hand from a terminal.
HC="${HYPRCTL:-hyprctl}"

# Is any NON-internal output physically connected? Read from the kernel, not
# from Hyprland: this must stay true even when the compositor has been told to
# ignore every output. eDP* is the internal panel and never counts.
externals_connected() {
  local f name
  for f in /sys/class/drm/card*-*/status; do
    [ -e "$f" ] || continue
    name=$(basename "$(dirname "$f")")
    case "$name" in *-eDP*) continue ;; esac
    if [ "$(cat "$f" 2>/dev/null)" = "connected" ]; then
      return 0
    fi
  done
  return 1
}

# Count of monitors Hyprland currently has ENABLED. Tolerant by design: this
# runs under `set -euo pipefail`, and a not-yet-ready hyprctl or a jq parse
# error must read as 0 rather than abort the guard.
enabled_count() {
  local out
  out=$("$HC" -j monitors 2>/dev/null) || { echo 0; return 0; }
  [ -n "$out" ] || { echo 0; return 0; }
  printf '%s' "$out" | jq 'length' 2>/dev/null || echo 0
}

edp_enabled() {
  local out
  out=$("$HC" -j monitors 2>/dev/null) || return 1
  printf '%s' "$out" | jq -e 'any(.[]; .name == "eDP-1")' >/dev/null 2>&1
}

force_edp_on() {
  "$HC" keyword monitor "$EDP" >/dev/null 2>&1 || true
}

# The invariant, applied once.
enforce() {
  if externals_connected; then
    # An external is available, so the user's saved layout is legitimate —
    # including disabling the panel. Only intervene if the result is NO usable
    # output at all (e.g. monitors.conf names a connector that is not the one
    # actually plugged in, which is precisely how 2026-10-21 played out).
    if [ "$(enabled_count)" -eq 0 ]; then
      force_edp_on
      "$HC" keyword monitor "$CATCHALL" >/dev/null 2>&1 || true
      notify-send -u critical "Display recovered" \
        "No output was enabled. eDP-1 forced on." 2>/dev/null || true
    fi
    return 0
  fi

  # Nothing external is plugged in. The panel is the only way to see anything,
  # so it is on. This deliberately OVERRIDES monitors.conf rather than editing
  # it — the saved docked layout is preserved and comes back when docked.
  if ! edp_enabled; then
    force_edp_on
    notify-send "Panel re-enabled" \
      "No external display is connected, so eDP-1 was forced on." 2>/dev/null || true
  fi
}

case "${1---daemon}" in
  --reset)
    # Unconditional, for the keybind. Move aside, never delete — the layout is
    # recoverable from the timestamped file.
    if [ -s "$CONF" ]; then
      mv -- "$CONF" "$CONF.broken-$(date +%Y%m%d-%H%M%S)"
    fi
    : > "$CONF"
    force_edp_on
    "$HC" keyword monitor "$CATCHALL" >/dev/null 2>&1 || true
    "$HC" reload >/dev/null 2>&1 || true
    notify-send -u critical "Display reset" \
      "monitors.conf moved aside; eDP-1 re-enabled." 2>/dev/null || true
    ;;

  --enforce)
    enforce
    ;;

  --daemon)
    # Hyprland may still be bringing outputs up; enforce once it answers.
    for _ in 1 2 3 4 5 6; do
      # Full `if`, NOT `[ ] && break`: under set -e a failing AND-list is itself
      # a failed command and would kill the guard on the first iteration.
      if [ "$(enabled_count)" -gt 0 ]; then break; fi
      sleep 1
    done
    enforce

    # Event-driven from here: socket2 emits monitoradded / monitorremoved on
    # dock and undock, and configreloaded when nwg-displays hits Apply — so all
    # three ways into the bad state are covered with no polling.
    SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hypr/${HYPRLAND_INSTANCE_SIGNATURE:-}/.socket2.sock"
    if [ -S "$SOCK" ]; then
      # `|| true` so a dropped socket ends the read loop instead of killing the
      # guard; the backstop loop below then takes over.
      while IFS= read -r line; do
        case "$line" in
          monitoradded*|monitorremoved*|configreloaded*)
            # Debounce: our own keyword call can wake this again.
            sleep 1
            enforce
            ;;
        esac
      done < <(socat -U - "UNIX-CONNECT:$SOCK" 2>/dev/null || true)
    fi

    # Backstop. Reached if socket2 was unavailable or went away. 30 s is slow on
    # purpose — it is a safety net, not the mechanism, and this runs on battery.
    while :; do
      sleep 30
      enforce
    done
    ;;

  *)
    echo "hypr-display-guard: usage: hypr-display-guard [--daemon|--enforce|--reset]" >&2
    exit 2
    ;;
esac
