#!/usr/bin/env bash
# 全工程を再実行: GDPデータ → 音声 → 映像（0〜34秒プレビュー／60秒本編）→ 結合 → 確認用素材 → 自動テスト
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
if [ ! -x "$PY" ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
  PY=.venv/bin/python
fi
(cd source && sha256sum -c SHA256SUMS)

$PY scripts/fetch_gdp.py || true
$PY scripts/build_audio.py
$PY scripts/render.py --start 0 --end 34 --out working/preview_video.mp4
$PY scripts/render.py --out working/video_v3.mp4

mux() {
  ffmpeg -v error -y -i "$1" -i working/audio/mix.wav -map 0:v -map 1:a -c:v copy \
    -c:a aac -b:a 192k -ar 48000 -t "$2" -movflags +faststart "$3"
}
mkdir -p output/stems
mux working/preview_video.mp4 34 output/preview_0-34_v3.mp4
mux working/video_v3.mp4 60 output/rough_cut_v3.mp4

ffmpeg -v error -y -i output/rough_cut_v3.mp4 -vf "fps=1,scale=180:320,tile=15x4:padding=3" \
  -frames:v 1 -q:v 3 output/contact_sheet_v3.jpg
for s in voice_guide bgm sfx ambience; do
  ffmpeg -v error -y -i "working/audio/stem_$s.wav" -c:a aac -b:a 160k "output/stems/$s.m4a"
done

$PY scripts/test_output.py
cp working/test_results.json output/test_results_v3.json
