# The `minisplit` CLI, packaged on its own so homelab-mcp can depend on the SAME
# derivation (same pattern as pkgs/blueiris).
{ writers, python3Packages }:

writers.writePython3Bin "minisplit" {
  libraries = [ python3Packages.tinytuya ];
  flakeIgnore = [ "E501" "E203" "W503" "W504" ];
} (builtins.readFile ./minisplit.py)
