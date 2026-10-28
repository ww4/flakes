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
# ⚠️ THIS SCRIPT HAS BEEN WRONG THREE TIMES. Every draft looked right and every
# one was caught by actually running it. Do not re-introduce any of them:
#
#  1. STARTUP-ONLY, ZERO-MONITORS-ONLY. Unplugging a dock mid-session left the
#     panel dark with the guard already exited; running nwg-displays while
#     undocked blacked it out instantly; and even when it fired there was a
#     six-second dark window. Fixed by enforcing an invariant continuously.
#
#  2. "CANNOT TELL" REPORTED AS SUCCESS. enabled_count() returned 0 when hyprctl
#     FAILED, so run outside a session the guard saw zero monitors, tried to fix
#     them, had every call fail into `|| true`, and exited 0 — and --daemon would
#     have looped forever enforcing nothing. See hypr_reachable() below.
#
#  3. THE SAFETY NET WAS UNREACHABLE. The periodic sweep sat AFTER the socket2
#     read loop. socat holds that connection open for the life of the session, so
#     the read loop never exited and the sweep NEVER STARTED. Proven on marcus
#     2026-10-27: eDP-1 was disabled by hand, socket2 emitted nothing for the
#     `keyword monitor` change, and the display stayed dark indefinitely with the
#     guard running and structurally unable to help. The sweep now runs in
#     PARALLEL. ⚠️ socket2 does NOT report `keyword monitor` changes, so the
#     sweep — not the event loop — is what catches that case.
#
# Regression-tested with a stubbed hyprctl and a socket2 that accepts and holds:
# the sweep fires across the whole window instead of exactly once.
#
# ✅ And one thing verified rather than assumed: re-enabling the connector
# through hyprctl DOES re-power the backlight (bl_power 4 -> 0,
# actual_brightness 0 -> 1515 on marcus), so this needs no privilege.
#
# MODES
#   --daemon   exec-once. Enforce now, then on every monitor/config event.
#   --enforce  One-shot enforcement.
#   --reset    Move monitors.conf aside and restore defaults. The manual escape
#              hatch on Super+Shift+D — must work with nothing on screen.
#
# EXIT CODES — "I cannot tell" is NOT success, and that distinction is the whole
# reason these exist (see the hypr_reachable comment below):
#   0  invariant holds: acted, or it was already fine
#   2  bad usage
#   3  CANNOT TELL — Hyprland unreachable (run it inside a session, not over SSH)
#   4  reachable, but the compositor REJECTED the monitor keyword

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

# ⚠️ CAN WE TALK TO HYPRLAND AT ALL? Asked separately from "how many monitors
# are enabled", and the reason is a bug this script was caught with in testing:
#
# enabled_count() returned 0 when hyprctl FAILED, conflating "no monitors" with
# "cannot ask". Run outside a Hyprland session (over SSH), `--enforce` therefore
# saw zero monitors, tried to fix it, had every hyprctl call fail into `|| true`,
# and **exited 0**. It reported success having done nothing — and `--daemon`
# would have looped every 30 s forever, enforcing nothing, saying nothing. That
# is precisely the no-op-looks-like-success failure this guard exists to
# prevent, reproduced inside the guard. Keep the two questions separate.
hypr_reachable() {
  "$HC" -j monitors >/dev/null 2>&1
}

# Count of ENABLED monitors. Only meaningful once hypr_reachable; callers check
# that first. A jq parse failure still reads as 0 — that is a real empty answer
# from a reachable compositor, not an unknown.
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

# Move a monitors.conf that disables eDP-1 out of the way. Needed before any
# `reload`, because reload re-reads that file and would re-apply the disable.
# Moves, never deletes.
neutralise_conf() {
  if [ -f "$CONF" ] && grep -qiE '^[[:space:]]*monitor[[:space:]]*=[[:space:]]*eDP-1[[:space:]]*,[[:space:]]*disable' "$CONF"; then
    mv -- "$CONF" "$CONF.broken-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || return 1
    : > "$CONF"
    echo "hypr-display-guard: monitors.conf disabled eDP-1; moved it aside." >&2
  fi
  return 0
}

# ⚠️ GET eDP-1 BACK, AND VERIFY IT ACTUALLY CAME BACK.
#
# This used to be a single `keyword monitor` call whose result was never checked.
# Measured on marcus 2026-10-27 with a 1 Hz sampler: after
# `hyprctl keyword monitor "eDP-1,disable"`, the guard issued
# `keyword monitor eDP-1,preferred,auto,1.25` roughly 25 times over 2m07s and the
# output stayed `enabled=disabled dpms=Off bl_power=4 actual=0` the entire time.
# Zero effect — not slow, INEFFECTIVE. Hyprland does not re-attach an output that
# was explicitly disabled just because a new rule for it arrives.
#
# So: try the cheap thing, CHECK, then escalate to a full `reload`, which
# re-evaluates every monitor rule from scratch. Order matters — neutralise the
# config BEFORE reloading, or reload re-applies the very disable we are undoing.
force_edp_on() {
  "$HC" keyword monitor "$EDP" >/dev/null 2>&1 || true
  sleep 1
  if edp_enabled; then return 0; fi

  # The keyword did not take. Escalate.
  neutralise_conf || true
  "$HC" keyword monitor "$EDP" >/dev/null 2>&1 || true
  "$HC" reload >/dev/null 2>&1 || true
  sleep 2
  if edp_enabled; then
    echo "hypr-display-guard: keyword alone failed; recovered via reload." >&2
    return 0
  fi

  return 1
}

complain_unreachable() {
  echo "hypr-display-guard: cannot reach Hyprland via $HC" >&2
  echo "hypr-display-guard: HYPRLAND_INSTANCE_SIGNATURE is probably unset — run this" >&2
  echo "hypr-display-guard: INSIDE a Hyprland session, not over SSH or from a bare TTY." >&2
}

# The invariant, applied once.
#
# Exit: 0 = invariant holds (acted or already fine), 3 = COULD NOT TELL, because
# Hyprland is unreachable. 3 is never silent — "I don't know" must not look like
# "all good".
enforce() {
  if ! hypr_reachable; then
    complain_unreachable
    return 3
  fi

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
    if force_edp_on; then
      notify-send "Panel re-enabled" \
        "No external display is connected, so eDP-1 was forced on." 2>/dev/null || true
    else
      # Reachable compositor that refused the keyword. Different from exit 3 and
      # genuinely unexpected, so it must not pass silently either.
      echo "hypr-display-guard: eDP-1 is off, no external is connected, and" >&2
      echo "hypr-display-guard: '$HC keyword monitor $EDP' was REJECTED." >&2
      return 4
    fi
  fi
  return 0
}

case "${1---daemon}" in
  --reset)
    # Unconditional, for the keybind. The FILE work happens regardless of
    # whether hyprctl answers — clearing a bad monitors.conf is worth doing even
    # from a TTY, because the next login will then come up sane. Move aside,
    # never delete; the layout stays recoverable from the timestamped file.
    if [ -s "$CONF" ]; then
      mv -- "$CONF" "$CONF.broken-$(date +%Y%m%d-%H%M%S)"
    fi
    : > "$CONF"
    echo "hypr-display-guard: monitors.conf cleared (previous saved alongside)." >&2

    # The LIVE part needs a reachable compositor. Say so rather than pretending.
    if ! hypr_reachable; then
      complain_unreachable
      echo "hypr-display-guard: the file is fixed; log in again to pick it up." >&2
      exit 3
    fi
    force_edp_on || true
    "$HC" keyword monitor "$CATCHALL" >/dev/null 2>&1 || true
    "$HC" reload >/dev/null 2>&1 || true
    notify-send -u critical "Display reset" \
      "monitors.conf moved aside; eDP-1 re-enabled." 2>/dev/null || true
    ;;

  --enforce)
    # Propagate 3 so a caller/script can tell "unknown" from "fine".
    enforce
    ;;

  --daemon)
    # Hyprland may still be starting. Wait for it to ANSWER (not for monitors to
    # appear — zero monitors is the very state we must act on).
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      # Full `if`, NOT `[ ] && break`: under set -e a failing AND-list is itself
      # a failed command and would kill the guard on the first iteration.
      if hypr_reachable; then break; fi
      sleep 1
    done

    # ⚠️ Refuse to run blind. A daemon that cannot reach the compositor can never
    # enforce anything, and looping quietly forever is worse than exiting: it
    # looks like protection that is not there. Exit non-zero so it lands in the
    # session log instead.
    if ! hypr_reachable; then
      complain_unreachable
      echo "hypr-display-guard: refusing to run as a daemon with no compositor." >&2
      exit 3
    fi

    enforce || true   # a single enforce failure must not kill the daemon

    # ⚠️ THE PERIODIC SWEEP RUNS IN PARALLEL, AND THIS IS LOAD-BEARING.
    #
    # It used to sit AFTER the socket2 read loop. socat holds that connection
    # open for the life of the session, so the read loop never exited and the
    # sweep NEVER STARTED. Proven on marcus 2026-10-27: eDP-1 was disabled by
    # hand, socket2 emitted nothing for a `keyword monitor` change, the read loop
    # blocked, and the display stayed dark indefinitely with the guard running
    # and structurally unable to help. A safety net reachable only when the
    # primary mechanism stops is not a safety net.
    #
    # 5 s, not 30: this is now the mechanism that actually catches a `keyword`
    # change, which socket2 does not report. Cost is one hyprctl per 5 s, and
    # only when the invariant is already satisfied does it do nothing else.
    (
      # Backoff matters now that enforce can escalate to `hyprctl reload`:
      # without it, an unfixable state would trigger a reload every 5 s forever.
      # (`reload` does not re-run exec-once, so it cannot spawn a second guard.)
      fails=0
      while :; do
        sleep 5
        if enforce; then
          fails=0
        else
          fails=$((fails + 1))
          if [ "$fails" -ge 3 ]; then
            echo "hypr-display-guard: could not restore eDP-1 in 3 attempts;" >&2
            echo "hypr-display-guard: backing off. Super+Shift+D forces a reset." >&2
            sleep 60
            fails=0
          fi
        fi
      done
    ) &
    SWEEP=$!
    # shellcheck disable=SC2064
    trap "kill $SWEEP 2>/dev/null || true" EXIT INT TERM

    # Event-driven as well, because it reacts instantly to dock/undock where the
    # sweep would take up to 5 s. ⚠️ NOT sufficient on its own — see above.
    SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hypr/${HYPRLAND_INSTANCE_SIGNATURE:-}/.socket2.sock"
    if [ -S "$SOCK" ]; then
      while IFS= read -r line; do
        case "$line" in
          monitoradded*|monitorremoved*|configreloaded*)
            # Debounce: our own keyword call can wake this again.
            sleep 1
            enforce || true
            ;;
        esac
      done < <(socat -U - "UNIX-CONNECT:$SOCK" 2>/dev/null || true)
    fi

    # If socket2 was unavailable or dropped, the sweep above is still running,
    # so just wait on it rather than starting a second loop.
    wait "$SWEEP" 2>/dev/null || true

    ;;

  *)
    echo "hypr-display-guard: usage: hypr-display-guard [--daemon|--enforce|--reset]" >&2
    exit 2
    ;;
esac
