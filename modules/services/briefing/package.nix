# The briefing python application.
#
# Stdlib only — it reads three files and writes one. The tests run in
# checkPhase for the same reason wx's do: almost everything this program
# decides is about what NOT to put on the card, and those decisions are only
# observable on the day they are wrong.
{ python3, lib, makeWrapper }:

python3.pkgs.buildPythonApplication {
  pname = "briefing";
  version = "1.0.0";
  format = "other";

  src = ./.;

  nativeBuildInputs = [ makeWrapper ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    # Sandbox path on purpose, mirroring the wx suite: a test run that can
    # reach live state will eventually corrupt it, and this program reads the
    # SilverBullet space.
    export BRIEFING_STATE="$TMPDIR/briefing-test"
    mkdir -p "$BRIEFING_STATE"
    ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/${python3.sitePackages}
    cp -r briefing $out/${python3.sitePackages}/

    makeWrapper ${python3.interpreter} $out/bin/briefing \
      --add-flags "-m briefing.cli" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}"
    runHook postInstall
  '';

  meta = with lib; {
    description = "The dashboard's briefing card — open issues, today's plan, something to read";
    mainProgram = "briefing";
    platforms = platforms.linux;
  };
}
