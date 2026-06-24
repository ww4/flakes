# Marcus — Hyprland quick start

Beginner cheatsheet for the Hyprland desktop configured in `hyprland.nix`.
KDE Plasma 6 is still installed — at the **SDDM login screen**, click the
session menu (bottom-left) and pick **Hyprland** or **Plasma** anytime.

`Super` = the Windows/Meta key.

## Essentials
| Keys | Action |
|------|--------|
| `Super` + `Return` | Open terminal (kitty) |
| `Super` + `D` (or `Space`) | App launcher (wofi) |
| `Super` + `E` | File manager (Dolphin) |
| `Super` + `B` | Browser (Chrome) |
| `Super` + `Q` | Close focused window |
| `Super` + `L` | Lock screen |
| `Super` + `Shift` + `Q` | Log out of Hyprland |

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
- **Lock** after 5 min idle, **screen off** at 6 min, **suspend** at 12 min (hypridle).
- Wallpaper is a solid Nord background (swaybg) — swap for an image via hyprpaper later.
- USB drives automount (udiskie tray icon); GUI password prompts work (polkit agent).

## Tweaking
Everything is declarative. Edit `hosts/marcus/hyprland.nix`, open a flake PR,
and after merge comin applies it. Common first tweaks:
- **HiDPI scaling:** change the trailing `1` in `monitor = ",preferred,auto,1"` to `1.25`/`1.5`.
- **Idle timeouts:** the `listener` blocks in `services.hypridle.settings`.
- **Bar contents:** `programs.waybar.settings.mainBar.modules-*`.
