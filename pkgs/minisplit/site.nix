# Where the mini split lives. ONE place, imported by kindle-segment.nix (DHCP
# reservation + cloud gate), services/minisplit.nix (CLI) and homelab-mcp.nix
# (IPAddressAllow), so the address can't drift between them.
{
  mac = "00:33:7a:bd:66:58";  # learned from its kea lease, 2026-11-22
  ip = "192.168.50.201";      # inside the internet-only .192/26 range
  protocol = "3.5";           # 3.3 and 3.4 fail against this unit
}
