# Piper voice catalogue for the switchboard — rhasspy/piper-voices v1.0.0.
#
# `voices.<name>` is a directory with <file>.onnx + <file>.onnx.json side by
# side (piper wants them together). `audition` renders every voice saying the
# same lines at 16 kHz so they can be compared from a handset (dial 9 —
# asterisk.nix). The production voice is `services.switchboard.voice`.
#
# Adding one: find it at https://huggingface.co/rhasspy/piper-voices/tree/v1.0.0,
# add the entry, put ANY hash, build, paste the real ones from the error.
{ lib, fetchurl, runCommand, piper-tts, sox, jq }:

let
  base = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0";

  # name = "<speaker>-<quality>"; label is what the audition says.
  catalogue = {
    "lessac-medium"     = { lang = "en/en_US"; label = "Lessac";               onnx = "sha256-Xv4J5pkCGHgnr2RuGm6dJp3udp+Yd9F7FrG0buqvAZ8="; json = "sha256-7+GcQXvtBV8taZCCSMa6ZQ+hNbyGiw5quz2hgdq2kKA="; };
    "hfc_female-medium" = { lang = "en/en_US"; label = "H F C female";         onnx = "sha256-kUxHN4j8H6i2Os4c3NtEWI9K5SPTqzffFTZhaDWhQLc="; json = "sha256-A/H6BiK4BGMoNZLZesqfbomuw0WlxWtyV3I+AJPFi2w="; };
    "hfc_male-medium"   = { lang = "en/en_US"; label = "H F C male";           onnx = "sha256-0R5AOgK99aZwyHez3Fbg4cjOzm+zAolYYxTf/cCnjLA="; json = "sha256-9mhHQkrtC/mey7XXz95HwKkG9Cag2vfEbzBefSGv2IY="; };
    "ryan-high"         = { lang = "en/en_US"; label = "Ryan";                 onnx = "sha256-s5kNdgbhg+yNv7pwpGBwdPFi3hoMQS4BgNH/YLsVTso="; json = "sha256-xtO5jwgxXLS+vw1J1Q/E/0kbUDxkuUDNPVyihUO0gBE="; };
    "amy-medium"        = { lang = "en/en_US"; label = "Amy";                  onnx = "sha256-s6bke1e4x/vmoM4lGBYaUPWanN2KUINcAssCvdYgbBg="; json = "sha256-laI+tNQpCdON9zu5rH9F9Zfb/N4tG/lSb96vVGaXfXc="; };
    "joe-medium"        = { lang = "en/en_US"; label = "Joe";                  onnx = "sha256-WK/OAyG42cRtfN+cFlAMxVp5O0IgIS26a3D7eIs7rwY="; json = "sha256-PW1UELN5XLGVBZUkfvjwYZBxnm/b+jojVtjsNo4arTM="; };
    "kristin-medium"    = { lang = "en/en_US"; label = "Kristin";              onnx = "sha256-WEmVf5Kcv3IMJY+EWGktYQP/8vDj07GcgllHS7BqGNQ="; json = "sha256-VoFCbUrq0iGV3nBTHu7t20ZJPPr/xXZLLqPbc0KLZRw="; };
    "alan-medium"       = { lang = "en/en_GB"; label = "Alan, British";        onnx = "sha256-CjCWaJMiBedigB8e/Cc2zUsBIDKWIq32K+CeVjOdMzA="; json = "sha256-wPDRJOWJXADnwDs13MgofzGaaZijZbGC3rXI51LujB4="; };
    "cori-high"         = { lang = "en/en_GB"; label = "Cori, British";        onnx = "sha256-RwtN1jTJj4pIUNdib/w9/JB3Riju72YFpt2PiPMKWQM="; json = "sha256-nn+1tWcWEsIvPIHL5Gwa6HsDGkYyvLUJ5Jna1vHirew="; };
    "northern_english_male-medium" = { lang = "en/en_GB"; label = "Northern English male"; onnx = "sha256-V6IZro5jiHPbfRiJMwS+UGnEKGjzkruVw/8X8GkNBok="; json = "sha256-aVV+09l0RjRT6bDAndmaftDlK4uHtks1fb7rJUCpfUc="; };
  };

  # "lessac-medium" + "en/en_US" -> file "en_US-lessac-medium", url dir ".../en/en_US/lessac/medium"
  mk = name: v:
    let
      locale = lib.last (lib.splitString "/" v.lang);
      speaker = lib.head (lib.splitString "-" name);
      quality = lib.last (lib.splitString "-" name);
      file = "${locale}-${name}";
      dir = "${base}/${v.lang}/${speaker}/${quality}";
      onnx = fetchurl { url = "${dir}/${file}.onnx"; hash = v.onnx; };
      json = fetchurl { url = "${dir}/${file}.onnx.json"; hash = v.json; };
    in
    runCommand "piper-voice-${file}" { passthru = { inherit file; inherit (v) label; }; } ''
      mkdir -p $out
      ln -s ${onnx} $out/${file}.onnx
      ln -s ${json} $out/${file}.onnx.json
    '';

  voices = lib.mapAttrs mk catalogue;
  order = lib.attrNames catalogue;   # alphabetical; the audition numbers them in this order

  # path to the .onnx for a catalogue name
  model = name: "${voices.${name}}/${voices.${name}.file}.onnx";
in
{
  inherit voices order model;

  # sample1..sampleN.sln16, 16 kHz s16 mono raw. Each voice introduces itself
  # from its own metadata (quality tier, training set, native rate, pace) so
  # the listener knows what is being compared, then says the same lines.
  audition = runCommand "switchboard-audition" { nativeBuildInputs = [ piper-tts sox jq ]; } ''
    mkdir -p $out
    n=0
    ${lib.concatMapStringsSep "\n" (name: ''
      n=$((n+1))
      j=${voices.${name}}/${voices.${name}.file}.onnx.json
      quality=$(jq -r .audio.quality $j)
      khz=$(jq -r '.audio.sample_rate / 1000' $j)
      pace=$(jq -r '.inference.length_scale' $j)
      dataset=$(jq -r '.dataset | gsub("_"; " ")' $j)
      printf 'Hi, my name is ${voices.${name}.label}, voice number %d. I am a Piper %s quality model trained on the %s voice, native rate %s kilohertz, played here at 16, pace %s. ... This is the Gromit switchboard. What would you like to know? ... CPU 34 degrees. NVMe 29 degrees. The hottest spinning drive is S D B at 44 degrees, across 8 drives. ... Any key for the next voice, star to repeat, pound to hang up.' "$n" "$quality" "$dataset" "$khz" "$pace" \
        | piper --model ${model name} --output_file raw.wav
      sox raw.wav -r 16000 -c 1 -b 16 -e signed-integer -t raw $out/sample$n.sln16
    '') order}
    echo $n > $out/count
  '';
}
