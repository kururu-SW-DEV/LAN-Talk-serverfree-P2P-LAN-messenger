# -*- coding: utf-8 -*-
"""widgets.py — 스플리터 접기 핸들, 맨 아래로 스크롤 버튼 등 재사용 커스텀 위젯.
lan_messenger.py에서 분리됨 (유지보수를 위해 여러 파일로 분할, v5.1)."""
import math
import os
import tkinter as tk
import tkinter.font as tkfont

from constants import *  # noqa: F401,F403 - 색상/폰트 상수 전체 사용
from canvas_utils import round_rect, safe_canvas_mousewheel, register_outside_click
from netutils import sanitize_chat_text
from winapi import apply_ime_font
import stickers

try:
    import ctypes
    _HAS_CTYPES = True
except ImportError:  # pragma: no cover - 표준 라이브러리라 사실상 항상 성공
    _HAS_CTYPES = False


# ========== Windows 레이어드 윈도우(진짜 알파 블렌딩) — 매끈한 원형 버튼용 ==========
# SetWindowRgn(창 리전 오려내기)은 GDI 리전이라 경계가 항상 계단식(안티앨리어싱 불가)이다.
# 진짜 부드러운 원을 만들려면 AC_SRC_ALPHA 블렌드로 픽셀마다 알파를 갖는 32bpp 비트맵을
# UpdateLayeredWindow로 합성해야 한다 — 뒤에 뭐가 있든(파란 말풍선이든) 경계가 매끄럽게 섞인다.
if _HAS_CTYPES and os.name == "nt":
    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    class _BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
        ]

    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _GWL_EXSTYLE = -20
    _WS_EX_LAYERED = 0x00080000
    _ULW_ALPHA = 0x00000002
    _AC_SRC_OVER = 0x00
    _AC_SRC_ALPHA = 0x01

    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    _user32.GetDC.restype = ctypes.c_void_p
    _user32.GetDC.argtypes = [ctypes.c_void_p]
    _user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.UpdateLayeredWindow.restype = ctypes.c_int
    _user32.UpdateLayeredWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
        ctypes.c_void_p, ctypes.POINTER(_POINT), ctypes.c_uint32,
        ctypes.POINTER(_BLENDFUNCTION), ctypes.c_uint32,
    ]
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    _gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    _gdi32.CreateDIBSection.restype = ctypes.c_void_p
    _gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint32]

    def _update_layered_argb(hwnd, size, argb_bgra_premul):
        """hwnd(정사각형 size x size 자식 창)의 내용을 premultiplied BGRA 픽셀 버퍼로
        완전히 교체해 합성한다. 실패해도 예외를 던지지 않고 조용히 포기한다(호출 쪽이
        평범한 Tk Canvas 렌더링으로 대체)."""
        try:
            ex = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex | _WS_EX_LAYERED)

            hdc_screen = _user32.GetDC(0)
            hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
            bmi = _BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.biWidth = size
            bmi.biHeight = -size  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0  # BI_RGB

            bits_ptr = ctypes.c_void_p()
            hbmp = _gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), 0,
                                           ctypes.byref(bits_ptr), None, 0)
            old = _gdi32.SelectObject(hdc_mem, hbmp)
            ctypes.memmove(bits_ptr, argb_bgra_premul, len(argb_bgra_premul))

            blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
            size_s = _SIZE(size, size)
            src_pt = _POINT(0, 0)
            _user32.UpdateLayeredWindow(hwnd, 0, None, ctypes.byref(size_s), hdc_mem,
                                        ctypes.byref(src_pt), 0, ctypes.byref(blend), _ULW_ALPHA)

            _gdi32.SelectObject(hdc_mem, old)
            _gdi32.DeleteObject(hbmp)
            _gdi32.DeleteDC(hdc_mem)
            _user32.ReleaseDC(0, hdc_screen)
            return True
        except Exception:
            return False
else:
    def _update_layered_argb(hwnd, size, argb_bgra_premul):
        return False


def _seg_dist(px, py, x1, y1, x2, y2):
    """점 (px,py)에서 선분 (x1,y1)-(x2,y2)까지 최단 거리."""
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

class SplitterHandle(tk.Canvas):
    """스플리터 기둥의 모던 슬림 접기/펼치기 핸들 위젯 (상하로 긴 편안한 클릭 영역)."""

    def __init__(self, master, command=None, width=18, height=84, **kw):
        super().__init__(master, width=width, height=height, bg=kw.get("bg", C_MAIN),
                         highlightthickness=0, cursor="hand2")
        self.command = command
        self._is_collapsed = False
        self._hovered = False
        self._w_val = width
        self._h_val = height
        self._draw()
        self.bind("<Enter>", lambda e: (setattr(self, "_hovered", True), self._draw()))
        self.bind("<Leave>", lambda e: (setattr(self, "_hovered", False), self._draw()))
        self.bind("<Button-1>", lambda e: self.invoke())

    def lift(self, aboveThis=None):
        tk.Misc.lift(self, aboveThis)

    def set_state(self, collapsed):
        self._is_collapsed = collapsed
        self._draw()

    def _draw(self):
        self.delete("all")
        w = getattr(self, "_w_val", 18)
        h = getattr(self, "_h_val", 84)
        r = 4
        bg_col = C_ME if self._hovered else C_CARD
        border_col = "#3b82f6" if self._hovered else C_BORDER
        fg_col = "#ffffff" if self._hovered else (C_TEXT if self._is_collapsed else C_MUTE)

        # 기하학적으로 왜곡 없는 깔끔한 둥근 사각형 렌더링 (4 코너 아크 + 사각형)
        d = r * 2
        self.create_arc(1, 1, 1 + d, 1 + d, start=90, extent=90, fill=bg_col, outline=border_col, width=1)
        self.create_arc(w - 1 - d, 1, w - 1, 1 + d, start=0, extent=90, fill=bg_col, outline=border_col, width=1)
        self.create_arc(w - 1 - d, h - 1 - d, w - 1, h - 1, start=270, extent=90, fill=bg_col, outline=border_col, width=1)
        self.create_arc(1, h - 1 - d, 1 + d, h - 1, start=180, extent=90, fill=bg_col, outline=border_col, width=1)
        self.create_rectangle(1 + r, 1, w - 1 - r, h, fill=bg_col, outline=bg_col)
        self.create_rectangle(1, 1 + r, w, h - 1 - r, fill=bg_col, outline=bg_col)
        self.create_line(1 + r, 1, w - 1 - r, 1, fill=border_col, width=1)
        self.create_line(1 + r, h - 1, w - 1 - r, h - 1, fill=border_col, width=1)
        self.create_line(1, 1 + r, 1, h - 1 - r, fill=border_col, width=1)
        self.create_line(w - 1, 1 + r, w - 1, h - 1 - r, fill=border_col, width=1)

        cx, cy = w // 2, h // 2

        # 상하 그립 포인트 (길어진 버튼의 터치/클릭 영역 시인성 극대화)
        grip_col = "#93c5fd" if self._hovered else C_BORDER
        for dy in (-26, -18, 18, 26):
            self.create_oval(cx - 1, cy + dy - 1, cx + 1, cy + dy + 1, fill=grip_col, outline="")

        # 폰트 렌더링 왜곡 없는 선명한 2px 벡터 셰브론 인디케이터
        if self._is_collapsed:
            # 펼치기 (오른쪽 화살표 >)
            self.create_line(cx - 2, cy - 7, cx + 3, cy, cx - 2, cy + 7,
                             fill=fg_col, width=2, capstyle="round", joinstyle="round")
        else:
            # 접기 (왼쪽 화살표 <)
            self.create_line(cx + 2, cy - 7, cx - 3, cy, cx + 2, cy + 7,
                             fill=fg_col, width=2, capstyle="round", joinstyle="round")

    def invoke(self):
        if self.command:
            self.command()

    def cget(self, key):
        if key == "text":
            return "▶" if self._is_collapsed else "◀"
        return super().cget(key)

    def config(self, **kw):
        if "text" in kw:
            t = kw.pop("text")
            self.set_state(t in ("▶", ">", "›"))
        else:
            self._draw()


class ScrollBottomButton(tk.Canvas):
    """카카오톡 스타일 우하단 플로팅 맨 아래로 스크롤 원형 버튼.

    Windows에서는 UpdateLayeredWindow로 픽셀마다 진짜 알파값을 가진 원을 합성해서
    그린다 — Tk Canvas의 create_oval만으로는(또는 SetWindowRgn으로 사각형을 오려내는
    방식으로는) 가장자리가 계단식으로 거칠어지는 것을 피할 수 없기 때문이다. 이 방식이
    안 되는 환경(비-Windows 등)에서는 예전처럼 Tk Canvas에 직접 그리는 방식으로
    자동 대체된다(가장자리가 약간 거칠 뿐 기능은 동일)."""

    def __init__(self, master, command=None, size=38, **kw):
        super().__init__(master, width=size, height=size, bg=kw.get("bg", C_MAIN),
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self._size = size
        self._hovered = False
        self._layered_ok = None  # None=아직 모름, True/False=시도 결과
        self._draw()
        self.bind("<Enter>", lambda e: (setattr(self, "_hovered", True), self._draw()))
        self.bind("<Leave>", lambda e: (setattr(self, "_hovered", False), self._draw()))
        self.bind("<Button-1>", lambda e: self.invoke())
        # 마우스 휠이 버튼 위에 있을 때도 대화창 스크롤이 자연스럽게 연동되도록 이벤트 전파
        self.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(master, e))

    def lift(self, aboveThis=None):
        tk.Misc.lift(self, aboveThis)

    def invoke(self):
        if self.command:
            self.command()

    # ---------- 매끈한 원 렌더링 (해석적 안티앨리어싱) ----------
    def _render_argb(self):
        """s×s premultiplied BGRA 픽셀 버퍼를 계산한다. 원·테두리·셰브론 전부
        "픽셀 중심에서 경계까지의 거리"로 커버리지(0~1)를 구하는 방식이라, 슈퍼샘플링
        없이도 매끄러운 경계가 나온다(거리 기반 analytic anti-aliasing)."""
        s = self._size
        cx = cy = s / 2.0
        r_fill = s / 2.0 - 2.5
        r_outer = s / 2.0 - 1.5
        if self._hovered:
            bg, border, icon = (243, 244, 246), (156, 163, 175), (17, 24, 39)
        else:
            bg, border, icon = (255, 255, 255), (229, 231, 235), (55, 65, 81)
        half_w = 1.3
        p1 = (cx - 6, cy - 3)
        p2 = (cx, cy + 3)
        p3 = (cx + 6, cy - 3)

        buf = bytearray(s * s * 4)
        for y in range(s):
            fy = y + 0.5
            row = y * s
            for x in range(s):
                fx = x + 0.5
                d = math.hypot(fx - cx, fy - cy)
                cov_outer = max(0.0, min(1.0, r_outer - d + 0.5))
                if cov_outer <= 0.0:
                    continue
                cov_fill = max(0.0, min(1.0, r_fill - d + 0.5))
                r_ = bg[0] * cov_fill + border[0] * (1 - cov_fill)
                g_ = bg[1] * cov_fill + border[1] * (1 - cov_fill)
                b_ = bg[2] * cov_fill + border[2] * (1 - cov_fill)
                dseg = min(_seg_dist(fx, fy, *p1, *p2), _seg_dist(fx, fy, *p2, *p3))
                cov_icon = max(0.0, min(1.0, half_w - dseg + 0.5))
                if cov_icon > 0.0:
                    r_ = r_ * (1 - cov_icon) + icon[0] * cov_icon
                    g_ = g_ * (1 - cov_icon) + icon[1] * cov_icon
                    b_ = b_ * (1 - cov_icon) + icon[2] * cov_icon
                idx = (row + x) * 4
                buf[idx + 0] = int(b_ * cov_outer)  # premultiplied
                buf[idx + 1] = int(g_ * cov_outer)
                buf[idx + 2] = int(r_ * cov_outer)
                buf[idx + 3] = int(cov_outer * 255)
        return bytes(buf)

    def _draw(self):
        if self._layered_ok is not False:
            try:
                self.update_idletasks()
                hwnd = int(self.winfo_id())
                ok = _update_layered_argb(hwnd, self._size, self._render_argb())
                if ok:
                    self._layered_ok = True
                    return
                self._layered_ok = False
            except Exception:
                self._layered_ok = False

        # 레이어드 윈도우를 못 쓰는 환경(비-Windows 등) — 예전 방식의 Tk 렌더링으로 대체
        self.delete("all")
        s = self._size
        pad = 2
        bg_col = "#f3f4f6" if self._hovered else "#ffffff"
        border_col = "#9ca3af" if self._hovered else "#e5e7eb"
        icon_col = "#111827" if self._hovered else "#374151"
        self.create_oval(pad, pad, s - pad, s - pad, fill=bg_col, outline=border_col, width=1)
        cx, cy = s / 2, s / 2
        self.create_line(cx - 6, cy - 3, cx, cy + 3, cx + 6, cy - 3,
                         fill=icon_col, width=2.5, capstyle="round", joinstyle="round")


class MinimalScrollbar(tk.Canvas):
    """카카오톡풍 미니멀 스크롤바 — 트랙(배경 바)은 안 보이고, 내용이 넘칠 때만
    양끝이 둥근 얇은 회색 막대(썸)가 떠 있는 것처럼 표시된다. ttk.Scrollbar 대신
    다크 테마에 맞춰 직접 그린다. `set(first, last)`만 있으면 되므로 기존
    `yscrollcommand`에 그대로 연결 가능하다."""

    def __init__(self, master, target, width=6, **kw):
        super().__init__(master, width=width, highlightthickness=0, bd=0,
                         bg=kw.pop("bg", C_MAIN), cursor="arrow", **kw)
        self.target = target  # yview_moveto()를 가진 위젯(Canvas 등)
        self._first = 0.0
        self._last = 1.0
        self._drag = None
        self._hovered = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.bind("<Enter>", lambda e: (setattr(self, "_hovered", True), self._draw()))
        self.bind("<Leave>", lambda e: (setattr(self, "_hovered", False), self._draw()))

    def set(self, first, last):
        """ttk.Scrollbar와 같은 인터페이스 — Canvas/Text의 yscrollcommand에 바로 연결."""
        try:
            self._first, self._last = float(first), float(last)
        except (TypeError, ValueError):
            return
        self._draw()

    def _thumb_bounds(self, h):
        span = max(0.0, self._last - self._first)
        min_frac = (24.0 / h) if h else 0.0
        thumb_span = min(1.0, max(span, min_frac))
        y1 = self._first * h
        y2 = y1 + thumb_span * h
        if y2 > h:
            y2 = h
            y1 = max(0.0, y2 - thumb_span * h)
        return y1, y2

    def _draw(self):
        self.delete("all")
        h = self.winfo_height()
        w = self.winfo_width()
        if h <= 1 or (self._last - self._first) >= 0.999:
            return  # 내용이 다 보이면 트랙조차 그리지 않음(카톡처럼 완전히 사라짐)

        # 은은한 스크롤 트랙 가이드 (너비의 중앙에 얇게 배치)
        track_w = max(2, w // 2)
        tx = (w - track_w) // 2
        round_rect(self, tx, 2, tx + track_w, h - 2,
                   r=track_w / 2, fill="#32353e", outline="")

        y1, y2 = self._thumb_bounds(h)
        pad_x = 1
        pad_y = 1
        color = "#9ca3af" if self._hovered else "#6b7280"
        round_rect(self, pad_x, y1 + pad_y, max(pad_x + 2, w - pad_x), max(y1 + pad_y + 2, y2 - pad_y),
                  r=(w - 2 * pad_x) / 2, fill=color, outline="")

    def _on_press(self, e):
        h = self.winfo_height()
        y1, y2 = self._thumb_bounds(h)
        if y1 <= e.y <= y2:
            self._drag = {"y": e.y, "first": self._first}
        elif h > 0:
            span = self._last - self._first
            target_first = max(0.0, min(1.0 - span, (e.y / h) - span / 2))
            self.target.yview_moveto(target_first)

    def _on_drag(self, e):
        if not self._drag:
            return
        h = self.winfo_height()
        if h <= 0:
            return
        span = self._last - self._first
        dy = (e.y - self._drag["y"]) / h
        new_first = max(0.0, min(1.0 - span, self._drag["first"] + dy))
        self.target.yview_moveto(new_first)


def make_search_icon(master, bg, fg=C_MUTE, size=16):
    """경량 벡터 돋보기 아이콘 (Segoe UI Emoji 폰트 폴백 1초 지연 방지용 Canvas)"""
    c = tk.Canvas(master, width=size, height=size, bg=bg, highlightthickness=0)
    c.create_oval(3, 3, size - 5, size - 5, outline=fg, width=1.8)
    c.create_line(size - 6, size - 6, size - 2, size - 2, fill=fg, width=2, capstyle="round")
    return c


class PillButton(tk.Canvas):
    """양끝이 완전히 둥근 알약(pill) 모양 버튼. tk.Button은 각진 사각형만 지원해서
    둥근 입력 바 안에 넣으면 어색해 보이길래, 다른 커스텀 위젯들처럼 Canvas에
    직접 그린다. tk.Button과 최소한 같은 크기·`configure(state=...)` 인터페이스를
    제공해 기존 코드에서 거의 그대로 바꿔 끼울 수 있게 했다."""

    def __init__(self, master, text, command=None, bg=C_ME, fg="white", hover_bg=None,
                 font=FONT_BTN, padx=10, pady=5, bg_outer=None):
        if text in ("😊", "⏱"):
            # 벡터 아이콘: Segoe UI Emoji 폰트 폴백(1초 지연) 방지
            f = tkfont.Font(font=font)
            linespace = f.metrics("linespace")
            w = linespace + padx * 2
            h = linespace + pady * 2
        else:
            f = tkfont.Font(font=font)
            w = f.measure(text) + padx * 2
            h = f.metrics("linespace") + pady * 2
        super().__init__(master, width=w, height=h, highlightthickness=0, bd=0,
                         bg=bg_outer or C_CARD, cursor="hand2")
        self.command = command
        self.text = text
        self.font = font
        self.bg_color = bg
        self.fg_color = fg
        self.hover_color = hover_bg or bg
        self._state = "normal"
        self._hovered = False
        # 주의: self._w는 tkinter 내부(Tcl 위젯 경로명)가 이미 쓰는 이름이라 절대 덮어쓰면 안 됨
        # (SplitterHandle이 _w_val을 쓰는 것과 같은 이유) — 그래서 _pw/_ph로 따로 둔다.
        self._pw, self._ph = w, h
        self._draw()
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _draw(self):
        self.delete("all")
        disabled = self._state == "disabled"
        if disabled:
            fill, text_fill = C_HOVER, C_MUTE
        else:
            fill = self.hover_color if self._hovered else self.bg_color
            text_fill = self.fg_color
        round_rect(self, 1, 1, self._pw - 1, self._ph - 1, r=self._ph / 2, fill=fill, outline="")
        cx, cy = self._pw / 2, self._ph / 2
        if self.text == "😊":
            r_val = min(cx, cy) - 6
            self.create_oval(cx - r_val, cy - r_val, cx + r_val, cy + r_val, outline=text_fill, width=1.5)
            er = 1.2
            self.create_oval(cx - 3.5 - er, cy - 2 - er, cx - 3.5 + er, cy - 2 + er, fill=text_fill, outline="")
            self.create_oval(cx + 3.5 - er, cy - 2 - er, cx + 3.5 + er, cy - 2 + er, fill=text_fill, outline="")
            self.create_arc(cx - 5, cy - 5, cx + 5, cy + 4, start=205, extent=130, style="arc", outline=text_fill, width=1.5)
        elif self.text == "⏱":
            r_val = min(cx, cy) - 6
            self.create_oval(cx - r_val, cy - r_val + 1, cx + r_val, cy + r_val + 1, outline=text_fill, width=1.5)
            self.create_line(cx - 2.5, cy - r_val - 2, cx + 2.5, cy - r_val - 2, fill=text_fill, width=1.5)
            self.create_line(cx, cy - r_val - 2, cx, cy - r_val + 1, fill=text_fill, width=1.5)
            self.create_line(cx, cy + 1, cx, cy - 4 + 1, fill=text_fill, width=1.5)
            self.create_line(cx, cy + 1, cx + 3.5, cy + 1, fill=text_fill, width=1.5)
        else:
            self.create_text(cx, cy, text=self.text, fill=text_fill, font=self.font)

    def cget(self, key):
        # tk.Canvas 자체에는 Button 같은 "state" 옵션이 없어서, 우리가 추적하는
        # self._state를 대신 돌려준다(Widget.__getitem__이 cget으로 위임하므로
        # btn["state"]/btn.cget("state") 둘 다 이 값을 본다).
        if key == "state":
            return self._state
        return super().cget(key)

    def _on_click(self, _e=None):
        if self._state != "disabled" and self.command:
            self.command()

    def _on_enter(self, _e=None):
        self._hovered = True
        self._draw()

    def _on_leave(self, _e=None):
        self._hovered = False
        self._draw()

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        if "state" in kw:
            self._state = kw.pop("state")
            self._draw()
            super().configure(cursor="arrow" if self._state == "disabled" else "hand2")
        if kw:
            super().configure(**kw)

    config = configure


class ChatSearchBar(tk.Frame):
    """대화방 내 텍스트 검색 플로팅 바 (Ctrl+F).
    [ 🔍 | 검색어 입력... | ▲ | ▼ | 1/5 | ✕ ]
    """
    def __init__(self, master, on_search=None, on_prev=None, on_next=None, on_close=None, **kw):
        super().__init__(master, bg=C_CARD, highlightthickness=1,
                         highlightbackground=C_BORDER, highlightcolor=C_ME, padx=6, pady=4, **kw)
        self.on_search = on_search
        self.on_prev = on_prev
        self.on_next = on_next
        self.on_close = on_close

        make_search_icon(self, bg=C_CARD, fg=C_MUTE, size=16).pack(side="left", padx=(4, 6))

        self.query_var = tk.StringVar()
        self.entry = tk.Entry(self, textvariable=self.query_var, bg=C_SEARCHBG, fg=C_TEXT,
                              font=FONT_SM, relief="flat", insertbackground=C_TEXT,
                              highlightthickness=0, width=18)
        self.entry.pack(side="left", ipady=4, padx=(0, 6))
        apply_ime_font(self.entry, FONT_FAM, FONT_SM[1])
        self.query_var.trace_add("write", lambda *a: self._on_text_change())

        self.count_lbl = tk.Label(self, text="", bg=C_CARD, fg=C_MUTE, font=FONT_XS)
        self.count_lbl.pack(side="left", padx=(0, 6))

        def make_icon_btn(text, cmd):
            b = tk.Label(self, text=text, bg=C_CARD, fg=C_TEXT, font=FONT_SM,
                         cursor="hand2", padx=4, pady=2)
            b.bind("<Enter>", lambda e: b.config(bg=C_HOVER))
            b.bind("<Leave>", lambda e: b.config(bg=C_CARD))
            b.bind("<Button-1>", lambda e: cmd() if cmd else None)
            return b

        self.btn_prev = make_icon_btn("▲", self._prev)
        self.btn_prev.pack(side="left", padx=1)
        self.btn_next = make_icon_btn("▼", self._next)
        self.btn_next.pack(side="left", padx=1)
        self.btn_close = make_icon_btn("✕", self._close)
        self.btn_close.pack(side="left", padx=(4, 2))

        self.entry.bind("<Return>", lambda e: self._on_enter(e))
        self.entry.bind("<Escape>", lambda e: self._close())
        self.bind("<Escape>", lambda e: self._close())

    def _on_enter(self, e):
        if int(e.state) & 0x0001:  # Shift + Enter
            self._prev()
        else:
            self._next()
        return "break"

    def _on_text_change(self):
        raw = self.query_var.get()
        cleaned = sanitize_chat_text(raw)
        if raw != cleaned:
            cursor = self.entry.index("insert")
            self.query_var.set(cleaned)  # write 트레이스를 재귀 호출해 마저 처리(idempotent)
            try:
                self.entry.icursor(min(cursor, len(cleaned)))
            except Exception:
                pass
            return
        q = cleaned
        if self.on_search:
            self.on_search(q)

    def _prev(self):
        if self.on_prev:
            self.on_prev()

    def _next(self):
        if self.on_next:
            self.on_next()

    def _close(self):
        if self.on_close:
            self.on_close()

    def set_counts(self, current, total):
        if total == 0:
            self.count_lbl.config(text="일치 없음" if self.query_var.get().strip() else "")
        else:
            self.count_lbl.config(text=f"{current}/{total}")

    def focus(self):
        self.entry.focus_set()
        self.entry.select_range(0, "end")


class ReplyBanner(tk.Frame):
    """입력창 위에 붙는 인용 답장 배너.
    [ ↩ | 답장: 김철수: 회의 관련 메시지 내용... | ✕ ]
    """
    def __init__(self, master, on_cancel=None, **kw):
        super().__init__(master, bg=C_REPLY_BG, highlightthickness=1,
                         highlightbackground=C_BORDER, padx=8, pady=4, **kw)
        self.on_cancel = on_cancel

        bar = tk.Frame(self, bg=C_REPLY_BAR, width=3)
        bar.pack(side="left", fill="y", padx=(0, 8), pady=1)

        self.text_lbl = tk.Label(self, text="", bg=C_REPLY_BG, fg=C_TEXT, font=FONT_XS,
                                 anchor="w", justify="left")
        self.text_lbl.pack(side="left", fill="x", expand=True)

        close_btn = tk.Label(self, text="✕", bg=C_REPLY_BG, fg=C_MUTE, font=FONT_XS_PAD,
                             cursor="hand2", padx=4, pady=2)
        close_btn.pack(side="right", padx=(6, 2))
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=C_TEXT))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=C_MUTE))
        close_btn.bind("<Button-1>", lambda e: self.on_cancel() if self.on_cancel else None)

    def set_target(self, sender_name, snippet):
        safe_snippet = (snippet or "").replace("\n", " ").strip()[:40]
        self.text_lbl.config(text=f"답장: {sender_name}님에게  ·  \"{safe_snippet}\"")


class PinBanner(tk.Frame):
    """대화방 상단에 붙는 카카오톡 스타일 공지사항 배너.
    [ 📌 | 공지: 김철수: 회의 관련 메시지 내용... | ✕ ]
    클릭하면 원본 메시지로 스크롤 이동, ✕를 누르면 공지 해제."""
    def __init__(self, master, on_click=None, on_unpin=None, **kw):
        super().__init__(master, bg=C_CARD, highlightthickness=1,
                         highlightbackground=C_BORDER, padx=10, pady=6, cursor="hand2", **kw)
        self.on_click = on_click
        self.on_unpin = on_unpin

        self.icon = tk.Label(self, text="", bg=C_CARD, fg=C_ME, font=FONT_SM)
        self.icon.pack(side="left", padx=(0, 8))

        self.text_lbl = tk.Label(self, text="", bg=C_CARD, fg=C_TEXT, font=FONT_XS,
                                 anchor="w", justify="left")
        self.text_lbl.pack(side="left", fill="x", expand=True)

        close_btn = tk.Label(self, text="✕", bg=C_CARD, fg=C_MUTE, font=FONT_XS_PAD,
                             cursor="hand2", padx=4, pady=2)
        close_btn.pack(side="right", padx=(6, 0))
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=C_TEXT))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=C_MUTE))
        close_btn.bind("<Button-1>", self._on_close_click)

        for w in (self, self.icon, self.text_lbl):
            w.bind("<Button-1>", self._on_body_click)

    def _on_close_click(self, _e=None):
        if self.on_unpin:
            self.on_unpin()
        return "break"

    def _on_body_click(self, _e=None):
        if self.on_click:
            self.on_click()

    def set_notice(self, sender, text):
        if not self.icon.cget("text"):
            self.icon.config(text="📌")
        snippet = (text or "").replace("\n", " ").strip()[:60]
        label = f"공지: {sender}: {snippet}" if sender else f"공지: {snippet}"
        self.text_lbl.config(text=label)


# 카카오톡풍 이모지 피커에 쓰는 카테고리별 이모지 목록. 실제 카카오톡 "이모티콘"(구매/
# 다운로드하는 캐릭터 스티커)과는 다르다 — 저작권이 있는 그림이라 만들 수 없고, 여기서는
# 표준 유니코드 이모지만 다룬다.
# (카테고리명, 이모지 목록, 표시 색상) — Tkinter Label은 컬러 이모지 폰트를 그대로
# 못 그리고 fg 색 하나로 단색 렌더링해버려서(실측: 전부 검게 나와 다크 테마에서 거의
# 안 보임), 카테고리별로 그나마 어울리는 색을 지정해 최소한 알아보기 쉽게 만든다.
class EmojiPicker(tk.Frame):
    """스티커 선택 패널 — 입력창 바로 위에 붙어서 열리는 내장 패널.

    (v6.25: 표준 유니코드 이모지 지원을 걷어내고 자체 제작 스티커 전용으로 단순화
    했다 — "이모지를 전부 스티커로 바꿔달라"는 요청. Windows가 "Segoe UI Emoji"
    글꼴로 문자를 처음 그릴 때 치르는 무거운 1회성 비용(카테고리 하나에 수백ms~
    수 초, v6.24에서 실측)이 이제 이 패널 어디에도 없다 — 스티커는 원·곡선·다각형
    등 도형을 직접 그리고, 탭 글자도 이모지 글리프가 아니라 평범한 한글 텍스트라
    특수 글꼴이 전혀 필요 없다. 클래스 이름은 다른 파일들과의 연결부(app.py)를
    그대로 두려고 EmojiPicker로 유지했다.)

    위쪽 "최근"/"스티커" 탭 + 아래쪽 스크롤 그리드로 구성되며, 스티커를 클릭하면
    on_select_sticker(sticker_id)를 호출하고 패널이 자동으로 닫힌다(스티커는
    고르는 즉시 전송되는 동작이라, 여러 개를 계속 고르는 이모지식 유지형과 달리
    바로 닫히는 게 자연스럽다)."""

    def __init__(self, master, before, get_recent=None, on_select_sticker=None):
        super().__init__(master, bg=C_CARD, highlightthickness=1, highlightbackground=C_BORDER)
        self._before = before  # pack(before=...) 기준 위젯 — 항상 이 위젯(입력창) 바로 위에 뜬다
        self.on_select_sticker = on_select_sticker
        self.get_recent = get_recent
        self._visible = False
        self._img_keepalive = []  # 스티커 캔버스의 PhotoImage GC 방지

        tabs_row = tk.Frame(self, bg=C_CARD)
        tabs_row.pack(fill="x", padx=4, pady=(4, 0))
        # 탭이 "최근"/"스티커" 둘뿐이라 짧은 한글 그대로 써도 좁은 채팅 영역에서
        # 겹치지 않는다(예전엔 카테고리가 8개라 아이콘 한 글자로 줄여야 했다) —
        # 그리고 결정적으로, 이모지 글리프 대신 평범한 텍스트라 "Segoe UI Emoji"
        # 글꼴을 아예 건드리지 않는다.
        self._cats = ([("최근", "__RECENT__", C_TEXT)] if get_recent else []) + [("스티커", "__ALL__", C_TEXT)]
        self._tab_btns = []
        for i, (name, _marker, _color) in enumerate(self._cats):
            tabs_row.columnconfigure(i, weight=1)
            # 선택된 탭은 사이드바의 "채팅"/"친구" 탭과 똑같은 방식(선택 배경색 +
            # 밝은 글자, 미선택은 카드 배경 + 흐린 글자)으로 표시해, 색만 살짝
            # 다른 것보다 훨씬 눈에 띄게(탭처럼) 구분되도록 한다.
            b = tk.Label(tabs_row, text=name, bg=C_CARD, fg=C_MUTE,
                        font=FONT_BTN, cursor="hand2", padx=6, pady=4, anchor="center")
            b.grid(row=0, column=i, sticky="ew")
            b.bind("<Button-1>", lambda e, i=i: self._switch_cat(i))
            b.bind("<Enter>", lambda e, i=i: self._tab_hover(i, True))
            b.bind("<Leave>", lambda e, i=i: self._tab_hover(i, False))
            self._tab_btns.append(b)

        # 닫기(✕) 버튼 — 고를 생각이 없을 때 굳이 이모지 버튼을 다시 누르거나
        # 채팅창을 클릭하지 않아도 바로 닫을 수 있게. weight를 안 줘서 탭처럼
        # 늘어나지 않고 항상 제 폭만 차지한다.
        close_btn = tk.Label(tabs_row, text="✕", bg=C_CARD, fg=C_MUTE,
                             font=("Segoe UI", 10), cursor="hand2", padx=6, pady=3)
        close_btn.grid(row=0, column=len(self._cats), sticky="e")
        close_btn.bind("<Button-1>", lambda e: self.hide())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=C_TEXT))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=C_MUTE))

        body_wrap = tk.Frame(self, bg=C_CARD)
        body_wrap.pack(fill="both", expand=True, padx=4, pady=(2, 4))
        self.canvas = tk.Canvas(body_wrap, bg=C_CARD, highlightthickness=0,
                                width=56 * 4, height=56 * 2)
        sb = MinimalScrollbar(body_wrap, target=self.canvas, width=8, bg=C_CARD)
        sb.pack(side="right", fill="y", padx=(2, 2))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=sb.set)
        self.inner = tk.Frame(self.canvas, bg=C_CARD)
        self._inner_win = self.canvas.create_window((0, 0), anchor="n", window=self.inner)
        self.inner.bind("<Configure>", self._reflow_inner)
        self.canvas.bind("<Configure>", self._reflow_inner)
        self.canvas.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(self.canvas, e))
        self.inner.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(self.canvas, e))
        body_wrap.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(self.canvas, e))
        self.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(self.canvas, e))

        self._cur_cols = 4
        # 최초로 열었을 때 보여줄 탭: "최근"은 처음엔 항상 비어 있어 곧바로 보여주기
        # 애매하므로, "최근"이 있으면 그다음(=스티커 전체) 탭을 기본으로 연다.
        self._cur_cat = 1 if get_recent else 0
        # 여기서 바로 _switch_cat()을 호출하지 않는다 — 스티커 28개를 한꺼번에
        # 그리는 비용 자체는 이모지 때와 달리 미미하지만(캔버스 도형이라 글꼴
        # 예열이 필요 없음), 그래도 창이 뜨기 전에 아무 이유 없이 일을 미리 할
        # 필요는 없어 그대로 지연 패턴을 유지한다. 최초 show()에서 그린다.
        self._rendered = False
        self._style_tabs()

    def _style_tabs(self):
        for i, b in enumerate(self._tab_btns):
            active = (i == self._cur_cat)
            b.config(bg=C_ROWSEL if active else C_CARD, fg=C_TEXT if active else C_MUTE)

    def _tab_hover(self, i, entering):
        if i == self._cur_cat:
            return  # 선택된 탭은 호버로 흐려 보이면 안 되니 그대로 둔다
        self._tab_btns[i].config(bg=C_HOVER if entering else C_CARD)

    # 스티커 한 칸(56px) + 앞뒤 여백(12px×2)의 실제 픽셀 폭 — 창 폭을 이 값으로
    # 나눈 몫이 지금 폭에 들어가는 열 수다(반응형 그리드).
    def _sticker_pitch(self):
        return 56 + 12 * 2

    def _cols_for(self, pitch):
        cw = self.canvas.winfo_width()
        if cw <= 1:
            cw = 56 * 4  # 아직 실제 화면에 배치되기 전(최초 렌더) 기본 추정치
        return max(1, cw // pitch)

    def _reflow_inner(self, _e=None):
        cw = self.canvas.winfo_width()
        self.inner.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.coords(self._inner_win, max(0, cw // 2), 0)
        if self._cols_for(self._sticker_pitch()) != self._cur_cols:
            self._switch_cat(self._cur_cat)  # 열 수가 바뀌었으면 현재 탭을 다시 그린다

    def _switch_cat(self, idx):
        self._cur_cat = idx
        self._rendered = True
        self._style_tabs()
        for w in self.inner.winfo_children():
            w.destroy()
        self._img_keepalive = []
        self.canvas.itemconfigure(self._inner_win, width=0, height=0)
        name, marker, _color = self._cats[idx]
        if marker == "__RECENT__" and self.get_recent:
            sticker_ids = self.get_recent()
        else:
            sticker_ids = list(stickers.STICKER_CATEGORY_ORDER)
        if not sticker_ids:
            tk.Label(self.inner, text="최근 사용한 스티커가 없습니다", bg=C_CARD, fg=C_MUTE,
                    font=FONT_XS).grid(row=0, column=0, padx=10, pady=20)
        else:
            self._render_stickers(sticker_ids)
        self.inner.update_idletasks()
        cw = self.canvas.winfo_width()
        self.canvas.coords(self._inner_win, max(0, cw // 2), 0)
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(0.0)

    def _render_stickers(self, sticker_ids):
        cell = 56
        cols = self._cols_for(self._sticker_pitch())
        self._cur_cols = cols
        for i, sid in enumerate(sticker_ids):
            r, c = divmod(i, cols)
            cv = tk.Canvas(self.inner, width=cell, height=cell, bg=C_CARD,
                          highlightthickness=0, cursor="hand2")
            cv.grid(row=r, column=c, padx=12, pady=12)
            stickers.draw_sticker(cv, cell / 2, cell / 2, cell / 2 - 9, sid, self._img_keepalive)
            cv.bind("<Button-1>", lambda e, s=sid: self._pick_sticker(s))
            cv.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(self.canvas, e))

    def _pick_sticker(self, sticker_id):
        if self.on_select_sticker:
            self.on_select_sticker(sticker_id)
        self.hide()

    def show(self):
        if self._visible:
            return
        self._visible = True
        # 최근 탭이 열려 있었다면 최신 내용으로 다시 그리고, 생성자에서 일부러 미뤄둔
        # 최초 렌더도(아직 한 번도 안 그려졌다면) 여기서 처음 치른다.
        if not self._rendered or (self._cur_cat == 0 and self.get_recent):
            self._switch_cat(self._cur_cat)
        # fill="x"로 row 폭에 정확히 맞춘다 — 이렇게 해야 채팅 영역이 좁을 때 패널이
        # 자기 내용물의 자연스러운 폭을 고집해 옆(사이드바)으로 삐져나오는 대신,
        # 항상 채팅창 안쪽 폭에 맞춰 탭이 줄어들며 화면 밖으로 벗어나지 않는다.
        self.pack(fill="x", side="top", pady=(0, 4), before=self._before)

    def hide(self):
        if not self._visible:
            return
        self._visible = False
        self.pack_forget()

    def is_visible(self):
        return self._visible


class MentionPopup(tk.Toplevel):
    """@ 멘션 입력 시 나타나는 멤버 자동완성 팝업."""
    def __init__(self, master, on_select=None):
        super().__init__(master)
        self.wm_overrideredirect(True)
        self.configure(bg=C_CARD, highlightthickness=1, highlightbackground=C_BORDER)
        self.on_select = on_select
        self._items = []
        self.withdraw()

        self.listbox = tk.Listbox(
            self, bg=C_CARD, fg=C_TEXT, selectbackground=C_ROWSEL, selectforeground=C_TEXT,
            font=FONT_SM, borderwidth=0, highlightthickness=0, activestyle="none", height=5
        )
        self.listbox.pack(fill="both", expand=True, padx=2, pady=2)
        self.listbox.bind("<ButtonRelease-1>", self._on_click)
        self._outside_click_armed = False
        # 앱 밖(다른 프로그램)을 클릭해도 이 팝업은 화면 맨 위(topmost 아님이지만
        # overrideredirect라 항상 보임)에 계속 떠 있던 문제 — 앱 전체가 포커스를
        # 잃으면(다른 창 클릭) 같이 닫는다. dialogs.py의 팝업 메뉴와 동일한 방식.
        master.bind("<FocusOut>", self._on_app_focus_out, add="+")

    def _on_app_focus_out(self, _e=None):
        if not self.is_visible():
            return
        def _check():
            try:
                if not self.is_visible():
                    return
                if self.master.focus_get() is None:
                    self.hide()
            except tk.TclError:
                self.hide()
        self.after(50, _check)

    def show(self, x, y, items):
        self._items = list(items)
        self.listbox.delete(0, "end")
        if not items:
            self.hide()
            return
        for name in self._items:
            self.listbox.insert("end", f"  @{name}")
        self.listbox.selection_set(0)
        h = min(len(self._items), 5) * 26 + 8
        w = max(160, min(300, max(len(name) * 12 for name in self._items) + 40))
        self.geometry(f"{w}x{h}+{x}+{max(0, y - h - 4)}")
        self.deiconify()
        self.lift()
        # 채팅창이나 다른 앱을 클릭해도 안 사라지고 계속 떠 있던 문제 — 팝업
        # 바깥쪽 클릭을 감지해 닫는다(dialogs.py의 팝업 메뉴와 동일 방식).
        # 이 팝업은 "@" 입력이라는 키보드 이벤트로 뜨므로, 여는 클릭과 겹쳐서
        # 바로 닫혀버릴 걱정 없이 매번 새로 무장해도 안전하다.
        if not self._outside_click_armed:
            self._outside_click_armed = True
            register_outside_click(self.master, self._on_outside_click)

    def _on_outside_click(self, e):
        if not self.is_visible():
            return
        try:
            wx1, wy1 = self.winfo_rootx(), self.winfo_rooty()
            wx2, wy2 = wx1 + self.winfo_width(), wy1 + self.winfo_height()
        except tk.TclError:
            return
        if not (wx1 <= e.x_root <= wx2 and wy1 <= e.y_root <= wy2):
            self.hide()

    def hide(self):
        self.withdraw()

    def is_visible(self):
        try:
            return bool(self.winfo_ismapped())
        except Exception:
            return False

    def select_prev(self):
        if not self._items:
            return
        cur = self.listbox.curselection()
        idx = (cur[0] - 1) % len(self._items) if cur else (len(self._items) - 1)
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)

    def select_next(self):
        if not self._items:
            return
        cur = self.listbox.curselection()
        idx = (cur[0] + 1) % len(self._items) if cur else 0
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)

    def get_selected(self):
        cur = self.listbox.curselection()
        if cur and cur[0] < len(self._items):
            return self._items[cur[0]]
        # 실제 선택이 없으면 아무것도 반환하지 않는다 — 예전엔 여기서 items[0]으로
        # 대체해서, 멘션을 취소하려고 팝업 빈 곳을 클릭해도 1번 멤버가 강제로
        # 자동완성되는 버그가 있었다.
        return None

    def _on_click(self, e):
        sel = self.get_selected()
        if sel and self.on_select:
            self.on_select(sel)
        self.hide()



