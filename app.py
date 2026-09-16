# -*- coding: utf-8 -*-
"""app.py — 텔레그램풍 GUI(App 클래스): 좌측 통합 대화 목록 / 우측 둥근 말풍선 채팅.
lan_messenger.py에서 분리됨 (유지보수를 위해 여러 파일로 분할, v5.1). v6.12에서
App 클래스 자체도 너무 커져서 dnd_handler/chat_search/chat_renderer/dialogs
믹스인으로 추가 분리했다 — 여기 남은 것은 초기화(__init__)와 나머지 잡다한
메서드들이다."""
import datetime
import os
import queue
import shutil
import threading
import time
import zlib
import tkinter as tk
from tkinter import filedialog, ttk

try:
    import ctypes
    _HAS_CTYPES = True
except ImportError:  # pragma: no cover - 표준 라이브러리라 사실상 항상 성공
    _HAS_CTYPES = False

from constants import *  # noqa: F401,F403 - 색상/폰트/레이아웃 상수 전체 사용
from constants import _ICON_PNG_B64
from netutils import (default_datadir, default_name, korea_time_str, sanitize_chat_text,
                       get_clipboard_image_bytes, get_clipboard_files)
from canvas_utils import round_rect, smooth_circle_photo, bind_scoped_mousewheel
from winapi import (Notifier, apply_dark_titlebar, apply_ime_font,
                     is_run_at_startup_enabled, set_run_at_startup, force_foreground_window)
from widgets import SplitterHandle, ScrollBottomButton, MinimalScrollbar, PillButton, ChatSearchBar, ReplyBanner, PinBanner, EmojiPicker, MentionPopup, make_search_icon
from engine import Engine
import stickers
from dnd_handler import DndMixin
from chat_search import ChatSearchMixin
from chat_renderer import ChatRendererMixin
from dialogs import DialogsMixin
from toast_popup import NotificationToast

_WHISTLE_WAV_CACHE = None


def _soft_whistle_wav():
    """"띵동" 도어벨 같은 리듬(높은 음 짧게 → 낮은 음 길게, 각각 확 울렸다가
    서서히 잦아드는 여운)을 순음(휘파람 음색)으로 합성한 알림음. 표준 라이브러리
    (wave/math)만으로 그 자리에서 만들어 WAV 바이트로 돌려준다(외부 파일·패키지
    불필요). 이전 버전은 음 안에서 피치가 스르륵 미끄러지는 "휘~후~" 방식이었는데,
    이번엔 각 음의 피치를 고정하고 대신 "확 울림 → 서서히 잦아듦" 엔벌로프로
    도어벨 느낌을 낸다. 한 번 만든 뒤에는 캐싱해 매번 다시 계산하지 않는다."""
    global _WHISTLE_WAV_CACHE
    if _WHISTLE_WAV_CACHE is not None:
        return _WHISTLE_WAV_CACHE
    import io
    import math
    import struct
    import wave

    rate = 22050

    def _ding(dur, freq, peak_amp, attack=0.012, decay_rate=3.2):
        """고정된 한 음을 짧게 확 울렸다가(attack) 지수적으로 잦아드는(decay)
        도어벨/종소리풍 엔벌로프로 합성한다."""
        n = max(1, int(rate * dur))
        phase = 0.0
        out = bytearray()
        for i in range(n):
            t = i / rate
            if t < attack:
                envelope = t / attack  # 0 -> 1로 빠르게 확 울림
            else:
                envelope = math.exp(-decay_rate * (t - attack) / dur)  # 서서히 잦아듦
            phase += 2 * math.pi * freq / rate
            amp = peak_amp * envelope
            sample = amp * math.sin(phase)
            val = int(max(-1.0, min(1.0, sample)) * 32767)
            out += struct.pack("<h", val)
        return out

    def _silence(dur):
        return bytearray(2 * max(0, int(rate * dur)))

    # "띵": 높고 짧게, 확 울렸다가 빠르게 잦아듦
    ding = _ding(0.20, 1300.0, 0.26)
    # 두 음 사이의 짧은 틈
    gap = _silence(0.06)
    # "동": 한 옥타브 정도 낮게, 더 오래 여운이 남도록 천천히 잦아듦
    dong = _ding(0.55, 780.0, 0.24, decay_rate=2.2)

    frames = ding + gap + dong
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    _WHISTLE_WAV_CACHE = buf.getvalue()
    return _WHISTLE_WAV_CACHE


class App(DialogsMixin, ChatRendererMixin, ChatSearchMixin, DndMixin):
    """텔레그램풍 GUI — 좌측 통합 대화 목록(1:1+그룹) / 우측 둥근 말풍선 채팅."""

    def __init__(self, root, args, extra_peers):
        self.root = root
        self.datadir = args.datadir or default_datadir()
        self.args = args
        self.extra_peers = extra_peers
        self.engine = None
        self.current = None            # ("dm", ip, port) 또는 ("grp", gid)
        self.unread = {}
        self._rows = {}                # key -> row 위젯 참조 딕셔너리 (diff 갱신용, destroy 최소화)
        self._chat_has_content = False
        self._last_day = None
        self._last_sender = None
        self._chat_y = PAD_TOP
        self._peer_avatar_bottom = 0
        self._scroll_btn_visible = False
        self._scroll_anim_jobs = []
        self._resize_job = None
        self.q = queue.SimpleQueue()
        self._notifier = None
        self._toast = None      # 인앱 알림 토스트 팝업(NotificationToast) — 트레이 알림 클릭 신뢰성 문제 우회
        self._tab = "chat"              # "chat" 또는 "friend"
        self._pending_sends = {}        # fid -> {name,size,path,is_image,target}
        self._burn_mode = {}            # ("dm",ip,port) -> 자동 폭파 타이머(초). 0/미설정=꺼짐
        self._chat_images = []          # 렌더된 PhotoImage 참조(GC 방지)
        self._avatar_img_cache = {}     # (av_hash, target크기) -> tk.PhotoImage (원형 프로필 사진 캐시)
        self._thumbnail_cache = {}      # path -> (mtime, tk.PhotoImage) (채팅창 이미지 썸네일 디코딩 캐시)
        self._sidebar_visible = True    # 좌측 사이드바 표시 여부
        self._sidebar_width = 195       # 이전/현재 사이드바 너비
        self._custom_sidebar_w = None   # 사용자가 드래그 조절한 너비
        self._splitter_drag = None      # 드래그 상태 정보
        self._reply_target = None       # {"name": ..., "text": ...} 인용 답장 대상
        self._search_matches = []       # 검색 일치 레코드 인덱스 목록
        self._search_cur_idx = -1       # 현재 보고 있는 검색 일치 인덱스
        self._record_y_positions = {}   # rec_idx -> canvas y 좌표(시작)
        self._record_y_end = {}         # rec_idx -> canvas y 좌표(끝, 검색 하이라이트 높이 계산용)
        self._mention_popup = None      # @ 멘션 자동완성 팝업 위젯
        self._search_result_widgets = []  # 사이드바 통합 검색 결과 위젯 리스트

        root.title("LAN Talk")
        root.geometry("450x800")
        root.minsize(360, 540)
        root.configure(bg=C_MAIN)
        self._set_app_icon()
        self._me_name = args.name

        self._build_shell()
        # 자식 위젯이 다 만들어진 뒤에 다크 타이틀바를 적용한다 — 빈 창 상태에서
        # 먼저 적용하면 DWM의 강제 리페인트(SetWindowPos SWP_FRAMECHANGED)가
        # Tk의 위젯 그리기와 타이밍이 겹쳐, 사이드바/헤더 경계에 1px 흰 점이
        # 남는 버그가 있었다.
        apply_dark_titlebar(root, C_SIDEBAR, C_TEXT)
        try:
            self.engine = Engine(args.name, port=self.args.port,
                                 datadir=self.datadir, extra_peers=self.extra_peers,
                                 on_event=self.q.put)
        except OSError:
            self.engine = None
            self.root.after(10, self._startup_fail)
            return

        if self.engine.always_on_top:
            try:
                self.root.attributes("-topmost", True)
            except tk.TclError:
                pass
        self.me_lbl.config(text=self.engine.name)
        self._refresh_me_avatar()
        self._show_empty("대화 상대를 선택하거나 상대를 연결해\n대화를 시작하세요")
        self._refresh_list()
        try:
            self._notifier = Notifier(self.root, on_click=self._on_notify_click)
        except Exception:
            self._notifier = None
        if self._notifier:
            # 창이 다시 포커스를 얻으면 작업표시줄 깜빡임을 확실히 멈춘다 — 보통
            # Windows가 FlashWindowEx(FLASHW_TIMERNOFG)를 자동으로 멈춰주지만,
            # 클릭 대신 Alt+Tab 등으로 돌아오는 경우까지 확실히 커버한다.
            self.root.bind("<FocusIn>",
                           lambda e: self._notifier.stop_flash() if e.widget is self.root else None,
                           add="+")
        self._dropped_files_queue = []
        self._notify_click_pending = False
        self.root.after(80, self._pump)
        self.root.after(350, self._prewarm_emoji_system)
        self.root.after(350, self._prewarm_crypto_pool)
        self.root.after(600, self._maybe_first_run)
        self.root.bind("<Configure>", self._on_root_resize)
        self.root.bind("<Control-b>", lambda e: self._toggle_sidebar())
        self.root.bind("<Control-B>", lambda e: self._toggle_sidebar())
        self.root.bind("<F4>", lambda e: self._toggle_sidebar())
        self.root.bind("<Control-f>", lambda e: self._open_search())
        self.root.bind("<Control-F>", lambda e: self._open_search())
        self.root.bind("<Control-Shift-H>", self._toggle_boss_key)
        self.root.bind("<Control-Shift-h>", self._toggle_boss_key)
        self.root.bind("<F12>", self._toggle_boss_key)
        self.root.after(100, self._setup_drag_and_drop)
        root.protocol("WM_DELETE_WINDOW", self._quit)

    def _prewarm_emoji_system(self):
        """스티커 28종의 원형 배경(96px, 38px)을 백그라운드 유휴 시간에 미리 캐시 생성하여
        첫 스티커 방 진입이나 스티커 메뉴 오픈 시 지연을 0으로 만든다."""
        try:
            import stickers
            stickers.prewarm_stickers()
        except Exception:
            pass

    def _prewarm_crypto_pool(self):
        """대용량 파일 청크 암호화용 프로세스 풀을 백그라운드 스레드에서 미리
        띄워둔다(약 200ms) — 안 하면 사용자가 처음 큰 파일을 보내는 순간
        그 지연을 그대로 겪는다."""
        import threading
        import crypto_layer
        threading.Thread(target=crypto_layer.prewarm_pool, daemon=True).start()

    def _toggle_boss_key(self, _e=None):
        """보스키: 메신저 창을 화면과 작업표시줄에서 즉시 숨기거나 복원한다."""
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        else:
            self.root.withdraw()
        return "break"

    # ---------- 아이콘 ----------
    def _set_app_icon(self):
        try:
            imgs = [tk.PhotoImage(data=_ICON_PNG_B64[sz]) for sz in (256, 128, 64, 48, 32, 16)]
            self._icon_imgs = imgs  # 가비지 컬렉션 방지용 참조 보관
            self.root.iconphoto(True, *imgs)
        except Exception:
            pass
        try:
            from winapi import apply_window_icon
            apply_window_icon(self.root)
        except Exception:
            pass


    # ---------- 위젯 헬퍼 ----------
    def _on_root_resize(self, e):
        if e.widget != self.root:
            return
        if not getattr(self, "_sidebar_visible", True):
            return
        w = e.width
        if getattr(self, "_custom_sidebar_w", None):
            max_w = max(110, int(w * 0.42)) if w <= 400 else max(120, int(w * 0.55))
            target_w = max(110, min(self._custom_sidebar_w, max_w))
        else:
            if w <= 360:
                target_w = max(110, int(w * 0.40))
            elif w <= 420:
                target_w = max(120, int(w * 0.44))
            elif w < 680:
                target_w = 195
            else:
                target_w = 270
        if getattr(self, "side", None) and self.side.winfo_exists():
            if str(self.side.cget("width")) != str(target_w):
                self.side.config(width=target_w)
                if hasattr(self, "side_inner"):
                    self.side_inner.place(x=0, y=0, width=target_w, relheight=1.0)



    # ---------- 커스텀 드롭다운 메뉴 (기본 tk.Menu 대신 — 앱 색상·폰트에 맞춘 테두리 없는 팝업) ----------

    # ---------- 커스텀 알림창 (messagebox 대신 — 기본 Tk 회색 대화상자 대체) ----------



    # ---------- 셸 ----------
    def _build_shell(self):
        side = tk.Frame(self.root, bg=C_SIDEBAR, width=195)
        self.side = side
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        # 사이드바 내부 래퍼 (애니메이션 슬라이딩 시 위젯 왜곡/압축 방지용 뷰포트 컨테이너)
        side_inner = tk.Frame(side, bg=C_SIDEBAR, width=195)
        self.side_inner = side_inner
        side_inner.place(x=0, y=0, width=195, relheight=1.0)

        # 내 프로필
        me = tk.Frame(side_inner, bg=C_SIDEBAR)
        me.pack(fill="x", padx=14, pady=(16, 10))
        av = tk.Canvas(me, width=40, height=40, bg=C_SIDEBAR, highlightthickness=0, cursor="hand2")
        me_name = getattr(self, "_me_name", None) or default_name()
        self.me_av = av
        self._me_avatar_color = self._avacolor(me_name)
        self._avatar(av, self._initial(me_name), self._me_avatar_color)
        av.bind("<Button-1>", lambda e: self._open_my_avatar_menu())
        av.pack(side="left")
        col = tk.Frame(me, bg=C_SIDEBAR)
        col.pack(side="left", padx=(10, 0))
        self.me_lbl = tk.Label(col, text="…", fg=C_TEXT, bg=C_SIDEBAR, font=FONT_HEAD,
                               anchor="w")
        self.me_lbl.pack(anchor="w")
        self.me_sub = tk.Label(col, text="온라인", fg=C_ONLINE,
                               bg=C_SIDEBAR, font=FONT_XS, anchor="w")
        self.me_sub.pack(anchor="w")
        rename = self._btn(col, "이름 변경", self._apply_name_dialog, C_SIDEBAR, C_MUTE,
                           C_SIDEBAR, font=FONT_XS_PAD, padx=0, pady=0)
        rename.pack(anchor="w", pady=(2, 0))

        tk.Frame(side_inner, bg=C_SEPAR, height=1).pack(fill="x", padx=14, pady=(0, 10))

        # 탭 (채팅 / 친구)
        tabrow = tk.Frame(side_inner, bg=C_SIDEBAR)
        tabrow.pack(fill="x", padx=14, pady=(0, 10))
        self.tab_chat_btn = self._btn(tabrow, "채팅", lambda: self._set_tab("chat"),
                                      C_ROWSEL, C_TEXT, C_HOVER, font=FONT_BTN, padx=8, pady=5)
        self.tab_chat_btn.pack(side="left", fill="x", expand=True)
        self.tab_friend_btn = self._btn(tabrow, "친구", lambda: self._set_tab("friend"),
                                        C_SIDEBAR, C_MUTE, C_HOVER, font=FONT_BTN, padx=8, pady=5)
        self.tab_friend_btn.pack(side="left", fill="x", expand=True)

        # 검색
        sw = tk.Frame(side_inner, bg=C_SEARCHBG, highlightthickness=1,
                      highlightbackground=C_SEPAR, highlightcolor=C_SEPAR)
        sw.pack(fill="x", padx=14, pady=(0, 8))
        self.search_var = tk.StringVar()
        self._search_debounce_id = None
        make_search_icon(sw, bg=C_SEARCHBG, fg=C_MUTE, size=16).pack(side="left", padx=(8, 4))
        self.search = tk.Entry(sw, textvariable=self.search_var, bg=C_SEARCHBG, fg=C_TEXT,
                               font=FONT_SM, relief="flat", insertbackground=C_TEXT,
                               highlightthickness=0, bd=0)
        self.search.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        apply_ime_font(self.search, FONT_FAM, FONT_SM[1])
        self._search_ph = True

        def _ph_on(e):
            if self._search_ph:
                self.search.delete(0, "end")
                self.search.config(fg=C_TEXT)
                self._search_ph = False

        def _ph_off(e):
            if not self.search.get() and not self._search_ph:
                self._search_ph = True
                self.search.insert(0, "이름 또는 IP 검색")
                self.search.config(fg=C_MUTE)

        self.search.bind("<FocusIn>", _ph_on)
        self.search.bind("<FocusOut>", _ph_off)
        self.search.bind("<Return>", lambda e: "break")

        def _on_search_change(*_a):
            if self._search_ph:
                return
            # textvariable을 쓰는 Entry는 <<Modified>>가 없지만, 어떤 방식으로
            # 내용이 바뀌든(타이핑·이모지 패널·IME) StringVar의 write 트레이스는
            # 예외 없이 걸리므로 여기서 채팅 입력창과 동일하게 실시간 살균한다.
            raw = self.search_var.get()
            cleaned = sanitize_chat_text(raw)
            if raw != cleaned:
                cursor = self.search.index("insert")
                self.search_var.set(cleaned)  # 이 set()이 트레이스를 재귀 호출해 마저 처리함
                try:
                    self.search.icursor(min(cursor, len(cleaned)))
                except Exception:
                    pass
                return
            if self._search_debounce_id is not None:
                try:
                    self.root.after_cancel(self._search_debounce_id)
                except Exception:
                    pass
                self._search_debounce_id = None
            self._search_debounce_id = self.root.after(150, self._refresh_list)

        self.search_var.trace_add("write", _on_search_change)

        # 목록 헤더
        hrow = tk.Frame(side_inner, bg=C_SIDEBAR)
        hrow.pack(fill="x", padx=14, pady=(2, 6))
        self.cnt_lbl = tk.Label(hrow, text="대화", fg=C_MUTE, bg=C_SIDEBAR, font=FONT_XS_PAD)
        self.cnt_lbl.pack(side="left")
        self.add_btn = self._btn(hrow, "＋", self._open_add_menu,
                                 C_SIDEBAR, C_TEXT, C_ROWSEL, font=FONT_SM, padx=10, pady=3)
        self.add_btn.pack(side="right")

        # 목록
        lw = tk.Frame(side_inner, bg=C_SIDEBAR)
        lw.pack(fill="both", expand=True, padx=(8, 0))
        self.plist = tk.Canvas(lw, bg=C_SIDEBAR, highlightthickness=0, bd=0)
        ps = MinimalScrollbar(lw, target=self.plist, bg=C_SIDEBAR)
        self.plist.configure(yscrollcommand=ps.set)
        self._ps = ps
        self.plist.pack(side="left", fill="both", expand=True)
        self.pinner = tk.Frame(self.plist, bg=C_SIDEBAR)
        self.plist.create_window((0, 0), anchor="nw", window=self.pinner, tags="inner")
        self.pinner.bind("<Configure>", self._plist_cfg)
        self.plist.bind("<Configure>", lambda e: self._plist_cfg(e or tk.Event()))
        self.plist.bind("<Configure>",
                        lambda e: self.plist.itemconfigure("inner", width=e.width))

        # 마우스 휠 스크롤: self.pinner 안의 각 대화 행은 캔버스 위에 그린 게 아니라
        # 실제 자식 위젯(Frame/Label/Canvas)이라, self.plist에만 <MouseWheel>을 걸면
        # 행 위에 마우스가 있을 때는 이벤트가 그 자식 위젯에서 끝나버려 휠이 안 먹는다
        # (채팅 캔버스는 전부 create_text/create_image라 이 문제가 없어 대비됨). 목록
        # 영역에 들어오고 나갈 때만 bind_all로 걸었다 풀었다 하면 자식이 몇 겹이든
        # 관계없이 항상 스크롤된다.
        bind_scoped_mousewheel(lw, self.plist)

        # 하단 상태 라벨 (문구 제거 요청 반영 — 미표시)
        self.gstatus = tk.Label(side_inner, text="", bg=C_SIDEBAR)

        # 좌측 탭과 채팅창 사이의 분할 기둥 (드래그 크기 조절 & 접기 핸들)
        splitter = tk.Frame(self.root, bg=C_BORDER, width=6, cursor="sb_h_double_arrow")
        self.splitter = splitter
        splitter.pack(side="left", fill="y")
        splitter.pack_propagate(False)

        splitter.bind("<Button-1>", self._on_splitter_press)
        splitter.bind("<B1-Motion>", self._on_splitter_motion)
        splitter.bind("<ButtonRelease-1>", self._on_splitter_release)
        splitter.bind("<Double-Button-1>", lambda e: self._toggle_sidebar())
        splitter.bind("<Enter>", lambda e: splitter.config(bg=C_ME))
        splitter.bind("<Leave>", lambda e: splitter.config(bg=C_BORDER))

        # 기둥 중앙 접기/펼치기 핸들 버튼 (모던 슬림 라운드 탭)
        s_btn = SplitterHandle(self.root, command=self._toggle_sidebar)
        self.splitter_btn = s_btn
        s_btn.place(in_=splitter, relx=0.5, rely=0.5, anchor="center", width=18, height=84)
        s_btn.lift()

        # 본문
        body = tk.Frame(self.root, bg=C_MAIN)
        self.body = body
        body.pack(side="left", fill="both", expand=True)

        head = tk.Frame(body, bg=C_CARD)
        head.pack(fill="x")
        self.ch_title = tk.Label(head, text="대화를 선택하세요", fg=C_TEXT, bg=C_CARD,
                                 font=FONT_HEAD, anchor="w", cursor="hand2")
        self.ch_title.pack(side="left", padx=(14, 0), pady=12)
        self.ch_sub = tk.Label(head, text="", fg=C_MUTE, bg=C_CARD, font=FONT_XS)
        self.ch_sub.pack(side="left", padx=(6, 0), pady=(14, 0))
        self.ip_btn = self._btn(head, "내 IP", self._dialog_info, C_CARD, C_TEXT, C_HOVER,
                                font=FONT_XS_PAD, padx=8, pady=4)
        self.ip_btn.pack(side="right", padx=(4, 10), pady=10)
        self.room_more_btn = self._btn(head, "⚙ 설정", self._open_room_more_menu, C_CARD, C_TEXT, C_HOVER,
                                       font=FONT_XS_PAD, padx=8, pady=4)
        self.member_btn = self._btn(head, "멤버", self._open_member_dialog, C_CARD, C_TEXT,
                                    C_HOVER, font=FONT_XS_PAD, padx=8, pady=4)
        self.ch_title.bind("<Button-1>", lambda e: self._on_header_rename_click())
        self.ch_title.bind("<Button-3>", lambda e: self._open_room_more_menu(e.x_root, e.y_root))
        self._hide_room_buttons()
        tk.Frame(body, bg=C_BORDER, height=1).pack(fill="x")

        # 대화방 공지사항 배너(카카오톡 스타일) — 공지가 없으면 숨김(pack_forget 상태로 시작)
        self.pin_banner = PinBanner(body, on_click=self._on_pin_banner_click,
                                    on_unpin=self._unpin_current_notice)
        self._pinned_notice = None  # 현재 방의 공지 dict 캐시({"mid","text","sender","ts"} 또는 None)

        # 하단 입력 영역 (창 높이가 작아져도 가려지지 않도록 chat_wrap보다 먼저 bottom으로 패킹)
        cnt = tk.Canvas(body, bg=C_BORDER, height=1, highlightthickness=0)
        cnt.pack(fill="x", side="bottom")
        statuslbl = tk.Frame(body, bg=C_CARD, height=26)
        statuslbl.pack(fill="x", side="bottom")
        statuslbl.pack_propagate(False)
        self.status = tk.StringVar(value="왼쪽 목록에서 대화 상대를 선택하세요")
        tk.Label(statuslbl, textvariable=self.status, fg=C_MUTE, bg=C_CARD, font=FONT_XS,
                 anchor="w").pack(fill="both", expand=True, padx=16)
        row = tk.Frame(body, bg=C_MAIN)
        row.pack(fill="x", side="bottom", padx=8, pady=8)
        self.input_row = row
        self.reply_banner = ReplyBanner(row, on_cancel=self._cancel_reply)
        # 카카오톡 스타일 스티커 자동 추천 줄 — 입력 중인 문구에 등록된 키워드가
        # 있으면 어울리는 스티커를 여기 띄운다(클릭하면 즉시 전송). 평소엔 숨김.
        self.sticker_suggest = tk.Frame(row, bg=C_MAIN)
        self._sticker_suggest_imgs = []
        self._cur_suggest_sids = None
        # 각진 Frame 테두리 대신 Canvas에 둥근 사각형을 직접 그려 알약(pill) 모양 입력 바를 만든다.
        inner = tk.Canvas(row, bg=C_MAIN, highlightthickness=0, height=1)
        inner.pack(fill="x")
        self.input_bar = inner

        # 첨부 버튼 (둥근 알약 버튼 — 입력창 배경과 구별되는 전용 색상 적용)
        self.attach_btn = PillButton(inner, "첨부", self._attach_file, bg="#374151", fg=C_TEXT,
                                     hover_bg="#4b5563", font=FONT_BTN, padx=10, pady=5)

        # 자동 폭파 타이머 버튼 — 눌러서 켜두면 그 대화방에서 이후 보내는 메시지가
        # 상대가 읽은 시점부터 정해진 시간 뒤 양쪽 PC에서 자동으로 영구 삭제된다.
        # 켜진 동안은 배경색이 바뀌어(C_ME) 무심코 계속 켜둔 채 잊어버리지 않게 한다.
        self.burn_btn = PillButton(inner, "⏱", self._open_burn_menu, bg="#374151", fg=C_TEXT,
                                   hover_bg="#4b5563", font=FONT_BTN, padx=8, pady=5)

        # 이모지 피커 버튼
        self.emoji_btn = PillButton(inner, "😊", self._toggle_emoji_picker, bg="#374151", fg=C_TEXT,
                                    hover_bg="#4b5563", font=FONT_BTN, padx=8, pady=5)

        # 전송 버튼 (둥근 알약 버튼)
        self.send_btn = PillButton(inner, "전송", self._send, bg=C_ME, fg="white",
                                   hover_bg=C_ME_D, font=FONT_BTN, padx=10, pady=5)

        # 텍스트 입력창 (너비는 _layout_input_bar가 가용 공간에 맞춰 매번 재계산)
        self.entry = tk.Text(inner, height=1, width=1, bg=C_CARD, fg=C_TEXT, font=FONT_MSG,
                             wrap="char", bd=0, relief="flat", padx=6, pady=5,
                             insertbackground=C_TEXT, highlightthickness=0)
        apply_ime_font(self.entry, FONT_FAM, FONT_MSG[1])
        for k in ("<Return>", "<Up>", "<Down>", "<Tab>", "<Escape>"):
            self.entry.bind(k, self._entry_key)
        self.entry.bind("<<Modified>>", self._entry_resize)
        self.entry.bind("<<Paste>>", self._on_entry_paste)
        self._mention_popup = MentionPopup(self.root, on_select=self._on_mention_select)
        self._chat_row = row
        self._emoji_picker = None
        self.root.bind("<Control-v>", self._on_window_paste)
        self.root.bind("<Control-V>", self._on_window_paste)

        self._attach_win = inner.create_window(0, 0, anchor="w", window=self.attach_btn)
        self._burn_win = inner.create_window(0, 0, anchor="w", window=self.burn_btn)
        self._emoji_win = inner.create_window(0, 0, anchor="w", window=self.emoji_btn)
        self._send_win = inner.create_window(0, 0, anchor="e", window=self.send_btn)
        self._entry_win = inner.create_window(0, 0, anchor="w", window=self.entry)
        inner.bind("<Configure>", self._layout_input_bar)
        self.root.after_idle(self._layout_input_bar)

        self.entry.configure(state="disabled")
        self.send_btn.configure(state="disabled")
        self.attach_btn.configure(state="disabled")
        self.burn_btn.configure(state="disabled")
        self.emoji_btn.configure(state="disabled")

        # 대화방 내 검색 플로팅 바 (Ctrl+F)
        self.search_bar = ChatSearchBar(body,
                                        on_search=self._on_search_query,
                                        on_prev=self._on_search_prev,
                                        on_next=self._on_search_next,
                                        on_close=self._close_search)

        # 중앙 대화 캔버스 (남은 공간을 모두 차지)
        chat_wrap = tk.Frame(body, bg=C_MAIN)
        chat_wrap.pack(fill="both", expand=True)
        self.chat_wrap = chat_wrap
        self.chat = tk.Canvas(chat_wrap, bg=C_MAIN, highlightthickness=0, bd=0, cursor="arrow")
        chat_scroll = MinimalScrollbar(chat_wrap, target=self.chat, bg=C_MAIN)

        def _on_chat_yscroll(first, last):
            chat_scroll.set(first, last)
            try:
                self._update_scroll_btn(float(first), float(last))
            except (ValueError, TypeError):
                pass

        self.chat.configure(yscrollcommand=_on_chat_yscroll)
        self.chat.pack(side="left", fill="both", expand=True)
        chat_scroll.pack(side="right", fill="y")
        self.chat.bind("<Configure>", self._on_chat_resize)
        bind_scoped_mousewheel(self.chat, self.chat)
        self.chat.bind("<Control-v>", self._on_entry_paste)
        self.chat.bind("<Control-V>", self._on_entry_paste)
        # 이모지/스티커 패널이 열려 있는 상태에서 채팅 영역(빈 곳이든 메시지든)을
        # 클릭하면 자동으로 닫는다 — 별도 팝업이 아니라 내장 패널이 된 뒤에도
        # 고를 생각이 없을 때 쉽게 치울 수 있어야 하기 때문.
        self.chat.bind("<Button-1>", self._maybe_hide_emoji_picker, add="+")

        # 카카오톡 스타일 우하단 맨 아래로 스크롤 플로팅 버튼
        self.scroll_btn = ScrollBottomButton(self.chat, command=self._on_scroll_btn_click)

    def _plist_cfg(self, _e=None):
        # scrollregion을 먼저 최신 내용 크기로 갱신해야, 그 다음에 읽는 yview()가
        # 방금 추가/삭제된 행을 반영한 값이 된다. 순서가 바뀌면(예전 scrollregion 기준으로
        # yview부터 읽으면) "필요한지" 판정이 항상 한 박자 늦어져서, 행이 한꺼번에 왕창
        # 추가되는 경우(_refresh_list) 스크롤바가 끝내 나타나지 않을 수 있었다(실측 확인).
        self.plist.configure(scrollregion=self.plist.bbox("all"))
        top, bot = self.plist.yview()
        need = (bot - top) < 0.999
        if need:
            if not self._ps.winfo_ismapped():
                # Tk pack의 알려진 함정: expand=True로 이미 전체 폭을 차지한 위젯(plist) 옆에
                # 나중에 형제 위젯(스크롤바)을 pack()해도, plist가 자리를 안 비켜줘서 스크롤바가
                # 폭 1px로 찌그러진 채 화면에 안 뜬다(mapped=False) — 실측으로 확인한 Tk 동작.
                # plist를 pack_forget 후 같은 옵션으로 다시 pack해야 Tk가 두 위젯의 폭을
                # 새로 재계산해서 스크롤바가 제 몫(20px)을 받고 실제로 보인다.
                self._ps.pack(side="right", fill="y")
                self.plist.pack_forget()
                self.plist.pack(side="left", fill="both", expand=True)
        else:
            self._ps.pack_forget()
            if top != 0.0:
                try:
                    self.plist.yview_moveto(0.0)
                except Exception:
                    pass

    # ---------- 헬퍼 (아바타) ----------
    def _avatar(self, cv, label, color, av_photo=None):
        cv.delete("all")
        if av_photo is not None:
            # 등록된 프로필 사진(원형 마스크가 이미 적용된 PNG) — 캔버스 중앙에 배치
            cx = int(cv.cget("width")) // 2
            cy = int(cv.cget("height")) // 2
            iid = cv.create_image(cx, cy, image=av_photo, anchor="center")
            cv._avatar_photo_ref = av_photo  # GC 방지 — 참조를 들고 있지 않으면 이미지가 사라짐
            return cv
        # create_oval은 Windows에서 경계가 계단식으로 거칠게 나와서, 실제 알파
        # 투명도를 가진 PNG로 매끄럽게 그린 이미지를 대신 쓴다(다른 원과 겹쳐도
        # 사각형 모서리가 엉뚱한 색으로 덮이지 않음).
        photo = smooth_circle_photo(37, color)
        cv.create_image(1, 1, image=photo, anchor="nw")
        cv._avatar_smooth_ref = photo  # GC 방지
        if label:
            cv.create_text(20, 20, text=label, fill="white", font=FONT_AV)
        return cv

    # ---------- 프로필 사진 (등록/캐시/원형 렌더) ----------
    def _get_avatar_photo(self, av_hash, target=36):
        """avatar_hash에 해당하는 원형 PNG(이미 원형 알파 마스크 적용됨)를 target 크기
        근처로 축소해 tk.PhotoImage로 캐시·반환. 없거나 아직 못 받았으면 None
        (호출 쪽이 이름 이니셜 원형으로 대체 표시)."""
        if not av_hash or self.engine is None:
            return None
        cache_key = (av_hash, target)
        cached = self._avatar_img_cache.get(cache_key)
        if cached is not None:
            return cached
        path = os.path.join(self.engine.avatardir, f"{av_hash}.png")
        if not os.path.exists(path):
            return None
        try:
            img = tk.PhotoImage(file=path)
            factor = max(1, img.width() // target) if img.width() > target else 1
            if factor > 1:
                img = img.subsample(factor, factor)
        except (tk.TclError, OSError):
            return None
        # 상대가 프로필 사진을 바꿀 때마다 새 av_hash가 생기는데, 이 캐시에는
        # 지우는 로직이 없어 오래 켜둔 세션일수록 계속 쌓이기만 했다(메모리 누수).
        # chat_renderer.py의 _thumbnail_cache와 동일하게 최대 64개로 캡을 씌워
        # 넘치면 가장 오래된 것부터 밀어낸다.
        if len(self._avatar_img_cache) >= 64:
            first_k = next(iter(self._avatar_img_cache))
            self._avatar_img_cache.pop(first_k, None)
        self._avatar_img_cache[cache_key] = img
        return img

    def _refresh_me_avatar(self):
        eng = self.engine
        if eng is None or not hasattr(self, "me_av"):
            return
        me_name = eng.name
        photo = self._get_avatar_photo(eng.my_avatar_hash, target=38) if eng.my_avatar_hash else None
        self._avatar(self.me_av, self._initial(me_name), self._me_avatar_color, av_photo=photo)




    def _avatar_group(self, cv, members):
        cv.delete("all")
        ms = sorted(members)[:2]
        colors = [self._avacolor(f"{ip}:{port}") for ip, port in ms]
        if not colors:
            colors = [C_MUTE]
        if len(colors) == 1 or colors[0] == colors[1]:
            idx = C_AVA.index(colors[0]) if colors[0] in C_AVA else 0
            alt_color = C_AVA[(idx + 4) % len(C_AVA)]
            if len(colors) == 1:
                colors.append(alt_color)
            else:
                colors[1] = alt_color
        # 인위적인 불투명 외곽선(ring) 없이, 4x4 서브픽셀 앤티앨리어싱과
        # 100% 알파 투명도를 가진 원형 이미지 2장을 겹쳐 그린다.
        photo1 = smooth_circle_photo(26, colors[0])
        photo2 = smooth_circle_photo(26, colors[1])
        cv.create_image(1, 9, image=photo1, anchor="nw")
        cv.create_image(13, 1, image=photo2, anchor="nw")
        cv._avatar_group_refs = (photo1, photo2)  # GC 방지

    def _initial(self, name):
        return (name or "?").strip()[:2] or "?"

    def _avacolor(self, name):
        s = (name or "?").encode("utf-8", "replace")
        return C_AVA[zlib.crc32(s) % len(C_AVA)]

    def _open_add_menu(self):
        x = self.add_btn.winfo_rootx()
        y = self.add_btn.winfo_rooty() + self.add_btn.winfo_height() + 4
        valid_hidden = self._hidden_conv_keys()
        hidden_cnt = len(valid_hidden)
        unhide_label = f"숨긴 대화 보기 ({hidden_cnt}개)" if hidden_cnt > 0 else "숨긴 대화 보기"
        top_on = bool(self.root.attributes("-topmost")) if self.engine else False
        startup_on = is_run_at_startup_enabled()
        sound_on = self.engine.notify_sound_enabled if self.engine else True
        items = [
            ("상대 연결", self._settings_dialog),
            ("새 그룹 만들기", self._group_create_dialog),
            ("여러 명에게 한 번에 보내기", self._broadcast_dialog),
            (None, None),
            ("다운로드 폴더 설정", self._download_dir_dialog),
            ("포트 번호 변경", self._change_port_dialog),
            (unhide_label, self._open_hidden_dialog),
            (None, None),
            (("✓ " if top_on else "　 ") + "항상 위에 표시", self._toggle_always_on_top),
            (("✓ " if startup_on else "　 ") + "Windows 시작 시 자동 실행", self._toggle_run_at_startup),
            (("✓ " if sound_on else "　 ") + "알림음", self._toggle_notify_sound),
            (None, None),
            ("사이드바 숨기기 (Ctrl+B)", self._toggle_sidebar),
        ]
        if self.current:
            items.insert(7, ("현재 대화 숨기기", lambda: self._hide_conversation(self.current)))
        self._popup_menu(x, y, items)

    def _toggle_always_on_top(self):
        if not self.engine:
            return
        new_state = not bool(self.root.attributes("-topmost"))
        try:
            self.root.attributes("-topmost", new_state)
        except tk.TclError:
            return
        self.engine.set_always_on_top(new_state)
        self.status.set("항상 위에 표시를 켰습니다" if new_state else "항상 위에 표시를 껐습니다")

    def _toggle_run_at_startup(self):
        new_state = not is_run_at_startup_enabled()
        if set_run_at_startup(new_state):
            self.status.set("Windows 시작 시 자동 실행을 켰습니다" if new_state else
                           "Windows 시작 시 자동 실행을 껐습니다")
        else:
            self._embed_alert("설정 실패", "자동 실행 설정을 변경하지 못했습니다.", kind="warning")

    def _toggle_notify_sound(self):
        if not self.engine:
            return
        new_state = not self.engine.notify_sound_enabled
        self.engine.set_notify_sound_enabled(new_state)
        self.status.set("알림음을 켰습니다" if new_state else "알림음을 껐습니다")

    def _unhide_all(self):
        if self.engine:
            keys = self._hidden_conv_keys()
            cnt = len(keys)
            if self.engine.unhide_all():
                self._refresh_list()
                self.status.set(f"숨긴 대화 {cnt}개를 모두 표시했습니다")
            else:
                self.status.set("숨겨진 대화가 없습니다")

    def _hidden_conv_keys(self):
        """숨긴 목록 중 실제로 복구할 대상이 남아 있는 키만 반환한다
        (그룹은 나가기/삭제됐으면 표시할 데이터 자체가 없으므로 제외하며, 고아 키는 자동 정리한다)."""
        eng = self.engine
        if not eng:
            return []
        keys = []
        to_remove = []
        with eng.glock:
            for key in list(eng.hidden):
                if key[0] == "grp":
                    if key[1] not in eng.groups or key[1] in eng.left_groups:
                        to_remove.append(key)
                        continue
                keys.append(key)
        if to_remove:
            for k in to_remove:
                eng.hidden.discard(k)
            eng._save_hidden()
        return sorted(keys)




    def _toggle_sidebar(self, animated=True):
        if getattr(self, "_sidebar_anim_job", None):
            try:
                self.root.after_cancel(self._sidebar_anim_job)
            except Exception:
                pass
            self._sidebar_anim_job = None

        self._sidebar_visible = not getattr(self, "_sidebar_visible", True)

        if not animated:
            if self._sidebar_visible:
                w = getattr(self, "_sidebar_width", 195) or 195
                self.side.config(width=w)
                if hasattr(self, "side_inner"):
                    self.side_inner.place(x=0, y=0, width=w, relheight=1.0)
                self.side.pack(side="left", fill="y", before=self.splitter)
                if hasattr(self, "splitter_btn"):
                    self.splitter_btn.config(text="◀")
                    self.splitter_btn.place_forget()
                    self.splitter_btn.place(in_=self.splitter, relx=0.5, rely=0.5, anchor="center", width=18, height=84)
                    self.splitter_btn.lift()
                if hasattr(self, "side_toggle_btn"):
                    self.side_toggle_btn.config(text="◀")
                self.status.set("사이드바 표시됨")
            else:
                self._sidebar_width = self.side.winfo_width() or getattr(self, "_sidebar_width", 195) or 195
                self.side.pack_forget()
                if hasattr(self, "side_inner"):
                    self.side_inner.place(x=0, y=0, width=self._sidebar_width, relheight=1.0)
                if hasattr(self, "splitter_btn"):
                    self.splitter_btn.config(text="▶")
                    self.splitter_btn.place_forget()
                    self.splitter_btn.place(in_=self.splitter, x=0, rely=0.5, anchor="w", width=20, height=84)
                    self.splitter_btn.lift()
                if hasattr(self, "side_toggle_btn"):
                    self.side_toggle_btn.config(text="▶")
                self.status.set("사이드바 숨김 (다시 열려면 ▶ 클릭 또는 기둥 드래그)")
            self.root.update_idletasks()
            self._on_chat_resize(None)
            return

        # 부드러운 슬라이딩 애니메이션 실행 (타임베이스 Hermite Smoothstep, 위젯 왜곡 없는 슬라이드 클리핑)
        #
        # v6.48: 애니메이션 중 self.side를 pack 상태로 둔 채 매 프레임 width만
        # 바꾸던 예전 방식은, side가 pack(fill="y")으로 다른 형제 위젯(특히 채팅
        # 캔버스)과 같은 레이아웃에 묶여 있어서 매 프레임마다 전체 pack 레이아웃을
        # 다시 계산하고, 그때마다 채팅 캔버스에 <Configure> 이벤트가 발생해
        # 뻑뻑하고 내부 위젯이 떨리는 원인이 됐다 — 애니메이션 하는 동안만 side를
        # pack에서 완전히 빼서 place()로 독립된 오버레이로 띄우게 고쳤다.
        #
        # 그래도 여전히 버벅인다는 피드백을 받아 한 번 더 원인을 좁혔다: side_inner
        # 안에는 대화 목록(피어마다 프레임·캔버스·라벨 여러 개로 구성된 진짜
        # 위젯들)이 들어있는데, 이 실제 위젯 묶음을 매 프레임 옆으로 슬라이드
        # 시키면 Windows가 그 많은 자식 창(HWND)을 프레임마다 전부 다시 배치·
        # 리페인트해야 해서 무거웠다. 이제는 애니메이션 동안 실제 목록은 건드리지
        # 않고 숨겨두고, 배경색만 있는 가벼운 "커튼" 패널 하나만 슬라이드시킨다
        # (자식 위젯이 하나도 없어 프레임당 비용이 거의 없음). 실제 목록은
        # 애니메이션이 끝난 뒤 제자리에 한 번만 나타난다 — 180ms 안팎의 짧은
        # 슬라이드 동안 글자를 읽을 수 없으니 그 사이에 바뀌는 건 체감상 안 보인다.
        if not hasattr(self, "_sidebar_curtain"):
            self._sidebar_curtain = tk.Frame(self.side, bg=C_SIDEBAR)

        target_w = getattr(self, "_sidebar_width", 195) or 195
        if self._sidebar_visible:
            # 펼치기 (Expand): 0 -> target_w
            start_w = 0
            end_w = target_w
            full_w = target_w
            if hasattr(self, "splitter_btn"):
                self.splitter_btn.config(text="◀")
                self.splitter_btn.place_forget()
                self.splitter_btn.place(in_=self.splitter, relx=0.5, rely=0.5, anchor="center", width=18, height=84)
                self.splitter_btn.lift()
            if hasattr(self, "side_toggle_btn"):
                self.side_toggle_btn.config(text="◀")
            self.status.set("사이드바 표시됨")
        else:
            # 접기 (Collapse): start_w -> 0
            cur_real_w = self.side.winfo_width()
            if cur_real_w > 40:
                target_w = cur_real_w
                self._sidebar_width = cur_real_w
            start_w = target_w
            end_w = 0
            full_w = target_w
            if hasattr(self, "splitter_btn"):
                self.splitter_btn.config(text="▶")
            if hasattr(self, "side_toggle_btn"):
                self.side_toggle_btn.config(text="▶")
            self.status.set("사이드바 숨김 (다시 열려면 ▶ 클릭 또는 기둥 드래그)")

        if hasattr(self, "side_inner"):
            self.side_inner.place_forget()
        self.side.pack_forget()
        self.side.place(x=0, y=0, width=start_w, relheight=1.0)
        self._sidebar_curtain.place(x=0, y=0, width=start_w, relheight=1.0)
        self._sidebar_curtain.lift()
        self.side.lift()

        duration = 0.22  # 220ms — 급하지 않고 고급스럽게 느껴지는 길이
        start_time = time.perf_counter()
        expanding = self._sidebar_visible

        def step():
            now = time.perf_counter()
            elapsed = now - start_time
            p = min(1.0, elapsed / duration)
            # Smoothstep (3p^2 - 2p^3): 양 끝에서 속도가 0으로 부드럽게 감속(Ease-In-Out)
            factor = p * p * (3.0 - 2.0 * p)
            cur_w = int(start_w + (end_w - start_w) * factor)

            if p >= 1.0 or (expanding and cur_w >= end_w) or (not expanding and cur_w <= 0):
                # _sidebar_anim_job은 여기서 바로 None으로 안 비우고 맨 아래에서
                # 비운다 — 아래 update_idletasks()가 이 블록에서 일어나는
                # pack()/config() 때문에 생기는 <Configure>를 곧바로 동기 처리하는데,
                # 그 시점에도 _on_chat_resize가 "애니메이션 진행 중"으로 보고
                # 자기 디바운스 타이머를 걸지 않게 하기 위함이다(중복 재렌더링·
                # 깜빡임 방지, v6.48).
                self.side.place_forget()
                self._sidebar_curtain.place_forget()
                if expanding:
                    self.side.config(width=end_w)
                    if hasattr(self, "side_inner"):
                        self.side_inner.place(x=0, y=0, width=end_w, relheight=1.0)
                    self.side.pack(side="left", fill="y", before=self.splitter)
                    self._sidebar_width = end_w
                else:
                    if hasattr(self, "side_inner"):
                        self.side_inner.place(x=0, y=0, width=full_w, relheight=1.0)
                    if hasattr(self, "splitter_btn"):
                        self.splitter_btn.place_forget()
                        self.splitter_btn.place(in_=self.splitter, x=0, rely=0.5, anchor="w", width=20, height=84)
                        self.splitter_btn.lift()
                self.root.update_idletasks()
                # side를 pack에 다시 넣거나 빼는 순간 채팅 캔버스 크기가 실제로
                # 바뀌어(애니메이션 시작·종료 각 1회) <Configure>가 발생하고,
                # 그게 _on_chat_resize의 120ms 디바운스 타이머를 건드린다. 그
                # 타이머를 안 지우면 바로 아래에서 다시 그리는 것과 별개로 약
                # 120ms 뒤에 한 번 더(중복) 다시 그려져서, 스크롤-맨아래 버튼
                # 같은 오버레이 위젯이 한 번 사라졌다 다시 나타나며 깜빡이는
                # 것처럼 보였다(v6.48). 중복 재렌더링을 막는다.
                if getattr(self, "_resize_job", None):
                    try:
                        self.root.after_cancel(self._resize_job)
                    except Exception:
                        pass
                    self._resize_job = None
                if self.current:
                    self._reload_chat(self.current, from_cache=True)
                self._sidebar_anim_job = None
                return

            self.side.place(x=0, y=0, width=cur_w, relheight=1.0)
            self._sidebar_curtain.place(x=0, y=0, width=cur_w, relheight=1.0)
            self._sidebar_anim_job = self.root.after(16, step)

        self._sidebar_anim_job = self.root.after(16, step)

    def _on_splitter_press(self, e):
        self._splitter_drag = {
            "x": e.x_root,
            "w": self.side.winfo_width() if self._sidebar_visible else 0,
            "was_hidden": not self._sidebar_visible
        }

    def _on_splitter_motion(self, e):
        if not getattr(self, "_splitter_drag", None):
            return
        dx = e.x_root - self._splitter_drag["x"]
        if self._splitter_drag["was_hidden"]:
            if dx > 15:
                self._toggle_sidebar()
                self._splitter_drag["was_hidden"] = False
                self._splitter_drag["x"] = e.x_root
                self._splitter_drag["w"] = self.side.winfo_width()
            return

        root_w = self.root.winfo_width()
        min_w = 130
        max_w = max(min_w + 50, int(root_w * 0.6))
        new_w = self._splitter_drag["w"] + dx

        if new_w < 70:
            if self._sidebar_visible:
                self._toggle_sidebar()
            return

        if not self._sidebar_visible:
            self._toggle_sidebar()

        target_w = max(min_w, min(max_w, new_w))
        self.side.config(width=target_w)
        if hasattr(self, "side_inner"):
            self.side_inner.place(x=0, y=0, width=target_w, relheight=1.0)
        self._sidebar_width = target_w
        self._custom_sidebar_w = target_w
        self._on_chat_resize(None)

    def _on_splitter_release(self, _e=None):
        self._splitter_drag = None

    # ---------- 대화방 헤더 버튼 제어 ----------
    def _hide_room_buttons(self):
        if hasattr(self, "room_more_btn"):
            self.room_more_btn.pack_forget()

    def _show_room_buttons(self):
        if hasattr(self, "room_more_btn"):
            self.room_more_btn.pack(side="right", padx=(4, 4), pady=10)

    def _on_header_rename_click(self):
        if self.current:
            self._rename_conversation_dialog(self.current)

    def _open_room_more_menu(self, x=None, y=None):
        if not self.current or not self.engine:
            return
        if x is None or y is None:
            try:
                x = self.room_more_btn.winfo_rootx()
                y = self.room_more_btn.winfo_rooty() + self.room_more_btn.winfo_height() + 4
            except tk.TclError:
                x, y = 100, 100
        is_grp = (self.current[0] == "grp")
        items = [
            ("대화방 이름 변경", lambda: self._rename_conversation_dialog(self.current)),
            ("대화 숨기기", lambda: self._hide_conversation(self.current)),
            (None, None),
            ("다운로드 폴더 열기", self._open_download_dir),
            ("다운로드 폴더 변경...", self._download_dir_dialog),
            ("다운로드 크기 조절...", self._max_file_size_dialog),
            (None, None),
            ("대화 기록 삭제", lambda: self._delete_conversation_history_dialog(self.current)),
        ]
        if is_grp:
            items.insert(2, ("멤버 보기 / 추가", self._open_member_dialog))
            items.append((None, None))
            items.append(("그룹 나가기", lambda: self._leave_group_dialog(self.current[1])))
        self._popup_menu(x, y, items)


    # ---------- 탭 ----------
    def _set_tab(self, tab):
        if self._tab == tab:
            return
        self._tab = tab
        for btn, active in ((self.tab_chat_btn, tab == "chat"), (self.tab_friend_btn, tab == "friend")):
            bg = C_ROWSEL if active else C_SIDEBAR
            fg = C_TEXT if active else C_MUTE
            btn.config(bg=bg, fg=fg)
            self._hover(btn, bg, C_HOVER)
        self._refresh_list()

    # ---------- 목록 (키 기반 diff 갱신 — destroy/재생성 최소화로 클릭 경합 방지) ----------
    def _refresh_list(self):
        if getattr(self, "_search_debounce_id", None) is not None:
            try:
                self.root.after_cancel(self._search_debounce_id)
            except Exception:
                pass
            self._search_debounce_id = None
        eng = self.engine
        if eng is None:
            return
        now = time.time()
        q = (self.search_var.get() or "").strip().lower()
        if self._search_ph:
            q = ""
        with eng.plock:
            peers = [dict(v) for v in eng.peers.values()]

        items = []
        if self._tab == "friend":
            for p in peers:
                key = ("dm", p["ip"], p["port"])
                if key in eng.hidden:
                    continue
                name = eng.get_alias(key) or p["name"] or p["ip"]
                if q and q not in name.lower() and q not in p["ip"].lower():
                    continue
                online = bool(p["last"]) and now - p["last"] < PEER_TIMEOUT
                category = eng.get_category(key) or "미분류"
                items.append({"key": key, "kind": "dm", "name": name, "online": online,
                             "peer": p, "category": category})

            def _cat_sort_key(c):
                return (1, "") if c == "미분류" else (0, c.lower())

            items.sort(key=lambda it: (_cat_sort_key(it["category"]),
                                       0 if it["online"] else 1, it["name"].lower()))
        else:
            with eng.glock:
                groups = [(gid, {"name": g["name"], "members": set(g["members"])})
                         for gid, g in eng.groups.items()]
            for p in peers:
                key = ("dm", p["ip"], p["port"])
                if key in eng.hidden:
                    continue
                name = eng.get_alias(key) or p["name"] or p["ip"]
                if q and q not in name.lower() and q not in p["ip"].lower():
                    continue
                online = bool(p["last"]) and now - p["last"] < PEER_TIMEOUT
                items.append({"key": key, "kind": "dm", "name": name, "online": online, "peer": p})
            for gid, g in groups:
                key = ("grp", gid)
                if key in eng.hidden:
                    continue
                name = g["name"] or "그룹"
                if q and q not in name.lower():
                    continue
                items.append({"key": key, "kind": "grp", "name": name, "online": True, "group": g})

            def sort_key(it):
                # 1순위: 읽지 않은 메시지 수신 방 우선 (0: 있음, 1: 없음)
                unread = 0 if self.unread.get(it["key"], 0) > 0 else 1
                # 2순위: 최근 대화 시각 최신순 (내림차순 -> -ts)
                rec = eng.get_last_history_record(it["key"])
                last_ts = float(rec.get("ts", 0) if rec else 0)
                # 3순위: 이름 가나다순
                return (unread, -last_ts, it["name"].lower())

            items.sort(key=sort_key)

        seen = set()
        for it in items:
            seen.add(it["key"])
            row = self._rows.get(it["key"])
            if row is None:
                row = self._mkrow(it)
                self._rows[it["key"]] = row
            self._update_row(row, it)
        for key in [k for k in self._rows if k not in seen]:
            self._rows.pop(key)["frame"].destroy()
        new_keys = [it["key"] for it in items]
        last_keys = getattr(self, "_last_packed_keys", None)
        last_tab = getattr(self, "_last_packed_tab", None)

        if self._tab == "friend" and items:
            new_cats = [it.get("category", "") for it in items]
            last_cats = getattr(self, "_last_packed_cats", None)
            order_changed = (new_keys != last_keys or new_cats != last_cats or self._tab != last_tab)
            if order_changed:
                self._last_packed_keys = new_keys
                self._last_packed_cats = new_cats
                self._last_packed_tab = self._tab
                for it in items:
                    self._rows[it["key"]]["frame"].pack_forget()
                for w in getattr(self, "_category_header_widgets", []):
                    try:
                        w.destroy()
                    except Exception:
                        pass
                self._category_header_widgets = []
                counts = {}
                for it in items:
                    cat = it.get("category", "미분류")
                    counts[cat] = counts.get(cat, 0) + 1
                last_cat = None
                for it in items:
                    cat = it.get("category", "미분류")
                    if cat != last_cat:
                        last_cat = cat
                        hdr = tk.Frame(self.pinner, bg=C_SIDEBAR)
                        hdr.pack(fill="x", padx=14, pady=(10, 2))
                        tk.Label(hdr, text=f"{last_cat} ({counts[last_cat]})", fg=C_MUTE, bg=C_SIDEBAR,
                                font=FONT_XS_PAD, anchor="w").pack(fill="x")
                        self._category_header_widgets.append(hdr)
                    self._rows[it["key"]]["frame"].pack(fill="x", pady=0)
        else:
            order_changed = (new_keys != last_keys or self._tab != last_tab)
            if order_changed:
                self._last_packed_keys = new_keys
                self._last_packed_cats = None
                self._last_packed_tab = self._tab
                for it in items:
                    self._rows[it["key"]]["frame"].pack_forget()
                for w in getattr(self, "_category_header_widgets", []):
                    try:
                        w.destroy()
                    except Exception:
                        pass
                self._category_header_widgets = []
                for it in items:
                    self._rows[it["key"]]["frame"].pack(fill="x", pady=0)

        # 전체 대화 통합 메시지 검색 결과 렌더링
        for w in getattr(self, "_search_result_widgets", []):
            try:
                w.destroy()
            except Exception:
                pass
        self._search_result_widgets = []

        # 검색어가 바뀌면 이전에 진행 중이던(아직 안 끝난) 검색 결과는 폐기한다 —
        # 세대 번호를 늘려서 나중에 도착하는 콜백이 "낡은 검색"임을 스스로 알게 함.
        self._search_gen = getattr(self, "_search_gen", 0) + 1
        if q and len(q) >= 1 and self._tab != "friend":
            self._start_async_log_search(q, self._search_gen)

        on = sum(1 for it in items if it["kind"] == "dm" and it["online"])
        count = len(items)
        if self._tab == "friend":
            if count == 0:
                self.cnt_lbl.config(text="표시할 동료가 없습니다" if q else "발견된 동료가 없습니다")
            else:
                self.cnt_lbl.config(text=f"접속 중 {on}명·전체 {count}명")
        elif count == 0:
            self.cnt_lbl.config(text="표시할 대화가 없습니다" if q else "연결된 대화가 없습니다")
        else:
            self.cnt_lbl.config(text=f"접속 중 {on}명·전체 {count}개")

        total_unread = sum(self.unread.values())
        if hasattr(self, "tab_chat_btn"):
            if total_unread > 0:
                self.tab_chat_btn.config(text=f"채팅 ({total_unread})")
            else:
                self.tab_chat_btn.config(text="채팅")

    def _start_async_log_search(self, q, gen):
        """전체 대화 로그 검색(디스크 I/O)을 백그라운드 스레드로 넘긴다 — 대화가 많이
        쌓인 상태에서 검색어를 입력할 때마다 GUI 스레드가 디스크를 읽느라 멈추는 문제가
        있었다. 결과는 self.q를 통해 GUI 스레드의 _pump 루프에 안전하게 되돌려 반영한다."""
        eng = self.engine
        if eng is None:
            return

        def _worker():
            try:
                results = eng.search_all_logs(q, limit=20)
            except Exception:
                results = []
            if hasattr(self, "q") and self.q:
                self.q.put({"ev": "search_results", "gen": gen, "q": q, "results": results})

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_search_results(self, gen, q, msg_results):
        if gen != getattr(self, "_search_gen", 0):
            return  # 검색어가 바뀐 뒤 뒤늦게 도착한 결과 — 버림
        if not self.root.winfo_exists():
            return
        if (self.search_var.get() or "").strip().lower() != q:
            return

        for w in getattr(self, "_search_result_widgets", []):
            try:
                w.destroy()
            except Exception:
                pass
        self._search_result_widgets = []

        if not msg_results:
            return

        hdr_f = tk.Frame(self.pinner, bg=C_SIDEBAR, pady=4)
        hdr_f.pack(fill="x", padx=10, pady=(6, 0))
        self._search_result_widgets.append(hdr_f)

        div = tk.Frame(hdr_f, bg=C_SEPAR, height=1)
        div.pack(fill="x", pady=(2, 6))

        lbl = tk.Label(hdr_f, text=f"메시지 검색 결과 ({len(msg_results)}건)",
                       fg=C_MUTE, bg=C_SIDEBAR, font=FONT_XS_PAD, anchor="w")
        lbl.pack(fill="x")

        for r in msg_results:
            row_f = tk.Frame(self.pinner, bg=C_SIDEBAR, padx=10, pady=5, cursor="hand2")
            row_f.pack(fill="x", pady=1)
            self._search_result_widgets.append(row_f)

            top_row = tk.Frame(row_f, bg=C_SIDEBAR)
            top_row.pack(fill="x")

            room_lbl = tk.Label(top_row, text=r["room_name"][:16], fg="#93c5fd",
                                bg=C_SIDEBAR, font=FONT_XS_PAD, anchor="w")
            room_lbl.pack(side="left")

            time_lbl = tk.Label(top_row, text=korea_time_str(r["ts"]), fg=C_MUTE,
                                bg=C_SIDEBAR, font=FONT_XS, anchor="e")
            time_lbl.pack(side="right")

            snip = f"{r['sender']}: {r['text']}".replace("\n", " ")[:36]
            snip_lbl = tk.Label(row_f, text=snip, fg=C_TEXT, bg=C_SIDEBAR,
                                font=FONT_XS, anchor="w")
            snip_lbl.pack(fill="x", pady=(2, 0))

            def _enter(e, f=row_f, t=top_row, rl=room_lbl, tl=time_lbl, sl=snip_lbl):
                for widget in (f, t, rl, tl, sl):
                    try: widget.config(bg=C_ROWHOVER)
                    except Exception: pass
            def _leave(e, f=row_f, t=top_row, rl=room_lbl, tl=time_lbl, sl=snip_lbl):
                for widget in (f, t, rl, tl, sl):
                    try: widget.config(bg=C_SIDEBAR)
                    except Exception: pass
            def _click(e, res=r):
                self._on_global_search_click(res)

            for w in (row_f, top_row, room_lbl, time_lbl, snip_lbl):
                w.bind("<Enter>", _enter)
                w.bind("<Leave>", _leave)
                w.bind("<Button-1>", _click)

    def _on_global_search_click(self, res):
        key = res.get("key")
        if not key:
            return
        self._select(key)
        rec_idx = res.get("rec_idx")
        if rec_idx is not None:
            self.root.after(120, lambda: self._scroll_to_match(rec_idx))
        self.status.set(f"'{res['room_name']}' 대화방 검색 위치로 이동")

    def _rec_preview_text(self, rec):
        # DM 파일 레코드는 "name" 필드에 파일명을 담는다(그룹은 "name"이 발신자라 "fname" 사용).
        if rec.get("kind") == "sticker":
            meta = stickers.STICKERS.get(rec.get("sticker_id"), {})
            return f"[이모티콘] {meta.get('name', '이모티콘')}"
        if rec.get("kind") == "file":
            tag = "사진" if rec.get("is_image") else "파일"
            return f"[{tag}] {rec.get('name', '')}"
        return rec.get("text", "")

    def _preview(self, key, p):
        last = self.engine.get_last_history_record(("dm", key[0], key[1]))
        if last:
            pre = "나: " if last.get("dir") == "out" else ""
            return (pre + self._rec_preview_text(last))[:26]
        return "연결 대기" if p.get("static") else "미연결"

    def _preview_group(self, gid):
        last = self.engine.get_last_history_record(("grp", gid))
        if last:
            pre = "나: " if last.get("mine") else f"{last.get('name', '')}: "
            if last.get("kind") == "sticker":
                meta = stickers.STICKERS.get(last.get("sticker_id"), {})
                tag_text = f"[이모티콘] {meta.get('name', '이모티콘')}"
            elif last.get("kind") == "file":
                tag = "[사진] " if last.get("is_image") else "[파일] "
                tag_text = tag + last.get("fname", "")
            else:
                tag_text = last.get("text", "")
            return (pre + tag_text)[:26]
        return "새 그룹"

    def _mkrow(self, it):
        row = tk.Frame(self.pinner, bg=C_SIDEBAR, cursor="hand2")
        av = tk.Canvas(row, width=38, height=42, bg=C_SIDEBAR, highlightthickness=0)
        av.pack(side="left", padx=(8, 6), pady=3)
        mid = tk.Frame(row, bg=C_SIDEBAR)
        mid.pack(side="left", fill="both", expand=True, pady=4)
        name_lbl = tk.Label(mid, fg=C_TEXT, bg=C_SIDEBAR, font=FONT_NAME, anchor="w")
        name_lbl.pack(anchor="w")
        prev_lbl = tk.Label(mid, fg=C_MUTE, bg=C_SIDEBAR, font=FONT_XS, anchor="w")
        prev_lbl.pack(anchor="w")
        right = tk.Frame(row, bg=C_SIDEBAR)
        right.pack(side="right", padx=(0, 8))
        badge = tk.Label(right, bg=C_BADGE, fg="white", font=FONT_BADGE, padx=5, pady=1)
        key = it["key"]
        row_ref = {"frame": row, "av": av, "mid": mid, "name": name_lbl, "prev": prev_lbl,
                  "right": right, "badge": badge, "key": key}

        def _click(_e=None, k=key):
            self._select(k)

        def _right_click(e, k=key):
            self._show_row_menu(k, e)

        def _set_bg(color):
            for w in (row, av, mid, name_lbl, prev_lbl, right):
                try:
                    w.config(bg=color)
                except tk.TclError:
                    pass
            # 아바타 원은 새 배경색에 맞춰 가장자리를 다시 섞어 그려야 매끄러움이 유지된다
            # (그냥 config(bg=)만 하면 이미 그려둔 이미지는 그대로라 가장자리가 예전
            # 배경색 기준으로 남아 살짝 어긋나 보인다).
            try:
                kind = row_ref.get("_av_kind")
                args = row_ref.get("_av_args")
                if kind == "dm" and args:
                    label, c, photo = args
                    self._avatar(av, label, c, av_photo=photo)
                    if row_ref.get("_av_online"):
                        av.create_oval(27, 27, 35, 35, fill=C_ONLINE, outline=color, width=1)
                elif kind == "grp" and args:
                    (members,) = args
                    self._avatar_group(av, members)
            except tk.TclError:
                pass

        def _on_enter(e, k=key):
            if self.current != k:
                _set_bg(C_ROWHOVER)

        def _on_leave(e, k=key):
            try:
                under = row.winfo_containing(e.x_root, e.y_root)
                if under in (row, av, mid, name_lbl, prev_lbl, right, badge):
                    return
            except tk.TclError:
                pass
            _set_bg(C_ROWSEL if self.current == k else C_SIDEBAR)

        for w in (row, av, mid, name_lbl, prev_lbl, right, badge):
            w.bind("<Button-1>", _click)
            w.bind("<Button-3>", _right_click)
            w.bind("<Enter>", _on_enter)
            w.bind("<Leave>", _on_leave)
        return row_ref

    def _get_conversation_name(self, key):
        if not key or not self.engine:
            return "대화"
        if key[0] == "grp":
            with self.engine.glock:
                g = self.engine.groups.get(key[1])
                return (g["name"] if g and g.get("name") else "그룹")
        elif key[0] == "dm":
            alias = self.engine.get_alias(key)
            if alias:
                return alias
            with self.engine.plock:
                p = self.engine.peers.get((key[1], key[2]))
                return (p["name"] if p and p.get("name") else key[1])
        return "대화"

    def _show_row_menu(self, key, event):
        is_dm = (key[0] == "dm")
        rename_label = "✏ 이름 변경 (별칭)" if (self._tab == "friend" or is_dm) else "✏ 대화방 이름 변경"
        hide_label = "👁 친구 목록에서 숨기기" if self._tab == "friend" else "👁 대화 숨기기"
        del_label = "🗑 대화 기록 삭제"
        x = getattr(event, "x_root", 100)
        y = getattr(event, "y_root", 100)
        items = [
            (rename_label, lambda: self._rename_conversation_dialog(key)),
        ]
        if self._tab == "friend" and is_dm:
            items.append(("📁 카테고리 지정", lambda: self._open_category_menu(key, x, y)))
        items.append((hide_label, lambda: self._hide_conversation(key)))
        items.append((del_label, lambda: self._delete_conversation_history_dialog(key)))
        if not is_dm and self._tab == "chat":
            items.append((None, None))
            items.append(("🚪 그룹 나가기", lambda: self._leave_group_dialog(key[1])))
        self._popup_menu(x, y, items)

    # ---------- 친구 카테고리(폴더) 지정 ----------
    def _open_category_menu(self, key, x, y):
        eng = self.engine
        if eng is None:
            return
        cur = eng.get_category(key)
        cats = eng.list_categories()
        items = []
        for c in cats:
            mark = "● " if c == cur else "    "
            items.append((f"{mark}{c}", lambda c=c: self._set_contact_category(key, c)))
        if cats:
            items.append((None, None))
        items.append(("+ 새 카테고리...", lambda: self._new_category_dialog(key)))
        if cur:
            items.append(("미분류로 이동", lambda: self._set_contact_category(key, "")))
        # 팝업 메뉴 두 개를 연달아 같은 클릭으로 열면 앞 메뉴를 닫는 bind_all이 뒷 메뉴를
        # 즉시 닫아버릴 수 있어(팝업 메뉴 구현 자체의 알려진 특성 — 다음 이벤트 루프
        # 틱까지 미루는 방식으로 대응), 한 틱 미뤄서 연다.
        self.root.after(1, lambda: self._popup_menu(x, y, items))

    def _set_contact_category(self, key, category):
        if self.engine:
            self.engine.set_category(key, category)
            self._refresh_list()


    def _clear_active_chat_view(self):
        """지금 열려 있던 대화방을 화면에서 완전히 비운다 — 대화 숨기기/그룹 나가기/
        대화 기록 삭제 등 "보던 방이 사라지는" 모든 경로에서 공통으로 쓰는 리셋.
        (예전에는 이 17줄이 app.py·dialogs.py 세 곳에 그대로 복사돼 있어서 버튼이나
        배너가 하나 늘 때마다 세 곳을 전부 고쳐야 했다.)"""
        self.current = None
        self._active_chat_key = None
        self._active_chat_records = None
        self.chat.delete("all")
        self.entry.delete("1.0", "end")
        self.entry.configure(state="disabled")
        self.send_btn.configure(state="disabled")
        self.attach_btn.configure(state="disabled")
        self.burn_btn.configure(state="disabled")
        self.emoji_btn.configure(state="disabled")
        if self._emoji_picker:
            self._emoji_picker.hide()
        self._pinned_notice = None
        self.pin_banner.pack_forget()
        self.ch_title.config(text="대화를 선택하세요")
        self.ch_sub.config(text="")
        self.member_btn.pack_forget()
        self._hide_room_buttons()
        self._show_empty("대화 상대를 선택하거나 상대를 연결해\n대화를 시작하세요")

    def _hide_conversation(self, key):
        if self.engine is None:
            return
        name = self._get_conversation_name(key)
        self.engine.hide_key(key)
        if self.current == key:
            self._clear_active_chat_view()
        self._refresh_list()
        self.status.set(f"'{name}' 대화를 숨겼습니다 (상단 ☰ 메뉴에서 언제든 다시 표시 가능)")



    def _update_row(self, row_ref, it):
        key = it["key"]
        selected = (self.current == key)
        bg = C_ROWSEL if selected else C_SIDEBAR
        if row_ref.get("_last_bg") != bg:
            row_ref["_last_bg"] = bg
            for w in (row_ref["frame"], row_ref["av"], row_ref["mid"], row_ref["name"],
                     row_ref["prev"], row_ref["right"]):
                w.config(bg=bg)
        if it["kind"] == "dm":
            p = it["peer"]
            name = it["name"]
            online = it["online"]
            is_away = (p.get("status") == "away")
            c = self._avacolor(name) if online else C_OFFLINE
            photo = self._get_avatar_photo(p.get("av"))
            av_state = (name, c, photo, is_away, online, bg)
            if row_ref.get("_av_state") != av_state:
                row_ref["_av_state"] = av_state
                self._avatar(row_ref["av"], self._initial(name), c, av_photo=photo)
                if online:
                    dot_c = C_AWAY if is_away else C_ONLINE
                    row_ref["av"].create_oval(27, 27, 35, 35, fill=dot_c, outline=bg, width=1)
            row_ref["name"].config(text=name[:17], fg=C_TEXT if online else C_MUTE,
                                   font=FONT_NAME if online else FONT_NAME_M)
            prev_txt = self._preview((key[1], key[2]), p)
            if row_ref.get("_last_prev") != prev_txt:
                row_ref["_last_prev"] = prev_txt
                row_ref["prev"].config(text=prev_txt)
            row_ref["_av_kind"] = "dm"
            row_ref["_av_args"] = (self._initial(name), c, photo)
            row_ref["_av_online"] = online
        else:
            g = it["group"]
            name = g["name"] or "그룹"
            members_tuple = tuple(sorted(g["members"]))
            av_state = (name, members_tuple, bg)
            if row_ref.get("_av_state") != av_state:
                row_ref["_av_state"] = av_state
                self._avatar_group(row_ref["av"], g["members"])
            row_ref["name"].config(text=name[:17], fg=C_TEXT, font=FONT_NAME)
            prev_txt = self._preview_group(key[1])
            if row_ref.get("_last_prev") != prev_txt:
                row_ref["_last_prev"] = prev_txt
                row_ref["prev"].config(text=prev_txt)
            row_ref["_av_kind"] = "grp"
            row_ref["_av_args"] = (g["members"],)
            row_ref["_av_online"] = False
        n = self.unread.get(key, 0)
        if n:
            row_ref["badge"].config(text=str(n))
            if not row_ref.get("_badge_packed"):
                row_ref["badge"].pack(anchor="e", pady=(2, 0))
                row_ref["_badge_packed"] = True
        else:
            if row_ref.get("_badge_packed"):
                row_ref["badge"].pack_forget()
                row_ref["_badge_packed"] = False

    # ---------- 선택/헤더 ----------
    def _select(self, key):
        eng = self.engine
        if eng is None:
            return
        self._hide_empty()
        self.current = key
        self.unread[key] = 0
        if key in eng.hidden:
            eng.unhide_key(key)
        if key[0] == "dm":
            _, ip, port = key
            eng.send_read_ack(ip, port)
            with eng.plock:
                p = eng.peers.get((ip, port)) or {}
                dname = eng.get_alias(key) or p.get("name") or ip
                online = bool(p.get("last")) and (time.time() - p.get("last", 0)) < PEER_TIMEOUT
                is_away = (p.get("status") == "away")
            status_txt = "  ·  자리비움" if is_away else "  ·  접속 중"
            status_fg = C_AWAY if is_away else C_ONLINE
            self.ch_title.config(text=dname)
            self.ch_sub.config(text=f"{ip}:{port}" + (status_txt if online else "  ·  대기"),
                               fg=status_fg if online else C_MUTE)
            self.member_btn.pack_forget()
            self._show_room_buttons()
        else:
            _, gid = key
            eng.send_group_read_ack(gid)
            with eng.glock:
                g = eng.groups.get(gid) or {"name": "그룹", "members": set()}
                gname = g["name"]
                n = len(g["members"])
            self.ch_title.config(text=gname)
            self.ch_sub.config(text=f"멤버 {n}명", fg=C_MUTE)
            self._show_room_buttons()
            self.member_btn.pack(side="right", padx=(4, 4), pady=10)
        self.entry.configure(state="normal", font=FONT_MSG)
        self.send_btn.configure(state="normal")
        self.attach_btn.configure(state="normal")
        self.burn_btn.configure(state="normal")
        self.emoji_btn.configure(state="normal")
        self._sync_burn_btn_visual()
        self.status.set("전송 준비 완료")
        self.entry.focus_set()
        self._reload_chat(key, force_bottom=True)
        self._refresh_list()
        self._update_pin_banner()
        self._hide_sticker_suggest()
        if self._emoji_picker:
            self._emoji_picker.hide()

    # ---------- 공지사항(카카오톡 스타일 고정 메시지) ----------
    def _update_pin_banner(self):
        eng = self.engine
        notice = eng.get_pinned_notice(self.current) if (eng and self.current) else None
        self._pinned_notice = notice
        if notice:
            self.pin_banner.set_notice(notice.get("sender", ""), notice.get("text", ""))
            self.pin_banner.pack(fill="x", before=self.chat_wrap)
        else:
            self.pin_banner.pack_forget()

    def _set_message_as_notice(self, mid, text, sender, ts):
        if not self.engine or not self.current or not mid:
            return
        self.engine.set_pinned_notice(self.current, mid, text, sender, ts=ts)
        self._update_pin_banner()
        self.status.set("공지로 등록했습니다")

    def _unpin_current_notice(self):
        if not self.engine or not self.current:
            return
        self.engine.clear_pinned_notice(self.current)
        self._update_pin_banner()
        self.status.set("공지를 해제했습니다")

    def _on_pin_banner_click(self):
        notice = self._pinned_notice
        if not notice:
            return
        mid = notice.get("mid")
        records = getattr(self, "_active_chat_records", None) or []
        for idx, rec in enumerate(records):
            if rec.get("mid") and rec.get("mid") == mid:
                self._scroll_to_match(idx)
                return
        self.status.set("공지로 등록된 원본 메시지를 찾을 수 없습니다(삭제되었을 수 있음)")

    # ---------- 채팅 캔버스 렌더 ----------


    # ---------- 맨 아래로 스크롤 플로팅 버튼 ----------









    # ---------- 파일·이미지 말풍선 ----------















    # ---------- 대화방 내 검색 (Ctrl+F) ----------








    # ---------- 인용 답장 제어 ----------
    def _start_reply(self, sender_name, snippet):
        self._reply_target = {"name": sender_name, "text": snippet[:60]}
        if hasattr(self, "reply_banner"):
            self.reply_banner.set_target(sender_name, snippet)
            self.reply_banner.pack(fill="x", side="top", pady=(0, 4), before=self.input_bar)
        self.entry.focus_set()

    def _cancel_reply(self):
        self._reply_target = None
        if hasattr(self, "reply_banner"):
            self.reply_banner.pack_forget()

    # ---------- 입력/전송 ----------
    def _entry_key(self, e):
        popup = getattr(self, "_mention_popup", None)
        if popup and popup.is_visible():
            if e.keysym == "Up":
                popup.select_prev()
                return "break"
            elif e.keysym == "Down":
                popup.select_next()
                return "break"
            elif e.keysym in ("Return", "Tab"):
                sel = popup.get_selected()
                if sel:
                    self._on_mention_select(sel)
                    return "break"
            elif e.keysym == "Escape":
                popup.hide()
                return "break"
        if e.keysym == "Return" and not (int(e.state) & 0x0001):
            self._send()
            return "break"
        return None

    def _entry_resize(self, _e=None):
        # <KeyRelease>는 물리적 키 입력에만 반응한다 — Windows 이모지 패널(Win+.)이나
        # 일부 IME 완성 결과처럼 진짜 키 이벤트 없이 텍스트가 꽂히는 입력 방식은 못
        # 잡아서, 그렇게 넣은 이형 문자 선택자가 실시간 살균을 그냥 통과했다(실측
        # 확인 — 붙여넣기는 <<Paste>>로 별도 방어돼 있어 문제없었지만 타이핑/이모지
        # 패널 경로만 새고 있었다). Text 위젯 내용이 "어떻게" 바뀌든 다 잡아내는
        # <<Modified>> 가상 이벤트로 바꿔서 원천 차단한다.
        try:
            self.entry.edit_modified(False)
        except tk.TclError:
            pass
        raw = self.entry.get("1.0", "end-1c")
        cleaned = sanitize_chat_text(raw)
        if raw != cleaned:
            cursor = self.entry.index("insert")
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", cleaned)
            try:
                self.entry.mark_set("insert", cursor)
            except Exception:
                pass
        self.entry.update_idletasks()
        line_c = int(self.entry.index("end-1c").split(".")[0])
        extra = 1 if self.entry.get("1.0", "end-1c").endswith("\n") else 0
        self.entry.configure(height=max(1, min(4, line_c + extra)))
        self._layout_input_bar()
        self._check_mention_trigger()
        self._check_sticker_suggest()

    def _check_sticker_suggest(self):
        """카카오톡처럼, 입력 중인 문구에 등록된 키워드가 있으면 어울리는 스티커를
        입력창 위에 추천해 보여준다(클릭 시 즉시 전송, 타이핑 중인 글은 그대로 유지)."""
        if not self.current or not self.engine:
            self._hide_sticker_suggest()
            return
        try:
            text = self.entry.get("1.0", "end-1c")
        except tk.TclError:
            text = ""
        matches = stickers.match_keywords(text)
        if not matches:
            self._hide_sticker_suggest()
            return
        self._show_sticker_suggest(matches[:4])

    def _show_sticker_suggest(self, sticker_ids):
        # 같은 추천 목록이면(예: "고"/"고마"/"고마워" 전부 '감사' 스티커 하나로 매칭)
        # 매 타자마다 캔버스를 부수고 다시 그리지 않는다 — 그 자체로도 낭비지만,
        # destroy+재생성 사이 순간적으로 깜빡이는 게 더 거슬린다.
        sids = tuple(sticker_ids)
        if getattr(self, "_cur_suggest_sids", None) == sids and self.sticker_suggest.winfo_manager():
            return
        self._cur_suggest_sids = sids
        for w in self.sticker_suggest.winfo_children():
            w.destroy()
        self._sticker_suggest_imgs = []
        tk.Label(self.sticker_suggest, text="이 이모티콘 보낼까요?", bg=C_MAIN, fg=C_MUTE,
                font=FONT_XS).pack(side="left", padx=(2, 8))
        for sid in sticker_ids:
            cv = tk.Canvas(self.sticker_suggest, width=44, height=44, bg=C_MAIN,
                          highlightthickness=0, cursor="hand2")
            cv.pack(side="left", padx=3)
            stickers.draw_sticker(cv, 22, 22, 20, sid, self._sticker_suggest_imgs)
            cv.bind("<Button-1>", lambda e, s=sid: self._send_sticker(s))
        self.sticker_suggest.pack(fill="x", side="top", pady=(0, 4), before=self.input_bar)

    def _hide_sticker_suggest(self):
        self.sticker_suggest.pack_forget()
        for w in self.sticker_suggest.winfo_children():
            w.destroy()
        self._sticker_suggest_imgs = []
        self._cur_suggest_sids = None

    def _send_sticker(self, sticker_id):
        eng = self.engine
        if eng is None or not self.current:
            return
        info = stickers.STICKERS.get(sticker_id)
        name = info["name"] if info else sticker_id
        burn_sec = self._burn_mode.get(self.current, 0)
        unread_count = None
        if self.current[0] == "grp":
            with eng.glock:
                g = eng.groups.get(self.current[1])
                unread_count = len(g["members"]) if g else 0
        self._append_sticker(True, "나", sticker_id, time.time(), unread=(self.current[0] == "dm"),
                             burn_sec=burn_sec, unread_count=unread_count)
        if self.current[0] == "dm":
            _, ip, port = self.current
            eng.send_message(ip, port, "", sticker_id=sticker_id, burn_sec=burn_sec)
        else:
            _, gid = self.current
            eng.send_group_message(gid, "", sticker_id=sticker_id, burn_sec=burn_sec)
        self.status.set(f"{name} 스티커를 보냈습니다")
        eng.add_recent_sticker(sticker_id)
        self._hide_sticker_suggest()
        self._refresh_list()

    def _check_mention_trigger(self):
        popup = getattr(self, "_mention_popup", None)
        if not popup or not self.current or not self.engine:
            return
        try:
            line_txt = self.entry.get("insert linestart", "insert")
        except tk.TclError:
            return
        if "@" not in line_txt:
            popup.hide()
            return
        token = line_txt.rsplit("@", 1)[1]
        if " " in token:
            popup.hide()
            return

        candidates = []
        if self.current[0] == "grp":
            gid = self.current[1]
            with self.engine.glock:
                g = self.engine.groups.get(gid) or {}
                members = list(g.get("members", []))
            with self.engine.plock:
                pnames = {k: v.get("name", "") for k, v in self.engine.peers.items()}
            for (ip, port) in members:
                mname = pnames.get((ip, port)) or ip
                if mname and mname != self.engine.name and mname not in candidates:
                    candidates.append(mname)
        # 1:1 대화는 멘션 대상이 상대방 한 명뿐이라 지목할 의미가 없고, 이메일
        # 주소처럼 "@"를 텍스트로 입력할 때 오히려 방해가 되므로 팝업을 안 띄운다.

        if token:
            matches = [c for c in candidates if token.lower() in c.lower()]
        else:
            matches = candidates

        if matches:
            try:
                bbox = self.entry.bbox("insert")
                bx = bbox[0] if bbox else 10
                x = self.entry.winfo_rootx() + bx
                y = self.entry.winfo_rooty()
                popup.show(x, y, matches)
            except Exception:
                popup.hide()
        else:
            popup.hide()

    def _on_mention_select(self, name):
        # 멘션 후보 이름은 상대가 네트워크로 알려온 값이라, 직접 타이핑할 때와 달리
        # <KeyRelease> 기반 실시간 살균(_entry_resize)을 안 거치고 곧장 삽입된다 —
        # 여기서 다시 한 번 살균해 이형 문자 선택자 등으로 인한 폰트 두부 상자를 막는다.
        name = sanitize_chat_text(name)
        try:
            line_txt = self.entry.get("insert linestart", "insert")
            if "@" in line_txt:
                prefix, token = line_txt.rsplit("@", 1)
                del_len = len(token) + 1
                start_idx = f"insert - {del_len} chars"
                self.entry.delete(start_idx, "insert")
                self.entry.insert("insert", f"@{name} ")
            else:
                self.entry.insert("insert", f"@{name} ")
        except Exception:
            self.entry.insert("insert", f"@{name} ")
        if getattr(self, "_mention_popup", None):
            self._mention_popup.hide()
        self.entry.focus_set()

    def _layout_input_bar(self, _e=None):
        """둥근 알약 모양 입력 바 배경을 그리고, 첨부/입력창/전송 버튼을 그 안에 배치한다.
        입력창 줄 수(1~4줄)나 창 너비가 바뀔 때마다 다시 호출된다."""
        cv = getattr(self, "input_bar", None)
        if cv is None or not cv.winfo_exists():
            return
        w = cv.winfo_width()
        if w <= 1:
            return
        cv.update_idletasks()
        entry_h = self.entry.winfo_reqheight()
        btn_h = max(self.attach_btn.winfo_reqheight(), self.send_btn.winfo_reqheight())
        h = max(entry_h, btn_h) + 6
        if int(float(cv.cget("height") or 0)) != h:
            cv.config(height=h)
        cv.delete("bar_bg")
        round_rect(cv, 1, 1, max(3, w - 1), max(3, h - 1), r=min(18, h / 2),
                  fill=C_CARD, outline=C_BORDER, tags="bar_bg")
        cv.tag_lower("bar_bg")
        attach_w = self.attach_btn.winfo_reqwidth()
        burn_w = self.burn_btn.winfo_reqwidth()
        emoji_w = self.emoji_btn.winfo_reqwidth()
        send_w = self.send_btn.winfo_reqwidth()
        cv.coords(self._attach_win, 4, h / 2)
        cv.coords(self._burn_win, 4 + attach_w + 4, h / 2)
        cv.coords(self._emoji_win, 4 + attach_w + 4 + burn_w + 4, h / 2)
        cv.coords(self._send_win, w - 4, h / 2)
        entry_x = attach_w + burn_w + emoji_w + 14
        entry_w = max(20, w - attach_w - burn_w - emoji_w - send_w - 24)
        cv.coords(self._entry_win, entry_x, h / 2)
        cv.itemconfigure(self._entry_win, width=entry_w)

    def _on_window_paste(self, e=None):
        w = self.root.focus_get()
        # 포커스가 공지창 등 자식 Toplevel(별도 대화상자) 안에 있으면 그 안의 위젯이
        # 자체적으로 붙여넣기를 처리해야 한다 — 여기서 가로채면 다이얼로그의 tk.Text
        # 입력칸에 붙여넣은 파일/텍스트가 엉뚱하게 메인 창의 현재 대화방으로 전송된다.
        if w is not None:
            try:
                if w.winfo_toplevel() != self.root:
                    return None
            except tk.TclError:
                return None
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Text)) and w != self.entry:
            return None
        if w == self.entry:
            return None  # self.entry는 <<Paste>>로 처리됨
        return self._on_entry_paste(e)

    def _on_entry_paste(self, _e=None):
        if not self.current:
            self.status.set("왼쪽 목록에서 대화 상대를 먼저 선택하세요")
            return "break"
        if not self.engine:
            return "break"

        # 1. 클립보드에 파일 목록이 있는 경우 (탐색기 복사, Windows 캡처 도구 ScreenClip 등)
        files = get_clipboard_files()
        if files:
            def _is_temp_screenshot(fpath):
                return (
                    "screenclip" in fpath.lower()
                    or "tempstate" in fpath.lower()
                    or "temp" in os.path.dirname(fpath).lower()
                    or (os.path.basename(fpath).startswith("{") and os.path.basename(fpath).endswith("}.png"))
                )

            # 캡처 도구가 만든 임시 스크린샷은 원래 취지(Ctrl+V로 캡처한 화면을
            # 바로 보내기)대로 확인 없이 전송한다. 하지만 탐색기에서 그냥 "복사"한
            # 일반 파일은, 텍스트를 붙여넣으려다 실수로 Ctrl+V를 눌러도 확인 없이
            # 즉시 상대에게 전송돼버리는 문제가 있었다 — 그런 경우만 한 번 확인받는다.
            regular_files = [f for f in files if os.path.isfile(f) and not _is_temp_screenshot(f)]
            if regular_files:
                names = "\n".join(f"· {os.path.basename(f)}" for f in regular_files[:10])
                more = f"\n...외 {len(regular_files) - 10}개" if len(regular_files) > 10 else ""
                if not self._embed_confirm(
                        "파일 전송 확인",
                        f"클립보드에 복사된 파일을 상대에게 전송할까요?\n\n{names}{more}"):
                    return "break"
            sent_any = False
            for fpath in files:
                if os.path.isfile(fpath):
                    is_temp = _is_temp_screenshot(fpath)
                    send_path = fpath
                    if is_temp:
                        try:
                            ext = os.path.splitext(fpath)[1] or ".png"
                            os.makedirs(self.engine.download_dir, exist_ok=True)
                            friendly_name = f"screenshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000}{ext}"
                            safe_copy = os.path.join(self.engine.download_dir, friendly_name)
                            shutil.copyfile(fpath, safe_copy)
                            send_path = safe_copy
                        except Exception:
                            send_path = fpath
                    if self._send_file_path(send_path):
                        sent_any = True
            if sent_any:
                self.status.set(f"클립보드의 파일({len(files)}개)을 전송했습니다")
                return "break"

        # 2. 클립보드에 순수 메모리 이미지(브라우저 이미지 복사, DIB 등)가 있는 경우
        img_bytes, ext = get_clipboard_image_bytes()
        if img_bytes:
            try:
                os.makedirs(self.engine.download_dir, exist_ok=True)
                friendly_name = f"screenshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000}{ext or '.png'}"
                tmp_path = os.path.join(self.engine.download_dir, friendly_name)
                with open(tmp_path, "wb") as f:
                    f.write(img_bytes)
                if self._send_file_path(tmp_path):
                    self.status.set("클립보드 이미지를 전송했습니다")
                    return "break"
            except Exception:
                pass

        # 3. 텍스트 클립보드 내용 확인
        try:
            content = self.root.clipboard_get()
        except (tk.TclError, Exception):
            return "break"

        # 사용자가 파일 경로(따옴표 포함 가능)를 복사해 붙여넣었을 때,
        # 해당 경로가 실제 파일이면 경로 문자열 대신 파일 자체를 전송
        raw_strip = content.strip().strip('"').strip("'")
        if os.path.isfile(raw_strip):
            if self._send_file_path(raw_strip):
                self.status.set(f"파일('{os.path.basename(raw_strip)}')을 전송했습니다")
                return "break"

        # 4. 일반 텍스트 붙여넣기
        cleaned = sanitize_chat_text(content)
        try:
            sel_first = self.entry.index("sel.first")
            sel_last = self.entry.index("sel.last")
            self.entry.delete(sel_first, sel_last)
        except tk.TclError:
            pass
        self.entry.insert("insert", cleaned)
        self._entry_resize()
        return "break"

    def _send(self, _e=None):
        eng = self.engine
        if eng is None:
            return "break"
        if not self.current:
            self.status.set("왼쪽 목록에서 대화 상대를 선택하세요")
            return "break"
        text = sanitize_chat_text(self.entry.get("1.0", "end-1c")).strip()
        if not text:
            return "break"
        self.entry.delete("1.0", "end")
        self._entry_resize()
        reply = getattr(self, "_reply_target", None)
        self._cancel_reply()
        burn_sec = self._burn_mode.get(self.current, 0)
        unread_count = None
        if self.current[0] == "grp":
            with eng.glock:
                g = eng.groups.get(self.current[1])
                unread_count = len(g["members"]) if g else 0
        self._append_bubble(True, "나", text, time.time(), reply=reply, unread=(self.current[0] == "dm"),
                            burn_sec=burn_sec, unread_count=unread_count)
        if self.current[0] == "dm":
            _, ip, port = self.current
            eng.send_message(ip, port, text, reply=reply, burn_sec=burn_sec)
        else:
            _, gid = self.current
            eng.send_group_message(gid, text, reply=reply, burn_sec=burn_sec)
        self.status.set("전송 중…")
        self._refresh_list()
        return "break"

    # ---------- 파일 전송(GUI) ----------
    def _send_file_path(self, path):
        if self.engine is None or not self.current:
            return False
        if not path or not os.path.exists(path):
            return False
        try:
            size = os.path.getsize(path)
        except OSError:
            self._embed_alert("파일 오류", "파일을 읽을 수 없습니다.", kind="error")
            return False
        if size > self.engine.max_file_size:
            self._embed_alert(
                "파일이 너무 큼",
                f"최대 {self.engine.max_file_size // (1024 * 1024)}MB까지 보낼 수 있습니다.\n"
                f"선택한 파일: {self._human_size(size)}", kind="warning")
            return False
        try:
            fid = self.engine.send_file(self.current, path)
        except ValueError as e:
            self._embed_alert("파일 전송 불가", str(e), kind="warning")
            return False
        if not fid:
            self._embed_alert("파일 오류", "파일을 읽을 수 없습니다.", kind="error")
            return False
        name = os.path.basename(path)
        self._pending_sends[fid] = {
            "name": name, "size": size, "path": path,
            "is_image": os.path.splitext(name)[1].lower() in ALL_IMAGE_EXTS, "target": self.current,
        }
        self.status.set(f"{name} 전송 준비 중…")
        return True

    # ---------- 이모지 피커 ----------
    def _get_recent_stickers(self):
        if not self.engine:
            return []
        try:
            return self.engine.get_recent_stickers()
        except Exception:
            return []

    def _maybe_hide_emoji_picker(self, _e=None):
        if self._emoji_picker and self._emoji_picker.is_visible():
            self._emoji_picker.hide()

    def _get_emoji_picker(self):
        # 이름은 EmojiPicker/emoji_btn 그대로지만(다른 곳과의 연결부를 안 건드리려고
        # 유지), v6.25부터는 스티커 전용 패널이다 — 표준 유니코드 이모지 지원은
        # 제거했다("이모지를 전부 스티커로 바꿔달라"는 요청). 생성 자체를 처음
        # 눌렀을 때로 미루는 지연 초기화는 그대로 유지 — 창이 뜨기 전에는 이
        # 패널을 만들 이유가 없다.
        if self._emoji_picker is None:
            self._emoji_picker = EmojiPicker(
                self._chat_row, self.input_bar,
                get_recent=self._get_recent_stickers,
                on_select_sticker=self._send_sticker,
            )
        return self._emoji_picker

    def _toggle_emoji_picker(self):
        if not self.current:
            return
        picker = self._get_emoji_picker()
        if picker.is_visible():
            picker.hide()
        else:
            picker.show()

    # ---------- 자동 폭파(Self-Destruct) 타이머 ----------
    _BURN_OPTIONS = [("끄기", 0), ("30초 후 삭제", 30), ("5분 후 삭제", 300), ("1시간 후 삭제", 3600), ("24시간 후 삭제", 86400)]

    def _open_burn_menu(self):
        if not self.current:
            return
        try:
            x = self.burn_btn.winfo_rootx()
            y = self.burn_btn.winfo_rooty() - 4
        except tk.TclError:
            return
        cur = self._burn_mode.get(self.current, 0)
        items = []
        for label, secs in self._BURN_OPTIONS:
            prefix = "✓ " if secs == cur else "　 "
            items.append((prefix + label, lambda s=secs: self._set_burn_mode(s)))
        self._popup_menu(x, y - 170, items)

    def _set_burn_mode(self, seconds):
        if not self.current:
            return
        if seconds:
            self._burn_mode[self.current] = seconds
            if self.current[0] == "grp":
                self.status.set(f"이 그룹방에서 보낼 메시지는 전송 후 {self._burn_label(seconds)} 뒤 자동 삭제됩니다")
            else:
                self.status.set(f"이 대화방에서 보내는 메시지는 상대가 읽은 뒤 {self._burn_label(seconds)} 뒤 자동 삭제됩니다")
        else:
            self._burn_mode.pop(self.current, None)
            self.status.set("자동 폭파 타이머를 껐습니다")
        self._sync_burn_btn_visual()

    def _sync_burn_btn_visual(self):
        # PillButton은 진짜 tk.Button이 아니라 Canvas에 직접 그리는 위젯이라
        # config(bg=...)로는 알약 색이 안 바뀐다 — bg_color/hover_color를 바꾸고
        # 다시 그려야 한다.
        armed = bool(self.current and self._burn_mode.get(self.current))
        if armed:
            self.burn_btn.bg_color, self.burn_btn.hover_color = C_ME, C_ME_D
        else:
            self.burn_btn.bg_color, self.burn_btn.hover_color = "#374151", "#4b5563"
        self.burn_btn._draw()

    def _attach_file(self):
        if self.engine is None or not self.current:
            self.status.set("먼저 대화 상대를 선택하세요")
            return
        path = filedialog.askopenfilename(parent=self.root, title="보낼 파일 선택")
        if path:
            self._send_file_path(path)


    def _on_file_progress(self, ev):
        p = self._pending_sends.get(ev.get("fid"))
        if not p:
            return
        self.status.set(f"{p['name']} 전송 중… {ev['sent']}/{ev['total']}")

    def _on_file_sent(self, ev):
        p = self._pending_sends.pop(ev.get("fid"), None)
        if not p:
            return
        key = p["target"]
        # 상대의 [파일전송 다운로드 최대크기] 설정이 이 파일보다 작아서 거절된
        # 경우 — 예전엔 그냥 "네트워크 문제"로만 안내돼 원인을 알 수 없었다(v6.48).
        if ev.get("reason") == "too_large":
            max_mb = ev.get("max_mb") or 0
            fail_msg = (f"{p['name']} 전송 실패 — 상대방의 파일 수신 최대 크기 설정"
                       f"({max_mb}MB)보다 파일이 커서 거절되었습니다.")
        else:
            fail_msg = f"{p['name']} 전송 실패 — 상대가 꺼져 있거나 네트워크 문제일 수 있습니다."
        if self.current == key:
            rec = {"mine": True, "label": "나", "ts": time.time(), "kind": "file",
                  "fname": p["name"], "size": p["size"], "path": p["path"], "is_image": p["is_image"]}
            self._append_active_record(rec)
            self._draw_record(rec)
            if not ev.get("ok"):
                self._draw_fail(fail_msg)
            self._finish_render()
        # 그룹 파일 전송은 멤버 수만큼 개별 전송이라 일부만 성공할 수 있다 —
        # sent_count/total_count가 있으면(그룹 전송) 부분 성공도 구분해 보여준다.
        sent_count = ev.get("sent_count")
        total_count = ev.get("total_count")
        if sent_count is not None and total_count and total_count > 1:
            if sent_count == total_count:
                self.status.set(f"{p['name']} 전송 완료 ({sent_count}/{total_count}명)")
            elif sent_count > 0:
                self.status.set(f"{p['name']} 일부 전송 완료 ({sent_count}/{total_count}명 — 오프라인이거나 실패한 멤버 있음)")
            else:
                self.status.set(f"{p['name']} 전송 실패 (0/{total_count}명)")
        elif ev.get("ok"):
            self.status.set(f"{p['name']} 전송 완료")
        else:
            self.status.set(fail_msg)
        self._refresh_list()

    def _on_file_recv(self, ev):
        key = ("dm", ev["peer"][0], ev["peer"][1])
        with self.engine.plock:
            peer = self.engine.peers.get((ev["peer"][0], ev["peer"][1]))
        name = self.engine.get_alias(key) or (peer or {}).get("name") or key[1]
        if key in self.engine.hidden:
            self.engine.unhide_key(key)
        if self.current == key:
            rec = {"mine": False, "label": name, "ts": ev.get("ts"), "kind": "file",
                  "fname": ev["name"], "size": ev["size"], "path": ev["path"], "is_image": ev["is_image"],
                  "state": "done"}
            self._append_active_record(rec)
            self._draw_record(rec)
            self._finish_render()
            self.status.set(f"{name} 님이 파일을 보냈습니다")
        else:
            self.unread[key] = self.unread.get(key, 0) + 1
        self._refresh_list()
        self._notify_if_needed(name, f"파일: {ev['name']}", key)
        self._play_notify_sound()

    def _on_gfile_recv(self, ev):
        gid = ev["gid"]
        key = ("grp", gid)
        who = ev.get("who") or {}
        name = who.get("name") or "그룹원"
        if key in self.engine.hidden:
            self.engine.unhide_key(key)
        if self.current == key:
            rec = {"mine": False, "label": name, "ts": ev.get("ts"), "kind": "file",
                  "fname": ev["name"], "size": ev["size"], "path": ev["path"], "is_image": ev["is_image"],
                  "state": "done"}
            self._append_active_record(rec)
            self._draw_record(rec)
            self._finish_render()
            self.status.set(f"{name} 님이 파일을 보냈습니다")
        else:
            self.unread[key] = self.unread.get(key, 0) + 1
        self._refresh_list()
        with self.engine.glock:
            g = self.engine.groups.get(gid)
            gname = g["name"] if g else "그룹"
        self._notify_if_needed(f"{gname} · {name}", f"파일: {ev['name']}", key)
        self._play_notify_sound()

    def _on_msg(self, ev):
        key = ("dm", ev["peer"][0], ev["peer"][1])
        name = self.engine.get_alias(key) or ev.get("name") or key[1]
        sticker_id = ev.get("sticker_id")
        if key in self.engine.hidden:
            self.engine.unhide_key(key)
        if self.current == key:
            if sticker_id:
                self._append_sticker(False, name, sticker_id, ev.get("ts"),
                                     burn_sec=ev.get("burn_sec", 0))
            else:
                self._append_bubble(False, name, ev["text"], ev.get("ts"), reply=ev.get("reply"),
                                    burn_sec=ev.get("burn_sec", 0))
            self.status.set(f"{name} 님 메시지 도착")
            if self.engine:
                self.engine.send_read_ack(ev["peer"][0], ev["peer"][1], mid=ev.get("mid", ""))
        else:
            self.unread[key] = self.unread.get(key, 0) + 1
        self._refresh_list()
        is_mention = ev.get("is_mention")
        title = f"[멘션] {name}" if is_mention else name
        notify_text = self._sticker_notify_text(sticker_id) if sticker_id else ev["text"]
        self._notify_if_needed(title, notify_text, key)
        self._play_notify_sound()

    def _on_gmsg(self, ev):
        gid = ev["gid"]
        key = ("grp", gid)
        who = ev.get("who") or {}
        name = who.get("name") or "그룹원"
        sticker_id = ev.get("sticker_id")
        if key in self.engine.hidden:
            self.engine.unhide_key(key)
        if self.current == key:
            if sticker_id:
                self._append_sticker(False, name, sticker_id, ev.get("ts"),
                                     burn_sec=ev.get("burn_sec", 0))
            else:
                self._append_bubble(False, name, ev["text"], ev.get("ts"), reply=ev.get("reply"),
                                    burn_sec=ev.get("burn_sec", 0))
            self.status.set(f"{name} 님 메시지 도착")
            if self.engine:
                self.engine.send_group_read_ack(gid, mid=ev.get("mid", ""))
        else:
            self.unread[key] = self.unread.get(key, 0) + 1
        self._refresh_list()
        with self.engine.glock:
            g = self.engine.groups.get(gid)
            gname = g["name"] if g else "그룹"
        is_mention = ev.get("is_mention")
        title = f"[멘션] {gname} · {name}" if is_mention else f"{gname} · {name}"
        notify_text = self._sticker_notify_text(sticker_id) if sticker_id else ev["text"]
        self._notify_if_needed(title, notify_text, key)
        self._play_notify_sound()

    @staticmethod
    def _sticker_notify_text(sticker_id):
        info = stickers.STICKERS.get(sticker_id)
        return f"{info['name']} 스티커를 보냈습니다" if info else "스티커를 보냈습니다"

    def _on_sent(self, ev):
        key = ("dm", ev["ip"], ev["port"])
        if self.current != key:
            return
        if ev["ok"]:
            self.status.set("전송 완료")
        else:
            self.status.set("전송 실패 (대기열 저장됨)")
            self._draw_fail(f"전송 지연 — 상대가 오프라인이거나 네트워크 대기 중입니다. 상대 접속 시 자동 재전송됩니다.")
            self._finish_render()

    def _on_gsent(self, ev):
        key = ("grp", ev["gid"])
        if self.current != key:
            return
        if ev.get("ok"):
            self.status.set("전송 완료")
            return
        self._draw_fail(f"{ev.get('ip', '')}:{ev.get('port', '')} 로 전송 실패 — 상대가 꺼져 있을 수 있습니다.")
        self._finish_render()

    # ---------- 알림 ----------
    def _notify_if_needed(self, title, text, key):
        # 알림은 "랜톡 창을 지금 보고 있지 않을 때"만 떠야 한다 — 이전에는 포커스가
        # 있어도 "지금 보고 있는 대화방과 다르면" 알림이 떴는데, 이러면 랜톡을 활발히
        # 쓰는 중에도(예: B와 대화하다가 그룹에 새 글이 오면) 팝업이 튀어나와 방해가
        # 됐다. 지금 보고 있는 대화방이 무엇이든, 창 자체가 포커스돼 있으면 무조건
        # 건너뛴다(최소화했거나 다른 창을 보고 있을 때만 알림).
        try:
            focused = self.root.focus_displayof() is not None
        except Exception:
            focused = True
        if focused:
            return
        self._last_notify_key = key  # 알림을 클릭했을 때 이동할 대화방
        self._show_toast(title, text, key)
        if self._notifier:
            # 트레이 풍선/액션 센터 토스트(Shell_NotifyIcon NIM_MODIFY)는 더 이상
            # 띄우지 않는다 — 위 _show_toast()의 인앱 팝업과 내용이 겹쳐서 화면에
            # 알림이 두 개(윈도 토스트 + 인앱 토스트) 동시에 뜨는 중복 문제가 있었다.
            # 작업표시줄 깜빡임(flash)만 그대로 유지한다.
            self._notifier.flash()

    def _show_toast(self, title, text, key):
        """트레이 풍선/액션 센터 토스트는 클릭해도 Windows가 클릭 콜백을 앱에
        전달하지 않는 경우가 실제로 있다(OS 자체의 알려진 한계 — 트레이 아이콘
        직접 클릭은 되는데 토스트 클릭만 무반응). 그래서 클릭 시 반드시 해당
        대화방으로 이동해야 하는 알림은 앱이 직접 그리는 팝업(NotificationToast)
        으로 띄운다 — 이 클릭은 Windows Shell을 거치지 않고 Tkinter 이벤트로
        바로 받으므로 100% 확실하게 동작한다."""
        def on_click():
            self._last_notify_key = key
            self._on_notify_click()
        try:
            if self._toast is not None and self._toast.winfo_exists():
                self._toast.update_content(title, text, on_click)
            else:
                self._toast = NotificationToast(self.root, title, text, on_click)
        except Exception:
            pass

    def _play_notify_sound(self):
        if self.engine is not None and not self.engine.notify_sound_enabled:
            return
        # "띵동" 도어벨 느낌의 알림음을 표준 라이브러리(wave·math)로 직접 합성해
        # 메모리에서 재생한다(임시 파일도, 외부 패키지도 필요 없음). 주의:
        # winsound.PlaySound는 SND_MEMORY와 SND_ASYNC를 같이 쓰면
        # "Cannot play asynchronously from memory" RuntimeError를 낸다(CPython
        # 자체 제약) — 이걸 넓은 try/except가 조용히 삼켜서 매번 예전 root.bell()
        # 소리로 조용히 되돌아가던 버그가 있었다. 그래서 GUI 스레드를 막지 않도록
        # 재생 자체를 데몬 스레드에서 동기(비-ASYNC)로 수행한다.
        try:
            import threading
            import winsound
            data = _soft_whistle_wav()

            def _play():
                try:
                    winsound.PlaySound(data, winsound.SND_MEMORY)
                except Exception:
                    pass

            threading.Thread(target=_play, daemon=True).start()
            return
        except Exception:
            pass
        try:
            self.root.bell()
        except Exception:
            pass

    def _on_notify_click(self):
        """트레이 풍선 알림이나 아이콘을 클릭하면 창을 앞으로 가져오고, 그 알림이
        어떤 대화방 소식이었는지(_last_notify_key)로 자동 이동한다."""
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
            # 복원(deiconify) 처리가 완전히 끝나기 전에 아래 force_foreground_window의
            # raw Win32 호출(SetWindowPos TOPMOST 토글 등)이 겹쳐 들어가면, 최소화에서
            # 돌아온 창이 위젯을 다 못 그린 채로 검은 화면이 되는 문제가 있었다.
            # update()로 지금까지 밀린 이벤트(복원에 따른 다시 그리기 포함)를 먼저
            # 확실히 다 처리시킨 뒤에 넘어간다.
            self.root.update()
            hwnd = int(self.root.wm_frame(), 16)
            force_foreground_window(hwnd)
            self.root.update_idletasks()
        except Exception:
            pass
        key = getattr(self, "_last_notify_key", None)
        if key and self.engine:
            self._select(key)

    def _pump(self):
        if getattr(self, "_dropped_files_queue", None):
            files = list(self._dropped_files_queue)
            self._dropped_files_queue.clear()
            self._on_files_dropped(files)
        if getattr(self, "_notify_click_pending", False):
            self._notify_click_pending = False
            self._on_notify_click()
        while True:
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            kind = ev.get("ev")
            if kind == "peer":
                self._refresh_list()
                self.gstatus.config(text=self._status_text())
                cur = self.current
                if cur and cur[0] == "dm":
                    with self.engine.plock:
                        p = self.engine.peers.get((cur[1], cur[2]))
                    if p:
                        name = self.engine.get_alias(cur) or p.get("name") or cur[1]
                        online = bool(p.get("last")) and (time.time() - p.get("last", 0)) < PEER_TIMEOUT
                        is_away = (p.get("status") == "away")
                        status_txt = "  ·  자리비움" if is_away else "  ·  접속 중"
                        status_fg = C_AWAY if is_away else C_ONLINE
                        self.ch_title.config(text=name)
                        self.ch_sub.config(text=f"{cur[1]}:{cur[2]}" + (status_txt if online else "  ·  대기"),
                                           fg=status_fg if online else C_MUTE)
            elif kind == "group":
                self._refresh_list()
                cur = self.current
                if cur and cur[0] == "grp":
                    with self.engine.glock:
                        g = self.engine.groups.get(cur[1])
                    if g:
                        self.ch_title.config(text=g["name"])
                        self.ch_sub.config(text=f"멤버 {len(g['members'])}명")
                    if ev.get("gid") == cur[1]:
                        self._reload_chat(cur)
            elif kind == "msg":
                self._on_msg(ev)
            elif kind == "gmsg":
                self._on_gmsg(ev)
            elif kind == "sent":
                self._on_sent(ev)
            elif kind == "gsent":
                self._on_gsent(ev)
            elif kind == "read_ack":
                peer = ev.get("peer")
                if self.current and self.current[0] == "dm" and (self.current[1], self.current[2]) == peer:
                    self._reload_chat(self.current, from_cache=False)
            elif kind == "burned":
                # 자동 폭파 타이머가 만료돼 메시지가 영구 삭제됨 — 지금 보고 있는
                # 대화방이면 화면에서도 즉시 사라지도록 새로고침한다.
                if self.current and self.current == ev.get("key"):
                    self._reload_chat(self.current, from_cache=False)
                    self._update_pin_banner()
                self._refresh_list()
            elif kind == "pinned":
                # 공지 지정/해제 — 내가 했든 상대(그룹원)가 했든 이 이벤트로 통일 처리.
                if self.current and self.current == ev.get("key"):
                    self._update_pin_banner()
            elif kind == "history_deleted":
                if self.current and self.current == ev.get("key"):
                    self._reload_chat(self.current, from_cache=False)
                self._refresh_list()
            elif kind == "gread_ack":
                gid = ev.get("gid")
                if self.current and self.current[0] == "grp" and self.current[1] == gid:
                    self._reload_chat(self.current, from_cache=False)
            elif kind == "file_progress":
                self._on_file_progress(ev)
            elif kind == "file_sent":
                self._on_file_sent(ev)
            elif kind == "file_recv":
                self._on_file_recv(ev)
            elif kind == "gfile_recv":
                self._on_gfile_recv(ev)
            elif kind == "avatar":
                self._refresh_list()
            elif kind == "my_avatar":
                self._refresh_me_avatar()
            elif kind == "search_results":
                self._apply_search_results(ev["gen"], ev["q"], ev["results"])
        self.root.after(80, self._pump)

    # ---------- 칸 비우기 ----------
    def _show_empty(self, msg):
        # 채팅 "메시지 영역"에만 오버레이를 얹는다(헤더까지 덮으면 대화가 하나도 없는
        # 최초 상태에서 [내 IP] 버튼이 가려져 IP를 알려줄 방법이 없어지는 문제가 있었음).
        self._hide_empty()
        holder = tk.Frame(self.chat_wrap, bg=C_MAIN)
        holder.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.empty_holder = holder
        lbl = tk.Label(holder, text=msg, fg=C_MUTE, bg=C_MAIN,
                       font=(FONT_FAM, 10, "bold"), justify="center")
        lbl.place(relx=0.5, rely=0.42, anchor="center")
        self.empty_lbl = lbl

    def _hide_empty(self):
        holder = getattr(self, "empty_holder", None)
        if holder is not None and holder.winfo_exists():
            holder.destroy()
        self.empty_holder = None

    # ---------- 기타 ----------
    def _status_text(self):
        return ""









    # ---------- 그룹 다이얼로그 ----------






    def _quit(self):
        if getattr(self, "_dnd_rehook_job", None) is not None:
            try:
                self.root.after_cancel(self._dnd_rehook_job)
            except Exception:
                pass
        if getattr(self, "_dnd_hook", None):
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(self._dnd_hook)
                self._dnd_hook = None
            except Exception:
                pass
        if self.engine is not None:
            self.engine.stop()
        if self._notifier is not None:
            self._notifier.close()
        self.root.destroy()


