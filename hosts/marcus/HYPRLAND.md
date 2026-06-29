# Marcus — Hyprland quick start

Beginner cheatsheet for the Hyprland desktop configured in `hyprland.nix`.
KDE Plasma 6 is still installed — at the **SDDM login screen**, click the
session menu (bottom-left) and pick **Hyprland** or **Plasma** anytime.

`Super` = the Windows/Meta key.

> **Forget a shortcut? Press `Super` + `/`** to pop up a searchable on-screen
> list of every keybind (generated live from the running config).

## Essentials
| Keys | Action |
|------|--------|
| `Super` + `/` | **Show all keybinds** (searchable overlay) |
| `Super` + `Return` | Open terminal (kitty) |
| `Super` + `D` (or `Space`) | App launcher (wofi) |
| `Super` + `E` | File manager (Dolphin) |
| `Super` + `B` | Browser (Chrome) |
| `Super` + `Q` | Close focused window |
| `Super` + `L` | Lock screen (fingerprint or password) |
| `Super` + `Shift` + `E` | Power menu — Lock / Logout / Suspend / Reboot / Shutdown |
| `Super` + `Shift` + `Q` | Log out of Hyprland (back to SDDM → switch user / pick Plasma) |

## Windows & workspaces
| Keys | Action |
|------|--------|
| `Super` + arrows / `h j k l` | Move focus |
| `Super` + `1`–`0` | Switch to workspace 1–10 |
| `Super` + `Shift` + `1`–`0` | Move window to workspace |
| `Super` + scroll | Cycle workspaces |
| `Super` + `F` | Fullscreen |
| `Super` + `V` | Toggle floating |
| `Super` + `J` | Toggle split direction |
| `Super` + drag (LMB/RMB) | Move / resize window |

## Media, screenshots, clipboard
| Keys | Action |
|------|--------|
| Volume / brightness keys | Adjust (held = repeat) |
| Play/Next/Prev keys | Media control (playerctl) |
| `Print` | Screenshot a region → edit (swappy) |
| `Super` + `Print` | Screenshot whole screen → edit |
| `Super` + `C` | Clipboard history picker |

## Auto behaviour
- **Lock screen** (sleep/idle) accepts your **fingerprint** or password; the
  password box stays visible so you can just start typing (no "press a key to
  reveal" step).
- Touchpad uses **traditional** scroll direction (content moves with the scroll
  keys, Windows-style) — flip `input.touchpad.natural_scroll` to change it.
- **Lock** after 5 min idle, **screen off** at 6 min, **suspend** at 12 min (hypridle).
- Wallpaper is a solid Nord background (swaybg) — swap for an image via hyprpaper later.
- USB drives automount (udiskie tray icon); GUI password prompts work (polkit agent).

## Tweaking
Everything is declarative. Edit `hosts/marcus/hyprland.nix`, open a flake PR,
and after merge comin applies it. Common first tweaks:
- **HiDPI scaling:** change the trailing `1` in `monitor = ",preferred,auto,1"` to `1.25`/`1.5`.
- **Idle timeouts:** the `listener` blocks in `services.hypridle.settings`.
- **Bar contents:** `programs.waybar.settings.mainBar.modules-*`.
