"""wx — severe weather alerting, and mining Ryan Hall for lead time.

Design: ww4/nixos-homelab-improvements docs/weather-watch-research.md.

Three layers with three latencies, three geofences and three output routes:

  LAYER 1  NWS alerts        seconds   tight (his coordinates)  -> ntfy, tiered
  LAYER 3  correlation       hourly    northern Kentucky        -> ad-hoc ntfy
  LAYER 2  Ryan Hall         daily     northern Kentucky        -> newsdesk lane

The load-bearing rule, and the reason these are separate units rather than one
pipeline: RYAN HALL IS NEVER THE WARNING LAYER. A tornado warning cannot wait
on a YouTube upload, a caption pass and an LLM call. Layer 1 exists so that
Layer 2 is allowed to be slow.
"""

__all__ = ["db", "geo", "nws", "spc", "ryan", "extract", "rules", "render", "notify"]
