# The newsdesk python application.
#
# Factored into its own file rather than a let-binding in default.nix for the
# reason PR #165 taught the hard way: a private `let` in one module is
# invisible to every sibling module, so a derivation defined that way lands on
# the interactive PATH via systemPackages while the unit's own `path` never
# gets it. Hand-runs work; every timer run dies. A file both importers can
# import cannot have that failure.
#
# Stdlib only — no third-party runtime dependencies at all. The tests run in
# checkPhase, so a broken pipeline fails the BUILD, which means a bad merge
# cannot deploy.
{ python3, lib, makeWrapper, cmark-gfm }:

python3.pkgs.buildPythonApplication {
  pname = "newsdesk";
  version = "1.0.0";
  format = "other";

  src = ./.;

  nativeBuildInputs = [ makeWrapper ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    # NEWSDESK_STATE is set to a sandbox path on purpose: the suite refuses to
    # run against anything under /var/lib.
    export NEWSDESK_STATE="$TMPDIR/newsdesk-test"
    ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/${python3.sitePackages} $out/share/newsdesk
    cp -r newsdesk $out/${python3.sitePackages}/
    cp -r data/. $out/share/newsdesk/
    cp judge-prompt.md $out/share/newsdesk/judge-prompt.md

    makeWrapper ${python3.interpreter} $out/bin/newsdesk \
      --add-flags "-m newsdesk.cli" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}" \
      --prefix PATH : "${lib.makeBinPath [ cmark-gfm ]}"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Personal news digest from esoteric RSS sources";
    mainProgram = "newsdesk";
    platforms = platforms.linux;
  };
}
