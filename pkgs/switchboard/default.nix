# switchboard — the voice front-end for the homelab (see src/switchboard/__init__.py).
#
# The binary is wrapped so the three external tools it shells out to (sox,
# piper, systemctl) and the voice model resolve regardless of the calling
# unit's PATH — the same lesson as newsdesk: `Environment=PATH=` in a unit
# silently defeats the systemd `path` option, so never depend on PATH from
# inside a unit.
#
# Models are separate derivations (fetchurl) so a voice or whisper model swap
# never rebuilds the Python package; the module points whisper-server at
# `passthru.whisperModel` by store path. Voices live in voices.nix.
{ lib, python3Packages, fetchurl, runCommand, makeWrapper, sox, piper-tts, systemd
, jq
, voice ? "lessac-medium" }:

let
  # The production voice: a catalogue name from voices.nix (module option
  # services.switchboard.voice). Every entry is a fetchurl, so a swap is a
  # one-line config change and a ~60 MB download.
  catalogue = import ./voices.nix { inherit lib fetchurl runCommand piper-tts sox jq; };
  voiceModel = catalogue.model voice;
  # ggml base.en (~148 MB): a couple of seconds per phone utterance on the
  # i5-4690K. small.en is noticeably better on names but ~4x slower.
  whisperModel = fetchurl {
    url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin";
    hash = "sha256-oDd5yG3zMjB19eeWyyzlAp8A7Ihp7uP9+4l6/jbG0AI=";
  };
in
python3Packages.buildPythonApplication {
  pname = "switchboard";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    httpx
    pydantic
    pydantic-settings
  ];

  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  pytestFlags = [ "tests/" ];
  pythonImportsCheck = [ "switchboard" ];

  postFixup = ''
    wrapProgram $out/bin/switchboard \
      --set-default SWITCHBOARD_SOX_BIN ${sox}/bin/sox \
      --set-default SWITCHBOARD_PIPER_BIN ${piper-tts}/bin/piper \
      --set-default SWITCHBOARD_SYSTEMCTL_BIN ${systemd}/bin/systemctl \
      --set-default SWITCHBOARD_PIPER_VOICE ${voiceModel}
  '';

  passthru = { inherit whisperModel catalogue; audition = catalogue.audition; };

  meta = with lib; {
    description = "Voice front-end for the homelab: Asterisk FastAGI -> whisper -> intents/agent -> piper";
    license = licenses.mit;
    mainProgram = "switchboard";
  };
}
