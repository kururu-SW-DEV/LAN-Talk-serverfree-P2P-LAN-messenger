# -*- coding: utf-8 -*-
"""winapi.py — Windows 전용 ctypes 연동: 트레이 풍선 알림(Notifier), 다크 타이틀바.
lan_messenger.py에서 분리됨 (유지보수를 위해 여러 파일로 분할, v5.1).
표준 라이브러리(ctypes)만 사용 — 외부 패키지 불필요."""
import os
import sys
import time

try:
    import ctypes
    from ctypes import wintypes
    _HAS_CTYPES = True
except ImportError:  # pragma: no cover - 표준 라이브러리라 사실상 항상 성공
    _HAS_CTYPES = False

from constants import C_SIDEBAR, C_TEXT
from netutils import app_dir, resource_dir


if _HAS_CTYPES and os.name == "nt":
    APP_AUMID = "LANTalk"
    APP_NAME = "LAN Talk"
    NIF_ICON = 0x2
    NIF_TIP = 0x4
    NIF_INFO = 0x10
    NIF_MESSAGE = 0x1
    NIM_ADD = 0x0
    NIM_MODIFY = 0x1
    NIM_DELETE = 0x2
    NIM_SETVERSION = 0x4
    NOTIFYICON_VERSION_4 = 4
    NIIF_NONE = 0x0
    NIIF_INFO = 0x1
    NIIF_USER = 0x4
    NIIF_NOSOUND = 0x10
    NIIF_LARGE_ICON = 0x20
    IDI_APPLICATION = 32512
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    LR_DEFAULTSIZE = 0x0040
    # 트레이 아이콘 클릭 콜백용 커스텀 메시지 및, 클릭으로 간주할 lParam 값들.
    # NIM_SETVERSION(버전 4)으로 등록한다 — Windows 10/11에서 풍선이 액션 센터
    # 토스트로 표시될 때, 구버전(0) 방식으로 두면 토스트를 실제로 클릭해도
    # NIN_BALLOONUSERCLICK 콜백이 전달되지 않는 경우가 있는 것으로 확인됨(트레이
    # 아이콘 자체 클릭은 되는데 토스트 클릭만 무반응). 버전 4에서는 아이콘 클릭이
    # 원시 마우스 메시지 대신 NIN_SELECT/NIN_KEYSELECT로 오므로 그 값들도 같이
    # "클릭"으로 잡아둔다(기존 원시 마우스 코드값도 호환을 위해 그대로 남겨둠).
    WM_TRAYICON = 0x0400 + 20
    # FlashWindowEx 플래그 — 창이 포커스를 얻으면 Windows가 자동으로 멈춘다.
    FLASHW_STOP = 0
    FLASHW_TRAY = 0x2
    FLASHW_TIMERNOFG = 0xC

    def setup_windows_app_id():
        """Windows 작업표시줄 및 알림창에 파이썬 기본 아이콘/이름 대신 LAN Talk으로 표시되도록 등록.
        tk.Tk()로 창을 생성하기 전에 반드시 호출되어야 Windows가 프로세스를 파이썬과 분리하여
        독립 앱으로 인식하고 전용 아이콘을 작업표시줄에 표시한다."""
        if not (_HAS_CTYPES and os.name == "nt"):
            return
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_AUMID)
        except Exception:
            pass
        try:
            import winreg
            base_dir = app_dir()
            # 단일 exe로 빌드된 경우(app.ico가 exe 옆에 없음) exe 자신에 내장된 아이콘
            # (PyInstaller --icon)을 가리킨다 — IconUri/IconLocation은 "경로,인덱스" 형식을 지원.
            if getattr(sys, "frozen", False):
                ico_path = sys.executable
            else:
                ico_path = os.path.join(base_dir, "app.ico")
            key_path = f"Software\\Classes\\AppUserModelId\\{APP_AUMID}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
                winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
                if os.path.exists(ico_path):
                    winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, ico_path)
                winreg.SetValueEx(k, "ShowInSettings", 0, winreg.REG_DWORD, 1)
        except Exception:
            pass
        try:
            programs = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")
            lnk_path = os.path.join(programs, f"{APP_NAME}.lnk")
            if not os.path.exists(lnk_path):
                base_dir = app_dir()
                if getattr(sys, "frozen", False):
                    # 단일 exe 배포: 시작 메뉴 바로가기가 .bat이 아니라 exe 자신을 가리킨다.
                    target_path = sys.executable
                    ico_path = sys.executable
                else:
                    target_path = os.path.join(base_dir, f"START_{APP_NAME}.bat")
                    ico_path = os.path.join(base_dir, "app.ico")
                vbs_path = os.path.join(base_dir, "_mk_lnk.vbs")
                vbs_content = f'''Set ws = CreateObject("WScript.Shell")\nSet s = ws.CreateShortcut("{lnk_path}")\ns.TargetPath = "{target_path}"\ns.WorkingDirectory = "{base_dir}"\ns.IconLocation = "{ico_path}"\ns.Save\n'''
                with open(vbs_path, "w", encoding="ansi") as f:
                    f.write(vbs_content)
                import subprocess
                CREATE_NO_WINDOW = 0x08000000
                subprocess.run(["cscript", "//nologo", vbs_path],
                               creationflags=CREATE_NO_WINDOW,
                               capture_output=True,
                               timeout=5)
                if os.path.exists(vbs_path):
                    os.remove(vbs_path)
        except Exception:
            pass

    def apply_window_icon(root):
        """Windows 작업표시줄 및 창 헤더에 파이썬 기본 아이콘 대신
        전용 앱 아이콘(app.ico)이 확실히 나타나도록 Win32 레벨(WM_SETICON, SetClassLongPtr, iconbitmap)에서 설정한다."""
        if not (_HAS_CTYPES and os.name == "nt"):
            return
        try:
            base_dir = resource_dir()
            ico_path = os.path.join(base_dir, "app.ico")
            if os.path.isfile(ico_path):
                try:
                    root.iconbitmap(default=ico_path)
                    root.iconbitmap(ico_path)
                except Exception:
                    pass

                user32 = ctypes.windll.user32
                IMAGE_ICON = 1
                LR_LOADFROMFILE = 0x0010
                user32.LoadImageW.restype = wintypes.HICON
                user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
                user32.SendMessageW.restype = ctypes.c_void_p
                user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

                # 고해상도(256x256) 아이콘을 큰 아이콘으로 지정하여 작업표시줄(DPI 125%, 150%, 200% 등)에서
                # 저해상도(32x32) 확대에 따른 깨짐 현상을 방지하고 선명하게 표시
                h_icon_big = user32.LoadImageW(None, ico_path, IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
                # 작은 아이콘은 32x32로 로드하여 창 타이틀바 등에서 선명하게 다운스케일되도록 함
                h_icon_small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
                if h_icon_big or h_icon_small:
                    WM_SETICON = 0x0080
                    ICON_SMALL = 0
                    ICON_BIG = 1
                    hwnd_inner = root.winfo_id()
                    hwnd_parent = user32.GetParent(hwnd_inner) or hwnd_inner
                    for h in (hwnd_inner, hwnd_parent):
                        if h:
                            if h_icon_big:
                                user32.SendMessageW(h, WM_SETICON, ICON_BIG, h_icon_big)
                            if h_icon_small:
                                user32.SendMessageW(h, WM_SETICON, ICON_SMALL, h_icon_small)

                    # Win32 윈도우 클래스 레벨(GCLP_HICON, GCLP_HICONSM)에도 고해상도 아이콘 등록
                    SetClassLong = getattr(user32, "SetClassLongPtrW", None) or getattr(user32, "SetClassLongW", None)
                    if SetClassLong:
                        try:
                            GCLP_HICON = -14
                            GCLP_HICONSM = -34
                            SetClassLong.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
                            SetClassLong.restype = ctypes.c_void_p
                            for h in (hwnd_inner, hwnd_parent):
                                if h:
                                    if h_icon_big:
                                        try:
                                            SetClassLong(h, GCLP_HICON, h_icon_big)
                                        except Exception:
                                            pass
                                    if h_icon_small:
                                        try:
                                            SetClassLong(h, GCLP_HICONSM, h_icon_small)
                                        except Exception:
                                            pass
                        except Exception:
                            pass
        except Exception:
            pass
    # ---------- Windows 로그인 시 자동 시작 ----------
    _RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _RUN_VALUE_NAME = APP_AUMID

    def _startup_command():
        """Windows 시작 시 이 앱을 다시 켜기 위한 커맨드 라인 문자열.
        exe로 빌드된 경우 exe 자신을 가리키고, 소스로 실행 중이면(python
        lan_messenger.py) pythonw(콘솔 창 없이) + 스크립트 경로를 가리킨다."""
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        script = os.path.join(app_dir(), "lan_messenger.py")
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pythonw if os.path.isfile(pythonw) else sys.executable
        return f'"{exe}" "{script}"'

    def is_run_at_startup_enabled():
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_READ) as k:
                winreg.QueryValueEx(k, _RUN_VALUE_NAME)
                return True
        except OSError:
            return False

    def set_run_at_startup(enabled):
        """Windows 로그인 시 자동 시작 등록/해제. HKCU\\...\\Run 레지스트리만
        건드리므로 관리자 권한이 필요 없고, 현재 사용자에게만 적용된다."""
        try:
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as k:
                if enabled:
                    winreg.SetValueEx(k, _RUN_VALUE_NAME, 0, winreg.REG_SZ, _startup_command())
                else:
                    try:
                        winreg.DeleteValue(k, _RUN_VALUE_NAME)
                    except FileNotFoundError:
                        pass
            return True
        except OSError:
            return False

    def _prep_force_foreground_sigs(user32, kernel32):
        # ctypes는 argtypes/restype을 안 정해주면 전부 32비트 c_int로 취급한다 —
        # HWND는 64비트 포인터라, 이걸 안 하면 GetForegroundWindow()의 반환값 같은
        # 게 잘못 잘려서 이 함수 전체가 조용히 오동작한다(실제로 처음 구현했을 때
        # 이 문제로 아예 안 먹혔다).
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetCurrentThreadId.argtypes = []
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.keybd_event.restype = None
        user32.keybd_event.argtypes = [ctypes.c_byte, ctypes.c_byte, wintypes.DWORD, ctypes.c_void_p]
        user32.RedrawWindow.restype = wintypes.BOOL
        user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]

    def force_foreground_window(hwnd):
        """SetForegroundWindow만으로는 안 될 때가 있다 — Windows는 "지금 포그라운드인
        프로세스가 직접, 방금 받은 입력에 대한 응답으로" 호출한 게 아니면 조용히
        무시해버리는 포그라운드 잠금(foreground lock) 정책이 있다. 트레이 알림 클릭은
        ctypes 훅 콜백 안에서 곧바로 처리할 수 없어(Tk API를 그 자리에서 부르면 GIL
        문제로 죽는다) 80ms 폴링 루프(_pump)를 거쳐 뒤늦게 처리되는데, 이 지연 때문에
        Windows가 더는 "방금 사용자가 누른 것에 대한 직접 응답"으로 안 쳐주는 것으로
        보인다 — 그래서 그냥 lift()/focus_force()만으로는 창이 맨 앞으로 안 나올 수
        있다. 두 가지 우회법을 함께 쓴다:
        1) Alt 키를 눌렀다 떼는 더미 키 입력을 보내 "방금 입력이 있었다"는 상태를
           만든다(SetForegroundWindow가 잠금을 통과하는 조건 중 하나).
        2) 그래도 막히는 경우를 대비해, 현재 포그라운드 창의 스레드에 입력 상태를
           잠깐 붙였다가(AttachThreadInput) 그 안에서 호출한다.
        마지막으로 SetWindowPos로 TOPMOST를 껐다 켰다 해서 z-order 맨 위로도
        강제로 올린다(항상 위에 표시 설정과는 무관 — 즉시 원상 복구됨)."""
        hwnd = wintypes.HWND(hwnd)
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            _prep_force_foreground_sigs(user32, kernel32)

            VK_MENU = 0x12
            KEYEVENTF_KEYUP = 0x2
            user32.keybd_event(VK_MENU, 0, 0, None)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)

            fg = user32.GetForegroundWindow()
            cur_thread = kernel32.GetCurrentThreadId()
            fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            attached = False
            if fg_thread and fg_thread != cur_thread:
                attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
            try:
                # 주의: 최소화 복원은 호출하는 쪽(app.py의 _on_notify_click)이 이미
                # self.root.deiconify()/state("normal")로 Tk 자신의 정상 경로를 통해
                # 처리한 뒤 이 함수를 부른다. 예전에는 여기서 ShowWindow(hwnd, 9)
                # (SW_RESTORE)를 한 번 더 호출했는데, Tk의 복원 처리가 끝나기 전에
                # 이 raw Win32 호출이 겹쳐 들어가면 창이 검은 화면으로 뜨는(위젯
                # 다시 그리기가 중간에 끊기는) 버그가 실제로 확인됐다. 그래서 이미
                # 정상 상태인데 또 복원 명령을 겹쳐 보내는 일이 없도록, 진짜
                # 최소화 상태로 남아 있을 때만 SW_RESTORE를 보낸다.
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
                HWND_TOPMOST = wintypes.HWND(-1)
                HWND_NOTOPMOST = wintypes.HWND(-2)
                SWP_NOSIZE = 0x1
                SWP_NOMOVE = 0x2
                SWP_SHOWWINDOW = 0x40
                flags = SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
                user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                RDW_INVALIDATE = 0x1
                RDW_ERASE = 0x4
                RDW_ALLCHILDREN = 0x80
                RDW_UPDATENOW = 0x100
                user32.RedrawWindow(hwnd, None, None,
                                     RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW)
            finally:
                if attached:
                    user32.AttachThreadInput(cur_thread, fg_thread, False)
        except Exception:
            try:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
else:
    def setup_windows_app_id():
        pass

    def apply_window_icon(root):
        pass

    def is_run_at_startup_enabled():
        return False

    def set_run_at_startup(enabled):
        return False

    def force_foreground_window(hwnd):
        pass


def ensure_dpi_awareness():
    """이 프로세스를 DPI 인식(Per-Monitor DPI Aware) 및 독립 애플리케이션 ID로 선언한다 — 반드시 tk.Tk()로
    첫 창을 만들기 전에 호출해야 효과가 있다."""
    if not (_HAS_CTYPES and os.name == "nt"):
        return
    setup_windows_app_id()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # Windows 7/8 폴백(시스템 DPI 인식)
    except Exception:
        pass


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


class Notifier:
    """Windows 작업표시줄 풍선(토스트) 알림 — 표준 라이브러리 ctypes만 사용.

    실패해도 앱을 절대 죽이지 않는다(보안 프로그램 간섭 가능성이 이미 알려져 있으므로
    모든 호출을 광범위하게 try/except로 감싼다).
    """

    _FLASH_INTERVAL_MS = 2500  # 재발동 간격 — Windows가 자체적으로 몇 초 만에 끄는 것보다 짧게
    _FLASH_MAX_MS = 5 * 60 * 1000  # 최대 5분까지만 반복 — 그 이후엔 자동으로 멈춰 무한 반복 방지

    def __init__(self, root, on_click=None):
        self.root = root
        self.ok = False
        self._nid = None
        self._shell32 = None
        self._user32 = None
        self._h_icon = None
        self._flash_active = False
        self._flash_job = None
        self._flash_start_ms = 0
        self.on_click = on_click  # 알림(풍선)이나 트레이 아이콘을 클릭했을 때 호출할 콜백
        if not (_HAS_CTYPES and os.name == "nt"):
            return
        setup_windows_app_id()
        try:
            self._shell32 = ctypes.windll.shell32
            self._user32 = ctypes.windll.user32
            self._shell32.Shell_NotifyIconW.restype = wintypes.BOOL
            self._shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
            self._user32.LoadIconW.restype = wintypes.HICON
            self._user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
            self._user32.FlashWindow.restype = wintypes.BOOL
            self._user32.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
            self._user32.FlashWindowEx.restype = wintypes.BOOL
            self._user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]

            base_dir = resource_dir()
            ico_path = os.path.join(base_dir, "app.ico")
            h_icon = None
            if os.path.exists(ico_path):
                try:
                    self._user32.LoadImageW.restype = wintypes.HICON
                    self._user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
                    h_icon = self._user32.LoadImageW(None, ico_path, IMAGE_ICON, 0, 0,
                                                     LR_LOADFROMFILE | LR_DEFAULTSIZE)
                except Exception:
                    h_icon = None
            if not h_icon:
                h_icon = self._user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))
            self._h_icon = h_icon

            try:
                hwnd = int(self.root.wm_frame(), 16)
            except Exception:
                hwnd = int(self.root.winfo_id())
            nid = NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = h_icon
            nid.szTip = APP_NAME
            if self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self._nid = nid
                self.ok = True
                try:
                    nid.uTimeoutOrVersion = NOTIFYICON_VERSION_4
                    self._shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
                except Exception:
                    pass
        except Exception:
            self.ok = False

    def notify(self, title, message):
        if not self.ok or self._nid is None:
            return
        try:
            nid = self._nid
            nid.uFlags = NIF_INFO | NIF_ICON | NIF_TIP | NIF_MESSAGE
            nid.uCallbackMessage = WM_TRAYICON
            nid.szInfoTitle = (title or "")[:63]
            nid.szInfo = (message or "")[:255]
            # 상단 앱 헤더 아이콘(NIF_ICON/AUMID)만 유지하고 본문 내 하단 큰 아이콘은 미표시.
            # NIIF_NOSOUND: Windows 기본 토스트 알림음을 끈다 — 안 끄면 앱이 직접 재생하는
            # "띵동" 알림음(_play_notify_sound)과 겹쳐서 소리가 두 번(따로) 난다.
            nid.dwInfoFlags = NIIF_NONE | NIIF_NOSOUND
            nid.hBalloonIcon = None
            self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def _flash_hwnd(self):
        # 트레이 아이콘 등록(NOTIFYICONDATA.hWnd)과 같은 기준으로, 가능하면 진짜
        # 최상위 프레임 창(wm_frame)을 쓴다 — winfo_id()는 Tk 내부 자식 창이라,
        # 작업표시줄과 직접 연관된 Win32 호출에는 프레임 쪽이 더 정확하다.
        try:
            return int(self.root.wm_frame(), 16)
        except Exception:
            return int(self.root.winfo_id())

    def _raw_flash(self, hwnd):
        try:
            info = FLASHWINFO()
            info.cbSize = ctypes.sizeof(FLASHWINFO)
            info.hwnd = hwnd
            info.dwFlags = FLASHW_TRAY | FLASHW_TIMERNOFG
            info.uCount = 0
            info.dwTimeout = 0
            self._user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            try:
                self._user32.FlashWindow(hwnd, True)
            except Exception:
                pass

    def flash(self):
        """작업표시줄 아이콘을 사용자가 창을 다시 보기 전까지 계속 깜빡이게 한다.

        실제로 확인해보니 FlashWindowEx에 FLASHW_TIMERNOFG(포커스를 얻을 때까지
        계속)를 줘도, 최신 Windows에서는 주황색 강조가 몇 초 지나면 저절로
        꺼진다(문서와 달리 무한정 유지되지 않음) — 그래서 알림이 뜬 직후
        바로 보지 못한 사용자에게는 "깜빡이지 않았다"처럼 보였다. 이를
        보완하려고, 창이 포커스를 되찾을 때까지 몇 초 간격으로 계속
        다시 깜빡이도록(재발동) 타이머를 건다. 무한 루프를 막기 위해 최대
        지속 시간(_FLASH_MAX_MS)을 두고, 그 이후엔 자동으로 멈춘다."""
        if not self.ok:
            return
        self._flash_active = True
        self._flash_start_ms = int(time.time() * 1000)
        hwnd = self._flash_hwnd()
        self._raw_flash(hwnd)
        self._schedule_reflash(hwnd)

    def _schedule_reflash(self, hwnd):
        try:
            self._flash_job = self.root.after(self._FLASH_INTERVAL_MS,
                                              lambda: self._reflash_tick(hwnd))
        except Exception:
            pass

    def _reflash_tick(self, hwnd):
        self._flash_job = None
        if not self._flash_active:
            return
        if int(time.time() * 1000) - self._flash_start_ms > self._FLASH_MAX_MS:
            self._flash_active = False
            return
        self._raw_flash(hwnd)
        self._schedule_reflash(hwnd)

    def stop_flash(self):
        self._flash_active = False
        if self._flash_job is not None:
            try:
                self.root.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None
        if not self.ok:
            return
        try:
            hwnd = self._flash_hwnd()
            info = FLASHWINFO()
            info.cbSize = ctypes.sizeof(FLASHWINFO)
            info.hwnd = hwnd
            info.dwFlags = FLASHW_STOP
            info.uCount = 0
            info.dwTimeout = 0
            self._user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def close(self):
        self._flash_active = False
        if self._flash_job is not None:
            try:
                self.root.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None
        if not self.ok or self._nid is None:
            return
        try:
            self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        except Exception:
            pass
        self.ok = False


def apply_dark_titlebar(win, bg_hex=C_SIDEBAR, text_hex=C_TEXT):
    """Windows 10/11 제목 표시줄 다크 모드 및 색상 적용 (DWM API).

    기본 Tkinter 창은 Windows에서 밝은 흰색 타이틀바로 표시되어
    다크 테마와 이질감이 발생하므로, DWM API를 통해 타이틀바와 최소화/전체화면/닫기
    버튼을 다크 테마 및 지정 색상으로 일치시킨다.
    """
    if not (_HAS_CTYPES and os.name == "nt"):
        return

    def _apply():
        try:
            if not win.winfo_exists():
                return
            # SetWindowPos(SWP_FRAMECHANGED) 아래에서 클라이언트 영역까지 강제로
            # 다시 그리게 하는데, 그 시점에 Tk 쪽 위젯 배치/그리기가 아직 안 끝난
            # 상태(특히 창을 만들자마자 자식 위젯을 채우기 전에 이 함수가 불릴 때)면
            # 그 미완성 상태가 그대로 굳어서 사이드바·헤더 경계에 1px 흰 점이 남는
            # 버그가 있었다. 강제 리페인트 전에 Tk 쪽 대기 중인 그리기를 먼저
            # 끝내둔다.
            win.update_idletasks()
            hwnd = int(win.winfo_id())
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi

            parent = user32.GetParent(wintypes.HWND(hwnd))
            root_h = user32.GetAncestor(wintypes.HWND(hwnd), 2)  # GA_ROOT = 2

            handles = set(filter(None, [root_h, parent, hwnd]))
            for h in handles:
                # 1. DWMWA_USE_IMMERSIVE_DARK_MODE (20: Win10 20H1+ 및 Win11, 19: 초기 Win10)
                val = ctypes.c_int(1)
                res = dwmapi.DwmSetWindowAttribute(wintypes.HWND(h), wintypes.DWORD(20),
                                                    ctypes.byref(val), ctypes.sizeof(val))
                if res != 0:
                    dwmapi.DwmSetWindowAttribute(wintypes.HWND(h), wintypes.DWORD(19),
                                                 ctypes.byref(val), ctypes.sizeof(val))

                # 2. Windows 11 DWMWA_CAPTION_COLOR (35) — 0x00BBGGRR 형식
                if bg_hex and bg_hex.startswith("#") and len(bg_hex) == 7:
                    r, g, b = int(bg_hex[1:3], 16), int(bg_hex[3:5], 16), int(bg_hex[5:7], 16)
                    c_color = ctypes.c_int((b << 16) | (g << 8) | r)
                    dwmapi.DwmSetWindowAttribute(wintypes.HWND(h), wintypes.DWORD(35),
                                                 ctypes.byref(c_color), ctypes.sizeof(c_color))

                # 3. Windows 11 DWMWA_TEXT_COLOR (36) — 0x00BBGGRR 형식
                if text_hex and text_hex.startswith("#") and len(text_hex) == 7:
                    tr, tg, tb = int(text_hex[1:3], 16), int(text_hex[3:5], 16), int(text_hex[5:7], 16)
                    t_color = ctypes.c_int((tb << 16) | (tg << 8) | tr)
                    dwmapi.DwmSetWindowAttribute(wintypes.HWND(h), wintypes.DWORD(36),
                                                 ctypes.byref(t_color), ctypes.sizeof(t_color))

                # 4. Windows 11 DWMWA_BORDER_COLOR (34) — 캡션/텍스트 색만 바꾸고 이
                #    속성을 그대로 두면, 창 맨 위 가장자리에 시스템 기본(밝은 회색/흰색)
                #    테두리 선이 1픽셀 그대로 남아 다크 테마 위에 흰 점/선으로 도드라진다.
                if bg_hex and bg_hex.startswith("#") and len(bg_hex) == 7:
                    dwmapi.DwmSetWindowAttribute(wintypes.HWND(h), wintypes.DWORD(34),
                                                 ctypes.byref(c_color), ctypes.sizeof(c_color))

                # 5. SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE (0x0037)
                user32.SetWindowPos(wintypes.HWND(h), None, 0, 0, 0, 0, 0x0037)
        except Exception:
            pass

    _apply()
    _mapped_once = [False]

    def _on_first_map(e):
        # <Map>은 최초 창 표시뿐 아니라 최소화→복원마다도 매번 발생한다.
        # DWM 속성은 HWND에 고정되어 최소화/복원으로 사라지지 않으므로,
        # 최초 1회 이후에는 재적용하지 않는다 — 안 그러면 복원할 때마다
        # SetWindowPos(SWP_FRAMECHANGED)가 다시 불려 화면이 번쩍인다.
        if _mapped_once[0]:
            return
        _mapped_once[0] = True
        win.after_idle(_apply)

    try:
        # Tkinter 창이 실제 화면에 매핑되는 순간에 프레임 HWND가 완성되므로 <Map> 이벤트 연동
        win.bind("<Map>", _on_first_map, add="+")
        win.after(30, _apply)
        win.after(100, _apply)
        win.after(300, _apply)
        win.after(600, _apply)
    except Exception:
        pass


def apply_ime_font(widget, family, point_size):
    """Windows IME 한글 조합(입력 중간 글자) 표시 글꼴을 위젯이 실제 쓰는 글꼴/크기로
    맞춘다.

    배경: Tk의 font 옵션은 "이미 완성돼 위젯에 들어간" 텍스트에만 적용된다. 한글처럼
    자모를 조합해서 완성하는 입력 방식은, 조합 중인 글자(아직 커밋 전)를 Tk가 아니라
    Windows IME가 자기 시스템 기본 글꼴로 직접 그려서 보여준다 — 그래서 "지금 막
    입력 중인 글자 한 개만" 폰트와 크기가 확 다르게 보이는 현상이 생긴다(실측 확인:
    화면에 박스/다른 크기로 보이던 문제의 진짜 원인 — 이모지·이형 문자 선택자와는
    무관했다). IMM32(`ImmSetCompositionFontW`)로 조합 글꼴을 위젯 글꼴과 동일하게
    직접 지정해서 맞춘다."""
    if not (_HAS_CTYPES and os.name == "nt"):
        return

    class LOGFONTW(ctypes.Structure):
        _fields_ = [
            ("lfHeight", ctypes.c_long), ("lfWidth", ctypes.c_long),
            ("lfEscapement", ctypes.c_long), ("lfOrientation", ctypes.c_long),
            ("lfWeight", ctypes.c_long), ("lfItalic", ctypes.c_byte),
            ("lfUnderline", ctypes.c_byte), ("lfStrikeOut", ctypes.c_byte),
            ("lfCharSet", ctypes.c_byte), ("lfOutPrecision", ctypes.c_byte),
            ("lfClipPrecision", ctypes.c_byte), ("lfQuality", ctypes.c_byte),
            ("lfPitchAndFamily", ctypes.c_byte), ("lfFaceName", ctypes.c_wchar * 32),
        ]

    def _apply():
        try:
            hwnd = widget.winfo_id()
            imm32 = ctypes.windll.imm32
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            himc = imm32.ImmGetContext(hwnd)
            if not himc:
                return
            try:
                hdc = user32.GetDC(hwnd)
                dpi = gdi32.GetDeviceCaps(hdc, 90) if hdc else 96  # LOGPIXELSY
                if hdc:
                    user32.ReleaseDC(hwnd, hdc)
                dpi = dpi or 96
                lf = LOGFONTW()
                lf.lfHeight = -int(round(abs(point_size) * dpi / 72))
                lf.lfWeight = 400  # FW_NORMAL
                lf.lfCharSet = 1   # DEFAULT_CHARSET — 얼굴 이름이 정확히 일치하면 그대로 씀
                lf.lfFaceName = family
                imm32.ImmSetCompositionFontW(himc, ctypes.byref(lf))
            finally:
                imm32.ImmReleaseContext(hwnd, himc)
        except Exception:
            pass

    try:
        widget.after(100, _apply)  # 위젯이 실제 화면에 매핑된 뒤 호출되도록 지연
        # IME 조합 컨텍스트는 포커스가 바뀔 때 재설정될 수 있어, 매번 다시 적용한다.
        widget.bind("<FocusIn>", lambda e: _apply(), add="+")
    except Exception:
        pass


def get_idle_seconds():
    """Windows GetLastInputInfo API를 사용해 시스템 전역 사용자 입력(키보드/마우스)
    유휴 시간(초)을 반환한다. 비-Windows 또는 실패 시 0.0 반환."""
    if not (_HAS_CTYPES and os.name == "nt"):
        return 0.0
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
            return max(0.0, millis / 1000.0)
    except Exception:
        pass
    return 0.0


