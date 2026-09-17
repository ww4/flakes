"""Generate the Liquidsoap script from the runtime station list.

Stations are runtime config now (the admin page adds specialty feeds), and
each needs its own outputs, so the script is rendered at service start
from stations.json rather than baked into the flake. Adding a station =
save + apply (the apply path unit restarts Liquidsoap).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from netradio.config import Config

HEADER = '''settings.log.stdout := true
settings.log.file := false
settings.log.level := 3
settings.server.socket := true
settings.server.socket.path := {socket}
settings.server.socket.permissions := 0o600
settings.server.timeout := -1.

password = environment.get("ICECAST_SOURCE_PASSWORD")
playlists = {playlists}
now_dir = {now_dir}

def station(mount, name) =
  # The DJ (netradio-dj) feeds q_<mount> two items ahead — tracks it chose,
  # and every few of them a rendered break. The plain shuffle is the
  # fallback: it plays whenever the queue is empty (the first track after a
  # wake, or the DJ being down), and hands back at the next track boundary.
  pl = playlist(id="pl_" ^ mount, mode="randomize", reload_mode="watch",
                playlists ^ "/" ^ mount ^ ".m3u")
  q = request.queue(id="q_" ^ mount)
  s = fallback(id="src_" ^ mount, track_sensitive=true, [q, pl])
  # Breaks carry liq_amplify (speech renders ~8 dB under the music).
  s = amplify(1., override="liq_amplify", s)
  s = crossfade(s)
  s = mksafe(s)
  # The first track's metadata is emitted BEFORE the Icecast connection is
  # up, so its ICY title can be lost; keep the last metadata and re-insert
  # it once the mount is connected.
  s = insert_metadata(s)
  last = ref([])
  # The last ten things played, as JSON for the radio page. The re-insert
  # fires this handler a second time for the same track: not added twice.
  history = ref([])
  s.on_metadata(synchronous=true, fun (m) -> begin
    last := m
    log(label=mount, level=3, "now playing: " ^ m["artist"] ^ " - " ^ m["title"])
    if m["title"] != "" then
      # `filename` is the request's path: the page's never-again / request
      # buttons need it (a break's filename is its rendered wav — harmless).
      entry = {{artist = m["artist"], title = m["title"], path = m["filename"],
               kind = (if m["dj"] == "true" then "break" else "track" end), at = time()}}
      same = (fun (h) -> h.artist == entry.artist and h.title == entry.title)
      if not (list.length(history()) > 0 and same(list.hd(default=entry, history()))) then
        history := list.prefix(10, [entry, ...history()])
        file.write(data=json.stringify(history()), now_dir ^ "/" ^ mount ^ ".json")
      end
    end
  end)
  # Two encodes of the same program: 192 kbps, and a 96 kbps "-lo" mount
  # for phones on cellular. Each is an on-demand output of its own.
  def out(id, mnt, kbps) =
    output.icecast(%mp3(bitrate=kbps), id=id, start=false,
                   host="127.0.0.1", port={port}, password=password,
                   mount="/" ^ mnt ^ ".mp3", name=name, genre=name,
                   description="Library station", public=false,
                   on_connect={{ if last() != [] then s.insert_metadata(last()) end }},
                   s)
  end
  out(mount, mount, 192)
  out(mount ^ "-lo", mount ^ "-lo", 96)
end

'''


def render(stations: list[dict], *, socket: str, playlists: str, now_dir: str, port: int) -> str:
    body = HEADER.format(socket=json.dumps(socket), playlists=json.dumps(playlists),
                         now_dir=json.dumps(now_dir), port=port)
    for s in stations:
        body += f"station({json.dumps(s['mount'])}, {json.dumps(s['name'])})\n"
    return body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--playlists", required=True)
    ap.add_argument("--now-dir", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    stations = Config(args.config).stations()
    if not stations:
        print("no stations in config — nothing to run", file=sys.stderr)
        return 1
    args.out.write_text(render(stations, socket=args.socket, playlists=args.playlists,
                               now_dir=args.now_dir, port=args.port))
    print(f"{args.out}: {len(stations)} stations")
    return 0
