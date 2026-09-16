# -*- coding: utf-8 -*-
"""dialogs.py — 모달 대화상자 및 팝업 믹스인 (설정, 그룹 생성, 멤버 초대/목록, 별칭/대화 관리).
app.py에서 분리됨 (유지보수 및 모듈화를 위한 리팩토링)."""
import os
import time
import tkinter as tk
from tkinter import filedialog

from constants import *
from netutils import local_ips, parse_target, sanitize_chat_text
from canvas_utils import safe_canvas_mousewheel, register_outside_click, bind_scoped_mousewheel
from winapi import apply_ime_font
from widgets import MinimalScrollbar


class DialogsMixin:
    """모달 다이얼로그 및 UI 팝업 믹스인."""

    # ---------- 위젯 유틸 ----------
    def _hover(self, w, base, hover):
        w.bind("<Enter>", lambda e: w.config(bg=hover))
        w.bind("<Leave>", lambda e: w.config(bg=base))

    def _btn(self, parent, text, cmd, bg, fg, hover, font=FONT_SM, padx=10, pady=5):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=hover, activeforeground=fg, font=font,
                      relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                      padx=padx, pady=pady)
        self._hover(b, bg, hover)
        return b

    # ---------- 커스텀 드롭다운 메뉴 ----------
    def _popup_menu(self, x, y, items):
        """items: [(라벨, 콜백), ...]. 라벨이 None이면 구분선.
        별도 창(Toplevel) 대신, 메신저 본창 안쪽에 place()로 배치되는 위젯으로
        띄운다 — 화면 좌표(x, y)를 본창 기준 상대 좌표로 바꾸고 본창 안쪽에만
        머물도록 clamp하므로, 우클릭 메뉴처럼 커서 위치를 따라가는 경우에도
        구조적으로 창 밖으로 나갈 수 없다."""
        container = tk.Frame(self.root, bg=C_BORDER)
        inner = tk.Frame(container, bg=C_CARD)
        inner.pack(padx=1, pady=1)
        closed = {"done": False}
        _unregister_outside = [None]

        def _close():
            if closed["done"]:
                return
            closed["done"] = True
            if _unregister_outside[0] is not None:
                _unregister_outside[0]()
            if container.winfo_exists():
                container.destroy()

        def _make_row(label, cmd):
            row = tk.Label(inner, text=label, bg=C_CARD, fg=C_TEXT, font=FONT_SM,
                           anchor="w", padx=18, pady=9, cursor="hand2")
            row.pack(fill="x")
            row.bind("<Enter>", lambda e: row.config(bg=C_HOVER))
            row.bind("<Leave>", lambda e: row.config(bg=C_CARD))

            def _click(_e=None):
                _close()
                if cmd:
                    cmd()

            row.bind("<Button-1>", _click)

        for label, cmd in items:
            if label is None:
                tk.Frame(inner, bg=C_SEPAR, height=1).pack(fill="x", padx=8, pady=4)
                continue
            _make_row(label, cmd)

        container.update_idletasks()
        w, h = container.winfo_reqwidth(), container.winfo_reqheight()
        # 화면 좌표(x, y)를 self.root 기준 상대 좌표로 바꾸고, 본창 안쪽에만
        # 머물도록 clamp — 이렇게 하면 구조적으로 창 밖으로 나갈 수 없다.
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        rx = x - self.root.winfo_rootx()
        ry = y - self.root.winfo_rooty()
        rx = min(max(0, rx), max(0, rw - w))
        ry = min(max(0, ry), max(0, rh - h))
        container.place(x=rx, y=ry)
        container.lift()

        def _on_outside_click(e):
            try:
                wx1, wy1 = container.winfo_rootx(), container.winfo_rooty()
                wx2, wy2 = wx1 + container.winfo_width(), wy1 + container.winfo_height()
            except tk.TclError:
                return
            if not (wx1 <= e.x_root <= wx2 and wy1 <= e.y_root <= wy2):
                _close()

        def _arm():
            if closed["done"]:
                return
            _unregister_outside[0] = register_outside_click(self.root, _on_outside_click)

        container.after(1, _arm)
        # Frame도 focus_set()하면 키 입력을 받을 수 있다 — Esc로 닫기.
        container.bind("<Escape>", lambda e: _close())
        container.focus_set()

    # ---------- 임베드 모달(별도 OS 창 대신 본창 안에 배경 차단막 + 패널) ----------
    def _make_modal_overlay(self):
        """본창 전체를 덮는 배경 오버레이. 임베드 모달이 떠 있는 동안 뒤쪽 대화
        목록·채팅창 클릭을 막는다.

        원래 Canvas에 stipple(격자 패턴)로 반투명처럼 보이게 그렸었는데, 이
        Windows Tk 빌드에서 stipple 채우기가 격자 대신 완전한 단색 검은 사각형으로
        렌더링돼(진짜 알파 투명도는 애초에 Toplevel의 -alpha 속성에만 있고 일반
        위젯엔 없다), 패널이 그 밑에 가려 화면이 통째로 새까맣게 보이는 버그가
        있었다. 대신 앱 배경색(C_MAIN)의 단순 단색 Frame으로 막는다 — 완전한
        반투명은 아니지만 어떤 Tk 빌드에서도 확실하게 렌더링된다."""
        overlay = tk.Frame(self.root, bg=C_MAIN, cursor="arrow")
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.lift()
        # 뒤쪽 위젯으로 클릭이 전달되지 않도록 오버레이 스스로 이벤트를 소비한다.
        overlay.bind("<Button-1>", lambda e: "break")
        return overlay

    def _make_embed_dialog(self, title, width, height, rely=0.46):
        """Toplevel 대신 오버레이 + 패널로 뜨는 "큰" 임베드 다이얼로그의 뼈대.
        이름 변경/내 IP 같은 단순 모달(_embed_prompt_text/_embed_alert)과
        달리, 그룹 만들기·브로드캐스트처럼 내용이 많은 다이얼로그도 같은
        틀에서 만들 수 있게 범용화했다. 제목 표시줄(제목 + ✕ 닫기)까지
        만들어 반환하므로, 호출자는 반환된 body 프레임 안에 내용만 채우면
        된다.
        반환값: (panel, body, close) — body는 실제 내용을 넣을 프레임,
        close()는 패널+오버레이를 함께 정리하는 함수."""
        overlay = self._make_modal_overlay()
        panel = tk.Frame(self.root, bg=C_CARD, highlightthickness=1, highlightbackground=C_BORDER)
        panel.place(relx=0.5, rely=rely, anchor="center", width=width, height=height)
        panel.lift()

        closed = {"done": False}

        def _close():
            if closed["done"]:
                return
            closed["done"] = True
            if panel.winfo_exists():
                panel.destroy()
            if overlay.winfo_exists():
                overlay.destroy()

        head = tk.Frame(panel, bg=C_CARD)
        head.pack(fill="x", padx=16, pady=(14, 0))
        tk.Label(head, text=title, bg=C_CARD, fg=C_TEXT, font=FONT_HEAD, anchor="w").pack(
            side="left")
        self._btn(head, "✕", _close, C_CARD, C_MUTE, C_HOVER, font=FONT_SM, padx=8, pady=2).pack(
            side="right")

        body = tk.Frame(panel, bg=C_CARD)
        body.pack(fill="both", expand=True)

        panel.bind("<Escape>", lambda e: _close())
        return panel, body, _close

    def _embed_prompt_text(self, title, message, initial="", ok_label="확인", cancel_label="취소"):
        """한 줄 텍스트 입력을 받는 임베드 모달 — 별도 OS 창 대신 본창 안에
        반투명 배경 + 패널로 뜬다. 확인 시 입력값을, 취소 시
        None을 반환한다(패널이 닫힐 때까지 wait_window로 대기)."""
        overlay = self._make_modal_overlay()
        panel = tk.Frame(self.root, bg=C_CARD, highlightthickness=1, highlightbackground=C_BORDER)
        panel.place(relx=0.5, rely=0.4, anchor="center")
        panel.lift()
        tk.Label(panel, text=title, bg=C_CARD, fg=C_TEXT, font=FONT_HEAD, anchor="w").pack(
            fill="x", padx=20, pady=(18, 2))
        tk.Label(panel, text=message, bg=C_CARD, fg=C_MUTE, font=FONT_SM, anchor="w",
                justify="left", wraplength=280).pack(fill="x", padx=20, pady=(0, 10))
        entry_var = tk.StringVar(value=initial or "")
        entry = tk.Entry(panel, textvariable=entry_var, font=FONT_MSG, relief="flat", bg=C_SEARCHBG,
                         fg=C_TEXT, highlightthickness=1, highlightbackground=C_BORDER,
                         highlightcolor=C_ME, insertbackground=C_TEXT, width=28)
        self._sanitize_var_trace(entry, entry_var)
        apply_ime_font(entry, FONT_FAM, FONT_MSG[1])
        entry.pack(fill="x", padx=20, ipady=7)
        result = {"value": None}

        def _close():
            if panel.winfo_exists():
                panel.destroy()
            if overlay.winfo_exists():
                overlay.destroy()

        def _ok(_e=None):
            result["value"] = entry_var.get()
            _close()

        def _cancel(_e=None):
            _close()

        entry.bind("<Return>", _ok)
        entry.bind("<Escape>", _cancel)
        btnrow = tk.Frame(panel, bg=C_CARD)
        btnrow.pack(fill="x", padx=20, pady=(8, 18))
        self._btn(btnrow, ok_label, _ok, C_ME, "white", C_ME_D,
                 font=FONT_BTN, padx=18, pady=7).pack(side="right")
        self._btn(btnrow, cancel_label, _cancel, C_CARD, C_MUTE, C_HOVER,
                 font=FONT_BTN, padx=14, pady=7).pack(side="right", padx=(0, 8))
        entry.focus_set()
        entry.select_range(0, "end")
        panel.wait_window(panel)
        return result["value"]

    def _embed_alert(self, title, message, kind="info"):
        """_alert와 같은 용도(확인 안내창)의 임베드 버전. [확인] 클릭 또는
        Escape/Return으로 닫는다(반환값 없음)."""
        accent = {"info": C_ME, "warning": "#d98c19", "error": C_FAIL}.get(kind, C_ME)
        glyph = {"info": "i", "warning": "!", "error": "×"}.get(kind, "i")
        overlay = self._make_modal_overlay()
        panel = tk.Frame(self.root, bg=C_CARD, highlightthickness=1, highlightbackground=C_BORDER)
        panel.place(relx=0.5, rely=0.4, anchor="center")
        panel.lift()
        head = tk.Frame(panel, bg=C_CARD)
        head.pack(fill="x", padx=20, pady=(20, 8))
        dot = tk.Canvas(head, width=28, height=28, bg=C_CARD, highlightthickness=0)
        dot.create_oval(1, 1, 27, 27, fill=accent, outline="")
        dot.create_text(14, 14, text=glyph, fill="white", font=(FONT_FAM, 11, "bold"))
        dot.pack(side="left")
        tk.Label(head, text=title, bg=C_CARD, fg=C_TEXT, font=FONT_HEAD, anchor="w").pack(
            side="left", padx=(10, 0))
        tk.Label(panel, text=message, bg=C_CARD, fg=C_TEXT, font=FONT_SM, anchor="w",
                justify="left", wraplength=320).pack(fill="x", padx=20, pady=(0, 18))
        btnrow = tk.Frame(panel, bg=C_CARD)
        btnrow.pack(fill="x", padx=20, pady=(0, 18))

        def _close(_e=None):
            if panel.winfo_exists():
                panel.destroy()
            if overlay.winfo_exists():
                overlay.destroy()

        self._btn(btnrow, "확인", _close, C_ME, "white", C_ME_D,
                 font=FONT_BTN, padx=18, pady=7).pack(side="right")
        panel.bind("<Return>", _close)
        panel.bind("<Escape>", _close)
        panel.focus_set()
        panel.wait_window(panel)

    def _embed_confirm(self, title, message, kind="warning", ok_label="확인", cancel_label="취소"):
        """messagebox.askyesno의 임베드 버전 — 별도 OS 창 대신 본창 안에
        반투명 배경 + 패널로 뜬다. [확인] 클릭 시 True, [취소]/Escape 시
        False를 반환한다(패널이 닫힐 때까지 wait_window로 대기)."""
        accent = {"info": C_ME, "warning": "#d98c19", "error": C_FAIL}.get(kind, C_ME)
        glyph = {"info": "i", "warning": "!", "error": "×"}.get(kind, "i")
        overlay = self._make_modal_overlay()
        panel = tk.Frame(self.root, bg=C_CARD, highlightthickness=1, highlightbackground=C_BORDER)
        panel.place(relx=0.5, rely=0.4, anchor="center")
        panel.lift()
        head = tk.Frame(panel, bg=C_CARD)
        head.pack(fill="x", padx=20, pady=(20, 8))
        dot = tk.Canvas(head, width=28, height=28, bg=C_CARD, highlightthickness=0)
        dot.create_oval(1, 1, 27, 27, fill=accent, outline="")
        dot.create_text(14, 14, text=glyph, fill="white", font=(FONT_FAM, 11, "bold"))
        dot.pack(side="left")
        tk.Label(head, text=title, bg=C_CARD, fg=C_TEXT, font=FONT_HEAD, anchor="w").pack(
            side="left", padx=(10, 0))
        tk.Label(panel, text=message, bg=C_CARD, fg=C_TEXT, font=FONT_SM, anchor="w",
                justify="left", wraplength=320).pack(fill="x", padx=20, pady=(0, 18))
        btnrow = tk.Frame(panel, bg=C_CARD)
        btnrow.pack(fill="x", padx=20, pady=(0, 18))
        result = {"value": False}

        def _close(value, _e=None):
            result["value"] = value
            if panel.winfo_exists():
                panel.destroy()
            if overlay.winfo_exists():
                overlay.destroy()

        self._btn(btnrow, ok_label, lambda: _close(True), C_ME, "white", C_ME_D,
                 font=FONT_BTN, padx=18, pady=7).pack(side="right")
        self._btn(btnrow, cancel_label, lambda: _close(False), C_CARD, C_MUTE, C_HOVER,
                 font=FONT_BTN, padx=14, pady=7).pack(side="right", padx=(0, 8))
        panel.bind("<Return>", lambda e: _close(True))
        panel.bind("<Escape>", lambda e: _close(False))
        panel.focus_set()
        panel.wait_window(panel)
        return result["value"]

    def _sanitize_var_trace(self, entry, var):
        def _on_change(*_a):
            raw = var.get()
            cleaned = sanitize_chat_text(raw)
            if raw != cleaned:
                try:
                    cursor = entry.index("insert")
                except tk.TclError:
                    cursor = len(cleaned)
                var.set(cleaned)
                try:
                    entry.icursor(min(cursor, len(cleaned)))
                except Exception:
                    pass
        var.trace_add("write", _on_change)

    # ---------- 프로필 사진 메뉴 / 다이얼로그 ----------
    def _open_my_avatar_menu(self):
        x = self.me_av.winfo_rootx()
        y = self.me_av.winfo_rooty() + self.me_av.winfo_height() + 4
        items = [("사진으로 프로필 등록/변경", self._set_my_avatar_dialog)]
        if self.engine and self.engine.my_avatar_hash:
            items.append(("기본 이미지로 복원", self._remove_my_avatar))
        self._popup_menu(x, y, items)

    def _set_my_avatar_dialog(self):
        if self.engine is None:
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="프로필 사진 선택",
            filetypes=[("이미지(PNG·GIF·BMP)", "*.png *.gif *.bmp")])
        if not path:
            return
        try:
            ok = self.engine.set_my_avatar(path)
        except Exception:
            ok = False
        if not ok:
            self._embed_alert("프로필 사진 등록 실패",
                        "이미지를 읽을 수 없거나 너무 큽니다.\n"
                        "PNG·GIF·BMP 형식만 지원합니다(JPG는 tkinter가 직접 열 수 없어 미지원).",
                        kind="warning")
            return
        self._refresh_me_avatar()
        self.status.set("프로필 사진을 등록했습니다")

    def _remove_my_avatar(self):
        if self.engine is None:
            return
        self.engine.set_my_avatar(None)
        self._refresh_me_avatar()
        self.status.set("프로필 사진을 기본 이미지로 되돌렸습니다")

    # ---------- 숨긴 대화 다이얼로그 ----------
    def _open_hidden_dialog(self):
        if self.engine is None:
            return

        if getattr(self, "_hidden_panel", None) and self._hidden_panel.winfo_exists():
            self._hidden_panel.lift()
            return

        overlay = self._make_modal_overlay()
        win = tk.Frame(self.root, bg=C_CARD, highlightthickness=1, highlightbackground=C_BORDER)
        win.place(relx=0.5, rely=0.46, anchor="center", width=360, height=460)
        win.lift()
        self._hidden_panel = win

        head = tk.Frame(win, bg=C_CARD)
        head.pack(fill="x", padx=16, pady=(16, 10))
        head_info = tk.Frame(head, bg=C_CARD)
        head_info.pack(side="left", fill="x", expand=True)
        count_lbl = tk.Label(head_info, text="숨긴 대화", bg=C_CARD, fg=C_TEXT, font=FONT_HEAD, anchor="w")
        count_lbl.pack(anchor="w")
        sub_lbl = tk.Label(head_info, text="복구할 대화를 골라 [복구]를 누르세요", bg=C_CARD, fg=C_MUTE,
                           font=FONT_XS, anchor="w")
        sub_lbl.pack(anchor="w")
        restore_all_btn = self._btn(head, "전체 복구", lambda: (self._unhide_all(), reload_hidden()),
                                    C_ME, "white", C_ME_D, font=FONT_BTN, padx=10, pady=6)
        restore_all_btn.pack(side="right")

        tk.Frame(win, bg=C_BORDER, height=1).pack(fill="x", padx=16, pady=(0, 6))

        list_wrap = tk.Frame(win, bg=C_CARD)
        list_wrap.pack(fill="both", expand=True, padx=16)

        canvas = tk.Canvas(list_wrap, bg=C_CARD, highlightthickness=0)
        sb = MinimalScrollbar(list_wrap, target=canvas, bg=C_CARD)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=C_CARD)
        canvas_win = canvas.create_window((0, 0), anchor="nw", window=inner)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(canvas_win, width=e.width))
        canvas.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(canvas, e))

        _disarm_hidden_wheel = bind_scoped_mousewheel(list_wrap, canvas)
        win.bind("<Destroy>", lambda e: _disarm_hidden_wheel() if e.widget == win else None, add="+")

        def reload_hidden():
            for w in inner.winfo_children():
                w.destroy()
            keys = self._hidden_conv_keys()
            count_lbl.config(text=f"숨긴 대화 ({len(keys)}개)" if keys else "숨긴 대화")
            restore_all_btn.configure(state=("normal" if keys else "disabled"))

            if not keys:
                tk.Label(inner, text="숨긴 대화가 없습니다", fg=C_MUTE, bg=C_CARD,
                         font=FONT_XS, pady=24).pack(fill="x")
                return

            for key in keys:
                row = tk.Frame(inner, bg=C_CARD)
                row.pack(fill="x", pady=3)

                av = tk.Canvas(row, width=38, height=40, bg=C_CARD, highlightthickness=0)
                av.pack(side="left", padx=(4, 8), pady=2)

                name = self._get_conversation_name(key)
                if key[0] == "dm":
                    with self.engine.plock:
                        p = dict(self.engine.peers.get((key[1], key[2]), {}))
                    c = self._avacolor(name)
                    self._avatar(av, self._initial(name), c, av_photo=self._get_avatar_photo(p.get("av")))
                    preview = self._preview((key[1], key[2]), p)
                else:
                    with self.engine.glock:
                        g = self.engine.groups.get(key[1])
                        members = set(g["members"]) if g else set()
                    self._avatar_group(av, members)
                    preview = self._preview_group(key[1])

                mid = tk.Frame(row, bg=C_CARD)
                mid.pack(side="left", fill="both", expand=True, pady=2)
                tk.Label(mid, text=name[:20], fg=C_TEXT, bg=C_CARD, font=FONT_NAME,
                        anchor="w").pack(anchor="w")
                tk.Label(mid, text=preview, fg=C_MUTE, bg=C_CARD, font=FONT_XS,
                        anchor="w").pack(anchor="w")

                def make_restore_cmd(target_key=key, target_name=name):
                    def _restore():
                        self.engine.unhide_key(target_key)
                        self._refresh_list()
                        self.status.set(f"'{target_name}' 대화를 다시 표시했습니다")
                        reload_hidden()
                    return _restore

                self._btn(row, "복구", make_restore_cmd(), C_CARD, C_ME, C_HOVER,
                         font=FONT_XS, padx=8, pady=4).pack(side="right", padx=(4, 0))

        reload_hidden()

        def _close(_e=None):
            self._hidden_panel = None
            if win.winfo_exists():
                win.destroy()
            if overlay.winfo_exists():
                overlay.destroy()

        win.bind("<Escape>", _close)
        bot = tk.Frame(win, bg=C_CARD)
        bot.pack(fill="x", padx=16, pady=(8, 14))
        self._btn(bot, "닫기", _close, C_CARD, C_MUTE, C_HOVER,
                  font=FONT_BTN, padx=16, pady=6).pack(side="right")
        win.focus_set()

    # ---------- 다운로드 폴더 ----------
    def _download_dir_dialog(self):
        if self.engine is None:
            return
        d = filedialog.askdirectory(parent=self.root, title="다운로드 폴더 선택",
                                    initialdir=self.engine.download_dir)
        if d:
            self.engine.set_download_dir(d)
            self.status.set(f"다운로드 폴더: {d}")

    def _open_download_dir(self):
        if not self.engine:
            return
        d = self.engine.download_dir
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)
        except OSError:
            self._embed_alert("폴더 열기 실패", f"폴더를 열 수 없습니다:\n{d}", kind="warning")

    # ---------- 그룹 나가기 다이얼로그 ----------
    def _leave_group_dialog(self, gid):
        if not self.engine:
            return
        with self.engine.glock:
            g = self.engine.groups.get(gid) or {}
            gname = g.get("name") or "그룹"
        ok = self._embed_confirm(
            "그룹 대화방 나가기",
            f"'{gname}' 대화방을 나가시겠습니까?\n\n대화 목록에서 삭제되고 그룹원들에게 퇴장 알림이 전송됩니다."
        )
        if ok:
            self.engine.leave_group(gid)
            if self.current == ("grp", gid):
                self._clear_active_chat_view()
            self._refresh_list()
            self.status.set(f"'{gname}' 대화방을 나갔습니다")

    # ---------- 카테고리 / 대화방 관리 다이얼로그 ----------
    def _new_category_dialog(self, key):
        name = self._embed_prompt_text("새 카테고리", "카테고리 이름을 입력하세요 (예: 개발팀)")
        if name and name.strip():
            self._set_contact_category(key, name.strip())

    def _rename_conversation_dialog(self, key=None):
        if key is None:
            key = self.current
        if not key or not self.engine:
            return
        if key[0] == "grp":
            gid = key[1]
            with self.engine.glock:
                g = self.engine.groups.get(gid) or {}
                cur_name = g.get("name", "그룹")
            new_name = self._embed_prompt_text(
                "대화방 이름 변경",
                "새로운 그룹 대화방 이름을 입력하세요:\n(모든 그룹원에게 변경된 이름이 반영됩니다)",
                initial=cur_name
            )
            if new_name is not None and new_name.strip():
                new_name = new_name.strip()[:60]
                if self.engine.rename_group(gid, new_name):
                    if self.current == key:
                        self.ch_title.config(text=new_name)
                    self._refresh_list()
                    self.status.set(f"대화방 이름을 '{new_name}'(으)로 변경했습니다")
        elif key[0] == "dm":
            _, ip, port = key
            with self.engine.plock:
                p = self.engine.peers.get((ip, port)) or {}
                orig_name = p.get("name") or ip
            cur_alias = self.engine.get_alias(key) or ""
            initial_val = cur_alias if cur_alias else orig_name
            new_alias = self._embed_prompt_text(
                "대화 상대 이름 변경",
                f"표시할 별칭(이름)을 입력하세요:\n(기본 이름: {orig_name} / 빈칸 입력 시 기본 이름 복원)",
                initial=initial_val
            )
            if new_alias is not None:
                new_alias = new_alias.strip()[:60]
                self.engine.set_alias(key, new_alias)
                display_name = new_alias if new_alias else orig_name
                if self.current == key:
                    self.ch_title.config(text=display_name)
                self._refresh_list()
                if new_alias:
                    self.status.set(f"대화 상대 이름을 '{new_alias}'(으)로 변경했습니다")
                else:
                    self.status.set(f"대화 상대 이름을 기본값('{orig_name}')으로 복원했습니다")

    def _delete_conversation_history_dialog(self, key=None):
        if key is None:
            key = self.current
        if not key or not self.engine:
            return
        name = self._get_conversation_name(key)
        ok = self._embed_confirm(
            "대화 기록 삭제",
            f"'{name}' 대화방의 모든 대화 및 파일 전송 기록을 영구 삭제하시겠습니까?\n\n이 작업은 되돌릴 수 없습니다."
        )
        if ok:
            self.engine.delete_history(key)
            if self.current == key:
                self._clear_active_chat_view()
            self._refresh_list()
            self.status.set(f"'{name}' 대화 기록을 삭제했습니다")

    # ---------- 시작/안내/설정 다이얼로그 ----------
    def _startup_fail(self):
        self._embed_alert("시작 실패",
                   f"포트 {self.args.port}를 사용할 수 없습니다.\n"
                   "이미 LAN Talk이 실행 중이거나 다른 프로그램이 포트를 사용하고 있을 수 있습니다.",
                   kind="error")
        self.root.destroy()

    def _maybe_first_run(self):
        flag = os.path.join(self.datadir, "firewall_notice_done")
        if self.engine is None or os.path.exists(flag):
            return
        try:
            os.makedirs(self.datadir, exist_ok=True)
            with open(flag, "w", encoding="utf-8"):
                pass
        except OSError:
            pass
        port = self.engine.port
        want = self._embed_confirm(
            "방화벽 안내",
            f"다른 PC와 통신하려면 Windows 방화벽에서 UDP {port}번(수신)을 허용해야 합니다.\n\n"
            "지금 자동으로 예외 규칙을 등록할까요? (관리자 권한 승인 창이 뜰 수 있습니다)\n"
            "나중에 [취소]해도 방화벽이 직접 [액세스 허용] 창을 띄워주면 그때 눌러주시면 됩니다.",
            kind="info", ok_label="자동 등록", cancel_label="나중에")
        if not want:
            return
        # _register_firewall_rule은 UAC 승인 대기 + 등록 확인을 위해 최대 몇 초간
        # time.sleep()으로 블로킹된다. 메인 스레드(Tk 이벤트 루프)에서 그대로
        # 부르면 그 몇 초 동안 화면이 전혀 다시 그려지지 않아, 방금 닫힌 확인창
        # 자리에 있던 빈 대화 안내 문구가 그 시간 뒤에야 한꺼번에 "튀어나오듯"
        # 다시 나타나는 것처럼 보이는 버그가 있었다(v6.48). 백그라운드 스레드로
        # 빼서 Tk 이벤트 루프가 계속 정상적으로 화면을 그리게 한다.
        self.status.set("방화벽 예외 규칙 등록 중... (관리자 권한 승인 창을 확인해주세요)")

        def worker():
            ok = self._register_firewall_rule(port)
            self.root.after(0, lambda: self._on_firewall_register_done(ok, port))

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _on_firewall_register_done(self, ok, port):
        if not self.root.winfo_exists():
            return
        try:
            self.status.set("왼쪽 목록에서 대화 상대를 선택하세요")
        except Exception:
            pass
        if ok:
            self._embed_alert("방화벽 등록 완료",
                              f"UDP {port}번(수신) 예외 규칙을 등록했습니다.", kind="info")
        else:
            self._embed_alert("방화벽 자동 등록 실패",
                              "관리자 권한 승인이 취소됐거나 등록에 실패했습니다.\n\n"
                              "최초 실행 시 뜨는 Windows 방화벽 창에서 [액세스 허용]을 눌러주시거나,\n"
                              f"관리팀에 UDP {port}번(수신) 허용을 요청하세요.",
                              kind="warning")

    def _register_firewall_rule(self, port):
        """netsh로 인바운드 UDP 예외 규칙을 등록한다. 관리자 권한이 없으면 UAC
        상승 창을 한 번 더 띄워 재시도한다(사용자가 이미 위 확인창에서 등록에
        동의한 뒤라 이중 승인이지만, Windows 정책상 UAC 자체는 생략할 수 없다)."""
        import subprocess
        rule_name = "LAN Talk"
        args = ["advfirewall", "firewall", "add", "rule",
               f"name={rule_name}", "dir=in", "action=allow", "protocol=UDP",
               f"localport={port}"]
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            r = subprocess.run(["netsh"] + args, capture_output=True, timeout=10,
                               creationflags=no_window)
            if r.returncode == 0:
                return True
        except Exception:
            pass
        # 관리자 권한 없이 실패했을 가능성 — UAC 상승으로 재시도.
        try:
            import ctypes
            # ShellExecuteW의 lpParameters는 subprocess.run(list)와 달리 하나의
            # 문자열을 Windows가 공백 기준으로 다시 토큰화한다 — "name=LAN Talk"처럼
            # 값에 공백이 있는 인자를 그냥 join만 하면 "name=LAN"과 "Talk"로 쪼개져
            # netsh가 규칙을 못 만들고 조용히 실패했다(v6.48 첫 구현 버그, 실제
            # 사용자 리포트로 발견). list2cmdline으로 Windows 커맨드라인 규칙에 맞게
            # 제대로 따옴표를 씌워야 한다.
            params = subprocess.list2cmdline(args)
            ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", params, None, 0)
            if ret <= 32:  # 32 이하는 실행 자체가 실패(예: 사용자가 UAC 취소)했다는 뜻
                return False
        except Exception:
            return False
        # 상승된 프로세스는 비동기로 실행되므로, 등록이 실제로 반영될 때까지
        # 잠깐 기다렸다가 규칙이 실제로 생겼는지(관리자 권한 없이도 조회는 가능)
        # 확인해서 정확한 성공 여부를 돌려준다.
        import time as _time
        for _ in range(10):
            _time.sleep(0.3)
            try:
                r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                                    f"name={rule_name}"], capture_output=True, timeout=5,
                                   creationflags=no_window)
                if r.returncode == 0 and rule_name.encode() in r.stdout:
                    return True
            except Exception:
                pass
        return False

    def _apply_name_dialog(self):
        nm = self._embed_prompt_text("이름 변경", "표시 이름을 입력하세요",
                                     initial=(self.engine.name if self.engine else self.me_lbl["text"]))
        if nm and nm.strip() and self.engine:
            self.engine.set_name(nm.strip()[:60])
            self.me_lbl.config(text=self.engine.name)
            self._me_avatar_color = self._avacolor(self.engine.name)
            self._refresh_me_avatar()
            self.me_sub.config(text="이름이 변경되었습니다", fg=C_MUTE)
            self.root.after(2000, lambda: self.me_sub.config(text="온라인", fg=C_ONLINE) if self.me_sub.winfo_exists() else None)
            self._refresh_list()

    def _dialog_info(self):
        if self.engine is None:
            return
        self._embed_alert(
            "내 PC 정보",
            f"이름: {self.engine.name}\n"
            f"내 IP: {', '.join(local_ips())}\n"
            f"포트: {self.engine.port}\n\n"
            "다른 네트워크 구역의 상대가 [＋ 상대 연결]을 할 때 위 IP를 입력하세요.",
            kind="info")

    def _settings_dialog(self):
        if self.engine is None:
            return
        if getattr(self, "_settings_win", None) and self._settings_win.winfo_exists():
            self._settings_win.lift()
            self._settings_win.focus_force()
            return
        win, body, _close = self._make_embed_dialog("상대 연결", 430, 460)
        self._settings_win = win

        tk.Label(body, text="같은 네트워크 구역 상대는 자동으로 연결됩니다.\n"
                           "다른 구역 상대만 IP를 등록하세요.\n형식: IP  또는  IP:포트",
                 bg=C_CARD, fg=C_MUTE, font=FONT_SM,
                 justify="left").pack(fill="x", padx=14, pady=(4, 6))
        row = tk.Frame(body, bg=C_CARD)
        row.pack(fill="x", padx=14, pady=8)
        self._ip_entry_var = tk.StringVar()
        self.ip_entry = tk.Entry(row, textvariable=self._ip_entry_var, font=FONT_MSG, relief="flat", bg=C_SEARCHBG, fg=C_TEXT,
                                 highlightthickness=1, highlightbackground=C_BORDER,
                                 highlightcolor=C_BORDER, insertbackground=C_TEXT)
        self._sanitize_var_trace(self.ip_entry, self._ip_entry_var)
        apply_ime_font(self.ip_entry, FONT_FAM, FONT_MSG[1])
        self.ip_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.ip_entry.bind("<Return>", lambda e: self._settings_add(win))
        self.ip_entry.bind("<Escape>", lambda e: _close())
        b1 = self._btn(row, "추가", lambda: self._settings_add(win), C_ME, "white", C_ME_D,
                       font=FONT_BTN, padx=14, pady=5)
        b1.pack(side="right")
        ip_listwrap = self._build_static_ip_list(body)
        ip_listwrap.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self._reload_static_ip_rows()
        self.ip_entry.focus_set()

    def _max_file_size_dialog(self):
        """⚙ 설정(대화방 더보기) 메뉴의 다운로드 관련 항목들 옆에 있는, 파일
        전송·다운로드 최대 크기 설정. 전에는 [상대 연결] 창에 섞여 있었는데,
        다운로드 폴더 설정과 성격이 같은 항목이라 이쪽으로 옮겼다."""
        if self.engine is None:
            return
        cur_mb = self.engine.max_file_size // (1024 * 1024)
        raw = self._embed_prompt_text(
            "다운로드 크기 조절",
            f"1~{MAX_FILE_SIZE_LIMIT_MB}MB(1GB) 사이로 입력하세요.\n"
            "받는 파일도 이 크기를 넘으면 거부됩니다.",
            initial=str(cur_mb))
        if raw is None:
            return
        raw = raw.strip()
        try:
            mb = int(raw)
        except ValueError:
            self._embed_alert("입력 확인", "숫자만 입력하세요.", kind="warning")
            return
        applied = self.engine.set_max_file_size_mb(mb)
        self.status.set(f"다운로드 크기를 {applied}MB로 설정했습니다")

    def _change_port_dialog(self):
        """사용 포트 번호 변경. 이미 열려서 수신 스레드가 쓰고 있는 소켓은 이 자리에서
        다시 묶을 수 없으므로, 값만 저장해두고 재시작 후에 실제로 적용된다 — 그래서
        입력창에도, 저장 뒤 안내창에도 재시작이 필요하다는 점을 분명히 알린다."""
        if self.engine is None:
            return
        raw = self._embed_prompt_text(
            "포트 번호 변경",
            "1~65535 사이로 입력하세요. 변경 사항은 앱을 재시작해야 적용됩니다.\n\n"
            "⚠ 포트번호를 무단으로 바꿀 경우 통신에 장애가 있을 수 있습니다.\n"
            "같은 네트워크의 동료들과 포트를 맞춰야 서로를 계속 찾을 수 있습니다.",
            initial=str(self.engine.port))
        if raw is None:
            return
        raw = raw.strip()
        try:
            port = int(raw)
        except ValueError:
            self._embed_alert("입력 확인", "숫자만 입력하세요.", kind="warning")
            return
        if not (1 <= port <= 65535):
            self._embed_alert("입력 확인", "1~65535 사이의 숫자를 입력하세요.", kind="warning")
            return
        applied = self.engine.set_port(port)
        self._embed_alert(
            "포트 번호 저장됨",
            f"다음 실행부터 {applied}번 포트를 사용합니다.\n"
            "지금 바로 적용하려면 LAN Talk을 재시작하세요.",
            kind="info")
        self.status.set(f"포트를 {applied}번으로 저장했습니다 (재시작 후 적용)")

    def _build_static_ip_list(self, parent):
        """직접 등록한 상대 IP 목록을, "새 그룹 만들기"/"여러 명에게 한 번에
        보내기"의 상대 선택 목록(_build_picker_list)과 같은 아바타·이름/IP
        2단 표시 행 스타일로 그린다. 체크박스 대신 각 행 오른쪽에 [삭제]
        버튼이 있어 바로 그 항목만 지울 수 있다."""
        wrap = tk.Frame(parent, bg=C_CARD)
        canvas = tk.Canvas(wrap, bg=C_CARD, highlightthickness=0)
        sb = MinimalScrollbar(wrap, target=canvas, bg=C_CARD)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=C_CARD)
        canvas.create_window((0, 0), anchor="nw", window=inner, tags="inner")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("inner", width=e.width))
        canvas.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(canvas, e))

        _disarm_wheel = bind_scoped_mousewheel(wrap, canvas)
        parent.bind("<Destroy>", lambda e: _disarm_wheel(), add="+")

        self._static_ip_inner = inner
        return wrap

    def _reload_static_ip_rows(self):
        inner = self._static_ip_inner
        for w in inner.winfo_children():
            w.destroy()
        ordered = sorted(self.engine.static)
        if not ordered:
            tk.Label(inner, text="등록된 상대가 없습니다.", bg=C_CARD, fg=C_MUTE,
                     font=FONT_SM).pack(pady=20)
            return
        now = time.time()
        with self.engine.plock:
            peers_snap = dict(self.engine.peers)
        for ip, port in ordered:
            p = peers_snap.get((ip, port))
            alias = self.engine.get_alias(("dm", ip, port))
            name = alias or (p.get("name") if p else None) or ip
            online = bool(p and p.get("last")) and now - p.get("last", 0) < PEER_TIMEOUT

            row = tk.Frame(inner, bg=C_CARD)
            row.pack(fill="x")
            av = tk.Canvas(row, width=38, height=40, bg=C_CARD, highlightthickness=0)
            av.pack(side="left", padx=(6, 8), pady=2)
            c = self._avacolor(name) if online else C_OFFLINE
            self._avatar(av, self._initial(name), c,
                        av_photo=self._get_avatar_photo(p.get("av")) if p else None)
            if online:
                av.create_oval(26, 27, 34, 35, fill=C_ONLINE, outline=C_CARD, width=1)
            mid = tk.Frame(row, bg=C_CARD)
            mid.pack(side="left", fill="both", expand=True, pady=3)
            tk.Label(mid, text=name[:22], fg=C_TEXT if online else C_MUTE, bg=C_CARD,
                    font=FONT_NAME if online else FONT_NAME_M, anchor="w").pack(anchor="w")
            tk.Label(mid, text=f"{ip}:{port}", fg=C_MUTE, bg=C_CARD, font=FONT_XS,
                    anchor="w").pack(anchor="w")
            self._btn(row, "삭제", lambda ip=ip, port=port: self._settings_del_ip(ip, port),
                     C_CARD, C_FAIL, C_HOVER, font=FONT_XS_PAD, padx=10, pady=4).pack(
                     side="right", padx=10)

    def _settings_add(self, win):
        try:
            ip, port = parse_target(self.ip_entry.get())
        except ValueError as e:
            self._embed_alert("입력 확인", str(e), kind="warning")
            return
        self.engine.set_static_targets(self.engine.static | {(ip, port)})
        self.ip_entry.delete(0, "end")
        self._reload_static_ip_rows()

    def _settings_del_ip(self, ip, port):
        self.engine.set_static_targets(self.engine.static - {(ip, port)})
        self._reload_static_ip_rows()

    # ---------- 그룹/피커 다이얼로그 ----------
    def _known_peers_sorted(self):
        with self.engine.plock:
            peers = [dict(v) for v in self.engine.peers.values()]
        peers.sort(key=lambda p: (self.engine.get_alias(("dm", p["ip"], p["port"])) or p["name"] or p["ip"]).lower())
        return peers

    def _build_picker_list(self, parent, peers):
        wrap = tk.Frame(parent, bg=C_CARD)
        canvas = tk.Canvas(wrap, bg=C_CARD, highlightthickness=0)
        sb = MinimalScrollbar(wrap, target=canvas, bg=C_CARD)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=C_CARD)
        canvas.create_window((0, 0), anchor="nw", window=inner, tags="inner")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("inner", width=e.width))
        canvas.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(canvas, e))

        _disarm_picker_wheel = bind_scoped_mousewheel(wrap, canvas)
        parent.bind("<Destroy>", lambda e: _disarm_picker_wheel(), add="+")

        vars_ = []
        now = time.time()
        for p in peers:
            v = tk.BooleanVar(value=False)
            key = (p["ip"], p["port"])
            alias = self.engine.get_alias(("dm", p["ip"], p["port"]))
            name = alias or p["name"] or p["ip"]
            online = bool(p.get("last")) and now - p.get("last", 0) < PEER_TIMEOUT

            row = tk.Frame(inner, bg=C_CARD)
            row.pack(fill="x")
            av = tk.Canvas(row, width=38, height=40, bg=C_CARD, highlightthickness=0)
            av.pack(side="left", padx=(6, 8), pady=2)
            c = self._avacolor(name) if online else C_OFFLINE
            self._avatar(av, self._initial(name), c, av_photo=self._get_avatar_photo(p.get("av")))
            if online:
                av.create_oval(26, 27, 34, 35, fill=C_ONLINE, outline=C_CARD, width=1)
            mid = tk.Frame(row, bg=C_CARD)
            mid.pack(side="left", fill="both", expand=True, pady=3)
            name_lbl = tk.Label(mid, text=name[:22], fg=C_TEXT if online else C_MUTE, bg=C_CARD,
                                font=FONT_NAME if online else FONT_NAME_M, anchor="w")
            name_lbl.pack(anchor="w")
            sub_lbl = tk.Label(mid, text=f"{p['ip']}:{p['port']}", fg=C_MUTE, bg=C_CARD,
                               font=FONT_XS, anchor="w")
            sub_lbl.pack(anchor="w")
            chk = tk.Canvas(row, width=22, height=22, bg=C_CARD, highlightthickness=0)
            chk.pack(side="right", padx=12)
            widgets = (row, av, mid, name_lbl, sub_lbl, chk)

            def _redraw_check(chk=chk, v=v):
                chk.delete("all")
                if v.get():
                    chk.create_oval(1, 1, 21, 21, fill=C_ME, outline="")
                    chk.create_text(11, 11, text="✓", fill="white", font=(FONT_FAM, 10, "bold"))
                else:
                    chk.create_oval(1, 1, 21, 21, fill="", outline=C_BORDER, width=2)

            def _toggle(_e=None, v=v, widgets=widgets, redraw=_redraw_check):
                v.set(not v.get())
                bg = C_ROWSEL if v.get() else C_CARD
                for w in widgets:
                    w.config(bg=bg)
                redraw()

            _redraw_check()
            for w in widgets:
                w.bind("<Button-1>", _toggle)
            vars_.append((key, v))
        return wrap, vars_

    def _group_create_dialog(self):
        if self.engine is None:
            return
        if getattr(self, "_group_create_win", None) and self._group_create_win.winfo_exists():
            self._group_create_win.lift()
            self._group_create_win.focus_force()
            return
        peers = self._known_peers_sorted()
        if not peers:
            self._embed_alert("새 그룹 만들기",
                       "먼저 대화 상대가 목록에 있어야 그룹을 만들 수 있습니다.", kind="info")
            return
        win, body, _close = self._make_embed_dialog("새 그룹 만들기", 380, 460)
        self._group_create_win = win
        tk.Label(body, text="그룹 이름", bg=C_CARD, fg=C_MUTE, font=FONT_SM,
                anchor="w").pack(fill="x", padx=16, pady=(10, 4))
        name_var = tk.StringVar()
        name_entry = tk.Entry(body, textvariable=name_var, font=FONT_MSG, relief="flat", bg=C_SEARCHBG, fg=C_TEXT,
                              highlightthickness=1, highlightbackground=C_BORDER,
                              highlightcolor=C_BORDER, insertbackground=C_TEXT)
        self._sanitize_var_trace(name_entry, name_var)
        apply_ime_font(name_entry, FONT_FAM, FONT_MSG[1])
        name_entry.pack(fill="x", padx=16, ipady=6)
        tk.Label(body, text="참여시킬 상대 선택", bg=C_CARD, fg=C_MUTE, font=FONT_SM,
                anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        listwrap, vars_ = self._build_picker_list(body, peers)
        listwrap.pack(fill="both", expand=True, padx=16)

        def do_create():
            name = name_entry.get().strip() or "새 그룹"
            members = {k for k, v in vars_ if v.get()}
            if not members:
                self._embed_alert("선택 필요", "한 명 이상 선택하세요.", kind="warning")
                return
            gid = self.engine.create_group(name, members)
            _close()
            self._refresh_list()
            self._select(("grp", gid))

        btnrow = tk.Frame(body, bg=C_CARD)
        btnrow.pack(fill="x", padx=16, pady=12)
        self._btn(btnrow, "만들기", do_create, C_ME, "white", C_ME_D,
                 font=FONT_BTN, padx=16, pady=6).pack(side="right")
        self._btn(btnrow, "취소", _close, C_CARD, C_MUTE, C_HOVER,
                 font=FONT_BTN, padx=12, pady=6).pack(side="right", padx=(0, 8))
        name_entry.bind("<Return>", lambda e: do_create())
        name_entry.bind("<Escape>", lambda e: _close())
        name_entry.focus_set()

    def _broadcast_dialog(self):
        if self.engine is None:
            return
        if getattr(self, "_broadcast_win", None) and self._broadcast_win.winfo_exists():
            self._broadcast_win.lift()
            self._broadcast_win.focus_force()
            return
        peers = self._known_peers_sorted()
        if not peers:
            self._embed_alert("여러 명에게 한 번에 보내기",
                       "먼저 대화 상대가 목록에 있어야 보낼 수 있습니다.", kind="info")
            return
        win, body, _close = self._make_embed_dialog("여러 명에게 한 번에 보내기", 380, 540)
        self._broadcast_win = win

        tk.Label(body, text="받을 사람 선택", bg=C_CARD, fg=C_MUTE, font=FONT_SM,
                anchor="w").pack(fill="x", padx=16, pady=(10, 4))
        listwrap, vars_ = self._build_picker_list(body, peers)
        listwrap.pack(fill="both", expand=True, padx=16)

        tk.Label(body, text="보낼 메시지 (선택한 사람 각자에게 1:1로 개별 전송됩니다)",
                bg=C_CARD, fg=C_MUTE, font=FONT_XS, anchor="w").pack(fill="x", padx=16, pady=(10, 4))
        msg_text = tk.Text(body, height=4, font=FONT_MSG, relief="flat", bg=C_SEARCHBG, fg=C_TEXT,
                           wrap="word", insertbackground=C_TEXT, highlightthickness=1,
                           highlightbackground=C_BORDER, highlightcolor=C_ME, padx=8, pady=6)
        msg_text.pack(fill="x", padx=16)
        apply_ime_font(msg_text, FONT_FAM, FONT_MSG[1])

        def _sanitize_msg(_e=None):
            try:
                msg_text.edit_modified(False)
            except tk.TclError:
                pass
            raw = msg_text.get("1.0", "end-1c")
            cleaned = sanitize_chat_text(raw)
            if raw != cleaned:
                cursor = msg_text.index("insert")
                msg_text.delete("1.0", "end")
                msg_text.insert("1.0", cleaned)
                try:
                    msg_text.mark_set("insert", cursor)
                except Exception:
                    pass

        msg_text.bind("<<Modified>>", _sanitize_msg)
        msg_text.bind("<Control-Return>", lambda e: (do_send(), "break")[1])
        msg_text.bind("<Control-KP_Enter>", lambda e: (do_send(), "break")[1])
        msg_text.bind("<Escape>", lambda e: _close())

        # 자동 폭파 타이머 — 대화방의 ⏱ 버튼과 별개로, 이 공지 전송 1회에만 적용되는
        # 선택 옵션이다(공지는 그룹이 아니라 각 수신자와의 1:1 메시지로 개별 전송되므로,
        # 여기서 고른 값이 그대로 각 1:1 메시지의 burn_sec으로 실려 기존 자동 폭파
        # 로직 — 상대가 읽은 시점부터 카운트다운 — 을 그대로 탄다).
        burn_state = {"sec": 0}

        def _update_burn_label():
            if burn_state["sec"]:
                burn_lbl.config(text=f"⏱ 자동 삭제: {self._burn_label(burn_state['sec'])} 후 (상대가 읽은 뒤부터)",
                               fg=C_ME)
            else:
                burn_lbl.config(text="⏱ 자동 삭제: 끄기 (클릭해서 설정)", fg=C_MUTE)

        def _open_broadcast_burn_menu(_e=None):
            try:
                x = burn_lbl.winfo_rootx()
                y = burn_lbl.winfo_rooty() - 4
            except tk.TclError:
                return
            items = []
            for label, secs in self._BURN_OPTIONS:
                prefix = "✓ " if secs == burn_state["sec"] else "　 "
                def _pick(s=secs):
                    burn_state["sec"] = s
                    _update_burn_label()
                items.append((prefix + label, _pick))
            self._popup_menu(x, y - 170, items)

        burn_lbl = tk.Label(body, bg=C_CARD, font=FONT_XS, anchor="w", cursor="hand2")
        burn_lbl.pack(fill="x", padx=16, pady=(8, 0))
        burn_lbl.bind("<Button-1>", _open_broadcast_burn_menu)
        _update_burn_label()

        def do_send():
            members = [k for k, v in vars_ if v.get()]
            text = sanitize_chat_text(msg_text.get("1.0", "end-1c")).strip()
            if not members:
                self._embed_alert("선택 필요", "받을 사람을 한 명 이상 선택하세요.", kind="warning")
                return
            if not text:
                self._embed_alert("내용 필요", "보낼 메시지를 입력하세요.", kind="warning")
                return
            burn_sec = burn_state["sec"]
            for ip, port in members:
                self.engine.send_message(ip, port, text, burn_sec=burn_sec)
            _close()
            self._refresh_list()
            if burn_sec:
                self.status.set(f"{len(members)}명에게 메시지를 보냈습니다 (각자 읽은 뒤 {self._burn_label(burn_sec)} 후 자동 삭제)")
            else:
                self.status.set(f"{len(members)}명에게 메시지를 보냈습니다")

        btnrow = tk.Frame(body, bg=C_CARD)
        btnrow.pack(fill="x", padx=16, pady=12)
        self._btn(btnrow, "보내기", do_send, C_ME, "white", C_ME_D,
                 font=FONT_BTN, padx=16, pady=6).pack(side="right")
        self._btn(btnrow, "취소", _close, C_CARD, C_MUTE, C_HOVER,
                 font=FONT_BTN, padx=12, pady=6).pack(side="right", padx=(0, 8))

    def _group_add_member_dialog(self, gid, on_added=None):
        with self.engine.glock:
            g = self.engine.groups.get(gid)
            existing = set(g["members"]) if g else set()
            grp_name = g["name"] if g else "그룹"
        peers = [p for p in self._known_peers_sorted() if (p["ip"], p["port"]) not in existing]

        win, body, _close = self._make_embed_dialog("그룹 멤버 초대", 380, 480)

        tk.Label(body, text=f"대상 그룹: {grp_name}", bg=C_CARD, fg=C_MUTE, font=FONT_XS,
                 anchor="w").pack(fill="x", padx=16, pady=(0, 8))

        if peers:
            tk.Label(body, text="친구 목록에서 선택", bg=C_CARD, fg=C_MUTE, font=FONT_SM,
                     anchor="w").pack(fill="x", padx=16, pady=(4, 2))
            listwrap, vars_ = self._build_picker_list(body, peers)
            listwrap.pack(fill="both", expand=True, padx=16, pady=(0, 6))
        else:
            vars_ = []
            no_box = tk.Frame(body, bg=C_SEARCHBG, padx=12, pady=16,
                              highlightthickness=1, highlightbackground=C_BORDER)
            no_box.pack(fill="x", padx=16, pady=(4, 8))
            tk.Label(no_box, text="추가 가능한 친구 목록이 없습니다.", bg=C_SEARCHBG,
                     fg=C_TEXT, font=FONT_SM).pack()
            tk.Label(no_box, text="아래에 상대방 IP를 직접 입력하여 초대할 수 있습니다.",
                     bg=C_SEARCHBG, fg=C_MUTE, font=FONT_XS).pack(pady=(4, 0))

        dir_frame = tk.Frame(body, bg=C_CARD)
        dir_frame.pack(fill="x", padx=16, pady=(4, 6))
        tk.Label(dir_frame, text="직접 IP:포트 입력 초대 (선택사항)", bg=C_CARD, fg=C_MUTE,
                 font=FONT_SM, anchor="w").pack(fill="x", pady=(0, 3))
        ip_var = tk.StringVar()
        ip_entry = tk.Entry(dir_frame, textvariable=ip_var, font=FONT_MSG, relief="flat", bg=C_SEARCHBG, fg=C_TEXT,
                            highlightthickness=1, highlightbackground=C_BORDER,
                            highlightcolor=C_BORDER, insertbackground=C_TEXT)
        self._sanitize_var_trace(ip_entry, ip_var)
        apply_ime_font(ip_entry, FONT_FAM, FONT_MSG[1])
        ip_entry.pack(fill="x", ipady=6)
        tk.Label(dir_frame, text="형식: IP 또는 IP:포트 (예: 192.168.0.15:50707)", bg=C_CARD,
                 fg=C_MUTE, font=FONT_XS, anchor="w").pack(fill="x", pady=(2, 0))

        def do_add():
            members = {k for k, v in vars_ if v.get()}
            raw_ip = ip_entry.get().strip()
            if raw_ip:
                try:
                    dip, dport = parse_target(raw_ip)
                    if self.engine._is_self(dip, dport):
                        self._embed_alert("입력 확인", "자기 자신은 추가할 수 없습니다.", kind="warning")
                        return
                    members.add((dip, dport))
                    self.engine.set_static_targets(self.engine.static | {(dip, dport)})
                except ValueError as e:
                    self._embed_alert("IP 입력 확인", str(e), kind="warning")
                    return
            if not members:
                self._embed_alert("선택 필요", "초대할 상대를 목록에서 선택하거나 IP를 입력하세요.",
                            kind="warning")
                return
            self.engine.add_group_members(gid, members)
            _close()
            self._refresh_list()
            if on_added:
                on_added()

        btnrow = tk.Frame(body, bg=C_CARD)
        btnrow.pack(fill="x", padx=16, pady=12)
        self._btn(btnrow, "초대", do_add, C_ME, "white", C_ME_D,
                 font=FONT_BTN, padx=16, pady=6).pack(side="right")
        self._btn(btnrow, "취소", _close, C_CARD, C_MUTE, C_HOVER,
                 font=FONT_BTN, padx=12, pady=6).pack(side="right", padx=(0, 8))

    def _open_member_dialog(self):
        if not self.current or self.current[0] != "grp":
            return
        gid = self.current[1]
        with self.engine.glock:
            g = self.engine.groups.get(gid)
            if g is None:
                return
            grp_name = g["name"]

        overlay = self._make_modal_overlay()
        win = tk.Frame(self.root, bg=C_CARD, highlightthickness=1, highlightbackground=C_BORDER)
        win.place(relx=0.5, rely=0.46, anchor="center", width=380, height=480)
        win.lift()

        def _close(_e=None):
            if win.winfo_exists():
                win.destroy()
            if overlay.winfo_exists():
                overlay.destroy()

        win.bind("<Escape>", _close)

        head = tk.Frame(win, bg=C_CARD)
        head.pack(fill="x", padx=16, pady=(16, 10))

        head_info = tk.Frame(head, bg=C_CARD)
        head_info.pack(side="left", fill="x", expand=True)

        count_lbl = tk.Label(head_info, text="", bg=C_CARD, fg=C_TEXT, font=FONT_HEAD, anchor="w")
        count_lbl.pack(anchor="w")
        grp_name_lbl = tk.Label(head_info, text=grp_name[:26], bg=C_CARD, fg=C_MUTE, font=FONT_XS, anchor="w")
        grp_name_lbl.pack(anchor="w")

        def open_add():
            self._group_add_member_dialog(gid, on_added=reload_members)

        self._btn(head, "＋ 멤버 추가", open_add, C_ME, "white", C_ME_D,
                  font=FONT_BTN, padx=12, pady=6).pack(side="right")

        tk.Frame(win, bg=C_BORDER, height=1).pack(fill="x", padx=16, pady=(0, 6))

        list_wrap = tk.Frame(win, bg=C_CARD)
        list_wrap.pack(fill="both", expand=True, padx=16)

        canvas = tk.Canvas(list_wrap, bg=C_CARD, highlightthickness=0)
        sb = MinimalScrollbar(list_wrap, target=canvas, bg=C_CARD)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=C_CARD)
        canvas_win = canvas.create_window((0, 0), anchor="nw", window=inner)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(canvas_win, width=e.width))
        canvas.bind("<MouseWheel>", lambda e: safe_canvas_mousewheel(canvas, e))

        _disarm_member_wheel = bind_scoped_mousewheel(list_wrap, canvas)
        win.bind("<Destroy>", lambda e: _disarm_member_wheel() if e.widget == win else None, add="+")

        def reload_members():
            for w in inner.winfo_children():
                w.destroy()

            with self.engine.glock:
                g_now = self.engine.groups.get(gid)
                cur_others = set(g_now["members"]) if g_now else set()
                cur_name = g_now["name"] if g_now else grp_name
            grp_name_lbl.config(text=cur_name[:26])
            total_count = 1 + len(cur_others)
            count_lbl.config(text=f"참여 멤버 {total_count}명")

            # 1. '나' (Me) Row
            me_row = tk.Frame(inner, bg=C_CARD)
            me_row.pack(fill="x", pady=4)

            me_av = tk.Canvas(me_row, width=38, height=40, bg=C_CARD, highlightthickness=0)
            me_av.pack(side="left", padx=(4, 8), pady=2)
            me_color = getattr(self, "_me_avatar_color", None) or self._avacolor(self.engine.name)
            me_photo = self._get_avatar_photo(self.engine.my_avatar_hash) if self.engine.my_avatar_hash else None
            self._avatar(me_av, self._initial(self.engine.name), me_color, av_photo=me_photo)
            me_av.create_oval(26, 27, 34, 35, fill=C_ONLINE, outline=C_CARD, width=1)

            me_mid = tk.Frame(me_row, bg=C_CARD)
            me_mid.pack(side="left", fill="both", expand=True, pady=2)

            me_name_row = tk.Frame(me_mid, bg=C_CARD)
            me_name_row.pack(anchor="w")
            tk.Label(me_name_row, text=self.engine.name[:20], fg=C_TEXT, bg=C_CARD,
                     font=FONT_NAME, anchor="w").pack(side="left")
            tk.Label(me_name_row, text=" 나 ", fg="white", bg=C_ME,
                     font=FONT_BADGE, padx=3, pady=0).pack(side="left", padx=6)

            my_ips = local_ips()
            my_ip_str = my_ips[0] if my_ips else "127.0.0.1"
            tk.Label(me_mid, text=f"{my_ip_str}:{self.engine.port} · 방 참여 중", fg=C_MUTE, bg=C_CARD,
                     font=FONT_XS, anchor="w").pack(anchor="w")

            # 2. Other Members Rows
            now = time.time()
            with self.engine.plock:
                peers_snapshot = {k: dict(v) for k, v in self.engine.peers.items()}

            sorted_others = sorted(
                cur_others,
                key=lambda m: (self.engine.get_alias(("dm", m[0], m[1])) or peers_snapshot.get(m, {}).get("name") or m[0]).lower()
            )

            for (ip, port) in sorted_others:
                peer = peers_snapshot.get((ip, port), {})
                p_name = peer.get("name")
                alias = self.engine.get_alias(("dm", ip, port))
                display_name = alias or p_name or ip
                online = bool(peer.get("last")) and (now - peer.get("last", 0) < PEER_TIMEOUT)

                row = tk.Frame(inner, bg=C_CARD)
                row.pack(fill="x", pady=3)

                av = tk.Canvas(row, width=38, height=40, bg=C_CARD, highlightthickness=0)
                av.pack(side="left", padx=(4, 8), pady=2)
                c = self._avacolor(display_name) if online else C_OFFLINE
                self._avatar(av, self._initial(display_name), c, av_photo=self._get_avatar_photo(peer.get("av")))
                if online:
                    av.create_oval(26, 27, 34, 35, fill=C_ONLINE, outline=C_CARD, width=1)

                mid = tk.Frame(row, bg=C_CARD)
                mid.pack(side="left", fill="both", expand=True, pady=2)

                top_mid = tk.Frame(mid, bg=C_CARD)
                top_mid.pack(anchor="w")
                name_lbl = tk.Label(top_mid, text=display_name[:20], fg=C_TEXT if online else C_MUTE,
                                    bg=C_CARD, font=FONT_NAME if online else FONT_NAME_M, anchor="w")
                name_lbl.pack(side="left")
                if alias and p_name and alias != p_name:
                    tk.Label(top_mid, text=f"({p_name[:10]})", fg=C_MUTE, bg=C_CARD,
                             font=FONT_XS).pack(side="left", padx=(4, 0))

                status_str = "접속 중" if online else "대기/오프라인"
                sub_lbl = tk.Label(mid, text=f"{ip}:{port} · {status_str}", fg=C_MUTE, bg=C_CARD,
                                   font=FONT_XS, anchor="w")
                sub_lbl.pack(anchor="w")

                btn_box = tk.Frame(row, bg=C_CARD)
                btn_box.pack(side="right", padx=(4, 0))

                def make_dm_cmd(target_ip=ip, target_port=port):
                    def _go_dm():
                        _close()
                        self._select(("dm", target_ip, target_port))
                    return _go_dm

                def make_alias_cmd(target_ip=ip, target_port=port):
                    def _edit_alias():
                        self._rename_conversation_dialog(("dm", target_ip, target_port))
                        reload_members()
                    return _edit_alias

                self._btn(btn_box, "1:1", make_dm_cmd(), C_CARD, C_TEXT, C_HOVER,
                          font=FONT_XS, padx=7, pady=3).pack(side="left", padx=2)
                self._btn(btn_box, "별칭", make_alias_cmd(), C_CARD, C_MUTE, C_HOVER,
                          font=FONT_XS, padx=6, pady=3).pack(side="left", padx=2)

        reload_members()

        bot = tk.Frame(win, bg=C_CARD)
        bot.pack(fill="x", padx=16, pady=(8, 14))
        self._btn(bot, "닫기", _close, C_CARD, C_MUTE, C_HOVER,
                  font=FONT_BTN, padx=16, pady=6).pack(side="right")
        win.focus_set()
