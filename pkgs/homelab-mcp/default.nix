# homelab-mcp — the MCP server that lets Claude-in-the-app read this
# knowledgebase, capture notes into it, and file work requests.
#
# Source of truth for development and the security design notes is the private
# repo ww4/homelab-mcp on Forgejo. The source is VENDORED here rather than
# pulled in as a flake input because comin deploys from the PUBLIC GitHub
# mirror of this repo (modules/agent/comin.nix) and therefore has no
# credentials for a private input — a private flake input would break every
# deploy, not just this service.
#
# Consequence worth knowing: everything in this directory is public. That is
# fine for the code (it holds no secrets — all configuration is environment or
# sops) but the design notes documenting the box's security posture are
# deliberately NOT vendored; they stay in the private repo.
{ lib, python3Packages }:

python3Packages.buildPythonApplication {
  pname = "homelab-mcp";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    mcp
    uvicorn
    pydantic
    pydantic-settings
  ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  # The traversal corpus is the security boundary of this service. Running it
  # at build time means a change that breaks path scoping fails the deploy
  # rather than shipping.
  pytestFlags = [ "tests/" ];

  pythonImportsCheck = [ "homelab_mcp" ];

  meta = with lib; {
    description = "MCP server bridging Claude chats to the SilverBullet knowledgebase";
    license = licenses.mit;
    mainProgram = "homelab-mcp";
  };
}
