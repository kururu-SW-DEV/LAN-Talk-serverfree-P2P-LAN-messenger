# -*- coding: utf-8 -*-
"""toast_popup.py — 앱이 직접 그리는 인앱 알림 토스트.
Windows 트레이 풍선/액션 센터 토스트는 클릭 시 NIN_BALLOONUSERCLICK 콜백이
안정적으로 오지 않는 경우가 실제로 확인되어(OS 자체의 알려진 한계), 클릭하면
반드시 해당 대화방으로 이동해야 하는 알림은 이 창이 직접 그리고 직접 클릭을
받는 팝업으로 대체한다. app.py에서 분리됨."""
import os
import tkinter as tk

from constants import C_CARD, C_BORDER, C_TEXT, C_MUTE, C_AVA, FONT_FAM, FONT_NAME, FONT_MSG, FONT_XS
from canvas_utils import smooth_circle_photo

try:
    import ctypes
    from ctypes import wintypes
    _HAS_CTYPES = True
except ImportError:
    _HAS_CTYPES = False


def _work_area():
    """작업표시줄을 제외한 화면 작업 영역(우하단 배치 기준)을 구한다."""
    if _HAS_CTYPES and os.name == "nt":
        try:
            SPI_GETWORKAREA = 0x0030
            rect = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
    return None


class NotificationToast(tk.Toplevel):
    """트레이 알림을 대신하는 인앱 토스트 팝업 — 화면 우하단에 뜨고,
    클릭하면 콜백을 실행한 뒤 스스로 닫힌다."""

    WIDTH = 320
    HEIGHT = 86
    LIFETIME_MS = 6000

    def __init__(self, master, title, text, on_click):
        super().__init__(master)
        self._on_click = on_click
        self._closing = False
        self._timer = None

        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        try:
            self.attributes("-alpha", 0.97)
        except Exception:
            pass
        self.configure(bg=C_BORDER)

        outer = tk.Frame(self, bg=C_BORDER)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        self._card = tk.Frame(outer, bg=C_CARD, width=self.WIDTH, height=self.HEIGHT)
        self._card.pack(fill="both", expand=True)
        self._card.pack_propagate(False)

        self._close_btn = tk.Label(self._card, text="✕", bg=C_CARD, fg=C_MUTE,
                                    font=FONT_XS, cursor="hand2")
        self._close_btn.place(x=self.WIDTH - 28, y=6, width=20, height=20)

        self._av = tk.Canvas(self._card, width=40, height=40, bg=C_CARD, highlightthickness=0)
        self._av.place(x=14, y=20)

        self._app_lbl = tk.Label(self._card, text="LAN Talk", bg=C_CARD, fg=C_MUTE, font=FONT_XS, anchor="w")
        self._app_lbl.place(x=66, y=8, width=self.WIDTH - 96)

        self._name_lbl = tk.Label(self._card, bg=C_CARD, fg=C_TEXT, font=FONT_NAME, anchor="w")
        self._name_lbl.place(x=66, y=25, width=self.WIDTH - 90)

        self._text_lbl = tk.Label(self._card, bg=C_CARD, fg=C_MUTE, font=FONT_MSG, anchor="w")
        self._text_lbl.place(x=66, y=47, width=self.WIDTH - 90)

        for w in (self._card, self._av, self._app_lbl, self._name_lbl, self._text_lbl):
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

        self.set_content(title, text)
        self._place()
        self._reset_timer()
        # 창이 화면에 막 나타나는 순간 그 자리에 남아있던 클릭이 이 창으로 잘못
        # 전달되는 경우가 있어(윈도우가 최상위/오버라이드리다이렉트로 뜨는 타이밍과
        # 겹치는 문제로 추정), 클릭 인식을 잠깐 늦춰 "뜨자마자 바로 닫힘/이동"되는
        # 오작동을 막는다.
        self.after(400, self._enable_clicks)

    def _enable_clicks(self):
        try:
            self._close_btn.bind("<Button-1>", lambda e: self._close(navigate=False))
            for w in (self._card, self._av, self._app_lbl, self._name_lbl, self._text_lbl):
                w.bind("<Button-1>", lambda e: self._close(navigate=True))
        except Exception:
            pass

    def _place(self):
        """최종 위치를 계산해두고, 화면 오른쪽 바깥에서 그 자리로 슬라이드
        들어오는 등장 애니메이션을 시작한다."""
        try:
            self.update_idletasks()
            area = _work_area()
            if area:
                _, _, right, bottom = area
            else:
                right = self.winfo_screenwidth()
                bottom = self.winfo_screenheight() - 48
            self._target_x = right - self.WIDTH - 16
            self._start_x = right + 8  # 화면 오른쪽 바깥(완전히 안 보이는 지점)에서 시작
            self._y = bottom - self.HEIGHT - 12
            self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{self._start_x}+{self._y}")
            self._animate_in(0)
        except Exception:
            pass

    def _animate_in(self, step):
        """오른쪽에서 왼쪽으로 부드럽게 미끄러져 들어오는 애니메이션
        (ease-out: 처음엔 빠르게, 끝에서 서서히 멈춤)."""
        total_steps = 14
        try:
            if step >= total_steps or self._closing:
                self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{self._target_x}+{self._y}")
                return
            t = (step + 1) / total_steps
            eased = 1 - (1 - t) ** 3  # ease-out cubic
            x = int(round(self._start_x + (self._target_x - self._start_x) * eased))
            self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{self._y}")
            self.after(14, lambda: self._animate_in(step + 1))
        except Exception:
            pass

    def set_content(self, title, text):
        title = title or ""
        text = (text or "").replace("\n", " ")
        title_short = title if len(title) <= 22 else title[:21] + "…"
        text_short = text if len(text) <= 42 else text[:41] + "…"
        self._name_lbl.configure(text=title_short)
        self._text_lbl.configure(text=text_short)

        color = C_AVA[hash(title) % len(C_AVA)] if title else C_AVA[0]
        self._av.delete("all")
        initial = (title.strip()[:1] or "?").upper()
        photo = smooth_circle_photo(40, color)
        self._av.create_image(0, 0, image=photo, anchor="nw")
        self._av._smooth_ref = photo  # GC 방지
        self._av.create_text(20, 20, text=initial, fill="#ffffff", font=(FONT_FAM, -14, "bold"))

    def update_content(self, title, text, on_click):
        """이미 떠 있는 토스트를 새 알림 내용으로 갱신하고 표시 시간을 리셋한다
        (짧은 시간에 여러 알림이 오면 팝업이 여러 개 겹치지 않도록)."""
        self._on_click = on_click
        self.set_content(title, text)
        self._reset_timer()
        try:
            self.lift()
        except Exception:
            pass

    def _reset_timer(self):
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except Exception:
                pass
        self._timer = self.after(self.LIFETIME_MS, lambda: self._close(navigate=False))

    def _close(self, navigate):
        if self._closing:
            return
        self._closing = True
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except Exception:
                pass
        if navigate and self._on_click:
            try:
                self._on_click()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass
