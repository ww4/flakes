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
# `passthru.whisperModel` by store path.
{ lib, python3Packages, fetchurl, runCommand, makeWrapper, sox, piper-tts, systemd }:

let
  voiceName = "en_US-lessac-medium";
  # rhasspy/piper-voices — en_US "lessac", medium quality (~60 MB). Other
  # voices: swap the path + hashes; the .onnx.json must sit beside the .onnx.
  piperVoiceOnnx = fetchurl {
    url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/${voiceName}.onnx";
    hash = "sha256-Xv4J5pkCGHgnr2RuGm6dJp3udp+Yd9F7FrG0buqvAZ8=";
  };
  piperVoiceJson = fetchurl {
    url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/${voiceName}.onnx.json";
    hash = "sha256-7+GcQXvtBV8taZCCSMa6ZQ+hNbyGiw5quz2hgdq2kKA=";
  };
  voiceDir = runCommand "piper-voice-${voiceName}" { } ''
    mkdir -p $out
    ln -s ${piperVoiceOnnx} $out/${voiceName}.onnx
    ln -s ${piperVoiceJson} $out/${voiceName}.onnx.json
  '';
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
      --set-default SWITCHBOARD_PIPER_VOICE ${voiceDir}/${voiceName}.onnx
  '';

  passthru = { inherit whisperModel voiceDir; };

  meta = with lib; {
    description = "Voice front-end for the homelab: Asterisk FastAGI -> whisper -> intents/agent -> piper";
    license = licenses.mit;
    mainProgram = "switchboard";
  };
}
