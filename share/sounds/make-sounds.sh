#!/usr/bin/env bash
set -euo pipefail

# Synthesized tones only: no sampled or third-party audio. Outputs are mono
# 22.05 kHz PCM, quieter than full scale, with tails that finish within a sweep.
out=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v ffmpeg >/dev/null || { printf 'ffmpeg is required\n' >&2; exit 1; }
work=$(mktemp -d "$out/.sounds.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

# Five pitch bands, lowest for weak/unknown signal. The envelope remains
# softer than full scale; runtime applies the relative signal-strength gain.
for band in 1 2 3 4 5; do
  frequency=$((520 + (band - 1) * 200))
  ffmpeg -hide_banner -loglevel error -y -f lavfi \
    -i "aevalsrc=0.20*sin(2*PI*$frequency*t)*exp(-8*t)*min(1\\,t/0.006):s=22050:d=0.42" \
    -af 'aecho=0.8:0.7:90|180:0.22|0.09,afade=t=out:st=0.48:d=0.12' \
    -t 0.6 -ac 1 -ar 22050 -c:a pcm_s16le -map_metadata -1 -fflags +bitexact \
    "$work/contact-$band.wav"
done
ffmpeg -hide_banner -loglevel error -y -f lavfi \
  -i 'aevalsrc=0.11*sin(2*PI*1320*t)*exp(-22*t)*min(1\,t/0.004):s=22050:d=0.20' \
  -af 'afade=t=out:st=0.14:d=0.06' \
  -ac 1 -ar 22050 -c:a pcm_s16le -map_metadata -1 -fflags +bitexact \
  "$work/blip.wav"

for band in 1 2 3 4 5; do
  mv -- "$work/contact-$band.wav" "$out/contact-$band.wav"
done
mv -- "$work/blip.wav" "$out/blip.wav"
# Obsolete once-per-revolution tone; no live component references it.
rm -f -- "$out/ping.wav"
printf 'Generated contact-1.wav through contact-5.wav and blip.wav in %s\n' "$out"
