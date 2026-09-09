# The `blueiris` CLI, packaged separately from the module that schedules it so
# that homelab-mcp can depend on the SAME derivation rather than rebuilding an
# identical one from the same source. Two copies of a `writePython3Bin` call
# means two copies of the flake8 ignore list, and those drift.
{ writers }:

writers.writePython3Bin "blueiris" {
  flakeIgnore = [ "E501" "E203" "W503" "W504" ];
} (builtins.readFile ./blueiris.py)
