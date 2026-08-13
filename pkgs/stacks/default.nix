# stacks — the physical book catalog: shelf inventory, edition-agnostic
# duplicate checking at book sales, loans, and a wishlist that knows what the
# 2025 flood took.
#
# Source of truth for development is the private repo ww4/stacks on Forgejo.
# The code is VENDORED here for the same reason as homelab-mcp: comin deploys
# from the PUBLIC GitHub mirror of this repo, so a private flake input would
# have no credentials and would break every deploy, not just this service.
#
# Consequence worth stating plainly: everything in this directory becomes
# public. That is fine for the code — it holds no secrets, all configuration is
# environment — but the family's library itself is NOT vendored. The Libib
# exports and the hand-written flood-loss document stay in the private repo
# under data/source/, and the deployed service never needs them: importing is a
# one-off admin task and the database already holds the result.
{ lib, python3Packages, nodejs }:

python3Packages.buildPythonApplication {
  pname = "stacks";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = [ python3Packages.setuptools ];

  dependencies = with python3Packages; [
    sqlalchemy
    alembic
    psycopg
    httpx
    pydantic
    pydantic-settings
    fastapi
    uvicorn
    typer
    rich
  ];

  nativeCheckInputs = [
    python3Packages.pytestCheckHook
    # loadcheck.js loads the browser scripts in one shared global scope, the
    # way a browser does. A duplicate top-level declaration between two files
    # is a SyntaxError that kills the later one silently; each file passes
    # `node --check` alone, so nothing else catches it.
    nodejs
  ];

  # The verdict engine decides whether to spend money on a book, and
  # "unverified never means skip" is the safety property the whole system rests
  # on. Running these at build time means breaking either fails the deploy
  # rather than shipping.
  #
  # The API and repair suites are excluded: they need a live PostgreSQL, which
  # a sandboxed build does not have.
  pytestFlags = [
    "tests/"
    "--ignore=tests/test_api.py"
    "--ignore=tests/test_repair.py"
  ];

  pythonImportsCheck = [ "stacks" "stacks.match" "stacks.api" ];

  # Prove the package can actually start before it is allowed to ship.
  #
  # The first deploy crash-looped 1797 times because the migration scripts were
  # located relative to a source checkout and are simply absent from an
  # installed copy. A unit that cannot start is not something to learn about
  # from a restart counter, so the build asserts the installed layout.
  postInstall = ''
    site=$out/${python3Packages.python.sitePackages}/stacks
    for required in migrations/env.py web/index.html web/app.js web/sw.js; do
      if [ ! -f "$site/$required" ]; then
        echo "PACKAGING ERROR: $required missing from the installed package" >&2
        exit 1
      fi
    done
    if ! ls "$site"/migrations/versions/*.py >/dev/null 2>&1; then
      echo "PACKAGING ERROR: no alembic versions installed" >&2
      exit 1
    fi
  '';

  meta = with lib; {
    description = "Physical book catalog — sale-day scanner, shelf inventory, loans";
    license = licenses.mit;
    mainProgram = "stacks";
  };
}
