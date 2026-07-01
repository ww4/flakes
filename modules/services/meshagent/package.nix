# MeshAgent — the endpoint agent that connects to a MeshCentral server.
# nixpkgs has no meshagent package, so we patchelf the prebuilt Linux binary
# that ships in the MeshCentral repo (dynamically linked, glibc-only).
{ stdenv, fetchurl, autoPatchelfHook, glibc }:
stdenv.mkDerivation (finalAttrs: {
  pname = "meshagent";
  version = "1.1.59";
  src = fetchurl {
    url = "https://github.com/Ylianst/MeshCentral/raw/${finalAttrs.version}/agents/meshagent_x86-64";
    hash = "sha256-RgrLs4sL2z0ifeZQELGjI/RI7BloYM5JecC4MUdj61Y=";
  };
  dontUnpack = true;
  nativeBuildInputs = [ autoPatchelfHook ];
  buildInputs = [ glibc stdenv.cc.cc.lib ];
  installPhase = ''
    runHook preInstall
    install -Dm755 $src $out/bin/meshagent
    runHook postInstall
  '';
  meta.mainProgram = "meshagent";
})
