# The netradio glue: the playlist scanner and the wake/idle controller.
# Own file rather than a `let` in default.nix for the reason newsdesk's
# package.nix records — a let-bound derivation is invisible to sibling
# modules and to the interactive PATH.
#
# mutagen is the only third-party dependency (tag reading); the wake server
# is stdlib. The tests run in checkPhase against fakes, so the encoder
# start/stop state machine cannot regress silently into a deploy.
{ python3, lib, makeWrapper }:

python3.pkgs.buildPythonApplication {
  pname = "netradio";
  version = "1.0.0";
  format = "other";

  src = ./.;

  nativeBuildInputs = [ makeWrapper ];
  propagatedBuildInputs = [ python3.pkgs.mutagen ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/${python3.sitePackages}
    cp -r netradio $out/${python3.sitePackages}/
    makeWrapper ${python3.interpreter} $out/bin/netradio \
      --add-flags "-m netradio.cli" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}:${python3.pkgs.mutagen}/${python3.sitePackages}"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Library radio stations: genre playlists + on-demand encoder control";
    mainProgram = "netradio";
    platforms = platforms.linux;
  };
}
