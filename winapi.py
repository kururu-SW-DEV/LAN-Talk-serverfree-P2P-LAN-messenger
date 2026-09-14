# -*- coding: utf-8 -*-
"""winapi.py — Windows 전용 ctypes 연동: 트레이 풍선 알림(Notifier), 다크 타이틀바.
lan_messenger.py에서 분리됨 (유지보수를 위해 여러 파일로 분할, v5.1).
표준 라이브러리(ctypes)만 사용 — 외부 패키지 불필요."""
import os
import sys

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
    NIIF_NONE = 0x0
    NIIF_INFO = 0x1
    NIIF_USER = 0x4
    NIIF_LARGE_ICON = 0x20
    IDI_APPLICATION = 32512
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    LR_DEFAULTSIZE = 0x0040
    # 트레이 아이콘 클릭 콜백용 커스텀 메시지 및, 클릭으로 간주할 lParam 값들.
    # NIM_SETVERSION을 안 불렀으므로 구버전 방식이라 풍선 클릭은 NIN_BALLOONUSERCLICK로,
    # 아이콘 자체 클릭은 원시 마우스 메시지(WM_LBUTTONUP 등)로 온다 — 실측 없이도 폭넓게
    # 잡히도록 흔한 값들을 전부 "클릭"으로 취급한다.
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
else:
    def setup_windows_app_id():
        pass

    def apply_window_icon(root):
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

    def __init__(self, root, on_click=None):
        self.root = root
        self.ok = False
        self._nid = None
        self._shell32 = None
        self._user32 = None
        self._h_icon = None
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
            # 상단 앱 헤더 아이콘(NIF_ICON/AUMID)만 유지하고 본문 내 하단 큰 아이콘은 미표시
            nid.dwInfoFlags = NIIF_NONE
            nid.hBalloonIcon = None
            self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def flash(self):
        """작업표시줄 아이콘을 사용자가 창을 다시 보기 전까지 계속 깜빡이게 한다.
        기존 FlashWindow(단발성 1회 반짝임)는 눈에 잘 안 띄어서, FlashWindowEx +
        FLASHW_TIMERNOFG(포커스를 얻을 때까지 계속)로 바꿨다 — 창이 포커스를
        얻으면 Windows가 자동으로 멈춘다."""
        if not self.ok:
            return
        try:
            hwnd = int(self.root.winfo_id())
            info = FLASHWINFO()
            info.cbSize = ctypes.sizeof(FLASHWINFO)
            info.hwnd = hwnd
            info.dwFlags = FLASHW_TRAY | FLASHW_TIMERNOFG
            info.uCount = 0
            info.dwTimeout = 0
            self._user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            try:
                self._user32.FlashWindow(int(self.root.winfo_id()), True)
            except Exception:
                pass

    def stop_flash(self):
        if not self.ok:
            return
        try:
            hwnd = int(self.root.winfo_id())
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


