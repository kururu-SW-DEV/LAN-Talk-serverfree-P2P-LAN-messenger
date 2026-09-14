# -*- coding: utf-8 -*-
"""dnd_handler.py — Windows WH_GETMESSAGE 스레드 훅 기반 드래그앤드롭 믹스인.
app.py에서 분리됨 (유지보수 및 모듈화를 위한 리팩토링)."""
import os

try:
    import ctypes
    from ctypes import wintypes
    _HAS_CTYPES = True
except ImportError:
    _HAS_CTYPES = False


class DndMixin:
    """Windows 네이티브 드래그 앤 드롭 및 트레이 알림 클릭 처리 믹스인."""

    def _setup_drag_and_drop(self):
        if not (_HAS_CTYPES and os.name == "nt"):
            return
        try:
            self.root.update_idletasks()
        except Exception:
            pass

        try:
            shell32 = ctypes.windll.shell32
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            shell32.DragQueryFileW.argtypes = [wintypes.WPARAM, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT
            shell32.DragFinish.argtypes = [wintypes.WPARAM]
            shell32.DragFinish.restype = None

            # Windows UIPI 메시지 필터 해제 (일반 권한 탐색기 -> 드래그 앤 드롭 수신 허용)
            for msg in (0x0233, 0x004A, 0x0049):
                try:
                    user32.ChangeWindowMessageFilter(msg, 1)
                except Exception:
                    pass

            # 모든 위젯 트리에 DragAcceptFiles 재귀 활성화 (창 어느 곳에 드롭해도 허용)
            def _enable_dnd(w):
                try:
                    wid = int(w.winfo_id())
                    shell32.DragAcceptFiles(wid, True)
                    for msg in (0x0233, 0x004A, 0x0049):
                        try:
                            user32.ChangeWindowMessageFilterEx(wid, msg, 1, None)
                        except Exception:
                            pass
                except Exception:
                    pass
                for ch in w.winfo_children():
                    _enable_dnd(ch)

            _enable_dnd(self.root)
            try:
                frame_hwnd = int(self.root.wm_frame(), 16)
                shell32.DragAcceptFiles(frame_hwnd, True)
                for msg in (0x0233, 0x004A, 0x0049):
                    try:
                        user32.ChangeWindowMessageFilterEx(frame_hwnd, msg, 1, None)
                    except Exception:
                        pass
            except Exception:
                pass

            if getattr(self, "_dnd_hook", None):
                return  # 이미 훅이 설치되어 있으면 중복 등록 방지

            WH_GETMESSAGE = 3
            WM_DROPFILES = 0x0233
            # 트레이 알림(풍선) 클릭 감지용 — winapi.Notifier가 NOTIFYICONDATA에 이
            # 값을 uCallbackMessage로 등록해두면, 아이콘/풍선을 클릭했을 때 Windows가
            # 이 메시지를 (wParam=아이콘 ID, lParam=클릭 종류)로 보내준다. 값은
            # winapi.py의 정의와 반드시 일치해야 한다.
            WM_TRAYICON = 0x0400 + 20
            TRAY_CLICK_LPARAMS = {0x0202, 0x0203, 0x0400, 0x0401, 0x0405}  # 좌클릭/더블클릭/풍선클릭 등

            class MSG(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM),
                    ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD),
                    ("pt_x", wintypes.LONG),
                    ("pt_y", wintypes.LONG),
                ]

            HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(MSG))

            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK

            user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(MSG)]
            user32.CallNextHookEx.restype = ctypes.c_longlong

            user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL

            def get_msg_proc(code, wp, p_msg):
                if code >= 0 and p_msg:
                    try:
                        msg = p_msg.contents
                        if msg.message == WM_DROPFILES:
                            h_drop = msg.wParam
                            count = shell32.DragQueryFileW(h_drop, 0xFFFFFFFF, None, 0)
                            paths = []
                            for i in range(count):
                                buf = ctypes.create_unicode_buffer(1024)
                                shell32.DragQueryFileW(h_drop, i, buf, 1024)
                                if buf.value:
                                    paths.append(buf.value)
                            shell32.DragFinish(h_drop)
                            msg.message = 0  # WM_NULL로 치환하여 Tkinter 충돌 방지
                            if paths:
                                self._dropped_files_queue.extend(paths)
                        elif msg.message == WM_TRAYICON and msg.lParam in TRAY_CLICK_LPARAMS:
                            # 주의: 이 훅 콜백 안에서 Tkinter API(root.after 포함)를 직접
                            # 부르면 Tcl이 GIL을 안 쥔 상태에서 호출돼 인터프리터가 패닉
                            # 나는 것으로 이미 확인된 바 있다(드래그 앤 드롭 크래시 때와
                            # 동일 원인). 그래서 순수 파이썬 불리언 플래그만 세워두고,
                            # 실제 처리는 80ms 메인 루프(_pump)에서 안전하게 수행한다.
                            self._notify_click_pending = True
                    except Exception:
                        pass
                return user32.CallNextHookEx(self._dnd_hook, code, wp, p_msg)

            self._dnd_hook_cb = HOOKPROC(get_msg_proc)  # GC 방지용 영구 인스턴스 보관
            thread_id = kernel32.GetCurrentThreadId()
            self._dnd_hook = user32.SetWindowsHookExW(WH_GETMESSAGE, self._dnd_hook_cb, None, thread_id)
            self._dnd_hook_active = True
            self._dnd_hook_user32 = user32
            self._dnd_hook_thread_id = thread_id
            self._dnd_rehook_job = None
            # 창을 이동/크기조절하는 동안(특히 타이틀바를 잡고 드래그할 때) 이 훅이
            # 스레드의 모든 메시지를 가로채면, Windows가 평소엔 밀린 WM_MOUSEMOVE를
            # 알아서 병합(coalescing)해 처리하던 것을 매번 개별적으로 훅까지 통과
            # 시키게 되어 창이 실제 마우스보다 눈에 띄게 느리게(무겁게) 따라오는
            # 현상이 생긴다 — py-spy로 실제 구동 중인 창을 떠서 확인된 원인이다.
            # 그래서 창이 실제로 움직이는 동안만 훅을 잠깐 내렸다가, 움직임이 멎으면
            # 자동으로 되살린다(<Configure>는 이동·크기조절 내내 계속 들어오므로,
            # 매번 들어올 때마다 재무장 타이머를 뒤로 미루는 디바운스 방식).
            self.root.bind("<Configure>", self._dnd_on_configure, add="+")
        except Exception:
            pass

    def _dnd_on_configure(self, e):
        if e.widget is not self.root:
            return
        if getattr(self, "_dnd_hook", None) and getattr(self, "_dnd_hook_active", False):
            try:
                self._dnd_hook_user32.UnhookWindowsHookEx(self._dnd_hook)
                self._dnd_hook_active = False
            except Exception:
                pass
        if getattr(self, "_dnd_rehook_job", None) is not None:
            try:
                self.root.after_cancel(self._dnd_rehook_job)
            except Exception:
                pass
        # 250ms로는 드래그 중 커서 속도가 느려지는 구간에서 <Configure> 간격이
        # 벌어져 훅이 잠깐 되살아났다가 곧바로 다시 내려가는 "깜빡임"이 실측으로
        # 확인되어(py-spy), 여유 있게 500ms로 늘렸다.
        self._dnd_rehook_job = self.root.after(500, self._dnd_rehook)

    def _dnd_rehook(self):
        self._dnd_rehook_job = None
        if getattr(self, "_dnd_hook_active", True) or not getattr(self, "_dnd_hook_cb", None):
            return
        try:
            WH_GETMESSAGE = 3
            self._dnd_hook = self._dnd_hook_user32.SetWindowsHookExW(
                WH_GETMESSAGE, self._dnd_hook_cb, None, self._dnd_hook_thread_id)
            self._dnd_hook_active = True
        except Exception:
            pass

    def _on_files_dropped(self, paths):
        if not paths:
            return
        if self.engine is None or not self.current:
            self.status.set("파일을 전송할 대화 상대를 먼저 선택해주세요")
            self._embed_alert("대화 상대 미선택",
                        "파일을 보낼 대화 상대를 왼쪽 대화 목록에서 먼저 선택한 후\n파일을 드래그앤드롭 해주세요.",
                        kind="info")
            return
        sent_count = 0
        skipped_dirs = []
        for fpath in paths:
            if os.path.isdir(fpath):
                skipped_dirs.append(os.path.basename(fpath))
                continue
            if os.path.isfile(fpath):
                if self._send_file_path(fpath):
                    sent_count += 1
        if skipped_dirs:
            # 폴더 하나마다 확인창을 띄우면(예: 실수로 폴더 10개를 한꺼번에
            # 끌어다 놓았을 때) 모달 알림이 연달아 10번 떠서 그때마다 "확인"을
            # 눌러야만 화면이 풀리는 문제가 있었다 — 전부 모아서 한 번만 띄운다.
            names = "\n".join(f"· {n}" for n in skipped_dirs[:10])
            more = f"\n...외 {len(skipped_dirs) - 10}개" if len(skipped_dirs) > 10 else ""
            self._embed_alert("폴더 전송 불가",
                        f"폴더는 전송할 수 없습니다({len(skipped_dirs)}개 건너뜀):\n\n{names}{more}\n\n"
                        "개별 파일이나 ZIP 압축 파일 형태로 드래그앤드롭 해주세요.",
                        kind="warning")
        if sent_count > 0:
            self.status.set(f"파일 {sent_count}건 드래그앤드롭 전송 시작")
