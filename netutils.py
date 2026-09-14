# -*- coding: utf-8 -*-
"""netutils.py — 시간/이름 포맷팅, 로컬 IP 캐시, 아바타 이미지 처리 등 순수 헬퍼.
lan_messenger.py에서 분리됨 (유지보수를 위해 여러 파일로 분할, v5.1)."""
import datetime
import io
import os
import re
import socket
import struct
import sys
import time
import unicodedata

from constants import DEFAULT_PORT

URL_REGEX = re.compile(r'(https?://[^\s<>"]+|ftp://[^\s<>"]+)')


def app_dir():
    """이 앱의 영구 위치 — 소스 실행 시엔 .py들이 있는 폴더, PyInstaller 단일 exe로 빌드된
    경우엔 그 exe가 있는 폴더(secret.key, data/ 등 "실행 위치 기준" 파일들을 둔다).
    PyInstaller onefile은 매 실행마다 __file__을 임시 압축 해제 폴더(_MEIPASS, 종료 시 삭제됨)
    아래로 잡으므로, frozen 상태에서 __file__ 기준으로 계산하면 secret.key/대화기록이 실행할
    때마다 초기화된다 — sys.executable(exe 자신의 경로)을 기준으로 잡아야 한다."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_dir():
    """읽기 전용 번들 리소스(app.ico 등)가 있는 폴더.
    PyInstaller onefile로 빌드된 경우 이런 리소스는 실행 중에만 존재하는 임시 폴더
    (_MEIPASS)에 풀리므로, 영구 데이터용 app_dir()과 분리해서 다룬다."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", app_dir())
    return app_dir()


_pil_vendor_ready = False


def ensure_vendored_pil():
    """JPG 미리보기(선택 기능)에 쓰는 Pillow를 설치 없이 바로 쓸 수 있게 한다.

    vendor/pil_cp<버전>/PIL 폴더에 Windows 64비트용 사전 빌드 Pillow(현재 3.14만 동봉)를
    동봉해뒀다 — pip install이나 인터넷 연결이 전혀 필요 없다. 폴더 전체를 다른 PC에
    복사하기만 하면, 그 PC의 Python 버전에 맞는 폴더가 자동으로 잡혀 그대로 동작한다
    (맞는 버전이 없으면 그냥 조용히 건너뛰고, JPG는 기존처럼 파일카드로 표시된다).
    시스템에 Pillow가 이미 설치돼 있어도 배포마다 동일하게 동작하도록 이 동봉 버전을
    우선한다."""
    global _pil_vendor_ready
    if _pil_vendor_ready:
        return
    _pil_vendor_ready = True
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    vendor_dir = os.path.join(app_dir(), "vendor", f"pil_{tag}")
    if os.path.isdir(os.path.join(vendor_dir, "PIL")) and vendor_dir not in sys.path:
        sys.path.insert(0, vendor_dir)


def date_str(ts=None):
    """날짜 구분선: 2026년 9월 10일 목요일"""
    t = datetime.date.fromtimestamp(ts) if ts is not None else datetime.date.today()
    days = ["월", "화", "수", "목", "금", "토", "일"]
    return f"{t.year}년 {t.month}월 {t.day}일 {days[t.weekday()]}요일"


def default_datadir():
    """대화 기록·설정·secret.key를 사용자별 %LOCALAPPDATA%가 아니라 이 프로그램 코드가
    있는 폴더 밑의 data/ 서브폴더에 둔다(포터블 실행용). app_dir()과 동일하게 __file__
    기준으로 매번 새로 계산하므로, USB 등에 폴더째 넣어 드라이브 문자가 바뀌거나
    다른 PC로 옮겨 실행해도 항상 "지금 실행 중인 폴더"를 정확히 가리킨다."""
    return os.path.join(app_dir(), "data")


def default_name():
    return (os.environ.get("USERNAME") or socket.gethostname() or "사용자")[:60]


def korea_time_str(ts=None):
    """초 단위 제거한 메신저용 시각(오후 8:42)"""
    t = datetime.datetime.fromtimestamp(ts) if ts is not None else datetime.datetime.now()
    ampm = "오전" if t.hour < 12 else "오후"
    h = t.hour % 12 or 12
    return f"{ampm} {h}:{t.minute:02d}"


_WIN_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}


def safe_name(s):
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(s or "")).strip("_")
    cleaned = cleaned.rstrip(". ")
    if not cleaned or cleaned in (".", "..") or all(c == "." for c in cleaned):
        return "peer"
    # Windows는 확장자와 무관하게 파일명 "본체"가 CON/PRN/AUX/NUL/COM1~9/LPT1~9와
    # (대소문자 무시하고) 같으면 예약 장치 이름으로 취급해 생성을 거부한다
    # (예: "con.txt" 저장 시 WinError 22) — 겹치면 접두사를 붙여 피해간다.
    stem, ext = os.path.splitext(cleaned)
    if stem.lower() in _WIN_RESERVED_NAMES:
        cleaned = f"_{stem}{ext}"
    return cleaned or "peer"


def sanitize_chat_text(text):
    """Tkinter에서 폰트 크기 왜곡(두부 상자/비정상적 크기 확장)을 유발하는
    이형 문자 선택자(Variation Selector U+FE00~U+FE0F, U+E0100~U+E01EF),
    유니코드 포맷 문자(Cf: ZWJ, ZWSP 등), 깨진 서로게이트(Cs) 및 유효하지 않은 제어 문자를 제거한다."""
    if not text:
        return ""
    cleaned = []
    for ch in text:
        code = ord(ch)
        # Variation selectors (U+FE00 ~ U+FE0F, U+E0100 ~ U+E01EF)
        if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
            continue
        # Format characters (ZWJ, ZWSP, LRM, RLM, etc.) & Surrogates
        if unicodedata.category(ch) in ("Cf", "Cs"):
            continue
        # Null bytes or invalid control characters (줄바꿈과 탭은 유지)
        if code < 32 and ch not in ("\n", "\t"):
            continue
        cleaned.append(ch)
    return "".join(cleaned)


def make_circular_avatar_png(path, size=80):
    """이미지 파일(PNG/GIF/BMP — tkinter 내장 디코더가 지원하는 형식)을 정사각형
    중앙 크롭 후 size×size로 축소하고, 가장자리가 투명한 원형 모양으로 만들어
    PNG 바이트로 반환한다. tkinter(PhotoImage)만 사용 — Pillow 등 외부 이미지
    라이브러리가 전혀 필요 없다(이 프로그램의 "표준 라이브러리만 사용" 원칙 유지).

    JPG는 tkinter가 직접 디코딩할 수 없어 지원하지 않는다 — 파일 전송 미리보기와
    동일한 제약(README 참고)."""
    import tkinter as tk

    src = tk.PhotoImage(file=path)
    w, h = src.width(), src.height()
    if w <= 0 or h <= 0:
        raise ValueError("이미지 크기를 읽을 수 없습니다.")

    # 1) 중앙 기준 정사각형 크롭 (Tk 네이티브 copy — 파이썬 픽셀 루프 없이 즉시 처리)
    side = min(w, h)
    x1, y1 = (w - side) // 2, (h - side) // 2
    square = tk.PhotoImage(width=side, height=side)
    square.tk.call(square.name, "copy", src.name, "-from", x1, y1, x1 + side, y1 + side)

    # 2) size 근처로 축소 (정수 배율만 지원하는 Tk subsample 특성상 정확히 size는 아닐 수 있음)
    factor = max(1, -(-side // size))  # 올림 나눗셈
    small = square.subsample(factor, factor) if factor > 1 else square
    out_size = small.width()

    # 3) 원형 알파 마스크 — 4x4 서브픽셀 슈퍼샘플링(16샘플)으로 매끄러운 앤티앨리어싱 적용
    from canvas_utils import _encode_png_rgba
    cx = cy = out_size / 2.0
    r_outer = out_size / 2.0 - 0.5
    r_outer_sq = r_outer * r_outer
    sub = (0.125, 0.375, 0.625, 0.875)

    buf = bytearray(out_size * out_size * 4)
    for y in range(out_size):
        row = y * out_size
        for x in range(out_size):
            cnt = 0
            for sy in sub:
                py = y + sy - cy
                py_sq = py * py
                for sx in sub:
                    px = x + sx - cx
                    if px * px + py_sq <= r_outer_sq:
                        cnt += 1
            if cnt == 0:
                continue  # 원 바깥은 100% 완전 투명 (RGBA 0,0,0,0)
            cov = cnt / 16.0
            r, g, b = small.get(x, y)
            idx = (row + x) * 4
            buf[idx] = r
            buf[idx + 1] = g
            buf[idx + 2] = b
            buf[idx + 3] = int(round(cov * 255))

    return _encode_png_rgba(out_size, out_size, buf)


_LOCAL_IPS_CACHE = {"ts": 0.0, "ips": ["127.0.0.1"]}

def local_ips(force=False):
    now = time.time()
    if not force and (now - _LOCAL_IPS_CACHE["ts"] < 30.0):
        return list(_LOCAL_IPS_CACHE["ips"])
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 브로드캐스트 주소로 connect()하려면 SO_BROADCAST가 먼저 켜져 있어야
        # 한다 — 없으면 Windows에서 실패해(WinError) 매번 이 1차 감지가 통째로
        # 건너뛰이고 항상 아래 gethostbyname_ex 폴백만 타게 된다.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.connect(("255.255.255.255", DEFAULT_PORT))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips.append(ip)
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    res = ips or ["127.0.0.1"]
    _LOCAL_IPS_CACHE["ts"] = now
    _LOCAL_IPS_CACHE["ips"] = res
    return list(res)


def parse_target(text):
    """'IP' 또는 'IP:포트' 입력을 검증해 (ip, port) 튜플로 돌려준다."""
    t = (text or "").strip()
    if not t:
        raise ValueError("IP를 입력하세요.")
    if ":" in t:
        ip, ps = t.rsplit(":", 1)
        if not ps.isdigit():
            raise ValueError(f"포트가 숫자가 아닙니다: {t}")
        port = int(ps)
    else:
        ip, port = t, DEFAULT_PORT
    ip = ip.strip()
    try:
        socket.inet_aton(ip)
    except OSError:
        raise ValueError(f"IPv4 형식이 아닙니다: {ip}")
    if not (1 <= port <= 65535):
        raise ValueError(f"포트 범위 오류: {port}")
    return ip, port


def get_clipboard_files():
    """클립보드에 복사된 파일 목록(탐색기 Ctrl+C, Windows 캡처 도구 ScreenClip 파일 등)이 있으면
    실제 로컬 디스크에 존재하는 파일 경로들의 리스트를 반환한다."""
    files = []
    # 1. PIL ImageGrab 시도 (파일 경로 리스트를 돌려주는 경우)
    try:
        from PIL import ImageGrab
        im = ImageGrab.grabclipboard()
        if isinstance(im, list):
            for item in im:
                if isinstance(item, str) and os.path.exists(item):
                    files.append(item)
            if files:
                return files
    except Exception:
        pass

    # 2. Windows ctypes CF_HDROP fallback (순수 표준 라이브러리)
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            CF_HDROP = 15
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.CloseClipboard.argtypes = []
            user32.CloseClipboard.restype = wintypes.BOOL
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            user32.GetClipboardData.restype = wintypes.HANDLE
            shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT

            if user32.OpenClipboard(None):
                try:
                    h = user32.GetClipboardData(CF_HDROP)
                    if h:
                        cnt = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
                        buf = ctypes.create_unicode_buffer(1024)
                        for i in range(cnt):
                            shell32.DragQueryFileW(h, i, buf, 1024)
                            fpath = buf.value
                            if os.path.exists(fpath):
                                files.append(fpath)
                finally:
                    user32.CloseClipboard()
            if files:
                return files
        except Exception:
            pass

    return files


def get_clipboard_image_bytes():
    """클립보드에 이미지가 복사되어 있으면 (이미지 바이트, 확장자)를 반환한다.
    이미지가 없으면 (None, None) 반환.
    Pillow ImageGrab을 우선 시도하고, 없으면 Windows ctypes CF_DIB API로 추출한다.
    클립보드가 파일 목록(예: Windows 캡처 도구 임시 PNG 등)인 경우에도 이미지 파일이면
    해당 바이트를 읽어 반환한다."""
    # 1. PIL ImageGrab 시도 (설치/동봉된 경우 가장 완전한 변환)
    try:
        from PIL import Image, ImageGrab
        im = ImageGrab.grabclipboard()
        if isinstance(im, Image.Image):
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue(), ".png"
        elif isinstance(im, list):
            for item in im:
                if isinstance(item, str) and os.path.isfile(item):
                    ext = os.path.splitext(item)[1].lower()
                    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
                        with open(item, "rb") as f:
                            return f.read(), ext
    except Exception:
        pass

    # 2. Windows ctypes CF_DIB fallback (외부 패키지 없이 순수 표준 라이브러리)
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            CF_DIB = 8
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.CloseClipboard.argtypes = []
            user32.CloseClipboard.restype = wintypes.BOOL
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            user32.GetClipboardData.restype = wintypes.HANDLE
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.restype = wintypes.BOOL
            kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalSize.restype = ctypes.c_size_t

            if user32.OpenClipboard(None):
                try:
                    h = user32.GetClipboardData(CF_DIB)
                    if h:
                        ptr = kernel32.GlobalLock(h)
                        try:
                            size = kernel32.GlobalSize(h)
                            if ptr and size > 40:
                                raw = ctypes.string_at(ptr, size)
                                bi_size = struct.unpack_from('<I', raw, 0)[0]
                                bi_bit_count = struct.unpack_from('<H', raw, 14)[0]
                                bi_clr_used = struct.unpack_from('<I', raw, 32)[0]
                                palette_size = 0
                                if bi_bit_count <= 8:
                                    palette_size = (bi_clr_used if bi_clr_used > 0 else (1 << bi_bit_count)) * 4
                                elif bi_size == 40 and struct.unpack_from('<I', raw, 16)[0] == 3:
                                    # BITMAPINFOHEADER(40B)+BI_BITFIELDS만 헤더 뒤에 컬러
                                    # 마스크(12B)가 별도로 붙는다. BITMAPV4/V5HEADER(108/124B)는
                                    # 마스크가 헤더 구조체 안에 이미 포함돼 있어 여기서 12바이트를
                                    # 더하면 픽셀 데이터 시작 위치(off_bits)가 밀려 이미지가 깨진다.
                                    palette_size = 12
                                off_bits = 14 + bi_size + palette_size
                                file_size = 14 + len(raw)
                                header = struct.pack('<2sIHHI', b'BM', file_size, 0, 0, off_bits)
                                return header + raw, ".bmp"
                        finally:
                            if ptr:
                                kernel32.GlobalUnlock(h)
                finally:
                    user32.CloseClipboard()
        except Exception:
            pass

    return None, None
