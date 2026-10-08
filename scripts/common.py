"""描画まわりの共通部品（フォント・テキスト・国旗・帯など）。"""
import csv
import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
W, H, FPS = 1080, 1920, 30

FONT_DISPLAY = str(ROOT / "assets/fonts/DelaGothicOne-Regular.ttf")
FONT_SANS = str(ROOT / "assets/fonts/NotoSansJP.ttf")

NAVY = (11, 22, 52)
GOLD = (255, 196, 0)
RED = (230, 0, 35)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

COUNTRIES = ["USA", "JPN", "DEU", "CHN", "FRA"]
NAME_JA = {"JPN": "日本", "USA": "アメリカ", "DEU": "ドイツ", "CHN": "中国", "FRA": "フランス"}
LINE_COLOR = {"JPN": (255, 45, 85), "USA": (90, 169, 255), "DEU": (255, 210, 63),
              "CHN": (255, 140, 66), "FRA": (179, 136, 255)}


def load_timeline():
    return json.loads((ROOT / "timeline.json").read_text())


def load_gdp():
    """{year: {code: usd}}。データが無ければ None（DATA_PENDING 表示に切り替える）。"""
    meta_p = ROOT / "data/gdp_source.json"
    csv_p = ROOT / "data/gdp_nominal_usd_5countries.csv"
    if not meta_p.exists() or not csv_p.exists():
        return None, {"status": "DATA_PENDING"}
    meta = json.loads(meta_p.read_text())
    if meta.get("status") != "OK":
        return None, meta
    data = {}
    for r in csv.DictReader(csv_p.open()):
        data[int(r["year"])] = {c: float(r[c]) for c in COUNTRIES}
    return data, meta


def ranks_for(values):
    order = sorted(values, key=lambda c: -values[c])
    return {c: order.index(c) + 1 for c in values}


@lru_cache(maxsize=64)
def font(kind, size, weight=900):
    if kind == "display":
        return ImageFont.truetype(FONT_DISPLAY, size)
    f = ImageFont.truetype(FONT_SANS, size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


def ease_out_back(x, s=1.70158):
    x = min(max(x, 0.0), 1.0) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


def ease_out(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def ease_in_out(x):
    x = min(max(x, 0.0), 1.0)
    return 3 * x * x - 2 * x * x * x


def clamp01(x):
    return min(max(x, 0.0), 1.0)


@lru_cache(maxsize=512)
def text_sprite(text, kind="sans", size=60, fill=WHITE, stroke=0, stroke_fill=BLACK,
                weight=900, spacing=10, align="center", shadow=0):
    """複数行テキストを RGBA スプライトにする（縁取り・影つき）。"""
    f = font(kind, size, weight)
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = tmp.multiline_textbbox((0, 0), text, font=f, stroke_width=stroke, spacing=spacing, align=align)
    box = (math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3]))
    pad = stroke + shadow + 6
    w, h = box[2] - box[0] + pad * 2, box[3] - box[1] + pad * 2
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    origin = (pad - box[0], pad - box[1])
    if shadow:
        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(sh).multiline_text((origin[0] + shadow // 2, origin[1] + shadow), text, font=f,
                                          fill=(0, 0, 0, 170), stroke_width=stroke,
                                          stroke_fill=(0, 0, 0, 170), spacing=spacing, align=align)
        im = Image.alpha_composite(im, sh.filter(ImageFilter.GaussianBlur(shadow / 2)))
    ImageDraw.Draw(im).multiline_text(origin, text, font=f, fill=fill, stroke_width=stroke,
                                      stroke_fill=stroke_fill, spacing=spacing, align=align)
    return im


def rounded_box(w, h, r, fill, outline=None, width=0):
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, w - 1, h - 1), r, fill=fill, outline=outline, width=width)
    return im


def boxed_text(text, kind="sans", size=56, fill=WHITE, box=(0, 0, 0, 170), pad=(28, 16), radius=18,
               stroke=0, stroke_fill=BLACK, weight=900, outline=None, outline_w=0):
    t = text_sprite(text, kind, size, fill, stroke, stroke_fill, weight)
    b = rounded_box(t.width + pad[0] * 2, t.height + pad[1] * 2, radius, box, outline, outline_w)
    b.alpha_composite(t, (pad[0], pad[1]))
    return b


def with_alpha(im, a):
    if a >= 0.999:
        return im
    im = im.copy()
    im.putalpha(im.getchannel("A").point(lambda v: int(v * a)))
    return im


def scaled(im, s):
    if abs(s - 1) < 1e-3:
        return im
    return im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.BICUBIC)


def paste_center(base, sprite, cx, cy, alpha=1.0, scale=1.0, rot=0.0):
    if alpha <= 0.01:
        return
    sp = scaled(sprite, scale)
    if rot:
        sp = sp.rotate(rot, resample=Image.BICUBIC, expand=True)
    sp = with_alpha(sp, alpha)
    base.paste(sp, (int(cx - sp.width / 2), int(cy - sp.height / 2)), sp)


def paste_at(base, sprite, x, y, alpha=1.0):
    if alpha <= 0.01:
        return
    sp = with_alpha(sprite, alpha)
    base.paste(sp, (int(x), int(y)), sp)


# ---------- 国旗（コード描画。比率は 3:2 に統一した簡略版） ----------

def _star(cx, cy, r, rot=-90):
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.382
        a = math.radians(rot + i * 36)
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


@lru_cache(maxsize=64)
def flag(code, w=120, h=80, border=True):
    s = 4  # スーパーサンプリング
    W_, H_ = w * s, h * s
    im = Image.new("RGBA", (W_, H_), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    if code == "JPN":
        r = H_ * 0.3
        d.ellipse((W_ / 2 - r, H_ / 2 - r, W_ / 2 + r, H_ / 2 + r), fill=(188, 0, 45))
    elif code == "DEU":
        for i, c in enumerate([(0, 0, 0), (221, 0, 0), (255, 206, 0)]):
            d.rectangle((0, H_ * i / 3, W_, H_ * (i + 1) / 3), fill=c)
    elif code == "FRA":
        for i, c in enumerate([(0, 85, 164), (255, 255, 255), (239, 65, 53)]):
            d.rectangle((W_ * i / 3, 0, W_ * (i + 1) / 3, H_), fill=c)
    elif code == "USA":
        for i in range(13):
            d.rectangle((0, H_ * i / 13, W_, H_ * (i + 1) / 13), fill=(178, 34, 52) if i % 2 == 0 else (255, 255, 255))
        cw, ch = W_ * 0.4, H_ * 7 / 13
        d.rectangle((0, 0, cw, ch), fill=(60, 59, 110))
        for row in range(9):
            for col in range(6 if row % 2 == 0 else 5):
                x = cw * (col + (0.5 if row % 2 == 0 else 1.0)) / 6
                y = ch * (row + 0.75) / 10
                rr = ch * 0.035
                d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=(255, 255, 255))
    elif code == "CHN":
        d.rectangle((0, 0, W_, H_), fill=(238, 28, 37))
        u = H_ / 20
        d.polygon(_star(5 * u, 5 * u, 3 * u), fill=(255, 255, 0))
        for sx, sy in [(10, 2), (12, 4), (12, 7), (10, 9)]:
            ang = math.degrees(math.atan2(5 - sy, 5 - sx))
            d.polygon(_star(sx * u, sy * u, u, rot=ang), fill=(255, 255, 0))
    im = im.resize((w, h), Image.LANCZOS)
    if border:
        ImageDraw.Draw(im).rectangle((0, 0, w - 1, h - 1), outline=(40, 40, 40, 255), width=1)
    return im


@lru_cache(maxsize=8)
def flag_badge(code, d=80, ring=WHITE):
    """丸いバッジ型の国旗（マーカー用）。"""
    f = flag(code, int(d * 1.5), d, border=False)
    f = f.crop(((f.width - d) // 2, 0, (f.width - d) // 2 + d, d))
    mask = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255)
    mask = mask.resize((d, d), Image.LANCZOS)
    out = Image.new("RGBA", (d + 12, d + 12), (0, 0, 0, 0))
    ImageDraw.Draw(out).ellipse((0, 0, d + 11, d + 11), fill=ring + (255,))
    f.putalpha(mask)
    out.alpha_composite(f, (6, 6))
    return out


# ---------- 演出用スプライト ----------

@lru_cache(maxsize=8)
def speed_lines(seed, cx=540, cy=900, r_in=360, density=90, alpha=170):
    rng = np.random.default_rng(seed)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for _ in range(density):
        a = rng.uniform(0, 2 * math.pi)
        r0 = r_in + rng.uniform(0, 260)
        r1 = r0 + rng.uniform(500, 1400)
        wdt = rng.uniform(0.004, 0.016)
        p = [(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
             (cx + r1 * math.cos(a - wdt), cy + r1 * math.sin(a - wdt)),
             (cx + r1 * math.cos(a + wdt), cy + r1 * math.sin(a + wdt))]
        d.polygon(p, fill=(255, 255, 255, int(rng.uniform(0.4, 1.0) * alpha)))
    return im


@lru_cache(maxsize=4)
def vignette(strength=0.6, color=(0, 0, 0)):
    y, x = np.mgrid[0:H, 0:W]
    r = np.sqrt(((x - W / 2) / (W / 2)) ** 2 + ((y - H / 2) / (H / 2)) ** 2)
    a = np.clip((r - 0.55) / 0.75, 0, 1) ** 1.5 * strength * 255
    arr = np.zeros((H, W, 4), np.uint8)
    arr[..., :3] = color
    arr[..., 3] = a.astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


@lru_cache(maxsize=4)
def hazard_band(w=W, h=64):
    im = Image.new("RGBA", (w, h), (255, 214, 0, 255))
    d = ImageDraw.Draw(im)
    for x in range(-h, w + h, 56):
        d.polygon([(x, h), (x + 28, h), (x + 28 + h, 0), (x + h, 0)], fill=(20, 20, 20, 255))
    return im


@lru_cache(maxsize=4)
def gradient(top, bottom, w=W, h=H):
    t = np.linspace(0, 1, h)[:, None]
    arr = (np.array(top)[None, :] * (1 - t) + np.array(bottom)[None, :] * t).astype(np.uint8)
    return Image.fromarray(np.repeat(arr[:, None, :], w, axis=1), "RGB")
