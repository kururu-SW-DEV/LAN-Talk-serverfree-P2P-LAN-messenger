# -*- coding: utf-8 -*-
"""stickers.py — 자체 제작 오리지널 스티커(표정 14종 + 메시지 14종) 정의 및 렌더링.

카카오톡·텔레그램의 캐릭터 스티커는 저작권이 있는 그림이라 그대로 쓸 수 없어서, 원 안에
표정을 그리고 문구가 필요한 건 도장처럼 새긴 오리지널 도형 스티커를 새로 만들었다(사용자
승인 시안 기준). 외부 이미지 파일이 전혀 없이 tkinter Canvas 기본 도형(원·선·다각형·텍스트)
만으로 매번 그린다 — 이미 아바타에 쓰고 있는 `smooth_circle_photo()`로 앤티앨리어싱된 원형
배경을 만들고, 그 위에 얼굴 이목구비를 벡터로 얹는 방식이라 별도 이미지 자산이 필요 없다.
"""
import re

from canvas_utils import round_rect, smooth_circle_photo
from constants import FONT_FAM


def _curve(c, cx, cy, s, x1, y1, x2, y2, x3, y3, color, width=4):
    pts = [cx + x1 * s, cy + y1 * s, cx + x2 * s, cy + y2 * s, cx + x3 * s, cy + y3 * s]
    return [c.create_line(*pts, smooth=True, fill=color, width=max(1, width * s), capstyle="round")]


def _line(c, cx, cy, s, x1, y1, x2, y2, color, width=3):
    return [c.create_line(cx + x1 * s, cy + y1 * s, cx + x2 * s, cy + y2 * s,
                          fill=color, width=max(1, width * s), capstyle="round")]


def _dot(c, cx, cy, s, x, y, r, color):
    x0, y0 = cx + x * s, cy + y * s
    return [c.create_oval(x0 - r * s, y0 - r * s, x0 + r * s, y0 + r * s, fill=color, outline="")]


def _ring(c, cx, cy, s, x, y, r, color, width=2.4):
    x0, y0 = cx + x * s, cy + y * s
    return [c.create_oval(x0 - r * s, y0 - r * s, x0 + r * s, y0 + r * s, outline=color, width=max(1, width * s))]


def _oval_fill(c, cx, cy, s, x, y, rx, ry, color):
    x0, y0 = cx + x * s, cy + y * s
    return [c.create_oval(x0 - rx * s, y0 - ry * s, x0 + rx * s, y0 + ry * s, fill=color, outline="")]


def _text(c, cx, cy, s, x, y, txt, color, size=16, bold=True):
    fsz = max(7, int(round(size * s)))
    weight = "bold" if bold else "normal"
    return [c.create_text(cx + x * s, cy + y * s, text=txt, fill=color, font=(FONT_FAM, fsz, weight))]


def _text_stroke(c, cx, cy, s, x, y, txt, fill, stroke, size=16, bold=True):
    """도장처럼 새긴 문구가 배경색과 대비가 낮아 잘 안 보인다는 피드백에 따라, 글자
    가장자리에 테두리(외곽선)를 둘러 어떤 배경에서도 또렷하게 보이게 한다. tkinter
    Canvas 텍스트는 SVG처럼 stroke 속성이 없어서, 같은 글자를 살짝씩 8방향으로
    어긋나게 먼저 그린 뒤 그 위에 원래 색 글자를 겹쳐 그리는 방식으로 흉내낸다."""
    fsz = max(7, int(round(size * s)))
    weight = "bold" if bold else "normal"
    x0, y0 = cx + x * s, cy + y * s
    off = max(1, round(1.6 * s))
    ids = []
    for ox, oy in ((-off, 0), (off, 0), (0, -off), (0, off),
                   (-off, -off), (off, -off), (-off, off), (off, off)):
        ids.append(c.create_text(x0 + ox, y0 + oy, text=txt, fill=stroke,
                                 font=(FONT_FAM, fsz, weight)))
    ids.append(c.create_text(x0, y0, text=txt, fill=fill, font=(FONT_FAM, fsz, weight)))
    return ids


def _tear(c, cx, cy, s, x, y, color):
    raw = [x, y - 8, x + 5, y - 2, x + 3, y + 6, x - 3, y + 6, x - 5, y - 2]
    pts = []
    for i in range(0, len(raw), 2):
        pts.extend([cx + raw[i] * s, cy + raw[i + 1] * s])
    return [c.create_polygon(*pts, smooth=True, fill=color, outline="")]


def _rrect(c, cx, cy, s, x1, y1, x2, y2, r, color):
    return [round_rect(c, cx + x1 * s, cy + y1 * s, cx + x2 * s, cy + y2 * s,
                       r=max(2, r * s), fill=color, outline="")]


def _sparkle(c, cx, cy, s, x, y, size, color):
    x0, y0 = cx + x * s, cy + y * s
    sz = size * s
    pts = [x0, y0 - sz, x0 + sz * 0.28, y0 - sz * 0.28, x0 + sz, y0, x0 + sz * 0.28, y0 + sz * 0.28,
           x0, y0 + sz, x0 - sz * 0.28, y0 + sz * 0.28, x0 - sz, y0, x0 - sz * 0.28, y0 - sz * 0.28]
    return [c.create_polygon(*pts, fill=color, outline="")]


def _zigzag(c, cx, cy, s, pts_xy, color, width=3.5):
    pts = []
    for x, y in pts_xy:
        pts.extend([cx + x * s, cy + y * s])
    return [c.create_line(*pts, fill=color, width=max(1, width * s), capstyle="round", joinstyle="round")]


def _moon(c, cx, cy, s, x, y, r, moon_color, bg_color):
    ids = _oval_fill(c, cx, cy, s, x, y, r, r, moon_color)
    ids += _oval_fill(c, cx, cy, s, x + r * 0.35, y - r * 0.35, r * 0.85, r * 0.85, bg_color)
    return ids


# ---------- 표정 스티커 14종 ----------

def _d_smile(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -19, -7, -13, -15, -7, -7, "#3a2a10")
    ids += _curve(c, cx, cy, s, 7, -7, 13, -15, 19, -7, "#3a2a10")
    ids += _dot(c, cx, cy, s, -20, 10, 6, "#ffcf9e")
    ids += _dot(c, cx, cy, s, 20, 10, 6, "#ffcf9e")
    ids += _curve(c, cx, cy, s, -22, 8, 0, 24, 22, 8, "#3a2a10", width=9)
    return ids


def _d_heart(c, cx, cy, s):
    ids = _text(c, cx, cy, s, -16, -8, "♥", "#c23860", size=18)
    ids += _text(c, cx, cy, s, 16, -8, "♥", "#c23860", size=18)
    ids += _curve(c, cx, cy, s, -18, 26, 0, 38, 18, 26, "#c23860", width=5)
    return ids


def _d_sad(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -18, -6, -13, -10, -8, -6, "#26264a", width=3)
    ids += _curve(c, cx, cy, s, 8, -6, 13, -10, 18, -6, "#26264a", width=3)
    ids += _dot(c, cx, cy, s, -13, -4, 4.5, "#26264a")
    ids += _dot(c, cx, cy, s, 13, -4, 4.5, "#26264a")
    ids += _curve(c, cx, cy, s, -16, 20, 0, 10, 16, 20, "#26264a", width=4)
    ids += _tear(c, cx, cy, s, 13, 6, "#6fc6ee")
    return ids


def _d_angry(c, cx, cy, s):
    ids = _line(c, cx, cy, s, -20, -10, -6, -4, "#5c1c12", width=4)
    ids += _line(c, cx, cy, s, 20, -10, 6, -4, "#5c1c12", width=4)
    ids += _dot(c, cx, cy, s, -10, 2, 3.6, "#5c1c12")
    ids += _dot(c, cx, cy, s, 10, 2, 3.6, "#5c1c12")
    ids += _curve(c, cx, cy, s, -14, 20, 0, 12, 14, 20, "#5c1c12", width=4)
    ids += _line(c, cx, cy, s, -32, -20, -26, -28, "#5c1c12", width=3)
    ids += _line(c, cx, cy, s, 32, -20, 26, -28, "#5c1c12", width=3)
    return ids


def _d_sleepy(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -18, -2, -12, 2, -6, -2, "#3c2d63", width=4)
    ids += _curve(c, cx, cy, s, 6, -2, 12, 2, 18, -2, "#3c2d63", width=4)
    ids += _oval_fill(c, cx, cy, s, 0, 16, 6, 4, "#3c2d63")
    ids += _text(c, cx, cy, s, 20, -22, "Z", "#3c2d63", size=15)
    ids += _text(c, cx, cy, s, 28, -30, "z", "#3c2d63", size=10)
    return ids


def _d_surprised(c, cx, cy, s):
    ids = _dot(c, cx, cy, s, -13, -4, 7, "#0d3d35")
    ids += _dot(c, cx, cy, s, 13, -4, 7, "#0d3d35")
    ids += _dot(c, cx, cy, s, -11, -7, 2, "#ffffff")
    ids += _dot(c, cx, cy, s, 15, -7, 2, "#ffffff")
    ids += _oval_fill(c, cx, cy, s, 0, 18, 9, 11, "#0d3d35")
    ids += _curve(c, cx, cy, s, -24, -20, -20, -28, -14, -23, "#0d3d35", width=3)
    ids += _curve(c, cx, cy, s, 24, -20, 20, -28, 14, -23, "#0d3d35", width=3)
    return ids


def _d_grateful(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -19, -6, -13, -13, -7, -6, "#5c3f0c", width=4)
    ids += _curve(c, cx, cy, s, 7, -6, 13, -13, 19, -6, "#5c3f0c", width=4)
    ids += _curve(c, cx, cy, s, -18, 10, 0, 24, 18, 10, "#5c3f0c", width=9)
    ids += _sparkle(c, cx, cy, s, -28, -20, 5, "#fff3cf")
    ids += _sparkle(c, cx, cy, s, 26, -26, 4, "#fff3cf")
    return ids


def _d_awkward(c, cx, cy, s):
    ids = _dot(c, cx, cy, s, -12, -4, 3.4, "#7a4a12")
    ids += _dot(c, cx, cy, s, 12, -4, 3.4, "#7a4a12")
    ids += _zigzag(c, cx, cy, s, [(-14, 13), (-8, 9), (-2, 13), (4, 9), (10, 13)], "#7a4a12")
    ids += _tear(c, cx, cy, s, 22, -14, "#6fc6ee")
    return ids


def _d_blank(c, cx, cy, s):
    ids = _line(c, cx, cy, s, -17, -4, -7, -4, "#2c313d", width=4)
    ids += _line(c, cx, cy, s, 7, -4, 17, -4, "#2c313d", width=4)
    ids += _line(c, cx, cy, s, -12, 14, 12, 14, "#2c313d", width=4)
    return ids


def _d_smug(c, cx, cy, s):
    ids = _rrect(c, cx, cy, s, -22, -10, 22, 0, 5, "#0f2454")
    ids += _dot(c, cx, cy, s, -11, -5, 3, "#5B8DEF")
    ids += _dot(c, cx, cy, s, 11, -5, 3, "#5B8DEF")
    ids += _curve(c, cx, cy, s, -12, 14, 0, 20, 13, 10, "#0f2454", width=4)
    return ids


def _d_mindblown(c, cx, cy, s):
    ids = _ring(c, cx, cy, s, -12, -4, 6, "#3a1c56", width=2.6)
    ids += _ring(c, cx, cy, s, 12, -4, 6, "#3a1c56", width=2.6)
    ids += _oval_fill(c, cx, cy, s, 0, 18, 7, 9, "#3a1c56")
    ids += _line(c, cx, cy, s, 0, -38, 0, -30, "#3a1c56", width=3)
    ids += _line(c, cx, cy, s, -25, -30, -20, -24, "#3a1c56", width=3)
    ids += _line(c, cx, cy, s, 25, -30, 20, -24, "#3a1c56", width=3)
    return ids


def _d_touched(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -19, -6, -13, -12, -7, -6, "#7a2049", width=4)
    ids += _curve(c, cx, cy, s, 7, -6, 13, -12, 19, -6, "#7a2049", width=4)
    ids += _curve(c, cx, cy, s, -16, 14, 0, 26, 16, 14, "#7a2049", width=8)
    ids += _tear(c, cx, cy, s, -20, 2, "#6fc6ee")
    ids += _tear(c, cx, cy, s, 20, 2, "#6fc6ee")
    return ids


def _d_curious(c, cx, cy, s):
    ids = _dot(c, cx, cy, s, -11, -2, 4.2, "#123c40")
    ids += _dot(c, cx, cy, s, 11, -5, 4.2, "#123c40")
    ids += _curve(c, cx, cy, s, 4, -16, 10, -20, 15, -16, "#123c40", width=3)
    ids += _oval_fill(c, cx, cy, s, -2, 15, 5, 6, "#123c40")
    ids += _text(c, cx, cy, s, 24, -22, "?", "#123c40", size=20)
    return ids


def _d_shy(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -18, -4, -13, -8, -8, -4, "#7a1f38", width=3.5)
    ids += _curve(c, cx, cy, s, 8, -4, 13, -8, 18, -4, "#7a1f38", width=3.5)
    ids += _dot(c, cx, cy, s, -20, 10, 8, "#ff9db6")
    ids += _dot(c, cx, cy, s, 20, 10, 8, "#ff9db6")
    ids += _curve(c, cx, cy, s, -8, 12, 0, 17, 8, 12, "#7a1f38", width=3.5)
    return ids


# ---------- 메시지 스티커 14종 ----------

def _d_ok(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -18, -10, -14, -16, -8, -10, "#0e3d26", width=3.5)
    ids += _curve(c, cx, cy, s, 8, -10, 14, -16, 18, -10, "#0e3d26", width=3.5)
    ids += _curve(c, cx, cy, s, -15, 5, 0, 15, 15, 5, "#0e3d26", width=3.5)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "OK", "#0e3d26", "#ffffff", size=17)
    return ids


def _d_no(c, cx, cy, s):
    ids = _line(c, cx, cy, s, -16, -6, -6, -6, "#4a0e10", width=4)
    ids += _line(c, cx, cy, s, 6, -6, 16, -6, "#4a0e10", width=4)
    ids += _line(c, cx, cy, s, -13, 10, 13, 10, "#4a0e10", width=4)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "NO", "#4a0e10", "#ffffff", size=17)
    return ids


def _d_lunch(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -17, -10, -12, -17, -7, -10, "#6b3210", width=3.5)
    ids += _curve(c, cx, cy, s, 7, -10, 12, -17, 17, -10, "#6b3210", width=3.5)
    ids += _curve(c, cx, cy, s, -14, 2, 0, 12, 14, 2, "#6b3210", width=3.5)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "점심콜?", "#6b3210", "#ffffff", size=16)
    return ids


def _d_dinner(c, cx, cy, s):
    ids = _moon(c, cx, cy, s, 14, -14, 12, "#f4e8b8", "#3C4A8C")
    ids += _curve(c, cx, cy, s, -17, -4, -12, -10, -7, -4, "#e7ecff", width=3.5)
    ids += _curve(c, cx, cy, s, 7, -4, 12, -10, 17, -4, "#e7ecff", width=3.5)
    ids += _curve(c, cx, cy, s, -14, 8, 0, 16, 14, 8, "#e7ecff", width=3.5)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "저녁콜?", "#e7ecff", "#1a2140", size=16)
    return ids


def _d_thumbsup(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -17, -8, -12, -14, -7, -8, "#0b3230", width=3.5)
    ids += _curve(c, cx, cy, s, 7, -8, 12, -14, 17, -8, "#0b3230", width=3.5)
    ids += _curve(c, cx, cy, s, -14, 4, 0, 12, 14, 4, "#0b3230", width=3.5)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "수고!", "#0b3230", "#ffffff", size=16)
    return ids


def _d_fighting(c, cx, cy, s):
    ids = _line(c, cx, cy, s, -20, -12, -6, -6, "#5c0f22", width=4)
    ids += _line(c, cx, cy, s, 20, -12, 6, -6, "#5c0f22", width=4)
    ids += _oval_fill(c, cx, cy, s, 0, 6, 10, 7, "#5c0f22")
    ids += _text_stroke(c, cx, cy, s, 0, 38, "파이팅!", "#5c0f22", "#ffffff", size=15)
    return ids


def _d_thanks(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -19, -6, -13, -13, -7, -6, "#5c3f0c", width=4)
    ids += _curve(c, cx, cy, s, 7, -6, 13, -13, 19, -6, "#5c3f0c", width=4)
    ids += _curve(c, cx, cy, s, -18, 8, 0, 22, 18, 8, "#5c3f0c", width=9)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "감사!", "#5c3f0c", "#ffffff", size=16)
    return ids


def _d_congrats(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -19, -8, -13, -15, -7, -8, "#3a1858", width=4)
    ids += _curve(c, cx, cy, s, 7, -8, 13, -15, 19, -8, "#3a1858", width=4)
    ids += _curve(c, cx, cy, s, -18, 5, 0, 18, 18, 5, "#3a1858", width=9)
    ids += _sparkle(c, cx, cy, s, -26, -24, 4, "#ffe27a")
    ids += _sparkle(c, cx, cy, s, 24, -20, 4, "#7af0d0")
    ids += _sparkle(c, cx, cy, s, -4, -34, 4, "#ff9fd6")
    ids += _text_stroke(c, cx, cy, s, 0, 38, "축하!", "#3a1858", "#ffffff", size=16)
    return ids


def _d_sorry(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -18, -3, -13, 1, -8, -3, "#26313f", width=3.5)
    ids += _curve(c, cx, cy, s, 8, -3, 13, 1, 18, -3, "#26313f", width=3.5)
    ids += _curve(c, cx, cy, s, -12, 16, 0, 10, 12, 16, "#26313f", width=4)
    ids += _tear(c, cx, cy, s, 14, 0, "#bfe3ff")
    ids += _text_stroke(c, cx, cy, s, 0, 38, "미안!", "#26313f", "#ffffff", size=16)
    return ids


def _d_goodmorning(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -19, -8, -13, -15, -7, -8, "#6b3a06", width=4)
    ids += _curve(c, cx, cy, s, 7, -8, 13, -15, 19, -8, "#6b3a06", width=4)
    ids += _curve(c, cx, cy, s, -16, 5, 0, 18, 16, 5, "#6b3a06", width=8)
    ids += _line(c, cx, cy, s, 0, -38, 0, -32, "#6b3a06", width=3)
    ids += _line(c, cx, cy, s, -23, -28, -19, -24, "#6b3a06", width=3)
    ids += _line(c, cx, cy, s, 23, -28, 19, -24, "#6b3a06", width=3)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "굿모닝", "#6b3a06", "#ffffff", size=16)
    return ids


def _d_goodnight(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -17, -2, -12, 2, -7, -2, "#c9d2ff", width=3.5)
    ids += _curve(c, cx, cy, s, 7, -2, 12, 2, 17, -2, "#c9d2ff", width=3.5)
    ids += _curve(c, cx, cy, s, -10, 10, 0, 15, 10, 10, "#c9d2ff", width=3.5)
    ids += _moon(c, cx, cy, s, 20, -26, 9, "#ffe9a8", "#29305C")
    ids += _sparkle(c, cx, cy, s, -24, -16, 4, "#ffe9a8")
    ids += _text_stroke(c, cx, cy, s, 0, 38, "굿나잇", "#c9d2ff", "#151a33", size=16)
    return ids


def _d_leaving(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -20, -10, -15, -18, -10, -10, "#0c3d24", width=4)
    ids += _curve(c, cx, cy, s, 10, -10, 15, -18, 20, -10, "#0c3d24", width=4)
    ids += _curve(c, cx, cy, s, -20, 4, 0, 18, 20, 4, "#0c3d24", width=8)
    ids += _text(c, cx, cy, s, -26, 12, "→", "#0c3d24", size=18)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "퇴근!", "#0c3d24", "#ffffff", size=16)
    return ids


def _d_wait(c, cx, cy, s):
    ids = _curve(c, cx, cy, s, -18, -6, -13, -12, -8, -6, "#5c2e08", width=3.5)
    ids += _curve(c, cx, cy, s, 8, -6, 13, -12, 18, -6, "#5c2e08", width=3.5)
    ids += _oval_fill(c, cx, cy, s, 0, 10, 6, 5, "#5c2e08")
    ids += _ring(c, cx, cy, s, 0, -24, 9, "#5c2e08", width=3)
    ids += _line(c, cx, cy, s, 0, -28, 0, -24, "#5c2e08", width=2.4)
    ids += _line(c, cx, cy, s, 0, -24, 3, -22, "#5c2e08", width=2.4)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "잠깐!", "#5c2e08", "#ffffff", size=14)
    return ids


def _d_best(c, cx, cy, s):
    ids = _text(c, cx, cy, s, -11, -8, "★", "#ffffff", size=14)
    ids += _text(c, cx, cy, s, 11, -8, "★", "#ffffff", size=14)
    ids += _curve(c, cx, cy, s, -14, 16, 0, 26, 14, 16, "#6b0f2e", width=6)
    ids += _text_stroke(c, cx, cy, s, 0, 38, "최고!", "#ffffff", "#6b0f2e", size=16)
    return ids


STICKERS = {
    "smile":      {"name": "활짝 웃음",   "bg": "#FFC93C", "draw": _d_smile,
                   "keywords": ["웃", "ㅋㅋ", "하하", "히히", "좋아", "재밌"]},
    "heart":      {"name": "하트뿅",     "bg": "#FF8FB1", "draw": _d_heart,
                   "keywords": ["사랑", "❤", "하트"]},
    "sad":        {"name": "시무룩",     "bg": "#8C97D6", "draw": _d_sad,
                   "keywords": ["슬퍼", "슬픔", "우울", "속상", "아쉽"]},
    "angry":      {"name": "화남",       "bg": "#FF6B57", "draw": _d_angry,
                   "keywords": ["화나", "화남", "짜증", "빡치", "열받"]},
    "sleepy":     {"name": "졸림",       "bg": "#B39CE0", "draw": _d_sleepy,
                   "keywords": ["졸려", "졸림", "피곤", "잠와", "나른"]},
    "surprised":  {"name": "깜짝 놀람",  "bg": "#5FD6C1", "draw": _d_surprised,
                   "keywords": ["놀람", "놀랐", "깜짝", "헐", "대박"]},
    "grateful":   {"name": "감사한 미소", "bg": "#E8B94A", "draw": _d_grateful,
                   "keywords": ["감사한", "고마운"]},
    "awkward":    {"name": "식은땀",     "bg": "#FFD08A", "draw": _d_awkward,
                   "keywords": ["당황", "민망", "어색", "식은땀"]},
    "blank":      {"name": "무표정",     "bg": "#8A93A6", "draw": _d_blank,
                   "keywords": ["무표정", "할말없음", "...", "음..."]},
    "smug":       {"name": "으쓱",       "bg": "#5B8DEF", "draw": _d_smug,
                   "keywords": ["으쓱", "당연하지", "자신감"]},
    "mindblown":  {"name": "멘붕",       "bg": "#A768D6", "draw": _d_mindblown,
                   "keywords": ["멘붕", "충격", "미쳤", "실화"]},
    "touched":    {"name": "감동",       "bg": "#F2A6C9", "draw": _d_touched,
                   "keywords": ["감동", "뭉클", "울컥"]},
    "curious":    {"name": "궁금",       "bg": "#6FCBD1", "draw": _d_curious,
                   "keywords": ["궁금", "뭐야", "뭐지", "뭔가요"]},
    "shy":        {"name": "수줍음",     "bg": "#FFC1CC", "draw": _d_shy,
                   "keywords": ["부끄러", "수줍", "쑥스러"]},
    "ok":         {"name": "OK",         "bg": "#4CC98A", "draw": _d_ok,
                   # 주의: "네"/"응"/"그래" 등은 거의 모든 한국어 문장 끝에("~하네", "~그래요")
                   # 흔히 등장해 오탐(false positive)이 너무 잦아 일부러 제외했다.
                   "keywords": ["ok", "오케이", "콜", "좋습니다", "좋아요"]},
    "no":         {"name": "NO",         "bg": "#E5484D", "draw": _d_no,
                   "keywords": ["no", "아니", "싫어", "안돼", "노노"]},
    "lunch":      {"name": "점심콜?",    "bg": "#FFA65C", "draw": _d_lunch,
                   "keywords": ["점심"]},
    "dinner":     {"name": "저녁콜?",    "bg": "#3C4A8C", "draw": _d_dinner,
                   "keywords": ["저녁"]},
    "thumbsup":   {"name": "수고!",      "bg": "#2FA8A3", "draw": _d_thumbsup,
                   "keywords": ["수고", "고생하셨", "고생했"]},
    "fighting":   {"name": "파이팅!",    "bg": "#FF5C7A", "draw": _d_fighting,
                   "keywords": ["화이팅", "파이팅", "힘내", "응원"]},
    "thanks":     {"name": "감사!",      "bg": "#E0A83E", "draw": _d_thanks,
                   "keywords": ["감사합니다", "감사해요", "고마워요", "고맙"]},
    "congrats":   {"name": "축하!",      "bg": "#9B59D0", "draw": _d_congrats,
                   "keywords": ["축하"]},
    "sorry":      {"name": "미안!",      "bg": "#7C93B0", "draw": _d_sorry,
                   "keywords": ["미안", "죄송"]},
    "goodmorning": {"name": "굿모닝",     "bg": "#FFB648", "draw": _d_goodmorning,
                   "keywords": ["굿모닝", "좋은 아침", "굿모닝이에요"]},
    "goodnight":  {"name": "굿나잇",     "bg": "#29305C", "draw": _d_goodnight,
                   "keywords": ["굿나잇", "잘자", "잘 자"]},
    "leaving":    {"name": "퇴근!",      "bg": "#3FBF7F", "draw": _d_leaving,
                   "keywords": ["퇴근"]},
    "wait":       {"name": "잠깐!",      "bg": "#E8934A", "draw": _d_wait,
                   "keywords": ["잠깐", "잠시만", "잠시 후"]},
    "best":       {"name": "최고!",      "bg": "#FF4F81", "draw": _d_best,
                   "keywords": ["최고"]},
}

STICKER_CATEGORY_ORDER = list(STICKERS.keys())


def draw_sticker(canvas, cx, cy, r, sticker_id, keepalive):
    """(cx, cy)를 중심으로 지름 2r 크기의 스티커를 그린다. keepalive는 PhotoImage가
    가비지 컬렉션되지 않도록 참조를 붙잡아둘 리스트(호출자가 관리, 보통 self._chat_images)."""
    info = STICKERS.get(sticker_id)
    if not info:
        return []
    size = max(8, int(round(r * 2)))
    photo = smooth_circle_photo(size, info["bg"])
    keepalive.append(photo)
    ids = [canvas.create_image(cx, cy, image=photo, anchor="center")]
    s = r / 46.0
    ids.extend(info["draw"](canvas, cx, cy, s))
    return ids


def match_keywords(text):
    """입력창에 타이핑 중인 텍스트에 등록된 키워드가 포함돼 있으면 매칭되는
    스티커 id들을 등록 순서대로 반환한다(카카오톡의 "추천 이모티콘" 기능과 동일한 개념).
    단순 부분 문자열 검사(`kw in text`)였을 때는 "피아니스트"에 "아니"(NO 스티커
    키워드)가 들어 있다는 이유만으로 엉뚱하게 추천되는 문제가 있어, 단어 경계
    (`\\b`)를 요구하도록 고쳤다 — 파이썬 정규식은 한글도 \\w로 인식하므로
    한글 단어 경계에도 정상 동작한다."""
    text = (text or "").strip()
    if not text:
        return []
    low = text.lower()
    out = []
    for sid, info in STICKERS.items():
        for kw in info.get("keywords", ()):
            if kw and re.search(r"\b" + re.escape(kw.lower()) + r"\b", low):
                out.append(sid)
                break
    return out


def prewarm_stickers():
    """앱 기동 후 유휴 시간에 호출되어, 스티커에 쓰이는 모든 배경 원형 이미지(96px 및 38px)를
    캐시에 미리 생성해 둔다 — 첫 스티커 방 진입이나 스티커 메뉴 오픈 시 지연을 0으로 만든다."""
    for info in STICKERS.values():
        bg = info.get("bg")
        if bg:
            smooth_circle_photo(96, bg)
            smooth_circle_photo(38, bg)

