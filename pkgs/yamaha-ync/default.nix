# yamaha-ync — Yamaha YNC network-receiver control: library, CLI, JSON API
# and MCP server. Public repo ww4/yamaha-ync on Forgejo (MIT); this is a
# pinned fetch, not a vendored copy — the repo is public, so comin can
# fetch it anonymously. Bump `rev`/`hash` to take a new version.
{ lib, python3Packages, fetchgit }:

python3Packages.buildPythonApplication {
  pname = "yamaha-ync";
  version = "0.1.0";
  pyproject = true;

  src = fetchgit {
    url = "https://git.rosemaryacres.com/ww4/yamaha-ync.git";
    rev = "de9d357345e3ad13bf41933b7fdbb4978b4f2e5f";
    hash = "sha256-eXqz9GrY3VUBa10VHYG6/Hyqq+2n7HXnVRtO7hyvHcs=";
  };

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [ httpx pydantic pydantic-settings mcp starlette uvicorn ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  pytestFlags = [ "tests/" ];
  pythonImportsCheck = [ "yamaha_ync" "yamaha_ync_mcp" ];

  meta = with lib; {
    description = "Yamaha YNC network-receiver control: library, CLI, JSON API, MCP server";
    license = licenses.mit;
    mainProgram = "ync";
  };
}
