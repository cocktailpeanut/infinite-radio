"""Typography and branded graphics for episodes: poster (thumbnail), intro card,
now-playing card and lockup.

Design rules, taken from how playlist channels present their videos: keep the
scene bright, confine shading to the band the text sits on, left-align a large
display title, and use one accent colour from the station mood.

Fonts: Bebas Neue (display) and Poppins (text), both SIL OFL, bundled under
assets/fonts. CJK text falls back to a system font that carries those glyphs.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

APP_DIR = Path(__file__).resolve().parent
FONT_DIR = APP_DIR / "assets" / "fonts"
BUNDLED = {
    "display": FONT_DIR / "BebasNeue-Regular.ttf",
    "semibold": FONT_DIR / "Poppins-SemiBold.ttf",
    "medium": FONT_DIR / "Poppins-Medium.ttf",
}
# System fonts with CJK coverage, tried in order (bold-ish first).
# System fonts per CJK script, tried in order. Hiragino has kana and kanji but no Hangul,
# so Korean must not fall through to it.
_NOTO = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"]
_UNIVERSAL = ["/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"]
CJK_FONTS = {
    "ko": ["/System/Library/Fonts/AppleSDGothicNeo.ttc", "C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/malgun.ttf"] + _NOTO + _UNIVERSAL,
    "ja": ["/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc", "C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/meiryob.ttc",
           "C:/Windows/Fonts/msgothic.ttc"] + _NOTO + _UNIVERSAL,
    "zh": ["/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/PingFang.ttc", "C:/Windows/Fonts/msyhbd.ttc",
           "C:/Windows/Fonts/msyh.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"] + _NOTO + _UNIVERSAL,
}
# Face index inside .ttc collections for each weight we use (display/semibold want bold, medium wants medium).
TTC_FACE = {
    "AppleSDGothicNeo.ttc": {"display": 6, "semibold": 4, "medium": 2},
    "Hiragino Sans GB.ttc": {"display": 2, "semibold": 2, "medium": 0},
    "NotoSansCJK-Regular.ttc": {}, "NotoSansCJK-Bold.ttc": {},
}
HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
KANA_RE = re.compile(r"[\u3040-\u30ff]")
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
INK = (10, 10, 20)
PAPER = (248, 248, 252)
MUTED = (200, 202, 216)

_cache: dict[tuple, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def needs_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


def cjk_script(text: str) -> str | None:
    """'ko', 'ja', 'zh' or None. Hangul wins (Korean titles may include hanja); kana means Japanese."""
    if not text:
        return None
    if HANGUL_RE.search(text):
        return "ko"
    if KANA_RE.search(text):
        return "ja"
    if CJK_RE.search(text):
        return "zh"
    return None


def _covers(f, text: str) -> bool:
    """True when the font draws the first CJK character instead of its missing-glyph box."""
    m = CJK_RE.search(text) or HANGUL_RE.search(text)
    if not m:
        return True
    try:
        return f.getmask(m.group(0)).getbbox() != f.getmask("\U0010FFFD").getbbox()
    except Exception:
        return True


def font(kind: str, size: int, text: str = "") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Bundled font of `kind` (display|semibold|medium); a CJK system font when the text needs it."""
    size = max(8, int(size))
    script = cjk_script(text)
    key = (kind, size, script)
    if key in _cache:
        return _cache[key]
    candidates: list[str] = []
    if script:
        candidates += CJK_FONTS[script]
    p = BUNDLED.get(kind)
    if p and p.exists():
        candidates.append(str(p))
    candidates += ["/System/Library/Fonts/Helvetica.ttc", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    f = None
    for c in candidates:
        if Path(c).exists():
            try:
                idx = TTC_FACE.get(Path(c).name, {}).get(kind, 0)
                cand = ImageFont.truetype(c, size, index=idx)
            except Exception:
                continue
            if script and not _covers(cand, text):
                continue
            f = cand
            break
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
    _cache[key] = f
    return f


def text_width(text: str, f) -> float:
    return ImageDraw.Draw(Image.new("RGBA", (1, 1))).textlength(text, font=f)


def fit(kind: str, text: str, max_width: int, start: int, minimum: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Largest font size between `minimum` and `start` at which `text` fits one line."""
    size = start
    while size > minimum:
        f = font(kind, size, text)
        if text_width(text, f) <= max_width:
            return f
        size -= max(2, size // 14)
    return font(kind, minimum, text)


def wrap(text: str, f, max_width: int, max_lines: int) -> list[str]:
    # A title with separators ("A · B") breaks there first.
    parts = [p.strip() for p in re.split(r"\s*[·|—–]\s*", text) if p.strip()]
    if 1 < len(parts) <= max_lines and all(text_width(p, f) <= max_width for p in parts):
        return parts
    words = text.split() if not needs_cjk(text) else list(text.replace(" ", ""))
    joiner = " " if not needs_cjk(text) else ""
    lines, cur = [], ""
    for w in words:
        trial = (cur + joiner + w).strip() if cur else w
        if text_width(trial, f) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,.") + "…"
    return lines


def spaced(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, f, fill, spacing: float = 0.12) -> float:
    """Draw letter-spaced text; returns the drawn width. Spacing is a fraction of the font size."""
    x, y = xy
    gap = f.size * spacing if hasattr(f, "size") else 2
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill)
        x += draw.textlength(ch, font=f) + gap
    return x - xy[0]


def shadow_text(img: Image.Image, xy: tuple[float, float], text: str, f, fill=PAPER, blur: int = 10, offset=(0, 4), alpha: int = 150) -> None:
    """Text with a soft drop shadow, composited onto `img` (RGBA)."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((xy[0] + offset[0], xy[1] + offset[1]), text, font=f, fill=(0, 0, 0, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(layer)
    ImageDraw.Draw(img).text(xy, text, font=f, fill=fill)


def scrim(img: Image.Image, top_frac: float = 0.42, max_alpha: int = 205, color=(4, 4, 10)) -> None:
    """Darken only the lower band so the subject stays bright."""
    w, h = img.size
    grad = Image.new("L", (1, h), 0)
    px = grad.load()
    y0 = int(h * top_frac)
    for y in range(y0, h):
        t = (y - y0) / max(1, h - y0)
        px[0, y] = int(max_alpha * (t ** 1.4))
    grad = grad.resize((w, h))
    layer = Image.new("RGBA", (w, h), color + (0,))
    layer.putalpha(grad)
    img.alpha_composite(layer)


def chip(img: Image.Image, xy: tuple[int, int], text: str, f, bg, fg=INK, pad=(18, 9), radius: int | None = None) -> int:
    """Rounded label; returns its width."""
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=f)
    th = getattr(f, "size", 20)
    w, h = int(tw + pad[0] * 2), int(th + pad[1] * 2)
    r = radius if radius is not None else h // 2
    d.rounded_rectangle([xy[0], xy[1], xy[0] + w, xy[1] + h], radius=r, fill=bg)
    d.text((xy[0] + pad[0], xy[1] + pad[1] - th * 0.12), text, font=f, fill=fg)
    return w


def enhance(frame: Image.Image) -> Image.Image:
    frame = ImageEnhance.Color(frame).enhance(1.12)
    return ImageEnhance.Contrast(frame).enhance(1.06)


# ---------- compositions ----------
def poster(frame: Image.Image, title: str, songs: int, duration_label: str, station: str, accent) -> Image.Image:
    """1280x720 thumbnail: bright scene, bottom scrim, big left-aligned title, accent chip."""
    W, H = 1280, 720
    img = enhance(frame.convert("RGB")).resize((W, H), Image.LANCZOS).convert("RGBA")
    scrim(img, 0.40, 215)
    d = ImageDraw.Draw(img)
    # kicker
    kf = font("semibold", 24, "INFINITE RADIO")
    d.ellipse([64, 63, 78, 77], fill=accent)
    spaced(d, (100, 56), "INFINITE RADIO", kf, (240, 240, 248, 235), 0.16)
    # title: one line if it fits, else two
    margin, max_w = 64, W - 128
    tf = fit("display", title, max_w, 176, 112)
    lines = [title] if text_width(title, tf) <= max_w else wrap(title, font("display", 118, title), max_w, 2)
    if len(lines) > 1:
        tf = font("display", 118, title)
    lh = int(tf.size * (1.0 if not needs_cjk(title) else 1.15))
    chip_y = H - 92
    y = chip_y - 22 - lh * len(lines)
    for ln in lines:
        shadow_text(img, (margin, y), ln, tf)
        y += lh
    # chips row
    cf = font("semibold", 26, "0")
    label = f"{songs} SONG{'S' if songs != 1 else ''}  ·  {duration_label}".upper()
    w = chip(img, (margin, chip_y), label, cf, accent)
    sf = font("medium", 26, station)
    ImageDraw.Draw(img).text((margin + w + 18, chip_y + 8), station, font=sf, fill=(226, 228, 240, 230))
    return img.convert("RGB")


def intro_overlay(W: int, H: int, title: str, songs: int, duration_label: str, station: str, accent) -> Image.Image:
    """Transparent 1920x1080 overlay for the first seconds: same layout as the poster at 1.5x."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    scrim(img, 0.38, 225)
    d = ImageDraw.Draw(img)
    kf = font("semibold", 34, "INFINITE RADIO")
    d.ellipse([96, 94, 116, 114], fill=accent)
    spaced(d, (146, 82), "INFINITE RADIO", kf, (240, 240, 248, 235), 0.16)
    margin, max_w = 96, W - 192
    tf = fit("display", title, max_w, 250, 160)
    lines = [title] if text_width(title, tf) <= max_w else wrap(title, font("display", 170, title), max_w, 2)
    if len(lines) > 1:
        tf = font("display", 170, title)
    lh = int(tf.size * (1.0 if not needs_cjk(title) else 1.15))
    chip_y = H - 140
    y = chip_y - 30 - lh * len(lines)
    for ln in lines:
        shadow_text(img, (margin, y), ln, tf, blur=14, offset=(0, 6))
        y += lh
    cf = font("semibold", 36, "0")
    label = f"{songs} SONG{'S' if songs != 1 else ''}  ·  {duration_label}".upper()
    w = chip(img, (margin, chip_y), label, cf, accent, pad=(26, 12))
    sf = font("medium", 36, station)
    ImageDraw.Draw(img).text((margin + w + 26, chip_y + 11), station, font=sf, fill=(226, 228, 240, 230))
    return img


def now_playing_card(width: int, height: int, index: int, total: int, title: str, subtitle: str, accent) -> Image.Image:
    """Card style: translucent box with an accent bar, kicker, title and optional subtitle.

    The box sits at the bottom of the canvas and is shorter when there is no subtitle.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    kf = font("medium", 21, "NOW PLAYING")
    tf = fit("semibold", title, width - 120, 50, 30)
    gf = font("medium", 22, subtitle)
    tw = max(text_width(title, tf), text_width(subtitle, gf) if subtitle else 0, 320)
    box_w = int(min(width, tw + 96))
    box_h = height if subtitle else min(height, 122)
    y0 = height - box_h
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, y0, box_w, height], radius=14, fill=(8, 8, 16, 170))
    d.rounded_rectangle([0, y0, 6, height], radius=3, fill=accent)
    x = 34
    spaced(d, (x, y0 + 22), f"NOW PLAYING  ·  {index:02d} / {total:02d}", kf, accent + (255,), 0.1)
    d.text((x, y0 + 54), title, font=tf, fill=PAPER + (255,))
    if subtitle:
        d.text((x, height - 46), subtitle[:90], font=gf, fill=MUTED + (255,))
    return img


def minimal_card(width: int, height: int, index: int, title: str, subtitle: str, accent) -> Image.Image:
    """Minimal style: track number and title with a soft shadow, no box, bottom-aligned."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    nf = font("semibold", 42, "00")
    num = f"{index:02d}"
    nw = text_width(num, nf)
    gap = 26
    tf = fit("semibold", title, width - int(nw) - gap - 40, 58, 34)
    sf = font("medium", 27, subtitle)
    title_y = 18 if subtitle else height - 82
    # number shares the title's baseline area; accent colour, no letter-spacing
    num_y = title_y + max(0, (tf.size - nf.size) * 0.55)
    shadow_text(img, (0, num_y), num, nf, fill=accent + (255,), blur=8, offset=(0, 3), alpha=170)
    d = ImageDraw.Draw(img)
    bar_x = int(nw + gap / 2) - 1
    d.rounded_rectangle([bar_x, title_y + 8, bar_x + 2, title_y + tf.size + 2], radius=1, fill=(255, 255, 255, 120))
    shadow_text(img, (nw + gap, title_y), title, tf, fill=PAPER + (255,), blur=10, offset=(0, 3), alpha=175)
    if subtitle:
        shadow_text(img, (nw + gap, title_y + tf.size + 14), subtitle[:90], sf, fill=(230, 232, 242, 255), blur=8, offset=(0, 2), alpha=165)
    return img


def lockup(station: str) -> Image.Image:
    f1 = font("semibold", 22, "INFINITE RADIO")
    f2 = font("medium", 22, station)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    w1 = sum(probe.textlength(ch, font=f1) + f1.size * 0.14 for ch in "INFINITE RADIO")
    w2 = probe.textlength(f"  ·  {station}", font=f2)
    img = Image.new("RGBA", (int(w1 + w2) + 12, 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    spaced(d, (4, 6), "INFINITE RADIO", f1, (240, 240, 248, 225), 0.14)
    d.text((4 + w1, 6), f"  ·  {station}", font=f2, fill=(236, 238, 246, 220))
    return img


def hex_rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _lum(rgb) -> float:
    r, g, b = [v / 255 for v in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def accent_from_palette(palette) -> tuple[int, int, int]:
    """Pick the brighter of the two palette colours; fall back to cyan if both are too dark to read on a scrim."""
    cands = [hex_rgb(c) for c in palette[:2]]
    best = max(cands, key=_lum)
    return best if _lum(best) >= 0.25 else (34, 211, 238)
