# The Roku glue: an ECP client, SSDP discovery, and the JSON service the
# remote talks to. Own file rather than a `let` in default.nix, for the reason
# netradio's package.nix records — a let-bound derivation is invisible to
# sibling modules and to the interactive PATH, and `roku-find` is exactly the
# sort of thing you want on the PATH at 10pm when nothing is answering.
#
# Dependencies: none. ECP is HTTP and XML, and both are in the standard
# library, so this builds anywhere Python does.
#
# The tests run in checkPhase against a fake device, so the key allow-list, the
# power gate and the discovery fallbacks cannot regress silently into a deploy.
# Nothing here talks to real hardware at build time.
{ python3, lib, makeWrapper }:

let
  pyEnv = python3;
in

python3.pkgs.buildPythonApplication {
  pname = "roku-ecp";
  version = "1.0.0";
  format = "other";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [ ./roku ./tests ];
  };

  nativeBuildInputs = [ makeWrapper ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/${python3.sitePackages}
    cp -r roku $out/${python3.sitePackages}/
    makeWrapper ${pyEnv}/bin/python3 $out/bin/roku-api \
      --add-flags "-c 'import sys; from roku.cli import main; sys.exit(main())'" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}"
    makeWrapper ${pyEnv}/bin/python3 $out/bin/roku-find \
      --add-flags "-c 'import sys; from roku.cli import find; sys.exit(find())'" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Control a Roku over its External Control Protocol (ECP)";
    mainProgram = "roku-api";
    platforms = platforms.linux;
  };
}
