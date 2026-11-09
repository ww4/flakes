# The wx python application.
#
# Factored out of default.nix for the reason PR #165 taught the hard way: a
# private `let` in one module is invisible to every sibling, so a derivation
# defined that way reaches the interactive PATH via systemPackages while the
# unit's own `path` never gets it. Hand-runs work; every timer run dies.
#
# Stdlib only — no third-party runtime dependencies. The tests run in
# checkPhase, so a broken threshold or a broken dedup fails the BUILD and a bad
# merge cannot deploy. That matters more here than in most modules: almost
# everything this program decides is only observable during an actual storm.
{ python3, lib, makeWrapper, yt-dlp, gromit-notify }:

python3.pkgs.buildPythonApplication {
  pname = "wx";
  version = "1.0.0";
  format = "other";

  src = ./.;

  nativeBuildInputs = [ makeWrapper ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    # Sandbox path on purpose: the suite refuses to run against anything under
    # /var/lib, because a test run that can reach live state will eventually
    # corrupt it.
    export WX_STATE="$TMPDIR/wx-test"
    mkdir -p "$WX_STATE"
    ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/${python3.sitePackages} $out/share/wx
    cp -r wx $out/${python3.sitePackages}/
    cp extract-prompt.md $out/share/wx/extract-prompt.md

    # yt-dlp for captions, gromit-notify for the ntfy path. Both are called by
    # name from python, so they must be on the wrapper's PATH rather than
    # assumed present in the unit environment.
    makeWrapper ${python3.interpreter} $out/bin/wx \
      --add-flags "-m wx.cli" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}" \
      --prefix PATH : "${lib.makeBinPath [ yt-dlp gromit-notify ]}"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Severe weather alerting, and mining Ryan Hall for lead time";
    mainProgram = "wx";
    platforms = platforms.linux;
  };
}
