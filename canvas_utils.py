# -*- coding: utf-8 -*-
"""canvas_utils.py — tkinter Canvas에 둥근 모서리 폴리곤을 그리는 헬퍼.
lan_messenger.py에서 분리됨 (유지보수를 위해 여러 파일로 분할, v5.1)."""
import struct
import tkinter as tk
import zlib

def _round_rect_points(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]


def round_rect(canvas, x1, y1, x2, y2, r=12, **kw):
    """진짜 둥근 모서리 말풍선을 위한 폴리곤 헬퍼(smooth=True로 곡선 보간)."""
    return canvas.create_polygon(_round_rect_points(x1, y1, x2, y2, r), smooth=True, **kw)


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _png_chunk(tag, data):
    chunk = tag + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xffffffff)


def _encode_png_rgba(width, height, rgba):
    """스택 이미지가 겹쳐도 실제 투명(alpha)이 유지되도록, RGBA 픽셀 버퍼를
    최소한의 PNG(비압축 필터 없음 + zlib)로 직접 인코딩한다. stdlib(zlib, struct)만
    사용 — tk.PhotoImage는 Tk 8.6부터 PNG를 알파 채널까지 그대로 읽는다."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # 스캔라인 필터: None
        raw.extend(rgba[y * stride:(y + 1) * stride])
    idat = zlib.compress(bytes(raw), 9)
    return sig + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


_smooth_circle_cache = {}
_PIL_MODULES = None


def _cache_circle(key, img):
    # (size, fill, ring, ring_width) 조합은 실제로는 고정된 아바타 색상
    # 팔레트·스티커 배경색 몇 개 × 소수의 크기라 자연히 작게 유지되지만,
    # _avatar_img_cache와 같은 관례로 상한을 둬 혹시 모를 증가를 막는다.
    if len(_smooth_circle_cache) >= 128:
        first_k = next(iter(_smooth_circle_cache))
        _smooth_circle_cache.pop(first_k, None)
    _smooth_circle_cache[key] = img


def _get_pil():
    global _PIL_MODULES
    if _PIL_MODULES is not None:
        return _PIL_MODULES
    try:
        from netutils import ensure_vendored_pil
        ensure_vendored_pil()
    except Exception:
        pass
    try:
        from PIL import Image, ImageDraw
        import io
        _PIL_MODULES = (Image, ImageDraw, io)
    except Exception:
        _PIL_MODULES = False
    return _PIL_MODULES


def smooth_circle_photo(size, fill_hex, ring_hex=None, ring_width=0):
    """아바타 원(이니셜·그룹 이중 원) 및 스티커 배경 원을 위한 매끄러운(anti-aliased)
    원형 PhotoImage를 반환한다. 원 바깥쪽은 100% 완전 투명(alpha=0) 처리되며, 경계면은
    Pillow(동봉 버전 포함)가 있으면 2배 슈퍼샘플링 + 바이리니어 리샘플링으로 초고속(수 ms)
    생성하고, Pillow가 없는 환경에서는 4x4(16샘플) 순수 파이썬 슈퍼샘플링으로 자동 폴백한다.
    (fill, ring, size) 조합별로 캐시하므로 같은 조합은 다시 계산하지 않는다."""
    key = (size, fill_hex, ring_hex, ring_width)
    cached = _smooth_circle_cache.get(key)
    if cached is not None:
        return cached

    pil = _get_pil()
    if pil:
        Image, ImageDraw, io = pil
        scale = 2
        w = size * scale
        im = Image.new("RGBA", (w, w), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if ring_hex and ring_width > 0:
            d.ellipse((0, 0, w - 1, w - 1), fill=ring_hex)
            rw = int(round(ring_width * scale))
            d.ellipse((rw, rw, w - 1 - rw, w - 1 - rw), fill=fill_hex)
        else:
            d.ellipse((0, 0, w - 1, w - 1), fill=fill_hex)

        im = im.resize((size, size), Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        img = tk.PhotoImage(data=buf.getvalue(), format="png")
        _cache_circle(key, img)
        return img

    # 순수 파이썬 폴백 (Pillow 부재 시)
    fill_rgb = _hex_to_rgb(fill_hex)
    ring_rgb = _hex_to_rgb(ring_hex) if ring_hex else None
    cx = cy = size / 2.0
    r_outer = size / 2.0 - 0.5
    r_inner = r_outer - ring_width if ring_rgb else r_outer
    r_outer_sq = r_outer * r_outer
    r_inner_sq = r_inner * r_inner if ring_rgb else r_outer_sq

    # 4x4 서브픽셀 슈퍼샘플링 오프셋 (16샘플링으로 최상의 앤티앨리어싱 품질)
    sub = (0.125, 0.375, 0.625, 0.875)

    buf = bytearray(size * size * 4)
    for y in range(size):
        row = y * size
        for x in range(size):
            cnt_outer = 0
            cnt_inner = 0
            for sy in sub:
                py = y + sy - cy
                py_sq = py * py
                for sx in sub:
                    px = x + sx - cx
                    dist_sq = px * px + py_sq
                    if dist_sq <= r_outer_sq:
                        cnt_outer += 1
                        if ring_rgb and dist_sq <= r_inner_sq:
                            cnt_inner += 1
            if cnt_outer == 0:
                continue  # 원 바깥은 100% 완전 투명 (RGBA 전부 0)

            cov_outer = cnt_outer / 16.0
            idx = (row + x) * 4
            if ring_rgb:
                cov_inner = cnt_inner / 16.0
                weight_fill = cov_inner / cov_outer if cov_outer > 0 else 0.0
                rr = fill_rgb[0] * weight_fill + ring_rgb[0] * (1.0 - weight_fill)
                gg = fill_rgb[1] * weight_fill + ring_rgb[1] * (1.0 - weight_fill)
                bb = fill_rgb[2] * weight_fill + ring_rgb[2] * (1.0 - weight_fill)
            else:
                rr, gg, bb = fill_rgb

            buf[idx] = int(round(rr))
            buf[idx + 1] = int(round(gg))
            buf[idx + 2] = int(round(bb))
            buf[idx + 3] = int(round(cov_outer * 255))

    png_bytes = _encode_png_rgba(size, size, buf)
    img = tk.PhotoImage(data=png_bytes, format="png")
    _cache_circle(key, img)
    return img


def register_outside_click(widget, callback):
    """전역 바깥쪽 클릭 감지를 위한 공용 디스패처.

    우클릭 메뉴, @ 멘션 자동완성 팝업처럼 서로 독립적으로 열릴 수 있는 팝업이
    여러 개 있는데, 각자 bind_all()로 걸어두고 닫힐 때 unbind_all()로 해제하면
    문제가 생긴다 — unbind_all(seq)는 그 시퀀스에 걸린 "모든" 핸들러를 지워버리는
    tkinter의 전역 동작이라, 하나가 닫히는 순간 다른 팝업의 바깥 클릭 감지까지
    영구히 함께 사라진다. 시퀀스당 디스패처를 단 한 번만 bind_all해 두고, 각
    호출자는 이 안의 리스트에 콜백을 등록/해제만 하도록 해서 서로 간섭하지 않게
    한다. 등록 해제 함수를 반환한다.

    widget.winfo_toplevel()이 아니라 widget._root()를 쓴다 — 팝업 다이얼로그
    (Toplevel) 안의 위젯으로 이 함수를 호출하면 winfo_toplevel()은 그 다이얼로그
    자신을 반환해서, 다이얼로그마다 서로 다른 "root"에 상태를 저장한 채 매번
    bind_all()을 다시 걸어버린다(bind_all은 위젯과 무관하게 앱 전체에 걸리는
    호출이라, 나중에 연 다이얼로그가 이전 디스패처를 덮어쓰고, 그 다이얼로그가
    닫히면 메인 창의 감지까지 죽어버린다). _root()는 Toplevel을 몇 겹 거치든
    항상 앱의 진짜 최상위 Tk() 인스턴스 하나로 귀결되므로 이 문제가 없다."""
    root = widget._root()
    registry = getattr(root, "_outside_click_registry", None)
    if registry is None:
        registry = {"<Button-1>": [], "<Button-2>": [], "<Button-3>": []}
        root._outside_click_registry = registry

        def _make_dispatch(seq):
            def _dispatch(e):
                for cb in list(registry.get(seq, [])):
                    try:
                        cb(e)
                    except Exception:
                        pass
            return _dispatch

        for seq in registry:
            root.bind_all(seq, _make_dispatch(seq), add="+")

    for seq in registry:
        registry[seq].append(callback)

    def _unregister():
        for seq in registry:
            try:
                registry[seq].remove(callback)
            except ValueError:
                pass
    return _unregister


def bind_scoped_mousewheel(hover_widget, canvas):
    """hover_widget 위에 마우스가 있는 동안만 canvas가 휠 스크롤을 받도록 연결한다.

    여러 스크롤 목록(메인 대화 목록, 채팅창, 팝업 다이얼로그의 멤버/피커 목록 등)이
    각자 bind_all("<MouseWheel>")로 걸었다 unbind_all()로 풀었다 하면 서로 부딪힌다
    — tkinter의 bind_all("<MouseWheel>")은 add="+" 없이는 그 시퀀스의 기존 전역
    핸들러를 통째로 "교체"하고, unbind_all()은 그 시퀀스에 걸린 핸들러를 전부
    "삭제"한다. 즉 다이얼로그 하나를 열고 닫는 것만으로 메인 목록의 휠 스크롤이
    영구히 끊길 수 있다. 대신 앱 전체에 디스패처를 단 한 번만 bind_all해 두고,
    "지금 스크롤 대상이 어느 canvas인지"만 갈아끼우는 방식으로 서로 간섭 없이
    여러 목록이 이 패턴을 함께 쓸 수 있게 한다.

    hover_widget.winfo_toplevel()이 아니라 hover_widget._root()를 쓴다 — 팝업
    다이얼로그(Toplevel) 안에서 이 함수를 호출하면 winfo_toplevel()은 그
    다이얼로그 자신을 반환해 버려서, 다이얼로그를 열 때마다 별개의 "root"에
    새 state를 만들고 bind_all()을 다시 걸게 된다. bind_all은 어떤 위젯을 통해
    불렀든 앱 전체에 걸리는 호출이라, 이러면 새 다이얼로그의 디스패처가 메인
    창의 디스패처를 덮어써 버리고, 그 다이얼로그가 닫히고 나면 메인 창 스크롤이
    영영 죽은 dispatcher를 참조하게 된다. _root()는 Toplevel을 몇 겹 거치든
    항상 앱의 진짜 최상위 Tk() 인스턴스 하나로 귀결되므로 이 문제가 없다.
    반환값: 강제로 스크롤 연결을 해제하는 함수(다이얼로그 파괴 시 호출용)."""
    root = hover_widget._root()
    state = getattr(root, "_mousewheel_scope", None)
    if state is None:
        state = {"active": None}
        root._mousewheel_scope = state

        def _dispatch(e):
            cv = state["active"]
            if cv is not None:
                try:
                    if cv.winfo_exists():
                        safe_canvas_mousewheel(cv, e)
                    else:
                        state["active"] = None
                except Exception:
                    state["active"] = None
        root.bind_all("<MouseWheel>", _dispatch)

    def _enter(_e=None):
        state["active"] = canvas

    def _leave(_e=None):
        if state["active"] is canvas:
            state["active"] = None

    hover_widget.bind("<Enter>", _enter, add="+")
    hover_widget.bind("<Leave>", _leave, add="+")
    return _leave


def safe_canvas_mousewheel(canvas, event_or_delta, step=1):
    """스크롤이 불필요한 상태(내용이 캔버스 화면 안에 모두 표시되어 스크롤바가
    비활성화된 상태)에서 마우스 휠을 굴릴 때, 캔버스 내부 좌표가 음수로 표류(drift)하여
    목록이나 메시지가 화면 아래로 밀려 내려가는 현상을 원천 방지한다.

    스크롤이 필요한 경우에만 정상 스크롤하며, 상단/하단 경계에 도달했을 때도
    오버스크롤로 인한 좌표 왜곡을 방지한다.
    """
    try:
        top, bot = canvas.yview()
    except Exception:
        return

    # 1. 전체 내용이 한 화면에 다 들어오는 경우 (스크롤 불필요)
    if (bot - top) >= 0.999:
        if top != 0.0:
            try:
                canvas.yview_moveto(0.0)
            except Exception:
                pass
        return

    # 2. 스크롤이 필요한 경우: 휠 방향에 따른 유닛 계산
    if isinstance(event_or_delta, (int, float)):
        delta = event_or_delta
    else:
        delta = getattr(event_or_delta, "delta", 0)
    if not delta:
        return
    units = int(-1 * (delta / 120)) * step
    if not units:
        units = -1 if delta > 0 else 1

    # 상단 도달 시 위로 스크롤 방지
    if units < 0 and top <= 0.001:
        try:
            canvas.yview_moveto(0.0)
        except Exception:
            pass
        return
    # 하단 도달 시 아래로 스크롤 방지
    if units > 0 and bot >= 0.999:
        try:
            canvas.yview_moveto(1.0)
        except Exception:
            pass
        return

    try:
        canvas.yview_scroll(units, "units")
        if canvas.yview()[0] < 0.0:
            canvas.yview_moveto(0.0)
    except Exception:
        pass

