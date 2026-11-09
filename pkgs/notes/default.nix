# notes — the Keep-style card wall over the flat markdown subset of the
# SilverBullet space (Keep/, Notes/, Inbox/ and the Grocery List).
#
# Vendored here rather than taken as a flake input, for the same reason
# homelab-mcp is: comin deploys from the PUBLIC GitHub mirror of this repo, so
# a private flake input would break every deploy. Everything in this directory
# is therefore public — which is fine, it holds no secrets (all configuration
# is environment, see src/notes/config.py).
#
# Why this exists at all: SilverBullet's editor was used to create five pages
# in sixteen months (two of them tests) while the agent wrote 817. The space —
# flat markdown on disk — was the part that worked; the markdown IDE in front
# of it was not (Chris, 2026-11-08: "it's just overwhelming when you look at
# it"). This serves the same files behind a card wall instead.
{ lib, python3Packages, librsvg }:

python3Packages.buildPythonApplication {
  pname = "notes";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    fastapi
    uvicorn
    pydantic
    pydantic-settings
  ];

  # The manifest needs real PNG icons — Android's installer does not take SVG
  # for the home-screen icon — so render them from the single SVG at build
  # time rather than committing binaries that can drift from it.
  nativeBuildInputs = [ librsvg ];

  preBuild = ''
    rsvg-convert -w 192 -h 192 -o src/notes/static/icon-192.png src/notes/static/icon.svg
    rsvg-convert -w 512 -h 512 -o src/notes/static/icon-512.png src/notes/static/icon.svg
  '';

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  # The traversal corpus is the security boundary of this service: it is
  # unauthenticated, and a note id is a client-supplied path. Running the
  # corpus at build time means a scope regression fails the deploy instead of
  # shipping. tests/test_store.py additionally pins the "don't reformat Chris's
  # files" promise — unknown frontmatter keys survive a write untouched.
  pytestFlags = [ "tests/" ];

  pythonImportsCheck = [ "notes" "notes.app" "notes.paths" "notes.store" ];

  # The front end is a single file shipped as package data. If hatchling's
  # include rules ever drop it the service still starts and then 500s on every
  # page load, so assert it landed rather than finding out at runtime.
  postInstall = ''
    static="$out/${python3Packages.python.sitePackages}/notes/static"
    for f in index.html sw.js service_worker.js manifest.webmanifest icon.svg icon-192.png icon-512.png; do
      if [ ! -s "$static/$f" ]; then
        echo "notes: $f missing from the wheel ($static/$f)" >&2
        exit 1
      fi
    done
    # A service worker that 404s leaves the app online-only and silent about
    # it, so the packaging is asserted rather than assumed.
  '';

  meta = with lib; {
    description = "Keep-style card wall over the SilverBullet space";
    mainProgram = "notes";
    platforms = platforms.linux;
  };
}
