# shoebox — the Keep-style card wall over the flat markdown subset of the
# SilverBullet space (Keep/, Notes/, Inbox/ and the Grocery List).
#
# Vendored here rather than taken as a flake input, for the same reason
# homelab-mcp is: comin deploys from the PUBLIC GitHub mirror of this repo, so
# a private flake input would break every deploy. Everything in this directory
# is therefore public — which is fine, it holds no secrets (all configuration
# is environment, see src/shoebox/config.py).
#
# Why this exists at all: SilverBullet's editor was used to create five pages
# in sixteen months (two of them tests) while the agent wrote 817. The space —
# flat markdown on disk — was the part that worked; the markdown IDE in front
# of it was not (Chris, 2026-11-08: "it's just overwhelming when you look at
# it"). This serves the same files behind a card wall instead.
{ lib, python3Packages }:

python3Packages.buildPythonApplication {
  pname = "shoebox";
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

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  # The traversal corpus is the security boundary of this service: it is
  # unauthenticated, and a note id is a client-supplied path. Running the
  # corpus at build time means a scope regression fails the deploy instead of
  # shipping. tests/test_store.py additionally pins the "don't reformat Chris's
  # files" promise — unknown frontmatter keys survive a write untouched.
  pytestFlags = [ "tests/" ];

  pythonImportsCheck = [ "shoebox" "shoebox.app" "shoebox.paths" "shoebox.store" ];

  # The front end is a single file shipped as package data. If hatchling's
  # include rules ever drop it the service still starts and then 500s on every
  # page load, so assert it landed rather than finding out at runtime.
  postInstall = ''
    page="$out/${python3Packages.python.sitePackages}/shoebox/static/index.html"
    if [ ! -s "$page" ]; then
      echo "shoebox: static/index.html missing from the wheel ($page)" >&2
      exit 1
    fi
  '';

  meta = with lib; {
    description = "Keep-style card wall over the SilverBullet space";
    mainProgram = "shoebox";
    platforms = platforms.linux;
  };
}
