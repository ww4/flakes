# roku

Control a Roku from a NixOS machine, over the protocol Roku already publish.

That protocol is **ECP** — the External Control Protocol — plain HTTP on port
8060, with no authentication, no pairing and no cloud account. It is what
Roku's own phone app speaks, which is why that app keeps working when the
internet does not.

It is a NixOS module. The smallest useful configuration is nothing at all,
because the address is found by SSDP:

```nix
services.roku.enable = true;
```

That gives you a JSON service on `127.0.0.1:8793` and a `roku-find` command.

---

## Why anything sits in front of ECP

A fair question, since the device is already an HTTP server. Four reasons:

| | |
|---|---|
| **A page cannot reach it** | The Roku is a bare LAN address with no CORS headers and no TLS. One proxied origin is the only shape a browser will accept |
| **XML in, JSON out** | Every ECP response is XML. Parsing it once here beats parsing it in every caller |
| **A typo is silent** | `POST /keypress/Hmoe` returns **200** and does nothing. Keys are checked against the protocol's own list, so a bad button is an error with a reason rather than one that mysteriously doesn't work |
| **Policy** | ECP has no notion of what a household wants to allow. The power keys are gated |

What it is *not* is a security boundary. It binds to localhost, and anything
that can reach it could already reach the Roku directly.

## Requirements

- NixOS, with the module imported
- A Roku on the same network, with *Settings → System → Advanced system
  settings → Control by mobile apps* left at its default (network access
  enabled). Disabling that turns ECP off and nothing here can turn it back on
- Nothing else. ECP is HTTP and XML; both are in the Python standard library

## Setting it up

### The minimum

```nix
{
  imports = [ ./modules/services/roku ];
  services.roku.enable = true;
}
```

### Pinning the address

Discovery is multicast, and multicast does not cross VLANs or most wifi client
isolation. If the Roku is somewhere SSDP cannot reach, name it:

```nix
services.roku = {
  enable = true;
  host = "192.168.1.89";
  name = "Living room";
};
```

### Letting it turn the television off

```nix
services.roku.allowPower = true;
```

Off by default, deliberately. Every other ECP key moves a cursor or starts
something, and the worst case is somebody pressing Back. Power is one request
from a dark television while a person is watching it, and a remote in a browser
is easy to leave open on a tablet.

The full option surface is in `default.nix`; `nixos-option services.roku` will
read the descriptions back to you.

---

## The API

Everything is JSON on `127.0.0.1:8793` unless noted.

| | |
|---|---|
| `GET /status` | name, model, software, network, power, and what is on screen |
| `GET /apps` | the installed channels |
| `GET /apps/<id>/icon` | that channel's artwork, passed through as an image |
| `POST /key/<name>` | one button. `?action=keydown` / `keyup` to hold one |
| `POST /type` | `{"text": "…"}` — types into whatever has focus |
| `POST /launch/<id>` | jump straight into a channel; query string is passed on |
| `POST /search` | `{"keyword": "…"}`, plus any of Roku's own search parameters |
| `GET /discover` | what SSDP can see right now |

The keys ECP accepts: `Home`, `Back`, `Up`, `Down`, `Left`, `Right`, `Select`,
`Play`, `Rev`, `Fwd`, `InstantReplay`, `Info`, `Search`, `Enter`, `Backspace`,
`FindRemote`, `VolumeUp`, `VolumeDown`, `VolumeMute`, the `Input*` family, and
— behind `allowPower` — `PowerOn`, `PowerOff`, `Power`.

### Two things worth knowing before you wire up a remote

**The volume keys probably aren't what you want.** A Roku's volume keys emit
HDMI-CEC or IR at the television. If the sound leaves the TV by optical to an
amplifier, those keys do nothing useful and the amplifier's own control is the
one to use.

**Typing is one request per character.** ECP has no "send a string": each
character is its own `Lit_` keypress. `POST /type` does the loop and reports
how many got through, so a half-sent word is visible rather than claimed as a
success.

## When it isn't answering

```sh
roku-find            # what SSDP can see, and whether each one answers ECP
```

A Roku that is **suspended can be silent to SSDP while answering ECP perfectly
well** — one was found in exactly that state on 2026-10-03, invisible to a
search and happy to report its own model number. So "nothing found" is not
"nothing there". If you know the address:

```sh
curl http://<address>:8060/query/device-info
```

If that answers and discovery doesn't, set `host` and move on. If that does not
answer either, check *Control by mobile apps* on the device.

---

## Developing

```sh
python3 -m unittest discover -s ./tests
```

The tests run against a fake device — the process boundary is the only thing
stubbed, so the key allow-list, the power gate and the error handling under
test are the shipped ones. Nothing talks to hardware at build time, and the
package's `checkPhase` runs them, so a regression cannot reach a deploy.

### Comments name people and dates

Comments like `(2026-10-03)` record *why* a decision was made and when — which
device behaved oddly, which measurement. They are deliberate. The history of a
system like this is most of its design rationale.

---

## Licence

See the repository root.
