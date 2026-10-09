#!/usr/bin/env python3
"""timeline.json に従って 1080x1920/30fps の映像を合成する。

  python scripts/render.py --start 0 --end 34 --out working/preview_video.mp4
  python scripts/render.py --out working/video_v2.mp4
  python scripts/render.py --stills 3.5,12.5,18 --out working/stills   # 確認用の静止画
"""
import argparse
import math
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (BLACK, COUNTRIES, FPS, GOLD, H, LINE_COLOR, NAME_JA, NAVY, RED, ROOT, W, WHITE,  # noqa: E402
                    boxed_text, clamp01, ease_in_out, ease_out, ease_out_back, flag, flag_badge, gradient,
                    hazard_band, load_gdp, load_timeline, paste_at, paste_center, ranks_for, rounded_box,
                    speed_lines, text_sprite, vignette, with_alpha)

TL = load_timeline()
GDP, GDP_META = load_gdp()
GDP_YEARS = sorted(GDP) if GDP else []
DUR = TL["meta"]["duration"]


# ---------------------------------------------------------------- 素材読み出し

class Reader:
    """ffmpeg で素材を 30fps・1080x1920 に正規化して順読みする。逆行や大きな飛びは開き直す。"""

    def __init__(self, path, dur=None):
        self.path = str(ROOT / path)
        self.dur = dur or float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", self.path]))
        self.proc, self.idx, self.frame = None, None, None

    def _open(self, idx):
        self.close()
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-ss", f"{idx / FPS:.4f}", "-i", self.path, "-an",
             "-vf", f"fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                    f"crop={W}:{H},format=rgb24",
             "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
        self.idx, self.frame = idx - 1, None

    def _read(self):
        buf = self.proc.stdout.read(W * H * 3)
        if len(buf) < W * H * 3:
            return False
        self.frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
        self.idx += 1
        return True

    def get(self, t):
        idx = int(round(min(max(t, 0.0), self.dur - 1.5 / FPS) * FPS))
        if self.proc is None or idx < self.idx or idx > self.idx + 90 or self.frame is None and idx == self.idx:
            self._open(idx)
        while self.idx < idx:
            if not self._read():
                break
        if self.frame is None:
            return np.zeros((H, W, 3), np.uint8)
        return self.frame

    def close(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


READERS = {k: Reader(v["path"], v["duration"]) for k, v in TL["sources"].items()}
ASSET_READERS = {}


def segment_at(t):
    for s in TL["video"]:
        if s["work_in"] <= t < s["work_out"]:
            return s
    return TL["video"][-1]


def src_time(seg, t):
    u, T = t - seg["work_in"], seg["work_out"] - seg["work_in"]
    mode = seg.get("speed", "linear")
    if mode == "freeze":
        return seg["src_in"]
    if mode == "ramp_to_freeze":  # 等速 1.0 → 0 まで直線的に減速
        return seg["src_in"] + u - u * u / (2 * T)
    return seg["src_in"] + u * (seg["src_out"] - seg["src_in"]) / T


def swap_asset(seg):
    p = seg.get("swap_asset")
    if p and (ROOT / p).exists():
        if p not in ASSET_READERS:
            ASSET_READERS[p] = Reader(p)
        return ASSET_READERS[p]
    return None


# ---------------------------------------------------------------- 画像処理

class View:
    """ズーム・揺れの変換。タグやカードの座標を画面に合わせるのに使う。"""

    def __init__(self, z=1.0, x0=0.0, y0=0.0):
        self.z, self.x0, self.y0 = z, x0, y0

    def map(self, x, y):
        return (x - self.x0) * self.z, (y - self.y0) * self.z


def zoom(img, z, cx=W / 2, cy=H / 2, dx=0.0, dy=0.0):
    if z <= 1.0001 and dx == 0 and dy == 0:
        return img, View()
    z = max(z, 1.0001)
    w, h = W / z, H / z
    x0 = min(max(cx - w / 2 + dx, 0), W - w)
    y0 = min(max(cy - h / 2 + dy, 0), H - h)
    return img.resize((W, H), Image.BILINEAR, box=(x0, y0, x0 + w, y0 + h)), View(z, x0, y0)


def grade_warm(img, k):
    if k <= 0.01:
        return img
    a = np.asarray(img).astype(np.float32)
    lum = a.mean(axis=2, keepdims=True)
    a = lum + (a - lum) * (1 + 0.45 * k)
    a *= np.array([1 + 0.10 * k, 1 + 0.02 * k, 1 - 0.12 * k], np.float32)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def grade_red(img, k):
    a = np.asarray(img).astype(np.float32)
    lum = a @ np.array([0.299, 0.587, 0.114], np.float32)
    red = np.stack([lum * 0.95 + 55, lum * 0.20, lum * 0.16], axis=2)
    return Image.fromarray(np.clip(a * (1 - k) + red * k, 0, 255).astype(np.uint8))


def grade_pause(img, sat=0.35, dark=0.82):
    a = np.asarray(img).astype(np.float32)
    lum = a.mean(axis=2, keepdims=True)
    a = (lum + (a - lum) * sat) * dark
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def overlay_color(img, color, alpha):
    if alpha <= 0.005:
        return img
    return Image.blend(img, Image.new("RGB", img.size, color), min(alpha, 1.0))


def shake(t, amp, freq=23.0, seed=0.0):
    return (amp * math.sin(t * freq + seed) * math.cos(t * freq * 0.37 + 1.3),
            amp * math.cos(t * freq * 1.13 + seed) * math.sin(t * freq * 0.53 + 0.4))


# ---------------------------------------------------------------- 部品スプライト

GATE_Y = {"beam": (735, 860), "door": (985, 1226), "name": 1240}
NUM_COL = [((255, 255, 255), BLACK), ((20, 20, 20), WHITE), ((220, 30, 40), WHITE),
           ((30, 80, 200), WHITE), ((250, 210, 0), BLACK)]


@lru_cache(maxsize=16)
def gate_sprite(open_q):
    """発走ゲート（5枠・画面サイズ）。open_q: 0〜10（扉の開き具合）。
    枠の並びは timeline.json の gate.stalls（B の 1 コマ目の走者の並び）。"""
    p = open_q / 10
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    green, dark = (22, 128, 62, 255), (10, 70, 34, 255)
    stalls = TL["gate"]["stalls"]
    x_left, sw = 40, 200
    b0, b1 = GATE_Y["beam"]
    d0, d1 = GATE_Y["door"]
    d.rounded_rectangle((x_left - 10, b0, x_left + sw * 5 + 10, b1), 16, fill=green, outline=dark, width=6)
    for i in range(6):  # 柱
        x = x_left + i * sw
        d.rectangle((x - 9, b1, x + 9, d1 + 4), fill=green, outline=dark, width=2)
    for i, c in enumerate(stalls):
        x0 = x_left + i * sw
        bg, fg = NUM_COL[i]
        d.rounded_rectangle((x0 + 16, b0 + 20, x0 + 70, b1 - 20), 10, fill=bg + (255,), outline=(0, 0, 0, 255), width=3)
        n = text_sprite(str(i + 1), "display", 54, fg)
        im.alpha_composite(n, (int(x0 + 43 - n.width / 2), int((b0 + b1) / 2 - n.height / 2)))
        f = flag(c, 104, 70)
        im.alpha_composite(f, (int(x0 + 82), int((b0 + b1) / 2 - 35)))
        door_w = (sw - 18) / 2 * (1 - p)
        if door_w >= 2:
            for bx0, bx1 in ((x0 + 9, x0 + 9 + door_w), (x0 + sw - 9 - door_w, x0 + sw - 9)):
                d.rectangle((bx0, d0, bx1, d1), fill=(245, 245, 245, 255), outline=dark, width=4)
                d.line((bx0, d0, bx1, d1), fill=green, width=6)
                d.line((bx0, d1, bx1, d0), fill=green, width=6)
        if p < 0.3:  # 扉が開いたら国名は消す（最初の字幕と重ねない）
            name = text_sprite(NAME_JA[c], "sans", 34, WHITE, stroke=5, stroke_fill=NAVY)
            im.alpha_composite(name, (int(x0 + sw / 2 - name.width / 2), GATE_Y["name"]))
    return im


@lru_cache(maxsize=64)
def tag_sprite(label, ptr=0):
    """日本タグ。ptr は矢印の位置（箱の中心からのずれ・px）。"""
    f = flag("JPN", 60, 40)
    t = text_sprite(label, "sans", 42, WHITE)
    w, h = f.width + t.width + 44, 74
    im = Image.new("RGBA", (w, h + 26), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), 20, fill=(220, 0, 40, 240), outline=(255, 255, 255, 255), width=4)
    px = min(max(w / 2 + ptr, 30), w - 30)
    d.polygon([(px - 18, h - 3), (px + 18, h - 3), (px, h + 24)], fill=(255, 255, 255, 255))
    d.polygon([(px - 11, h - 4), (px + 11, h - 4), (px, h + 14)], fill=(220, 0, 40, 255))
    im.alpha_composite(f, (16, (h - f.height) // 2))
    im.alpha_composite(t, (f.width + 26, (h - t.height) // 2 - 2))
    return im


@lru_cache(maxsize=16)
def card_sprite(text, sub, glow=False):
    cw, ch = 360, 150
    im = Image.new("RGBA", (cw + 40, ch + 40), (0, 0, 0, 0))
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((26, 28, cw + 26, ch + 28), 10, fill=(0, 0, 0, 140))
    im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(8)))
    d = ImageDraw.Draw(im)
    if glow:
        g = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(g).rounded_rectangle((8, 8, cw + 32, ch + 32), 18, fill=(255, 210, 0, 230))
        im.alpha_composite(g.filter(ImageFilter.GaussianBlur(9)))
    d.rounded_rectangle((20, 20, cw + 20, ch + 20), 8, fill=(255, 250, 236, 255), outline=(210, 0, 30, 255), width=7)
    t = text_sprite(text, "display", 66, NAVY)
    im.alpha_composite(t, (20 + (cw - t.width) // 2, 34))
    s = text_sprite(sub, "sans", 28, (90, 90, 90), weight=700)
    im.alpha_composite(s, (20 + (cw - s.width) // 2, 118))
    # セロハンテープ
    for x, rot in ((44, 28), (cw - 4, -28)):
        tape = Image.new("RGBA", (90, 30), (235, 225, 190, 190)).rotate(rot, expand=True)
        im.alpha_composite(tape, (int(x - tape.width / 2), int(22 - tape.height / 2)))
    return im


@lru_cache(maxsize=2)
def top_shade():
    a = np.zeros((900, W, 4), np.uint8)
    a[..., 3] = (np.linspace(170, 0, 900)[:, None]).astype(np.uint8)
    return Image.fromarray(a, "RGBA")


def pause_pill():
    im = rounded_box(250, 76, 38, (0, 0, 0, 170), outline=(255, 255, 255, 220), width=3)
    d = ImageDraw.Draw(im)
    d.rectangle((30, 20, 42, 56), fill=WHITE)
    d.rectangle((52, 20, 64, 56), fill=WHITE)
    t = text_sprite("一時停止", "sans", 34, WHITE)
    im.alpha_composite(t, (82, (76 - t.height) // 2 - 2))
    return im


PAUSE_PILL = pause_pill()


# ---------------------------------------------------------------- セグメント演出

def fx_gate(img, t, seg):
    # 静止させた B の 1 コマ目を拡大し、各走者を自分の枠に入れる。扉が開いたら等倍へ戻す
    g = TL["gate"]
    z = 1 + (g["freeze_zoom"] - 1) * (1 - ease_in_out(clamp01((t - 3.3) / 0.7)))
    img, view = zoom(img, z, g["freeze_cx"], g["freeze_cy"])
    shade = with_alpha(top_shade(), 1 - clamp01((t - 3.5) / 0.45))  # 4.0秒の切り替えで明るさが跳ねないように
    img.paste(shade, (0, 0), shade)
    out = 1 - clamp01((t - 3.5) / 0.4)
    for text, col, t0, y in (("このレース、", WHITE, 0.15, 285), ("政治経済で", GOLD, 0.45, 440),
                             ("動いてます。", WHITE, 0.75, 595)):
        p = clamp01((t - t0) / 0.35)
        if p > 0:
            sp = text_sprite(text, "display", 118, col, stroke=12, stroke_fill=NAVY)
            paste_center(img, sp, W / 2, y, alpha=min(1.0, p * 2.5) * out, scale=0.55 + 0.45 * ease_out_back(p))
    if 1.3 <= t < 3.25:
        blink = 0.65 + 0.35 * math.sin(t * 10)
        paste_center(img, boxed_text("ゲートイン完了 ― まもなくスタート", size=36, box=(0, 0, 0, 160)),
                     W / 2, 1365, alpha=clamp01((t - 1.3) / 0.25) * blink)
    open_p = ease_out(clamp01((t - 3.25) / 0.3))
    fade = clamp01((t - 3.5) / 0.3)  # その場で消す（字幕の帯 y≥1242 には入らない）
    paste_at(img, gate_sprite(round(open_p * 10)), 0, 0, alpha=clamp01(t / 0.3) * (1 - fade))
    return img, view


def fx_pre_rush(img, t, seg):
    u = clamp01((t - seg["work_in"]) / (seg["work_out"] - seg["work_in"]))
    img, view = zoom(img, 1 + 0.07 * u * u)
    img = grade_warm(img, 0.6 * u)
    if u > 0.45:
        sl = with_alpha(speed_lines(int(t * 15) % 4, alpha=150), (u - 0.45) / 0.55 * 0.7)
        img.paste(sl, (0, 0), sl)
    return img, view


def fx_rush(img, t, seg):
    u = t - seg["work_in"]
    dx, dy = shake(t, 10)
    img, view = zoom(img, 1.12 + 0.18 * u / 3, cy=H * 0.52, dx=dx, dy=dy)
    img = grade_warm(img, 1.0)
    sl = speed_lines(int(t * 30 / 2) % 4, alpha=200)
    img.paste(sl, (0, 0), sl)
    img.paste(vignette(0.7, (90, 20, 0)), (0, 0), vignette(0.7, (90, 20, 0)))
    beat = (u % 0.4) / 0.4
    img = overlay_color(img, (255, 240, 200), 0.22 * math.exp(-beat * 6))
    p1 = clamp01((t - 17.08) / 0.18)
    if p1 > 0:
        paste_center(img, text_sprite("高度経済成長", "display", 128, (255, 214, 0), stroke=14,
                                      stroke_fill=(150, 0, 20)), W / 2, 760, scale=2.2 - 1.2 * ease_out(p1),
                     alpha=min(1, p1 * 3))
    p2 = clamp01((t - 17.45) / 0.18)
    if p2 > 0:
        pulse = 1 + 0.045 * math.sin(t * 2 * math.pi * 2.5)
        paste_center(img, text_sprite("RUSH!!", "display", 200, WHITE, stroke=16, stroke_fill=RED),
                     W / 2, 960, scale=(2.4 - 1.4 * ease_out(p2)) * pulse, alpha=min(1, p2 * 3), rot=6)
    img = overlay_color(img, WHITE, clamp01((t - 19.78) / 0.2) ** 2)
    return img, view


def fx_flash_in(img, t, seg):
    u = t - seg["work_in"]
    if u < 0.45:
        img, view = zoom(img, 1 + 0.10 * (1 - ease_out(u / 0.45)))
        img = overlay_color(img, WHITE, 1 - ease_out(u / 0.35))
        return img, view
    return img, View()


def fx_oilshock(img, t, seg):
    u = t - seg["work_in"]
    amp = 22 * math.exp(-u * 3) + 5
    dx, dy = shake(t, amp, 31)
    img, view = zoom(img, 1.06, dx=dx, dy=dy)
    img = grade_red(img, 0.88 - 0.25 * clamp01((u - 1.9) / 0.6))
    img = overlay_color(img, (255, 0, 0), 0.16 * (0.5 + 0.5 * math.sin(t * 2 * math.pi * 2.2)))
    img = overlay_color(img, WHITE, 0.9 * math.exp(-u * 18))
    band = hazard_band(W + 112)
    off = int(t * 140) % 56
    img.paste(band, (-off, 92), band)
    img.paste(band, (off - 56, 1610), band)
    p = clamp01((t - 33.55) / 0.16)
    if p > 0:
        sx, sy = shake(t, 6 * (1 - clamp01(u / 1.5)), 40, 2)
        paste_center(img, text_sprite("オイルショック！", "display", 106, (255, 230, 0), stroke=12,
                                      stroke_fill=BLACK), W / 2 + sx, 450 + sy, scale=1.9 - 0.9 * ease_out(p))
    return img, view


def policy_layer(img, t, view=View(), glow_from=38.35):
    for i, c in enumerate(TL["policy_cards"]):
        t_appear = 36.05 + 0.08 * i
        if t < t_appear:
            continue
        lx, ly = 250, 700 + 165 * i
        tx, ty = view.map(c["x"], c["y"])
        fly = clamp01((t - (c["t"] - 0.22)) / 0.22)
        x, y = lx + (tx - lx) * ease_in_out(fly), ly + (ty - ly) * ease_in_out(fly)
        rot = c["rot"] * ease_in_out(fly)
        glow = t >= glow_from and int((t - glow_from) * 8) % 2 == 0
        sc = 0.6 + 0.4 * ease_out_back(clamp01((t - t_appear) / 0.2))
        if t >= c["t"]:
            sc = 1 + 0.25 * (1 - ease_out(clamp01((t - c["t"]) / 0.14)))
        paste_center(img, card_sprite(c["text"], c["sub"], glow), x, y, scale=sc, rot=rot)
        ring_p = clamp01((t - c["t"]) / 0.25)
        if 0 < ring_p < 1:
            r = 60 + 160 * ring_p
            ring = Image.new("RGBA", (int(2 * r + 8), int(2 * r + 8)), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse((4, 4, 2 * r + 4, 2 * r + 4), outline=(255, 255, 255, int(255 * (1 - ring_p))),
                                         width=10)
            img.paste(ring, (int(x - r - 4), int(y - r - 4)), ring)


def fx_policy(img, t, seg):
    img = grade_pause(img)
    paste_at(img, PAUSE_PILL, 1020 - PAUSE_PILL.width, 220)
    policy_layer(img, t)
    img = overlay_color(img, WHITE, clamp01((t - 38.85) / 0.15) ** 2 * 0.9)
    return img, View()


def fx_dash(img, t, seg):
    u = t - seg["work_in"]
    img, view = zoom(img, 1.06)
    img = grade_warm(img, 0.8)
    sl = speed_lines(int(t * 15) % 4, alpha=210)
    img.paste(sl, (0, 0), sl)
    img = overlay_color(img, WHITE, 0.9 * (1 - ease_out(u / 0.25)))
    return img, view


SEG_FX = {"gate": fx_gate, "pre_rush": fx_pre_rush, "rush": fx_rush, "flash_in": fx_flash_in,
          "oilshock": fx_oilshock, "policy": fx_policy, "dash": fx_dash}


# ---------------------------------------------------------------- GDP 年表

CH_X0, CH_X1, CH_GOAL = 170, 820, 978
LANE_Y0, LANE_H = 460, 150


def lane_y(rank):
    return LANE_Y0 + (rank - 1) * LANE_H


def year_x(y):
    return CH_X0 + (y - GDP_YEARS[0]) / (GDP_YEARS[-1] - GDP_YEARS[0]) * (CH_X1 - CH_X0)


@lru_cache(maxsize=1)
def chart_bg():
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # 文字レイヤー
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    for r in range(1, 6):
        y = lane_y(r)
        d.rounded_rectangle((136, y - 68, 1018, y + 68), 22,
                            fill=(255, 255, 255, 14 if r % 2 else 24))
        lbl = text_sprite(f"{r}位", "sans", 34, (200, 210, 235))
        lay.alpha_composite(lbl, (int(96 - lbl.width / 2), int(y - lbl.height / 2)))
    head = text_sprite("名目GDP順位の推移", "sans", 56, WHITE)
    im.alpha_composite(head, (60, 238))
    sub = text_sprite("5か国中の順位 ／ 出典：世界銀行（名目GDP・米ドル）", "sans", 27, (190, 200, 225), weight=600)
    im.alpha_composite(sub, (62, 318))
    if GDP_YEARS:
        for y in (GDP_YEARS[0], 1980, 2000, GDP_YEARS[-1]):
            lb = text_sprite(str(y), "sans", 30, (190, 200, 225), weight=700)
            im.alpha_composite(lb, (int(year_x(y) - lb.width / 2), LANE_Y0 + 4 * LANE_H + 82))
        fz = text_sprite("未来", "sans", 30, GOLD, weight=800)
        im.alpha_composite(fz, (int((CH_X1 + CH_GOAL) / 2 + 22 - fz.width / 2), LANE_Y0 + 4 * LANE_H + 82))
        # 未来ゾーン
        zone = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(zone).rectangle((CH_X1 + 12, LANE_Y0 - 70, CH_GOAL - 14, LANE_Y0 + 4 * LANE_H + 70),
                                       fill=(255, 196, 0, 30))
        lay.alpha_composite(zone)
        # ゴール
        for i, yy in enumerate(range(LANE_Y0 - 70, LANE_Y0 + 4 * LANE_H + 70, 26)):
            for j in range(2):
                c = (255, 255, 255, 255) if (i + j) % 2 == 0 else (20, 20, 20, 255)
                d.rectangle((CH_GOAL + j * 13, yy, CH_GOAL + 13 + j * 13, yy + 26), fill=c)
        g = text_sprite("GOAL", "display", 38, GOLD, stroke=5, stroke_fill=NAVY)
        im.alpha_composite(g, (int(min(CH_GOAL + 13 - g.width / 2, 1020 - g.width)), LANE_Y0 - 70 - g.height - 2))
    bg = Image.alpha_composite(gradient((9, 18, 46), (20, 38, 84)).convert("RGBA"), lay)
    return Image.alpha_composite(bg, im)


def jp_callouts():
    ranks = {y: ranks_for(GDP[y])["JPN"] for y in GDP_YEARS}
    best = min(ranks.values())
    first_best = next(y for y in GDP_YEARS if ranks[y] == best)
    out = [GDP_YEARS[0], first_best]
    prev = best
    for y in GDP_YEARS:
        if y <= first_best or ranks[y] == prev:
            continue
        nxt = [ranks.get(y + k) for k in range(3)]
        if all(r == ranks[y] for r in nxt if r is not None) and len([r for r in nxt if r]) >= 3:
            out.append(y)
            prev = ranks[y]
    if GDP_YEARS[-1] not in out:
        out.append(GDP_YEARS[-1])
    return [(y, ranks[y]) for y in sorted(set(out))]


def chart_frame(t, seg_in=39.9):
    if not GDP:
        im = gradient((9, 18, 46), (20, 38, 84)).copy()
        paste_center(im, boxed_text("DATA_PENDING\n世界銀行データ未取得\n（順位は表示しません）", size=54,
                                    box=(150, 0, 0, 220)), W / 2, 820)
        return im
    im = chart_bg().copy()
    y0, y1 = GDP_YEARS[0], GDP_YEARS[-1]
    prog = y0 + (y1 - y0) * ease_in_out(clamp01((t - (seg_in + 0.15)) / 1.45))
    fut = ease_out(clamp01((t - (seg_in + 1.6)) / 0.45))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    ranks = {y: ranks_for(GDP[y]) for y in GDP_YEARS}

    def pts(c):
        p = [(year_x(y), lane_y(ranks[y][c])) for y in GDP_YEARS if y <= prog]
        yi = int(prog)
        if yi < y1:
            f = prog - yi
            p.append((year_x(prog), lane_y(ranks[yi][c] * (1 - f) + ranks[yi + 1][c] * f)))
        return p

    heads = {}
    for c in [c for c in COUNTRIES if c != "JPN"] + ["JPN"]:
        p = pts(c)
        if len(p) >= 2:
            if c == "JPN":
                d.line(p, fill=(255, 255, 255, 255), width=20, joint="curve")
                d.line(p, fill=LINE_COLOR[c] + (255,), width=12, joint="curve")
            else:
                d.line(p, fill=LINE_COLOR[c] + (220,), width=7, joint="curve")
        heads[c] = p[-1]
    im.alpha_composite(layer)
    for c in COUNTRIES:
        if c == "JPN":
            continue
        hx, hy = heads[c]
        f = flag(c, 54, 36)
        im.alpha_composite(f, (int(hx + 10), int(hy - 18)))
        if prog >= y1:
            n = text_sprite(NAME_JA[c], "sans", 24, LINE_COLOR[c], weight=800)
            im.alpha_composite(n, (int(hx + 10), int(hy + 20)))
    for y, r in jp_callouts():
        if prog + 0.01 >= y:
            a = 1.0 if prog >= y1 else clamp01((prog - y) / 1.5 + 0.4)
            lb = boxed_text(f"{y} {r}位", size=30, box=(220, 0, 40, 235), pad=(14, 6), radius=12,
                            outline=(255, 255, 255, 255), outline_w=3)
            ly = lane_y(r) - 76 if r > 1 and y != y1 else lane_y(r) + 76
            paste_center(im, lb, year_x(y), ly, alpha=a)
    jx, jy = heads["JPN"]
    if fut > 0:
        gx = CH_X1 + (CH_GOAL - 52 - CH_X1) * fut
        dd = ImageDraw.Draw(im)
        for x in range(int(CH_X1), int(gx), 22):
            dd.line((x, jy, min(x + 12, gx), jy), fill=(255, 45, 85), width=10)
        jx = gx
        q = text_sprite("?", "display", 60, GOLD, stroke=6, stroke_fill=NAVY)
        im.alpha_composite(q, (int(jx - q.width / 2), int(jy - 118)))
    badge = flag_badge("JPN", 70)
    im.alpha_composite(badge, (int(jx - badge.width / 2), int(jy - badge.height / 2)))
    yr = "未来" if fut > 0 else str(int(prog))
    ys = text_sprite(yr, "display", 72, GOLD, stroke=6, stroke_fill=NAVY)
    paused = t >= seg_in + 2.05
    if paused:
        im = Image.fromarray((np.asarray(im.convert("RGB")).astype(np.float32) * 0.86).astype(np.uint8)).convert("RGBA")
        im.alpha_composite(PAUSE_PILL, (1020 - PAUSE_PILL.width, 250))
    else:
        im.alpha_composite(ys, (W - ys.width - 50, 230))
    return im.convert("RGB")


# ---------------------------------------------------------------- 問い・プレースホルダー

@lru_cache(maxsize=1)
def question_bg():
    im = gradient((6, 12, 32), (18, 30, 70)).convert("RGBA")
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    for i in range(6):
        r = 520 + i * 120
        d.arc((W / 2 - r * 1.3, 1500 - r * 0.55, W / 2 + r * 1.3, 1500 + r * 0.55), 180, 360,
              fill=(255, 255, 255, 22), width=4)
    im.alpha_composite(lay)
    im.alpha_composite(vignette(0.6))
    return im.convert("RGB")


def graphic_question(t, seg):
    u = t - seg["work_in"]
    im = question_bg().copy()
    if u < 0.35:
        im = Image.blend(chart_frame(43.99), im, ease_out(u / 0.35))
    for text, col, size, t0, y in (("その順位を", WHITE, 100, 44.35, 420), ("決めているのは、", WHITE, 100, 45.1, 545),
                                   ("何だろう？", GOLD, 136, 45.9, 700)):
        p = clamp01((t - t0) / 0.3)
        if p > 0:
            paste_center(im, text_sprite(text, "display", size, col, stroke=10, stroke_fill=NAVY), W / 2, y,
                         alpha=min(1, p * 2), scale=0.7 + 0.3 * ease_out_back(p))
    for i, wd in enumerate(TL["question_words"]):
        p = clamp01((t - wd["t"]) / 0.28)
        if p <= 0:
            continue
        cx, cy = (300 if i % 2 == 0 else 780), (990 if i < 2 else 1215)
        tile = word_tile(wd["text"], wd["sub"], tuple(wd["color"]))
        pulse = 1 + 0.02 * math.sin((t - 48.6) * 5) if t > 48.6 else 1
        paste_center(im, tile, cx, cy, alpha=min(1, p * 2), scale=(0.5 + 0.5 * ease_out_back(p)) * pulse)
    im = Image.blend(im, Image.new("RGB", im.size, BLACK), clamp01((t - 49.75) / 0.25) * 0.6)
    return im


@lru_cache(maxsize=8)
def word_tile(text, sub, color):
    tw, th = 450, 190
    im = rounded_box(tw, th, 28, color + (255,), outline=(255, 255, 255, 255), width=5)
    t = text_sprite(text, "display", 86 if len(text) <= 2 else 70, WHITE, stroke=6, stroke_fill=(0, 0, 0, 90))
    im.alpha_composite(t, ((tw - t.width) // 2, 22))
    s = text_sprite(sub, "sans", 30, WHITE, weight=800)
    im.alpha_composite(s, ((tw - s.width) // 2, th - s.height - 18))
    return im


def placeholder_frame(kind, title, spec):
    im = Image.new("RGB", (W, H), (35, 39, 46))
    d = ImageDraw.Draw(im)
    for x in range(-H, W, 90):
        d.line((x, 0, x + H, H), fill=(42, 47, 56), width=34)
    if kind == "lecture":
        d.rectangle((120, 540, 960, 880), fill=(110, 78, 46))
        d.rectangle((140, 560, 940, 860), fill=(30, 77, 58))
        chalk = text_sprite("政治 × 経済 × 社会", "sans", 60, (235, 240, 230), weight=700)
        im.paste(chalk, (int(540 - chalk.width / 2), 650), chalk)
        d.line((220, 780, 560, 780), fill=(220, 225, 215), width=4)
        d.line((620, 770, 860, 810), fill=(220, 225, 215), width=4)
        for row in range(3):
            y = 930 + row * 80
            for col in range(5):
                x = 170 + col * 185 - row * 6
                d.ellipse((x + 40, y - 40, x + 90, y + 10), fill=(70, 76, 88))
                d.rounded_rectangle((x, y + 8, x + 130, y + 40), 8, fill=(90, 96, 110))
    else:
        hz = 640
        d.polygon([(0, H), (W, H), (W, hz), (0, hz)], fill=(60, 66, 76))
        d.polygon([(-200, 1500), (1280, 1500), (700, hz), (380, hz)], fill=(176, 82, 59))
        for i in range(-3, 4):
            d.line((540 + i * 250, 1500, 540 + i * 45, hz), fill=(240, 240, 240), width=6)
        d.rectangle((0, hz - 4, W, hz), fill=(200, 200, 200))
    d.rectangle((40, 40, W - 40, H - 40), outline=(255, 214, 0), width=6)
    pill = boxed_text("未撮影｜差し替え用プレースホルダー", size=36, fill=BLACK, box=(255, 214, 0, 255))
    im.paste(pill, (int(W / 2 - pill.width / 2), 218), pill)
    tt = text_sprite(title, "sans", 46, WHITE)
    im.paste(tt, (int(W / 2 - tt.width / 2), 310), tt)
    sp = text_sprite(spec, "sans", 27, (215, 220, 230), weight=600, spacing=8)
    im.paste(sp, (int(W / 2 - sp.width / 2), 385), sp)
    return im


@lru_cache(maxsize=2)
def placeholder_cached(kind):
    if kind == "lecture":
        return placeholder_frame(kind, "大学の授業風景（政治・経済の講義）",
                                 "推奨：縦9:16・約6.5秒\n教室後方からのワイド → 板書・ノートのアップ\n"
                                 "assets/05_lecture.mp4 を置くと自動で差し替え")
    return placeholder_frame(kind, "被り物を外した学生が走り出す",
                             "推奨：縦9:16・約3.5秒\nトラックの正面から → 背中を追う\n"
                             "assets/06_future_run.mp4 を置くと自動で差し替え")


def graphic_frame(seg, t):
    g = seg["graphic"]
    if g == "gdp_timeline":
        return chart_frame(t, seg["work_in"])
    if g == "question":
        return graphic_question(t, seg)
    return placeholder_cached(g).copy()


def fx_future(img, t):
    p = clamp01((t - 57.0) / 0.3)
    if p > 0:
        paste_center(img, text_sprite("未来のレースを、\n誰がつくる？", "display", 98, WHITE, stroke=12,
                                      stroke_fill=NAVY, spacing=24), W / 2, 880,
                     alpha=min(1, p * 2), scale=0.7 + 0.3 * ease_out_back(p))
    ec = TL["endcard"]
    p = clamp01((t - ec["in"]) / 0.35)
    if p > 0:
        card = rounded_box(860, 190, 30, (255, 255, 255, 250), outline=(220, 0, 40, 255), width=8)
        tx = text_sprite(ec["text"], "sans", 82, NAVY)
        card.alpha_composite(tx, ((card.width - tx.width) // 2, (card.height - tx.height) // 2 - 4))
        small = text_sprite("このレース、政治経済で動いてます。", "sans", 32, WHITE, stroke=4, stroke_fill=NAVY)
        paste_center(img, small, W / 2, 1150 + 40 * (1 - ease_out(p)), alpha=p)
        paste_center(img, card, W / 2, 1290 + 60 * (1 - ease_out(p)), alpha=p)
    return img


# ---------------------------------------------------------------- 共通オーバーレイ

class State:
    board_pos = {}
    chip_label = None
    chip_changed = 0.0


def board_year(t):
    keys = TL["board"]["year_keys"]
    if t <= keys[0][0]:
        return keys[0][1]
    for (a, ya), (b, yb) in zip(keys, keys[1:]):
        if a <= t < b:
            return ya + (yb - ya) * (t - a) / (b - a)
    return keys[-1][1]


SAFE_X = (60, 1020)


def draw_tags(img, t, seg, view):
    if seg.get("kind") != "source":
        return
    st = src_time(seg, t)
    for tag in TL["tags"]:
        if seg["src"] != tag["src"]:
            continue
        for a, b in tag["windows"]:
            if a <= t < b:
                tr = tag["track"]
                if not tr[0][0] <= st <= tr[-1][0]:
                    continue
                for (t0, x0, y0), (t1, x1, y1) in zip(tr, tr[1:]):
                    if t0 <= st <= t1:
                        f = (st - t0) / (t1 - t0)
                        x, y = view.map(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
                        alpha = clamp01((t - a) / 0.15) * clamp01((b - t) / 0.15)
                        w = tag_sprite(tag["label"]).width
                        bx = min(max(x - w / 2, SAFE_X[0]), SAFE_X[1] - w)  # 箱はセーフエリア内、矢印で頭を指す
                        sp = tag_sprite(tag["label"], int(round((x - (bx + w / 2)) / 8) * 8))
                        paste_at(img, sp, bx, y - 105 * view.z - sp.height, alpha)
                        break


CHIP_LEFT = 62


def chip_entry(t):
    cur = None
    for e in TL["year_chip"]:
        if e[0] <= t:
            cur = e
    return cur


def draw_year_chip(img, t):
    e = chip_entry(t)
    if not e or e[1] is None:
        State.chip_label = None
        return
    label, sub = e[1], e[2]
    if label == "board":
        label, sub = str(int(board_year(t))), ("高度経済成長" if board_year(t) < 1973 else "")
    if label != State.chip_label:
        State.chip_label, State.chip_changed = label, t
    red = label == "1973"
    ys = text_sprite(label, "display", 76, WHITE if red else NAVY)
    bw = ys.width + 28  # 文字幅から箱を作る（はみ出し防止）
    box = rounded_box(bw, 106, 20, ((220, 0, 30, 245) if red else (255, 196, 0, 245)),
                      outline=(255, 255, 255, 255), width=4)
    box.alpha_composite(ys, ((bw - ys.width) // 2, (106 - ys.height) // 2 - 2))
    pop = 1 + 0.08 * (1 - ease_out(clamp01((t - State.chip_changed) / 0.22)))
    left = CHIP_LEFT + bw * 0.04  # 拡大しても x=60 より内側
    paste_center(img, box, left + bw / 2, 200 + 53, scale=pop)
    if sub:
        s = boxed_text(sub, size=28, box=(11, 22, 52, 220), pad=(16, 6), radius=12)
        paste_at(img, s, left + bw + 14, 200 + 53 - s.height / 2)


def fmt_oku(usd):
    return f"{usd / 1e8:,.0f}億ドル"  # 全行を同じ単位にする


def draw_board(img, t):
    b = TL["board"]
    if not b["work_in"] <= t < b["work_out"]:
        State.board_pos = {}
        return
    a = clamp01((t - b["work_in"] - 0.1) / 0.3)
    slide = -60 * (1 - ease_out(a))
    bx, by, bw = 62 + slide, 385, 500
    rows_h = 66
    panel = rounded_box(bw, 60 + rows_h * 5 + 44, 20, (11, 22, 52, 205), outline=(255, 255, 255, 90), width=2)
    hd = text_sprite(b["title"], "sans", 30, GOLD)
    panel.alpha_composite(hd, (20, 12))
    if not GDP:
        msg = text_sprite("DATA_PENDING\n世界銀行データ未取得", "sans", 34, (255, 120, 120))
        panel.alpha_composite(msg, (20, 120))
        paste_at(img, panel, bx, by, a)
        return
    yr = int(board_year(t))
    yr = min(max(yr, GDP_YEARS[0]), GDP_YEARS[-1])
    vals = GDP[yr]
    rk = ranks_for(vals)
    for c in COUNTRIES:
        cur = State.board_pos.get(c, rk[c])
        State.board_pos[c] = cur + (rk[c] - cur) * 0.28
    for c in sorted(COUNTRIES, key=lambda c: c == "JPN"):
        ry = 60 + (State.board_pos[c] - 1) * rows_h
        row = Image.new("RGBA", (bw - 24, rows_h - 8), (0, 0, 0, 0))
        dr = ImageDraw.Draw(row)
        if c == "JPN":
            dr.rounded_rectangle((0, 0, row.width - 1, row.height - 1), 12, fill=(220, 0, 40, 235),
                                 outline=(255, 255, 255, 255), width=3)
        else:
            dr.rounded_rectangle((0, 0, row.width - 1, row.height - 1), 12, fill=(255, 255, 255, 22))
        rnum = text_sprite(str(rk[c]), "display", 36, GOLD if rk[c] == 1 else WHITE)
        row.alpha_composite(rnum, (14, (row.height - rnum.height) // 2))
        f = flag(c, 66, 44)
        row.alpha_composite(f, (58, (row.height - f.height) // 2))
        nm = text_sprite(NAME_JA[c], "sans", 32, WHITE)
        row.alpha_composite(nm, (136, (row.height - nm.height) // 2 - 2))
        v = text_sprite(fmt_oku(vals[c]), "sans", 26, WHITE, weight=700)
        row.alpha_composite(v, (row.width - v.width - 12, (row.height - v.height) // 2 - 1))
        panel.alpha_composite(row, (12, int(ry)))
    ft = text_sprite(b["source_label"], "sans", 21, (190, 200, 225), weight=600)
    panel.alpha_composite(ft, (20, panel.height - 36))
    paste_at(img, panel, bx, by, a)


@lru_cache(maxsize=16)
def caption_sprite(text, sub):
    t1 = text_sprite(text, "sans", 46, NAVY)
    t2 = text_sprite(sub, "sans", 28, (70, 70, 80), weight=700) if sub else None
    w = max(t1.width, t2.width if t2 else 0) + 70
    h = t1.height + (t2.height + 2 if t2 else 0) + 26
    im = rounded_box(w, h, 14, (255, 255, 255, 240))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, 16, h - 1), 6, fill=(220, 0, 40, 255))
    im.alpha_composite(t1, (42, 10))
    if t2:
        im.alpha_composite(t2, (42, 10 + t1.height))
    return im


def draw_captions(img, t):
    for c in TL["captions"]:
        if c["in"] <= t < c["out"]:
            p = ease_out(clamp01((t - c["in"]) / 0.25))
            a = p * clamp01((c["out"] - t) / 0.2)
            sp = caption_sprite(c["text"], c.get("sub", ""))
            paste_at(img, sp, (W - sp.width) / 2 - 80 * (1 - p), 1050, a)


@lru_cache(maxsize=64)
def subtitle_sprite(role, text, style):
    if style == "big":
        return text_sprite(text, "display", 80, WHITE, stroke=11, stroke_fill=NAVY, spacing=18)
    t = text_sprite(text, "sans", 58, WHITE, stroke=8, stroke_fill=BLACK, spacing=14)
    box = rounded_box(max(t.width + 60, 360), t.height + 40, 22, (0, 0, 0, 120))
    box.alpha_composite(t, ((box.width - t.width) // 2, 20))
    lab = boxed_text(role, size=26, box=((220, 0, 40, 255) if role == "実況" else (30, 60, 140, 255)),
                     pad=(14, 4), radius=10)
    out = Image.new("RGBA", (box.width, box.height + lab.height - 12), (0, 0, 0, 0))
    out.alpha_composite(box, (0, lab.height - 12))
    out.alpha_composite(lab, (18, 0))
    return out


def draw_camera_badge(img, t):
    for c in TL.get("camera_badges", []):
        if c["in"] <= t < c["out"]:
            sp = boxed_text(c["text"], size=30, box=(20, 20, 20, 200), pad=(16, 6), radius=12,
                            outline=(255, 255, 255, 230), outline_w=2)
            a = clamp01((t - c["in"]) / 0.15) * clamp01((c["out"] - t) / 0.15)
            paste_at(img, sp, SAFE_X[1] - sp.width, 312, a)


def draw_subtitles(img, t):
    for ln in TL["lines"]:
        if ln.get("subtitle", True) and ln["in"] <= t < ln["out"]:
            a = clamp01((t - ln["in"]) / 0.12) * clamp01((ln["out"] - t) / 0.15)
            style = ln.get("style", "box")
            sp = subtitle_sprite(ln["role"], ln.get("display", ln["text"]), style)
            cy = {"top": 470, "box": 1345}.get(ln.get("pos", "box"), 1345)
            if style == "big":
                cy = 1300 - 30 * (1 - ease_out(clamp01((t - ln["in"]) / 0.25)))
            paste_center(img, sp, W / 2, cy, alpha=a)


@lru_cache(maxsize=1)
def badge():
    return boxed_text("ROUGH v3｜仮音声・仮グラフィック", size=26, box=(0, 0, 0, 150), pad=(12, 4), radius=8,
                      weight=700)


# ---------------------------------------------------------------- フレーム合成

def render_frame(t):
    seg = segment_at(t)
    rd = swap_asset(seg)
    view = View()
    if rd is not None:
        img = Image.fromarray(rd.get(t - seg["work_in"]))
        swapped = True
    else:
        swapped = False
        if seg["kind"] == "source":
            img = Image.fromarray(READERS[seg["src"]].get(src_time(seg, t)))
        else:
            img = graphic_frame(seg, t)
        for fx in seg.get("fx", []):
            img, v = SEG_FX[fx](img, t, seg)
            if v.z != 1 or v.x0 or v.y0:
                view = v
    if not swapped:
        draw_tags(img, t, seg, view)
    draw_year_chip(img, t)
    draw_board(img, t)
    draw_captions(img, t)
    draw_camera_badge(img, t)
    if seg.get("graphic") == "future_run":
        img = fx_future(img, t)
    draw_subtitles(img, t)
    paste_at(img, badge(), SAFE_X[1] - badge().width, 160)
    return img


def encoder(out):
    out.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-maxrate", "6M", "-bufsize", "12M",
         "-profile:v", "high", "-pix_fmt", "yuv420p",
         "-r", str(FPS), "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=DUR)
    ap.add_argument("--out", default="working/video_v3.mp4")
    ap.add_argument("--stills", default="")
    args = ap.parse_args()
    out = ROOT / args.out
    if args.stills:
        out.mkdir(parents=True, exist_ok=True)
        for s in args.stills.split(","):
            t = float(s)
            # 盤面の補間状態を作るため少し手前から回す
            for k in range(12, 0, -1):
                render_frame(max(0.0, t - k / FPS))
            render_frame(t).save(out / f"still_{t:06.2f}.png")
        return
    n0, n1 = int(round(args.start * FPS)), int(round(args.end * FPS))
    enc = encoder(out)
    for i in range(n0, n1):
        t = i / FPS
        enc.stdin.write(render_frame(t).convert("RGB").tobytes())
        if i % 150 == 0:
            print(f"  {t:5.1f}s", flush=True)
    enc.stdin.close()
    enc.wait()
    for r in list(READERS.values()) + list(ASSET_READERS.values()):
        r.close()
    print("wrote", out)


if __name__ == "__main__":
    main()
