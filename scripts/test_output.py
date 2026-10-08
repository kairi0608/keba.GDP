#!/usr/bin/env python3
"""納品動画の自動テスト。結果を表示し working/test_results.json に保存する。

  python scripts/test_output.py
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_timeline  # noqa: E402

TL = load_timeline()
RESULTS = []

# 意図して静止させている区間（ポーズ演出・グラフィック）
INTENDED_STILL = [(35.4, 39.05), (41.9, 44.05), (44.0, 60.0), (0.0, 4.0)]
RACE = (4.0, 33.5)
SAFE = {"x0": 60, "x1": 1020, "y0": 160, "y1": 1500}  # 下 420px は各アプリの UI で隠れやすい


def check(name, ok, detail=""):
    RESULTS.append({"test": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def ffprobe(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                   "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,"
                                   "pix_fmt,nb_frames,sample_rate,channels",
                                   "-of", "json", str(path)])
    return json.loads(out)


def ffmpeg_log(args):
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats"] + args, capture_output=True, text=True).stderr


def test_container(path, expect_dur):
    info = ffprobe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    dur = float(info["format"]["duration"])
    tag = path.name
    check(f"{tag}: 尺 {expect_dur}s 以下・±0.05s", dur <= expect_dur + 0.05 and abs(dur - expect_dur) <= 0.05,
          f"{dur:.3f}s")
    check(f"{tag}: 画角 1080x1920 (9:16)", (v["width"], v["height"]) == (1080, 1920), f"{v['width']}x{v['height']}")
    check(f"{tag}: 30fps", v["r_frame_rate"] == "30/1", v["r_frame_rate"])
    check(f"{tag}: H.264 / yuv420p", v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p",
          f"{v['codec_name']} {v['pix_fmt']}")
    check(f"{tag}: フレーム数", int(v.get("nb_frames", 0)) == round(expect_dur * 30), v.get("nb_frames"))
    check(f"{tag}: AAC 音声あり", bool(a) and a[0]["codec_name"] == "aac",
          f"{a[0]['codec_name']} {a[0]['sample_rate']}Hz {a[0]['channels']}ch" if a else "なし")


def test_decode(path):
    log = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                         capture_output=True, text=True).stderr.strip()
    check(f"{path.name}: デコードエラーなし（映像乱れ）", log == "", log[:200])


def test_black(path):
    log = ffmpeg_log(["-i", str(path), "-vf", "blackdetect=d=0.1:pix_th=0.10", "-an", "-f", "null", "-"])
    hits = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", log)
    check(f"{path.name}: 黒コマ（0.1秒以上）なし", not hits, str(hits))


def test_freeze(path):
    log = ffmpeg_log(["-i", str(path), "-vf", "freezedetect=n=-55dB:d=0.4", "-an", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", log)]
    ends += [TL["meta"]["duration"]] * (len(starts) - len(ends))
    spans = list(zip(starts, ends))
    bad = [(a, b) for a, b in spans
           if not any(lo - 0.05 <= a and b <= hi + 0.05 for lo, hi in INTENDED_STILL)]
    race_bad = [(a, b) for a, b in spans if a < RACE[1] and b > RACE[0]]
    check(f"{path.name}: レース本編 4〜33.5秒に静止なし", not race_bad, str(race_bad))
    check(f"{path.name}: 意図しない静止なし", not bad,
          "検出: " + ", ".join(f"{a:.2f}-{b:.2f}" for a, b in spans))


def test_audio(path):
    log = ffmpeg_log(["-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    summary = log[log.rfind("Summary:"):]
    i = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))
    tp = float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary).group(1))
    check(f"{path.name}: ラウドネス -16〜-12 LUFS", -16 <= i <= -12, f"{i} LUFS")
    check(f"{path.name}: トゥルーピーク -1.0 dBTP 以下", tp <= -1.0, f"{tp} dBTP")
    log = ffmpeg_log(["-i", str(path), "-vn", "-af", "silencedetect=n=-50dB:d=1.5", "-f", "null", "-"])
    sil = re.findall(r"silence_start: ([\d.]+)", log)
    check(f"{path.name}: 1.5秒以上の無音なし", not sil, str(sil))


def test_sources():
    sums = (ROOT / "source/SHA256SUMS").read_text().split("\n")
    for line in filter(None, sums):
        h, name = line.split()
        got = hashlib.sha256((ROOT / "source" / name).read_bytes()).hexdigest()
        check(f"元素材が無改変: {name}", got == h, got[:16])


def test_timeline():
    segs = TL["video"]
    gaps = [(a["id"], b["id"]) for a, b in zip(segs, segs[1:]) if abs(a["work_out"] - b["work_in"]) > 1e-6]
    check("タイムライン: 0〜60秒が隙間・重なりなく連続", segs[0]["work_in"] == 0 and
          segs[-1]["work_out"] == TL["meta"]["duration"] and not gaps, str(gaps))
    bad = []
    for s in segs:
        if s["kind"] != "source":
            continue
        dur = TL["sources"][s["src"]]["duration"]
        if not (0 <= s["src_in"] <= s["src_out"] <= dur):
            bad.append(s["id"])
    check("タイムライン: 素材時計が素材の尺に収まる", not bad, str(bad))
    race = [s for s in segs if s["work_in"] < RACE[1] and s["work_out"] > RACE[0]]
    check("A/B が 4〜33.5秒のレース本編を占める",
          all(s["kind"] == "source" and s["src"] in ("A", "B") for s in race)
          and race[0]["work_in"] <= RACE[0] and race[-1]["work_out"] == RACE[1],
          ", ".join(f"{s['id']}({s['src']} {s['src_in']}-{s['src_out']})" for s in race))
    at = {s["id"]: s for s in segs}
    check("RUSH は作品17秒開始・A のカーブ直後", at["V04_rush"]["work_in"] == 17.0 and
          at["V03_A_curve"]["src_in"] == 21.0 and at["V03_A_curve"]["src_out"] == 23.5)
    check("B は作品20秒で再開（素材 0.0秒から）", at["V05_B_growth"]["work_in"] == 20.0 and
          at["V05_B_growth"]["src_in"] == 0.0)
    check("節目: 33.5 オイルショック / 36 政策 / 39台 GDP年表 / 44 問い",
          at["V07_oilshock"]["work_in"] == 33.5 and at["V08_policy"]["work_in"] == 36.0
          and 39.0 <= at["V10_gdp_timeline"]["work_in"] < 40.0 and at["V11_question"]["work_in"] == 44.0)
    late = [s for s in segs if s["work_in"] >= 44.0 and s["kind"] == "source"]
    check("終盤 44〜60秒で A/B を流用していない", not late, str([s["id"] for s in late]))
    lines = TL["lines"]
    ov = [(a["id"], b["id"]) for a, b in zip(lines, lines[1:]) if a["out"] > b["in"] + 1e-6]
    check("字幕どうしが重ならない", not ov, str(ov))
    vt = ROOT / "working/audio/voice_timing.json"
    if vt.exists():
        timing = json.loads(vt.read_text())
        nxt = {a["id"]: b["in"] for a, b in zip(lines, lines[1:])}
        clash = [r["id"] for r in timing if r["id"] in nxt and r["voice_end"] - 0.35 > nxt[r["id"]]]
        check("仮音声が次の台詞に食い込まない", not clash, str(clash))


def test_layout():
    import render  # noqa: WPS433（重いのでここで読む）
    from PIL import Image
    issues = []
    cap_bottom = []
    for c in TL["captions"]:
        sp = render.caption_sprite(c["text"], c.get("sub", ""))
        x0 = (1080 - sp.width) / 2
        box = (x0, 1050, x0 + sp.width, 1050 + sp.height)
        cap_bottom.append((c, box))
        if box[0] < SAFE["x0"] or box[2] > SAFE["x1"] or box[3] > SAFE["y1"]:
            issues.append(f"テロップ {c['text']} {tuple(int(v) for v in box)}")
    for ln in TL["lines"]:
        if not ln.get("subtitle", True):
            continue
        style = ln.get("style", "box")
        sp = render.subtitle_sprite(ln["role"], ln.get("display", ln["text"]), style)
        cy = 1300 if style == "big" else {"top": 470}.get(ln.get("pos"), 1345)
        box = ((1080 - sp.width) / 2, cy - sp.height / 2, (1080 + sp.width) / 2, cy + sp.height / 2)
        if box[0] < SAFE["x0"] or box[2] > SAFE["x1"] or box[1] < SAFE["y0"] or box[3] > SAFE["y1"]:
            issues.append(f"字幕 {ln['id']} {tuple(int(v) for v in box)}")
        for c, cb in cap_bottom:
            if c["in"] < ln["out"] and ln["in"] < c["out"] and cb[3] > box[1] and cb[1] < box[3]:
                issues.append(f"字幕 {ln['id']} とテロップ {c['text']} が重なる")
    check("字幕・テロップがセーフエリア内で重ならない", not issues, "; ".join(issues))
    smallest = min(render.subtitle_sprite(ln["role"], ln.get("display", ln["text"]), ln.get("style", "box")).height
                   for ln in TL["lines"] if ln.get("subtitle", True))
    check("字幕の可読サイズ（本文 58px 以上）", smallest >= 58, f"最小の字幕スプライト高さ {smallest}px")
    _ = Image


def main():
    out = ROOT / "output/rough_cut_v2.mp4"
    prev = ROOT / "output/preview_0-34.mp4"
    test_sources()
    test_timeline()
    test_layout()
    for p, d in ((out, TL["meta"]["duration"]), (prev, 34.0)):
        if p.exists():
            test_container(p, d)
            test_decode(p)
            test_black(p)
            test_freeze(p)
        else:
            check(f"{p.name} が存在する", False)
    if out.exists():
        test_audio(out)
    (ROOT / "working").mkdir(exist_ok=True)
    (ROOT / "working/test_results.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1))
    n_fail = sum(not r["ok"] for r in RESULTS)
    print(f"\n{len(RESULTS) - n_fail}/{len(RESULTS)} passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
