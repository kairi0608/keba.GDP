#!/usr/bin/env python3
"""timeline.json の lines / sfx / bgm / ambience から 60 秒の音声を作る。

- BGM と効果音は numpy で一から合成（既存音源・実況音源は使わない）。
- 実況とナレーションは Open JTalk（pyopenjtalk）による「仮音声」。本番は人の声で録り直す前提。
- 出力: working/audio/mix.wav（-14 LUFS）と各ステム。

  python scripts/build_audio.py [--no-voice]
"""
import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_timeline  # noqa: E402

SR = 48000
TL = load_timeline()
DUR = TL["meta"]["duration"]
N = int(DUR * SR)
OUT = ROOT / "working/audio"
RNG = np.random.default_rng(7)


# ---------------------------------------------------------------- 基本部品

def T(d):
    return np.arange(int(d * SR)) / SR


def place(buf, sig, t, gain=1.0):
    i = int(round(t * SR))
    if i >= len(buf) or len(sig) == 0:
        return
    j = min(len(buf), i + len(sig))
    s0 = max(0, -i)
    buf[max(i, 0):j] += sig[s0:j - i] * gain


def filt(x, kind, fc, order=2):
    if kind == "band":
        b, a = signal.butter(order, [fc[0] / (SR / 2), fc[1] / (SR / 2)], "band")
    else:
        b, a = signal.butter(order, fc / (SR / 2), kind)
    return signal.lfilter(b, a, x)


def mx(*parts):
    """長さの違う信号を頭揃えで足す。"""
    out = np.zeros(max(len(p) for p in parts))
    for p in parts:
        out[:len(p)] += p
    return out


def noise(d):
    return RNG.uniform(-1, 1, int(d * SR))


def edecay(d, tau):
    return np.exp(-T(d) / tau)


def fade(x, a=0.005, r=0.02):
    x = x.copy()
    na, nr = int(a * SR), int(r * SR)
    if na:
        x[:na] *= np.linspace(0, 1, na)
    if nr:
        x[-nr:] *= np.linspace(1, 0, nr)
    return x


def mtof(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def saw(f, d, detune=0.0):
    t = T(d)
    out = np.zeros_like(t)
    for df in ((0,) if not detune else (-detune, 0, detune)):
        ph = (t * f * (1 + df)) % 1.0
        out += 2 * ph - 1
    return out / (1 if not detune else 3)


def sweep(f0, f1, d, shape="exp"):
    t = T(d)
    f = f0 * (f1 / f0) ** (t / d) if shape == "exp" else f0 + (f1 - f0) * t / d
    return np.sin(2 * np.pi * np.cumsum(f) / SR)


def varlp(x, fc):
    """時間で変わるカットオフの一次ローパス（fc は配列）。"""
    a = np.exp(-2 * np.pi * np.asarray(fc) / SR)
    y = np.zeros_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc = (1 - a[i]) * x[i] + a[i] * acc
        y[i] = acc
    return y


# ---------------------------------------------------------------- 打楽器

def kick(d=0.32, f0=140, f1=42):
    return sweep(f0, f1, d) * edecay(d, 0.09)


def snare(d=0.18):
    return fade(filt(noise(d), "band", (1200, 7000)) * edecay(d, 0.05) * 0.8
                + np.sin(2 * np.pi * 190 * T(d)) * edecay(d, 0.03) * 0.5)


def hat(d=0.05):
    return filt(noise(d), "high", 7000) * edecay(d, 0.012) * 0.5


def crash(d=1.4):
    return filt(noise(d), "high", 4000) * edecay(d, 0.45) * 0.5


def hoof(f=95):
    d = 0.09
    return (np.sin(2 * np.pi * f * T(d)) * edecay(d, 0.025)
            + filt(noise(d), "band", (300, 1800)) * edecay(d, 0.012) * 0.6)


# ---------------------------------------------------------------- 音程楽器

def stab(midis, d, cutoff=2500, detune=0.004):
    x = sum(saw(mtof(m), d, detune) for m in midis) / len(midis)
    env = np.minimum(1, T(d) / 0.008) * edecay(d, d * 0.45)
    return fade(filt(x, "low", cutoff) * env)


def pad(midis, d, cutoff=1400):
    x = sum(saw(mtof(m), d, 0.006) for m in midis) / len(midis)
    env = np.minimum(1, T(d) / 0.35) * np.minimum(1, (d - T(d)) / 0.4)
    return filt(x, "low", cutoff) * env


def piano(m, d=1.2):
    f = mtof(m)
    t = T(d)
    x = sum(np.sin(2 * np.pi * f * k * t) * (0.6 ** (k - 1)) * np.exp(-t * (2.2 + k)) for k in range(1, 6))
    return fade(x * np.minimum(1, t / 0.004))


def bass(m, d):
    x = filt(saw(mtof(m), d), "low", 420)
    return fade(x * np.minimum(1, T(d) / 0.005) * edecay(d, d * 0.8))


def bell(f, d=1.2, partials=(1, 2.76, 5.4, 8.93)):
    t = T(d)
    return sum(np.sin(2 * np.pi * f * p * t) * np.exp(-t * (2.5 + p * 0.9)) / (i + 1)
               for i, p in enumerate(partials))


# ---------------------------------------------------------------- BGM

CHORDS = [[0, 4, 7], [7, 11, 14], [9, 12, 16], [5, 9, 12]]  # I V vi IV
ROOTS = {"D": 50, "E": 52}


def bgm_race(buf, t0, t1, bpm, root):
    beat = 60 / bpm
    r = ROOTS[root]
    n_beats = int((t1 - t0) / beat + 1e-6)
    for b in range(n_beats):
        tb = t0 + b * beat
        chord = CHORDS[(b // 4) % 4]
        if b % 2 == 0:
            place(buf, kick(), tb, 0.9)
        else:
            place(buf, snare(), tb, 0.55)
        for k in range(2):
            place(buf, hat(), tb + k * beat / 2, 0.35)
        for k, g in ((0, 0.35), (beat / 6, 0.25), (beat / 3, 0.45)):  # ギャロップ
            place(buf, hoof(80 if k else 110), tb + k, g)
        for k in range(2):
            place(buf, bass(r - 12 + chord[0], beat / 2 * 0.9), tb + k * beat / 2, 0.5)
        if b % 2 == 1:
            place(buf, stab([r + 12 + c for c in chord], beat * 0.45), tb + beat / 2, 0.32)
        if bpm >= 160:
            for k in range(4):
                m = r + 24 + chord[(b * 4 + k) % 3]
                place(buf, stab([m], beat / 4 * 0.9, cutoff=4000, detune=0), tb + k * beat / 4, 0.10)


def bgm_build(buf, t0, t1):
    d = t1 - t0
    t = T(d)
    f = mtof(50) * 2 ** (t / d)
    x = filt(np.sin(2 * np.pi * np.cumsum(f) / SR) + 0.5 * ((np.cumsum(f) / SR) % 1 * 2 - 1), "low", 2500)
    place(buf, x * (t / d) ** 1.5 * 0.45, t0)
    k, tt = 0, t0
    while tt < t1 - 0.02:
        place(buf, snare(0.1), tt, 0.25 + 0.5 * (tt - t0) / d)
        if k % 4 == 0:
            place(buf, kick(), tt, 0.7)
        step = 0.2 * (1 - 0.7 * (tt - t0) / d)
        tt += step
        k += 1
    place(buf, stab([62, 66, 69, 74], d * 0.98, cutoff=1800) * np.linspace(0.2, 1, int(d * 0.98 * SR)), t0, 0.35)


def bgm_intro(buf, t0, t1):
    d = t1 - t0
    place(buf, pad([38, 45, 50], d, 600) * np.linspace(0.4, 1, int(d * SR)), t0, 0.6)


def bgm_tension(buf, t0, t1):
    d = t1 - t0
    place(buf, pad([38, 44], d, 500), t0, 0.5)
    tt = t0
    while tt < t1:
        place(buf, hat(0.03), tt, 0.4)
        tt += 0.25
    tt = t0 + 0.1
    while tt < t1 - 0.3:
        place(buf, kick(0.25, 90, 40), tt, 0.6)
        place(buf, kick(0.25, 90, 40), tt + 0.18, 0.4)
        tt += 0.75


def bgm_heartbeat(buf, t0, t1):
    d = t1 - t0
    place(buf, pad([26, 33], d, 300), t0, 0.35)
    tt = t0 + 0.15
    while tt < t1 - 0.3:
        place(buf, kick(0.3, 80, 38), tt, 0.7)
        place(buf, kick(0.3, 80, 38), tt + 0.2, 0.45)
        tt += 0.85


def bgm_thoughtful(buf, t0, t1):
    prog = [[47, 50, 54], [43, 47, 50], [38, 42, 45], [45, 49, 52]]  # Bm G D A
    step = 60 / 96 / 2
    i, tt = 0, t0 + 0.05
    while tt < t1 - 0.2:
        ch = prog[int((tt - t0) / (step * 8)) % 4]
        place(buf, piano(ch[i % 3] + 12 + (12 if i % 6 == 5 else 0), 1.0), tt, 0.28)
        i, tt = i + 1, tt + step
    for k in range(4):
        ch = prog[k]
        s = t0 + k * step * 8
        if s < t1:
            place(buf, pad([c for c in ch], min(step * 8 + 0.4, t1 - s), 900), s, 0.22)


def bgm_hopeful(buf, t0, t1):
    prog = [[50, 54, 57], [45, 49, 52], [47, 50, 54], [43, 47, 50]]  # D A Bm G
    bar = 1.6
    k, tt = 0, t0
    while tt < 57.6 - 0.05:
        ch = prog[k % 4]
        d = min(bar + 0.3, 57.6 - tt + 0.3)
        place(buf, pad(ch + [ch[0] + 12], d, 1200 + 150 * k), tt, 0.25 + 0.03 * k)
        for i in range(4):
            if tt + i * bar / 4 < 57.6:
                place(buf, piano(ch[i % 3] + 24, 0.9), tt + i * bar / 4, 0.18)
        if tt >= 52.0:
            place(buf, kick(), tt, 0.5)
            place(buf, kick(), tt + bar / 2, 0.35)
        k, tt = k + 1, tt + bar
    d = t1 - 57.6
    end = pad([38, 50, 54, 57, 62, 66], d, 2200) * np.linspace(1, 0, int(d * SR)) ** 0.8
    place(buf, end, 57.6, 0.5)
    for m in (62, 66, 69, 74):
        place(buf, piano(m, 2.2), 57.6, 0.2)


# ---------------------------------------------------------------- 効果音

def sfx(name, cue):
    d = cue.get("dur", 1.0)
    if name == "drumroll":
        out, tt, k = np.zeros(int(d * SR)), 0.0, 0
        while tt < d - 0.05:
            seg = mx(snare(0.08) * (0.3 + 0.7 * tt / d), kick(0.12, 70, 45) * 0.3 * (k % 2))
            place(out, seg, tt)
            tt += 0.11 - 0.06 * tt / d
            k += 1
        return out
    if name == "gate_bell":
        x = bell(1320, 0.9) * (0.6 + 0.4 * np.sign(np.sin(2 * np.pi * 24 * T(0.9))))
        return fade(x) * 0.6
    if name == "gate_clank":
        out = np.zeros(int(0.6 * SR))
        for i in range(5):
            c = mx(filt(noise(0.25), "band", (900, 5000)) * edecay(0.25, 0.04),
                   bell(420 + 37 * i, 0.25, (1, 2.4, 3.9)) * 0.4, kick(0.2, 120, 60) * 0.5)
            place(out, c, i * 0.018)
        return out * 0.6
    if name == "crowd":
        x = filt(noise(d), "band", (300, 2500), 1)
        mod = filt(RNG.uniform(0, 1, len(x)), "low", 6) * 6
        env = np.minimum(1, T(d) / 0.2) * np.linspace(1, 0.2, len(x))
        return x * (0.6 + mod) * env * 0.5
    if name == "gallop":
        out, tt = np.zeros(int(d * SR)), 0.0
        while tt < d - 0.2:
            for k, g in ((0, 0.8), (0.075, 0.6), (0.15, 1.0)):
                place(out, hoof(95 + RNG.uniform(-10, 10)), tt + k + RNG.uniform(0, 0.008), g)
            tt += 0.42
        return out
    if name == "whoosh":
        dd = 0.6
        env = np.sin(np.pi * np.minimum(1, T(dd) / dd)) ** 2
        fc = 300 + 3500 * env
        return varlp(noise(dd), fc) * env * 1.2
    if name == "riser":
        t = T(d)
        fc = 200 * (40 ** (t / d))
        x = varlp(noise(d), fc) * (t / d) ** 1.3 * 1.5
        return x + sweep(200, 800, d) * (t / d) ** 2 * 0.15
    if name == "hit":
        return mx(kick(0.4, 160, 45), snare(0.25) * 0.8, crash(1.2) * 0.8, stab([50, 57, 62, 66], 0.6) * 0.5)
    if name == "impact_big":
        return mx(sweep(70, 28, 1.5) * edecay(1.5, 0.35) * 1.2, filt(noise(0.5), "low", 1500) * edecay(0.5, 0.08),
                  crash(1.8) * 0.9)
    if name == "rank_up":
        out = np.zeros(int(0.8 * SR))
        place(out, bell(1318.5, 0.6, (1, 2, 3)), 0)
        place(out, bell(1760, 0.6, (1, 2, 3)), 0.09)
        return out * 0.5
    if name == "rank_up_big":
        out = np.zeros(int(1.2 * SR))
        for i, f in enumerate((1318.5, 1661.2, 1975.5, 2637)):
            place(out, bell(f, 0.8, (1, 2, 3)), i * 0.07)
        return out * 0.45
    if name == "rank_down":
        out = np.zeros(int(0.6 * SR))
        place(out, fade(filt(saw(440, 0.18), "low", 1500) * edecay(0.18, 0.08)), 0)
        place(out, fade(filt(saw(330, 0.25), "low", 1200) * edecay(0.25, 0.1)), 0.15)
        return out * 0.6
    if name == "alarm":
        t = T(d)
        f = np.where(np.sin(2 * np.pi * 2.2 * t) >= 0, 960, 760)
        x = np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR))
        return filt(x, "low", 2500) * 0.35 * np.minimum(1, (d - t) / 0.3)
    if name == "power_down":
        return fade(sweep(900, 35, 1.0) * np.linspace(1, 0.2, int(SR)))
    if name == "pause_click":
        return mx(kick(0.12, 300, 120) * 0.6, filt(noise(0.02), "high", 2000) * edecay(0.02, 0.004))
    if name == "slap":
        return mx(filt(noise(0.09), "band", (700, 5500)) * edecay(0.09, 0.012) * 1.3, kick(0.08, 220, 110) * 0.6)
    if name == "power_up":
        out = np.zeros(int(0.8 * SR))
        for i, m in enumerate((62, 66, 69, 74, 78, 81)):
            place(out, stab([m], 0.2, cutoff=5000, detune=0.003), i * 0.08, 0.6)
        return out
    if name == "soft_hit":
        return mx(sweep(90, 45, 1.0) * edecay(1.0, 0.25), filt(noise(0.4), "low", 800) * edecay(0.4, 0.1) * 0.4)
    if name == "chime":
        return bell(1046.5 * 2 ** (cue.get("pitch", 0) / 12), 1.3, (1, 2, 3, 4.2)) * 0.6
    if name == "final_hit":
        return mx(sweep(80, 35, 1.6) * edecay(1.6, 0.4), crash(2.0), stab([50, 57, 62, 66, 69], 1.2) * 0.6)
    raise KeyError(name)


# ---------------------------------------------------------------- 仮音声

def tts_line(ln):
    import pyopenjtalk
    x, sr = pyopenjtalk.tts(ln["text"], speed=ln["tts"]["speed"], half_tone=ln["tts"]["half_tone"])
    x = x.astype(np.float64) / 32768.0
    if sr != SR:
        x = signal.resample_poly(x, SR, sr)
    env = np.abs(x) > 10 ** (-38 / 20)
    idx = np.nonzero(env)[0]
    x = x[max(0, idx[0] - int(0.02 * SR)): idx[-1] + int(0.05 * SR)]
    if ln["role"] == "実況":
        x = filt(x, "high", 180)
        x = x + filt(x, "band", (2000, 4500)) * 0.4
        x = np.tanh(x / np.abs(x).max() * 1.8)
    else:
        x = filt(x, "high", 70)
        rev = np.zeros(len(x) + int(0.4 * SR))
        for dl, g in ((0.031, 0.25), (0.047, 0.2), (0.071, 0.15), (0.113, 0.1)):
            place(rev, x, dl, g)
        place(rev, x, 0)
        x = rev
    return fade(x / np.abs(x).max() * 0.9, 0.003, 0.03)


def read_source_audio(path, start, dur):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(dur), "-i", str(ROOT / path),
                          "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(np.float64)


def write_wav(path, x, ch=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(x, -1, 1)
    if ch == 2:
        data = np.stack([data, data], axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((data * 32767).astype(np.int16).tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-voice", action="store_true")
    args = ap.parse_args()

    voice, music, fx, amb = (np.zeros(N) for _ in range(4))
    timing = []
    if not args.no_voice:
        for ln in TL["lines"]:
            x = tts_line(ln)
            d = len(x) / SR
            place(voice, x, ln["in"])
            over = ln["in"] + d - ln["out"]
            timing.append({"id": ln["id"], "in": ln["in"], "voice_end": round(ln["in"] + d, 2), "out": ln["out"],
                           "over_out_s": round(over, 2)})
            if over > 0.35:
                print(f"  WARN {ln['id']}: 仮音声が字幕終了より {over:.2f}s 長い", file=sys.stderr)

    for seg in TL["bgm"]:
        a, b, st = seg["in"], seg["out"], seg["style"]
        if st == "race":
            bgm_race(music, a, b, seg["bpm"], seg["root"])
        elif st != "silence":
            globals()[f"bgm_{st}"](music, a, b)
    # オイルショックで BGM を断ち切る（前区間の余韻も消す）
    music[int(33.5 * SR):int(36.0 * SR)] = 0

    for cue in TL["sfx"]:
        place(fx, sfx(cue["name"], cue), cue["t"], cue.get("gain", 0.5))

    for a in TL["ambience"]:
        x = read_source_audio(TL["sources"][a["src"]]["path"], a["src_in"], a["dur"])
        rms = np.sqrt(np.mean(x ** 2)) + 1e-9
        x = x / rms * 10 ** (-36 / 20)
        place(amb, fade(x, 0.2, 0.3), a["work_in"], a.get("gain", 1.0))

    music = music / (np.abs(music).max() + 1e-9) * 0.55
    fx = fx / (np.abs(fx).max() + 1e-9) * 0.75
    venv = filt(np.abs(voice), "low", 8)
    duck = 1 - 0.5 * np.clip(venv / 0.08, 0, 1)
    mix = voice * 0.95 + music * duck + fx * (0.6 + 0.4 * duck) + amb
    mix[-int(0.25 * SR):] *= np.linspace(1, 0, int(0.25 * SR))
    mix = np.tanh(mix * 1.1) / np.tanh(1.1)

    OUT.mkdir(parents=True, exist_ok=True)
    suffix = "_novoice" if args.no_voice else ""
    raw = OUT / f"mix_raw{suffix}.wav"
    write_wav(raw, mix * 0.8, ch=2)
    for name, x in (("voice_guide", voice), ("bgm", music), ("sfx", fx), ("ambience", amb)):
        if not (args.no_voice and name == "voice_guide"):
            write_wav(OUT / f"stem_{name}.wav", x)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af",
                    "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s16le",
                    str(OUT / f"mix{suffix}.wav")], check=True)
    (OUT / "voice_timing.json").write_text(json.dumps(timing, ensure_ascii=False, indent=1))
    print("wrote", OUT / f"mix{suffix}.wav")


if __name__ == "__main__":
    main()
