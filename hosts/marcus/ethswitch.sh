# ethswitch — quick static-IP profile switcher for the WIRED ethernet port.
#
# Profiles come from two places, merged (user entries win on a name clash):
#   - the flake seed file ($ETHSWITCH_SEED), and
#   - an editable user file (~/.config/ethswitch/profiles)
# each a list of "Name|CIDR|GW" lines.
#
# Applying a profile reconfigures a dedicated NM connection ("ethswitch") bound
# to the wired NIC. A persistent toggle decides whether the wired port becomes
# the default route (steals internet from wifi) or stays mgmt-only (never-default).
# State (active profile + default-route flag) is recorded for the Waybar module.
#
# Runs as the user in a terminal popup; privileged nmcli calls go through
# passwordless sudo. Tools (nmcli/fzf/ip/awk/grep) are on PATH via runtimeInputs.

CONN="ethswitch"
USERFILE="${XDG_CONFIG_HOME:-$HOME/.config}/ethswitch/profiles"
STATE="${XDG_CACHE_HOME:-$HOME/.cache}/ethswitch/state"
mkdir -p "$(dirname "$USERFILE")" "$(dirname "$STATE")"
[ -f "$USERFILE" ] || printf '# Your own profiles — one per line: Name|CIDR|GW\n' > "$USERFILE"

cur_profile=""
cur_defroute="no"
if [ -f "$STATE" ]; then
  cur_profile=$(sed -n 's/^profile=//p' "$STATE")
  d=$(sed -n 's/^defroute=//p' "$STATE")
  [ -n "$d" ] && cur_defroute="$d"
fi

write_state() { printf 'profile=%s\ndefroute=%s\n' "$1" "$2" > "$STATE"; }
# Nudge the Waybar netaddr module to refresh now (it listens on SIGRTMIN+8), so
# the bar + default-route note update instantly instead of on the 5s poll.
refresh_bar() { pkill -RTMIN+8 waybar 2>/dev/null || true; }

# Merged profile list: user file first so its lines win the dedup-by-name.
profiles() {
  { cat "$USERFILE"; [ -n "${ETHSWITCH_SEED:-}" ] && cat "$ETHSWITCH_SEED"; } \
    | grep -vE '^[[:space:]]*(#|$)' \
    | awk -F'|' 'NF>=3 && !seen[$1]++ { print }'
}

dev=$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="ethernet"{print $1; exit}')
if [ -z "$dev" ]; then
  echo "No wired ethernet device found (is the cable in?)."
  read -r -p "Press Enter to close… " _ || true
  exit 1
fi
# Ensure our dedicated connection exists, bound to the wired NIC.
if ! nmcli -t -f NAME connection show | grep -qx "$CONN"; then
  sudo nmcli connection add type ethernet con-name "$CONN" ifname "$dev" >/dev/null
fi

apply_static() {  # name cidr gw
  local nd="yes"
  [ "$cur_defroute" = "yes" ] && nd="no"   # default-route ON => never-default OFF
  sudo nmcli connection modify "$CONN" \
    ipv4.method manual ipv4.addresses "$2" ipv4.gateway "$3" \
    ipv4.never-default "$nd" connection.autoconnect yes
  sudo nmcli connection up "$CONN" >/dev/null
  write_state "$1" "$cur_defroute"
  refresh_bar
}

while true; do
  deflabel="OFF"; [ "$cur_defroute" = "yes" ] && deflabel="ON"
  menu=""
  while IFS='|' read -r n c g; do
    menu+="$n — $c gw $g"$'\n'
  done < <(profiles)
  menu+="↻ DHCP — revert wired to automatic"$'\n'
  menu+="+ New — add a profile"$'\n'
  menu+="⇄ Default route via wired — [$deflabel] (toggle)"$'\n'
  menu+="⚙ nmtui — advanced editor"$'\n'
  menu+="✕ Close"

  sel=$(printf '%s' "$menu" | fzf \
        --prompt="wired ($dev) ▸ " \
        --header="active: ${cur_profile:-DHCP}   default-route: $deflabel" \
        --height=100% --reverse) || exit 0
  [ -n "$sel" ] || exit 0

  case "$sel" in
    "↻ DHCP"*)
      sudo nmcli connection modify "$CONN" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.never-default no
      sudo nmcli connection up "$CONN" >/dev/null || true
      write_state "" "$cur_defroute"; refresh_bar
      echo "Wired port set to DHCP."; sleep 1; exit 0 ;;
    "+ New"*)
      read -r -p "Name: " nn
      read -r -p "IP/CIDR (e.g. 192.168.1.22/24): " nc
      read -r -p "Gateway: " ng
      if [ -n "$nn" ] && [ -n "$nc" ]; then
        printf '%s|%s|%s\n' "$nn" "$nc" "$ng" >> "$USERFILE"
        echo "Saved profile '$nn'."
      else
        echo "Skipped (name and IP/CIDR are required)."
      fi
      sleep 1 ;;
    "⇄ Default route"*)
      if [ "$cur_defroute" = "yes" ]; then cur_defroute="no"; else cur_defroute="yes"; fi
      nd="yes"; [ "$cur_defroute" = "yes" ] && nd="no"
      sudo nmcli connection modify "$CONN" ipv4.never-default "$nd"
      if nmcli -t -f NAME connection show --active | grep -qx "$CONN"; then
        sudo nmcli connection up "$CONN" >/dev/null || true
      fi
      write_state "$cur_profile" "$cur_defroute"; refresh_bar ;;
    "⚙ nmtui"*) nmtui || true ;;
    "✕ Close") exit 0 ;;
    *)
      name=${sel%% — *}
      line=$(profiles | awk -F'|' -v n="$name" '$1==n{print; exit}')
      if [ -n "$line" ]; then
        IFS='|' read -r pn pc pg <<<"$line"
        apply_static "$pn" "$pc" "$pg"
        cur_profile="$pn"
        echo "Applied '$pn' ($pc) on $dev."; sleep 1; exit 0
      fi ;;
  esac
done
