# -*- coding: utf-8 -*-
"""chat_renderer.py — Tkinter Canvas 기반 대화방 렌더링(말풍선, 파일카드, 배지, 스크롤) 믹스인.
app.py에서 분리됨 (유지보수 및 모듈화를 위한 리팩토링)."""
import os
import shutil
import subprocess
import tempfile
import webbrowser
import tkinter as tk
from tkinter import filedialog

from constants import *
from netutils import (date_str, korea_time_str, sanitize_chat_text, URL_REGEX,
                       ensure_vendored_pil)
from canvas_utils import round_rect, smooth_circle_photo
import stickers


class ChatRendererMixin:
    """채팅 캔버스 렌더링 및 말풍선/미디어 관리 믹스인."""

    _CHAT_IMAGES_CAP = 1000  # 대화방 하나를 계속 켜둔 채 오래 스크롤·수신해도 메모리가
    # 무한정 늘지 않도록 둔 상한(v6.48). 대화방을 전환하면 어차피 리스트 자체가 새로
    # 비워지므로(room switch 시 _chat_images = []), 이 상한은 "방 하나를 몇 시간이고
    # 계속 켜놓고 이미지를 잔뜩 주고받는" 극단적인 경우에만 작동한다. 다만 이 리스트는
    # 이미 캔버스에 그려진 PhotoImage가 가비지 컬렉션되지 않게 붙잡아두는 용도라, 상한을
    # 넘겨 가장 오래된 참조를 밀어내면 그 시점 이후 아주 옛날로 스크롤해 올라갈 때 해당
    # 이미지가 빈 칸으로 보일 수 있다 — 상한을 넉넉하게 잡아 그 가능성을 낮췄다.

    def _push_chat_image(self, img):
        self._chat_images.append(img)
        if len(self._chat_images) > self._CHAT_IMAGES_CAP:
            del self._chat_images[:len(self._chat_images) - self._CHAT_IMAGES_CAP]

    # ---------- 채팅 캔버스 렌더 ----------
    def _on_chat_resize(self, _e=None):
        # 사이드바 슬라이드 애니메이션 중에는(_toggle_sidebar) side를 잠깐 pack에서
        # 뺐다가 애니메이션이 끝난 뒤 다시 넣는데, 그 두 순간(시작·종료)에 채팅
        # 캔버스가 실제로 크기 변경 이벤트(<Configure>)를 받는다. 이 디바운스
        # 타이머가 그대로 걸리면 애니메이션이 끝나기도 전에(120ms 후) 채팅 전체가
        # 다시 그려지거나, _toggle_sidebar가 애니메이션 종료 시 직접 부르는
        # 재렌더링과 겹쳐 두 번 그려지면서 스크롤-맨아래 버튼 같은 오버레이
        # 위젯이 깜빡이는 것처럼 보였다(v6.48) — 애니메이션 진행 중에는 이 타이머를
        # 걸지 않는다. 애니메이션이 끝나면 _toggle_sidebar가 직접 재렌더링한다.
        if getattr(self, "_sidebar_anim_job", None):
            return
        if self._resize_job:
            try:
                self.root.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.root.after(120, self._resize_apply)

    def _resize_apply(self):
        self._resize_job = None
        if not self.current:
            return
        # 말풍선 줄바꿈/레이아웃은 가로 폭에만 의존한다 — 창 높이만 바뀐 경우(세로
        # 크기 조절, 스플리터 드래그로 폭이 그대로인 경우 등)까지 매번 전체를
        # 다시 그리는 건 낭비라 폭이 실제로 달라졌을 때만 재렌더링한다.
        new_w = self._chat_width()
        if new_w == getattr(self, "_last_rendered_width", None):
            return
        self._last_rendered_width = new_w
        self._reload_chat(self.current, from_cache=True)

    # ---------- 맨 아래로 스크롤 플로팅 버튼 ----------
    def _update_scroll_btn(self, first=None, last=None):
        if not hasattr(self, "scroll_btn"):
            return
        if not self.current or not self._chat_has_content:
            self._hide_scroll_btn()
            return
        if first is None or last is None:
            try:
                first, last = self.chat.yview()
            except Exception:
                return
        # 맨 아래에 있거나(last >= 0.98), 전체 내용이 한 화면에 다 들어오는 경우 숨김
        if last >= 0.98 or (first <= 0.001 and last >= 0.999):
            self._hide_scroll_btn()
        else:
            self._show_scroll_btn()

    def _show_scroll_btn(self):
        if not self._scroll_btn_visible:
            self.scroll_btn.place(in_=self.chat, relx=1.0, rely=1.0, x=-16, y=-16, anchor="se")
            self.scroll_btn.lift()
            self._scroll_btn_visible = True

    def _hide_scroll_btn(self):
        if getattr(self, "_scroll_btn_visible", False):
            self.scroll_btn.place_forget()
            self._scroll_btn_visible = False

    def _on_scroll_btn_click(self):
        self._hide_scroll_btn()
        self._scroll_to_bottom(smooth=True)

    def _scroll_to_bottom(self, smooth=True):
        for j in getattr(self, "_scroll_anim_jobs", []):
            try:
                self.root.after_cancel(j)
            except Exception:
                pass
        self._scroll_anim_jobs = []

        if not smooth:
            self.chat.yview_moveto(1.0)
            self._update_scroll_btn()
            return

        try:
            top, bot = self.chat.yview()
        except Exception:
            self.chat.yview_moveto(1.0)
            return

        span = bot - top
        if span >= 0.999 or bot >= 0.999:
            self.chat.yview_moveto(1.0)
            return

        target_top = max(0.0, 1.0 - span)
        start_top = top
        steps = 7
        step_ms = 14
        for i in range(1, steps + 1):
            t = i / steps
            ease = 1 - (1 - t) * (1 - t)
            p = start_top + (target_top - start_top) * ease
            if i == steps:
                job = self.root.after(i * step_ms, lambda: self.chat.yview_moveto(1.0))
            else:
                job = self.root.after(i * step_ms, lambda pos=p: self.chat.yview_moveto(pos))
            self._scroll_anim_jobs.append(job)

    def _chat_width(self):
        w = self.chat.winfo_width()
        return w if w and w > 10 else 700

    def _draw_date_sep(self, d):
        w = self._chat_width()
        y = self._chat_y + 8
        tid = self.chat.create_text(w / 2, y, text=d, font=FONT_XS_PAD, fill=C_MUTE, anchor="n")
        bbox = self.chat.bbox(tid)
        pad = 6
        rid = round_rect(self.chat, bbox[0] - pad * 2, bbox[1] - pad, bbox[2] + pad * 2,
                         bbox[3] + pad, r=10, fill=C_SEARCHBG, outline="")
        self.chat.tag_lower(rid, tid)
        self._peer_avatar_bottom = 0
        self._chat_y = bbox[3] + pad + 12

    def _draw_system_sep(self, text):
        w = self._chat_width()
        y = self._chat_y + 6
        tid = self.chat.create_text(w / 2, y, text=text, font=FONT_XS, fill=C_MUTE, anchor="n")
        bbox = self.chat.bbox(tid)
        pad = 6
        rid = round_rect(self.chat, bbox[0] - pad * 2, bbox[1] - pad, bbox[2] + pad * 2,
                         bbox[3] + pad, r=8, fill=C_SEARCHBG, outline="")
        self.chat.tag_lower(rid, tid)
        self._peer_avatar_bottom = 0
        self._chat_y = bbox[3] + pad + 10

    @staticmethod
    def _burn_label(seconds):
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return ""
        if seconds <= 0:
            return ""
        if seconds < 60:
            return f"{seconds}초"
        if seconds < 3600:
            return f"{seconds // 60}분"
        return f"{seconds // 3600}시간"

    def _draw_label(self, mine, label, tmin, unread=False, rec=None):
        w = self._chat_width()
        burn_sec = (rec or {}).get("burn_sec")
        burn_tag = f"🔥{self._burn_label(burn_sec)}  " if burn_sec else ""
        if mine:
            self._peer_avatar_bottom = 0
            unread_cnt = (rec or {}).get("unread_count")
            if unread_cnt is not None:
                has_unread = (unread_cnt > 0)
                badge_num = str(unread_cnt)
            else:
                has_unread = bool(unread)
                badge_num = "1"
            full_txt = f"{burn_tag}{badge_num}  {tmin}" if has_unread else f"{burn_tag}{tmin}"
            tid = self.chat.create_text(w - MARGIN_SIDE, self._chat_y, text=full_txt,
                                        font=FONT_XS, fill=C_AWAY if has_unread else C_MUTE, anchor="ne")
            bbox = self.chat.bbox(tid)
            self._chat_y = bbox[3] + 4
        else:
            # 카카오톡풍 상대 프로필 원형 아바타 + 시인성 높은 굵은 발신자 이름 + 시각 라벨
            ax1 = MARGIN_SIDE
            ay1 = self._chat_y
            ax2 = ax1 + PEER_AVATAR_SIZE
            ay2 = ay1 + PEER_AVATAR_SIZE
            bg = self._avacolor(label)

            # 1) 등록된 프로필 사진 확인 (DM 또는 레코드)
            av_hash = (rec or {}).get("av")
            if not av_hash and self.current and self.current[0] == "dm" and self.engine:
                with self.engine.plock:
                    p = self.engine.peers.get((self.current[1], self.current[2])) or {}
                    av_hash = p.get("av")
            photo = self._get_avatar_photo(av_hash, target=PEER_AVATAR_SIZE) if av_hash else None

            if photo is not None:
                cx = ax1 + PEER_AVATAR_SIZE // 2
                cy = ay1 + PEER_AVATAR_SIZE // 2
                self.chat.create_image(cx, cy, image=photo, anchor="center")
                self._push_chat_image(photo)
            else:
                # 2) 사진이 없는 경우: 4x4 슈퍼샘플링 앤티앨리어싱된 부드러운 단색 원형 이미지 + 중앙 이니셜
                photo = smooth_circle_photo(PEER_AVATAR_SIZE, bg)
                self.chat.create_image(ax1, ay1, image=photo, anchor="nw")
                self._push_chat_image(photo)
                cx = ax1 + PEER_AVATAR_SIZE / 2
                cy = ay1 + PEER_AVATAR_SIZE / 2
                self.chat.create_text(cx, cy, text=self._initial(label),
                                      font=FONT_CHAT_AV, fill="white", anchor="center")

            self._peer_avatar_bottom = ay2
            name_x = PEER_BUBBLE_X
            name_y = ay1 + 1
            max_name_w = max(100, int(w - PEER_BUBBLE_X - MARGIN_SIDE - 70))
            nid = self.chat.create_text(name_x, name_y, text=(label or "상대방"),
                                        font=FONT_SENDER, fill=C_TEXT, anchor="nw",
                                        width=max_name_w)
            nb = self.chat.bbox(nid)
            tid = self.chat.create_text(nb[2] + 7, nb[3] - 1, text=f"{burn_tag}{tmin}",
                                        font=FONT_XS, fill=C_MUTE, anchor="sw")
            tb = self.chat.bbox(tid)
            tb3 = tb[3] if tb else nb[3]
            self._chat_y = max(nb[3], tb3) + 4

    def _bind_copy(self, text, *item_ids):
        def _copy(_e=None):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                self.status.set("메시지를 복사했습니다")
            except tk.TclError:
                pass

        for iid in item_ids:
            self.chat.tag_bind(iid, "<Button-1>", _copy)
            self.chat.tag_bind(iid, "<Enter>", lambda e: self.chat.config(cursor="hand2"))
            self.chat.tag_bind(iid, "<Leave>", lambda e: self.chat.config(cursor="arrow"))

    def _draw_bubble(self, mine, text, reply=None, sender_name="", mid=None, ts=None):
        text = sanitize_chat_text(text or "")
        w = self._chat_width()
        top = self._chat_y
        if mine:
            max_w = max(140, min(int(w * 0.72), 460))
        else:
            avail_w = max(140, w - MARGIN_SIDE - PEER_BUBBLE_X)
            max_w = max(140, min(int(avail_w * 0.82), 440))

        # 인용 답장(Quote Reply) 렌더
        reply_h = 0
        reply_tid = None
        reply_bar_id = None
        reply_bg_id = None
        if reply and isinstance(reply, dict):
            r_sender = (reply.get("name") or "답장").strip()
            r_snippet = (reply.get("text") or "").replace("\n", " ").strip()[:35]
            q_text = f"{r_sender}\n{r_snippet}"
            if mine:
                reply_tid = self.chat.create_text(w - MARGIN_SIDE, top + BUBBLE_PAD_V, text=q_text, font=FONT_XS,
                                                  fill="#bfdbfe", anchor="ne",
                                                  width=max_w - 12, justify="right")
            else:
                reply_tid = self.chat.create_text(PEER_BUBBLE_X, top + BUBBLE_PAD_V, text=q_text, font=FONT_XS,
                                                  fill=C_MUTE, anchor="nw",
                                                  width=max_w - 12, justify="left")
            rb = self.chat.bbox(reply_tid)
            reply_h = (rb[3] - rb[1]) + 8

        if mine:
            tid = self.chat.create_text(w - MARGIN_SIDE, top + BUBBLE_PAD_V + reply_h, text=text, font=FONT_MSG,
                                        fill="white", anchor="ne",
                                        width=max_w, justify="right")
        else:
            tid = self.chat.create_text(PEER_BUBBLE_X, top + BUBBLE_PAD_V + reply_h, text=text, font=FONT_MSG,
                                        fill=C_TEXT, anchor="nw",
                                        width=max_w, justify="left")
        bx1, by1, bx2, by2 = self.chat.bbox(tid)
        content_w = bx2 - bx1
        if reply_tid:
            rb = self.chat.bbox(reply_tid)
            content_w = max(content_w, rb[2] - rb[0] + 10)
        bw = content_w + 2 * BUBBLE_PAD_H
        if mine:
            rx2 = w - MARGIN_SIDE
            rx1 = rx2 - bw
        else:
            rx1 = PEER_BUBBLE_X
            rx2 = rx1 + bw
        ry1 = top
        ry2 = by2 + BUBBLE_PAD_V
        if mine:
            dx = (rx2 - BUBBLE_PAD_H) - bx2
        else:
            dx = (rx1 + BUBBLE_PAD_H) - bx1
        if dx:
            self.chat.move(tid, dx, 0)
        if reply_tid:
            rb = self.chat.bbox(reply_tid)
            if mine:
                rdx = (rx2 - BUBBLE_PAD_H - 8) - rb[2]
            else:
                rdx = (rx1 + BUBBLE_PAD_H + 8) - rb[0]
            self.chat.move(reply_tid, rdx, 0)
            rb = self.chat.bbox(reply_tid)
            reply_bg_id = round_rect(self.chat, rx1 + 6, ry1 + 6, rx2 - 6, ry1 + reply_h + 2, r=6,
                                     fill=("#1e3a8a" if mine else C_REPLY_BG), outline="")
            reply_bar_id = self.chat.create_rectangle(rx1 + 6, ry1 + 6, rx1 + 9, ry1 + reply_h + 2,
                                                      fill=("#93c5fd" if mine else C_REPLY_BAR), width=0)
            self.chat.tag_raise(reply_tid, reply_bg_id)
            self.chat.tag_raise(reply_bar_id, reply_bg_id)

        fill = C_ME if mine else C_PEER
        outline = "" if mine else C_BORDER
        if not mine and self.engine and f"@{self.engine.name}" in text:
            outline = "#38bdf8"
        rid = round_rect(self.chat, rx1, ry1, rx2, ry2, r=BUBBLE_RADIUS, fill=fill, outline=outline)
        self.chat.tag_lower(rid, tid)
        if reply_bg_id:
            self.chat.tag_lower(reply_bg_id, tid)
            self.chat.tag_lower(rid, reply_bg_id)

        # URL 탐지 및 컨텍스트 메뉴
        urls = URL_REGEX.findall(text)
        def _open_url(u):
            if not u.startswith(("http://", "https://", "ftp://")):
                u = "https://" + u
            webbrowser.open(u)

        def _context_menu(e):
            items = [
                ("복사", lambda: (self.root.clipboard_clear(), self.root.clipboard_append(text), self.status.set("메시지를 복사했습니다"))),
                ("답장", lambda: self._start_reply(sender_name or ("나" if mine else "상대방"), text)),
            ]
            if mid:
                current_notice = getattr(self, "_pinned_notice", None)
                if current_notice and current_notice.get("mid") == mid:
                    items.append(("📌 공지 해제", self._unpin_current_notice))
                else:
                    items.append(("📌 공지로 등록",
                                 lambda: self._set_message_as_notice(mid, text, sender_name or ("나" if mine else "상대방"), ts)))
            if urls:
                items.append((None, None))
                for u in urls[:3]:
                    items.append((f"🔗 링크 열기 ({u[:24]}...)", lambda u=u: _open_url(u)))
            self._popup_menu(e.x_root, e.y_root, items)

        all_ids = [rid, tid]
        if reply_tid:
            all_ids.extend([reply_tid, reply_bg_id, reply_bar_id])
        self._bind_copy(text, *all_ids)
        for iid in all_ids:
            self.chat.tag_bind(iid, "<Button-3>", _context_menu)
            if urls:
                self.chat.tag_bind(iid, "<Double-Button-1>", lambda e, u=urls[0]: _open_url(u))

        next_y = ry2 + 2
        if not mine and self._peer_avatar_bottom:
            next_y = max(next_y, self._peer_avatar_bottom + 4)
            self._peer_avatar_bottom = 0
        self._chat_y = next_y

    # ---------- 파일·이미지 말풍선 ----------
    def _human_size(self, n):
        n = n or 0
        if n < 1024:
            return f"{n}B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f}KB"
        return f"{n / 1024 / 1024:.1f}MB"

    def _show_in_folder(self, path):
        if not path or not os.path.exists(path):
            self._embed_alert("파일 없음", "파일이 존재하지 않습니다.", kind="warning")
            return
        try:
            subprocess.run(["explorer", f"/select,{os.path.normpath(path)}"])
        except Exception:
            try:
                os.startfile(os.path.dirname(path))
            except OSError:
                pass

    def _save_file_as(self, path):
        if not path or not os.path.exists(path):
            self._embed_alert("파일 없음", "파일이 존재하지 않습니다.", kind="warning")
            return
        fname = os.path.basename(path)
        dest = filedialog.asksaveasfilename(
            parent=self.root,
            title="다른 이름으로 저장",
            initialfile=fname,
            initialdir=self.engine.download_dir if self.engine else None
        )
        if dest:
            try:
                shutil.copy2(path, dest)
                self.status.set(f"저장 완료: {dest}")
            except OSError as e:
                self._embed_alert("저장 실패", f"파일을 저장하지 못했습니다:\n{e}", kind="error")

    def _bind_open(self, path, *item_ids):
        def _open(_e=None):
            ext = os.path.splitext(path)[1].lower()
            if ext in RISKY_FILE_EXTS:
                ok = self._embed_confirm(
                    "실행 파일 열기",
                    f"'{os.path.basename(path)}'은(는) 실행 가능한 파일입니다.\n"
                    "출처를 신뢰할 수 없다면 악성 코드일 수 있습니다.\n\n그래도 실행하시겠습니까?")
                if not ok:
                    return
            try:
                os.startfile(path)
            except OSError:
                self._embed_alert("파일 열기 실패", f"파일을 열 수 없습니다:\n{path}", kind="warning")

        def _context_menu(e):
            items = [
                ("열기", _open),
                ("폴더에서 보기", lambda: self._show_in_folder(path)),
                ("다른 이름으로 저장...", lambda: self._save_file_as(path)),
                (None, None),
                ("다운로드 폴더 설정...", self._download_dir_dialog),
            ]
            self._popup_menu(e.x_root, e.y_root, items)

        for iid in item_ids:
            self.chat.tag_bind(iid, "<Button-1>", _open)
            self.chat.tag_bind(iid, "<Button-3>", _context_menu)
            self.chat.tag_bind(iid, "<Enter>", lambda e: self.chat.config(cursor="hand2"))
            self.chat.tag_bind(iid, "<Leave>", lambda e: self.chat.config(cursor="arrow"))

    def _load_thumbnail_pil(self, path):
        ensure_vendored_pil()
        try:
            from PIL import Image
        except ImportError:
            return None
        tmp_path = None
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                fd, tmp_path = tempfile.mkstemp(suffix=".ppm", prefix="lanmsg_thumb_")
                os.close(fd)
                im.save(tmp_path, format="PPM")
            return tk.PhotoImage(file=tmp_path)
        except Exception:
            return None
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _load_thumbnail(self, path):
        if not path:
            return None
        mtime = 0.0
        try:
            mtime = os.path.getmtime(path)
            cached = getattr(self, "_thumbnail_cache", {}).get(path)
            if cached and cached[0] == mtime:
                return cached[1]
        except OSError:
            pass

        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in PIL_IMAGE_EXTS:
                img = self._load_thumbnail_pil(path)
                if img is None:
                    return None
            else:
                img = tk.PhotoImage(file=path)
        except (tk.TclError, OSError):
            return None
        max_side = 220
        factor = max(1, -(-max(img.width(), img.height()) // max_side))  # 올림 나눗셈
        if factor > 1:
            try:
                img = img.subsample(factor, factor)
            except tk.TclError:
                pass

        if not hasattr(self, "_thumbnail_cache"):
            self._thumbnail_cache = {}
        if len(self._thumbnail_cache) >= 64:
            first_k = next(iter(self._thumbnail_cache))
            self._thumbnail_cache.pop(first_k, None)
        self._thumbnail_cache[path] = (mtime, img)
        return img

    def _draw_image_bubble(self, mine, img, path):
        w = self._chat_width()
        top = self._chat_y
        iw, ih = img.width(), img.height()
        ix = (w - MARGIN_SIDE - iw) if mine else PEER_BUBBLE_X
        iid = self.chat.create_image(ix, top, image=img, anchor="nw")
        self._push_chat_image(img)  # GC 방지
        pad = 3
        rid = round_rect(self.chat, ix - pad, top - pad, ix + iw + pad, top + ih + pad,
                         r=BUBBLE_RADIUS, fill=(C_ME if mine else C_PEER),
                         outline=("" if mine else C_BORDER))
        self.chat.tag_lower(rid, iid)
        exists = bool(path) and os.path.exists(path)
        if exists:
            self._bind_open(path, rid, iid)
        next_y = top + ih + pad + 2
        if not mine and self._peer_avatar_bottom:
            next_y = max(next_y, self._peer_avatar_bottom + 4)
            self._peer_avatar_bottom = 0
        self._chat_y = next_y

    def _draw_file_card(self, mine, rec):
        w = self._chat_width()
        fname = rec.get("fname") or "파일"
        size_txt = self._human_size(rec.get("size"))
        path = rec.get("path") or ""
        exists = bool(path) and os.path.exists(path)
        done = rec.get("state") == "done"
        if mine:
            card_w = min(260, max(160, int(w * 0.72)))
        else:
            avail_w = max(160, w - MARGIN_SIDE - PEER_BUBBLE_X)
            card_w = min(260, max(160, int(avail_w * 0.82)))
        pad = 12
        icon_w = 34
        top = self._chat_y
        name_tid = self.chat.create_text(0, 0, text=fname, font=FONT_NAME,
                                         fill=("white" if mine else C_TEXT), anchor="nw",
                                         width=card_w - 2 * pad - icon_w - 20)
        nb = self.chat.bbox(name_tid)
        if exists:
            sub = size_txt
        elif done:
            # 전송 자체는 완료됐지만(수신 기록에 남음) 사용자가 다운로드 폴더에서
            # 파일을 직접 지웠거나 옮긴 경우 — "전송 중…"이라고 하면 영영 끝나지
            # 않을 것처럼 보여 혼란을 준다("전송 중" 버그, v6.48).
            sub = f"{size_txt} · 파일 없음(삭제되었거나 이동됨)"
        else:
            sub = f"{size_txt} · 전송 중…"
        sub_tid = self.chat.create_text(0, 0, text=sub, font=FONT_XS,
                                        fill=("#dbe7fb" if mine else C_MUTE), anchor="nw")
        sb = self.chat.bbox(sub_tid)
        text_h = (nb[3] - nb[1]) + 4 + (sb[3] - sb[1])
        card_h = max(52, text_h + 2 * pad)
        if mine:
            cx2 = w - MARGIN_SIDE
            cx1 = cx2 - card_w
        else:
            cx1 = PEER_BUBBLE_X
            cx2 = cx1 + card_w
        cy1, cy2 = top, top + card_h
        rid = round_rect(self.chat, cx1, cy1, cx2, cy2, r=BUBBLE_RADIUS,
                         fill=(C_ME if mine else C_PEER), outline=("" if mine else C_BORDER))
        icon_cx, icon_cy = cx1 + pad + 12, cy1 + card_h / 2
        file_dot_bg = "#ffffff" if mine else C_SEARCHBG
        photo_file_dot = smooth_circle_photo(28, file_dot_bg)
        dot_id = self.chat.create_image(icon_cx - 14, icon_cy - 14, image=photo_file_dot, anchor="nw")
        self._push_chat_image(photo_file_dot)
        glyph_id = self.chat.create_text(icon_cx, icon_cy, text="📄", font=(FONT_FAM, 12))
        tx, ty = cx1 + pad + icon_w, cy1 + pad
        self.chat.coords(name_tid, tx, ty)
        self.chat.coords(sub_tid, tx, ty + (nb[3] - nb[1]) + 4)
        for iid in (rid, dot_id, glyph_id):
            self.chat.tag_lower(iid, name_tid)
        self.chat.tag_raise(dot_id, rid)
        self.chat.tag_raise(glyph_id, dot_id)
        if exists:
            save_id = self.chat.create_text(cx2 - pad - 6, cy1 + card_h / 2, text="💾",
                                            font=(FONT_FAM, 10), fill=("#dbe7fb" if mine else C_MUTE))
            self.chat.tag_bind(save_id, "<Button-1>", lambda e: self._save_file_as(path))
            self.chat.tag_bind(save_id, "<Enter>", lambda e: (self.chat.config(cursor="hand2"),
                                                             self.chat.itemconfigure(save_id, fill=C_ME)))
            self.chat.tag_bind(save_id, "<Leave>", lambda e: (self.chat.config(cursor="arrow"),
                                                             self.chat.itemconfigure(save_id, fill=("#dbe7fb" if mine else C_MUTE))))
            self._bind_open(path, rid, dot_id, glyph_id, name_tid, sub_tid)
        next_y = cy2 + 2
        if not mine and self._peer_avatar_bottom:
            next_y = max(next_y, self._peer_avatar_bottom + 4)
            self._peer_avatar_bottom = 0
        self._chat_y = next_y

    def _draw_file_item(self, mine, rec):
        path = rec.get("path") or ""
        if rec.get("is_image") and path and os.path.exists(path):
            img = self._load_thumbnail(path)
            if img is not None:
                self._draw_image_bubble(mine, img, path)
                return
        self._draw_file_card(mine, rec)

    def _draw_sticker_item(self, mine, rec):
        """스티커는 카카오톡·텔레그램처럼 말풍선 배경 없이 그림만 떠 있는 형태로
        그린다(둥근 배경은 stickers.draw_sticker 안에서 이미 원형으로 그려짐)."""
        w = self._chat_width()
        d = STICKER_DIAMETER
        r = d / 2
        top = self._chat_y
        cx = (w - MARGIN_SIDE - r) if mine else (PEER_BUBBLE_X + r)
        cy = top + r
        stickers.draw_sticker(self.chat, cx, cy, r, rec.get("sticker_id"), self._chat_images)
        next_y = top + d + 8
        if not mine and self._peer_avatar_bottom:
            next_y = max(next_y, self._peer_avatar_bottom + 4)
            self._peer_avatar_bottom = 0
        self._chat_y = next_y

    def _draw_fail(self, text):
        w = self._chat_width()
        tid = self.chat.create_text(w / 2, self._chat_y, text=text, font=FONT_FAIL, fill=C_FAIL,
                                    anchor="n", width=w - 2 * MARGIN_SIDE, justify="center")
        bbox = self.chat.bbox(tid)
        self._chat_y = bbox[3] + 10

    def _finish_render(self, force_bottom=False, restore_top=None):
        bottom_y = self._chat_y + PAD_BOTTOM
        w = self._chat_width()
        self.chat.configure(scrollregion=(0, 0, w, max(bottom_y, 10)))
        self._chat_has_content = self._chat_y > PAD_TOP
        was_bottom = True
        try:
            top, bot = self.chat.yview()
            was_bottom = (bot >= 0.95 or (bot - top) >= 0.999)
        except Exception:
            top, bot = 0.0, 1.0
            was_bottom = True
        if (bot - top) >= 0.999:
            self.root.after_idle(lambda: (self.chat.yview_moveto(0.0), self._update_scroll_btn()))
        elif force_bottom or (was_bottom and restore_top is None):
            self.root.after_idle(lambda: (self.chat.yview_moveto(1.0), self._update_scroll_btn()))
        elif restore_top is not None:
            self.root.after_idle(lambda: (self.chat.yview_moveto(restore_top), self._update_scroll_btn()))
        else:
            self.root.after_idle(self._update_scroll_btn)

    def _draw_record(self, rec, rec_idx=None):
        if rec.get("is_system"):
            self._draw_system_sep(rec.get("text", ""))
            return
        ts = rec.get("ts")
        d = date_str(ts)
        if d and self._last_day != d:
            self._last_day = d
            self._last_sender = None
            self._last_ts = None
            self._last_burn_sec = 0
            self._last_has_unread = False
            self._last_is_sticker = False
            self._chat_y += 6
            self._draw_date_sep(d)
        mine = rec["mine"]
        label = rec["label"]
        tmin = korea_time_str(ts)
        sender_key = (mine, label)
        unread = bool(rec.get("unread")) and mine
        burn_sec = rec.get("burn_sec") or 0
        last_burn = getattr(self, "_last_burn_sec", 0)
        unread_cnt = rec.get("unread_count")
        has_unread = (unread_cnt > 0) if unread_cnt is not None else unread
        last_unread = getattr(self, "_last_has_unread", False)

        is_same_group = (
            self._last_sender == sender_key
            and self._last_ts is not None
            and ts is not None
            and abs(ts - self._last_ts) <= GROUP_TIME_GAP
            and not burn_sec
            and not last_burn
            and (not mine or has_unread == last_unread)
        )
        if not is_same_group:
            if self._last_sender is not None:
                self._chat_y += BUBBLE_GAP_GROUP - 2
            self._draw_label(mine, label, tmin, unread=unread, rec=rec)
        else:
            is_sticker = (rec.get("kind") == "sticker")
            last_is_sticker = getattr(self, "_last_is_sticker", False)
            if is_sticker or last_is_sticker:
                self._chat_y += 10
            else:
                self._chat_y += BUBBLE_GAP_STACK

        record_start_y = self._chat_y
        if rec_idx is not None and hasattr(self, "_record_y_positions"):
            self._record_y_positions[rec_idx] = record_start_y

        if rec.get("kind") == "file":
            self._draw_file_item(mine, rec)
        elif rec.get("kind") == "sticker":
            self._draw_sticker_item(mine, rec)
        else:
            self._draw_bubble(mine, rec.get("text", ""), reply=rec.get("reply"), sender_name=label,
                              mid=rec.get("mid"), ts=ts)
        if rec_idx is not None and hasattr(self, "_record_y_end"):
            self._record_y_end[rec_idx] = self._chat_y
        self._last_sender = sender_key
        self._last_ts = ts
        self._last_burn_sec = burn_sec
        self._last_has_unread = has_unread
        self._last_is_sticker = (rec.get("kind") == "sticker")

    def _rec_from_raw(self, r, dname_or_none):
        """1:1/그룹 원본 로그 레코드를 렌더 파이프라인이 쓰는 공통 형태로 변환."""
        if dname_or_none is not None:  # DM
            mine = r.get("dir") == "out"
            label = "나" if mine else dname_or_none
            rec = {"mine": mine, "label": label, "ts": r.get("ts"), "reply": r.get("reply"),
                   "unread": bool(r.get("unread")), "mid": r.get("mid"), "av": r.get("av"),
                   "is_mention": bool(r.get("is_mention")), "burn_sec": r.get("burn_sec") or 0}
            if r.get("kind") == "sticker":
                rec.update(kind="sticker", sticker_id=r.get("sticker_id"))
            elif r.get("kind") == "file":
                rec.update(kind="file", fname=r.get("name", ""), size=r.get("size", 0),
                          path=r.get("path", ""), is_image=bool(r.get("is_image")))
            else:
                rec.update(kind="text", text=sanitize_chat_text(r.get("text", "")))
            return rec
        mine = bool(r.get("mine"))
        label = "나" if mine else (r.get("name") or "그룹원")
        rec = {"mine": mine, "label": label, "ts": r.get("ts"), "reply": r.get("reply"),
               "is_system": bool(r.get("is_system")), "av": r.get("av"), "mid": r.get("mid"),
               "unread_count": r.get("unread_count"), "is_mention": bool(r.get("is_mention")),
               "burn_sec": r.get("burn_sec") or 0}
        if r.get("kind") == "sticker":
            rec.update(kind="sticker", sticker_id=r.get("sticker_id"))
        elif r.get("kind") == "file":
            rec.update(kind="file", fname=r.get("fname", ""), size=r.get("size", 0),
                      path=r.get("path", ""), is_image=bool(r.get("is_image")))
        else:
            rec.update(kind="text", text=sanitize_chat_text(r.get("text", "")))
        return rec

    def _reload_chat(self, key, from_cache=False, force_bottom=False):
        saved_top = None
        was_bottom = True
        try:
            top, bot = self.chat.yview()
            was_bottom = (bot >= 0.95 or (bot - top) >= 0.999)
            saved_top = top
        except Exception:
            pass

        self._hide_scroll_btn()
        self.chat.delete("all")
        self._chat_y = PAD_TOP
        self._last_day = None
        self._last_sender = None
        self._last_ts = None
        self._last_burn_sec = 0
        self._last_has_unread = False
        self._last_is_sticker = False
        self._peer_avatar_bottom = 0
        self._chat_images = []
        self._record_y_positions = {}
        self._record_y_end = {}
        if from_cache and getattr(self, "_active_chat_key", None) == key and getattr(self, "_active_chat_records", None) is not None:
            records = self._active_chat_records
        else:
            if key[0] == "dm":
                _, ip, port = key
                with self.engine.plock:
                    p = self.engine.peers.get((ip, port)) or {}
                dname = self.engine.get_alias(key) or p.get("name") or ip
                raw = self.engine.load_history(ip, port)
                records = [self._rec_from_raw(r, dname) for r in raw]
            else:
                _, gid = key
                raw = self.engine.load_group_history(gid)
                records = [self._rec_from_raw(r, None) for r in raw]
            self._active_chat_key = key
            self._active_chat_records = list(records)
        for idx, rec in enumerate(records):
            self._draw_record(rec, rec_idx=idx)
        restore_y = None if (force_bottom or was_bottom) else saved_top
        # 리드로우 전에 판단한 was_bottom을 그대로 넘겨야 한다 — 폭이 바뀌어 말풍선이
        # 다시 줄바꿈되면 총 높이가 달라져서, 리드로우 "후"에 yview()로 다시 판정하면
        # 절대 위치는 그대로인데도 비율이 바뀌어 "맨 아래가 아님"으로 오판된다.
        # (스플리터를 조절하면 맨 아래 스크롤이 살짝 위로 올라오던 버그의 원인)
        self._finish_render(force_bottom=(force_bottom or was_bottom), restore_top=restore_y)

    def _append_bubble(self, mine, label, text, ts, reply=None, unread=False, burn_sec=0, unread_count=None):
        rec = {"mine": mine, "label": label, "ts": ts, "kind": "text", "text": text,
               "reply": reply, "unread": unread, "burn_sec": burn_sec}
        if unread_count is not None:
            rec["unread_count"] = unread_count
        if getattr(self, "_active_chat_records", None) is not None:
            idx = len(self._active_chat_records)
            self._append_active_record(rec)
            self._draw_record(rec, rec_idx=idx)
        else:
            self._draw_record(rec)
        self._finish_render(force_bottom=mine)

    def _append_sticker(self, mine, label, sticker_id, ts, unread=False, burn_sec=0, unread_count=None):
        rec = {"mine": mine, "label": label, "ts": ts, "kind": "sticker", "sticker_id": sticker_id,
               "unread": unread, "burn_sec": burn_sec}
        if unread_count is not None:
            rec["unread_count"] = unread_count
        if getattr(self, "_active_chat_records", None) is not None:
            idx = len(self._active_chat_records)
            self._append_active_record(rec)
            self._draw_record(rec, rec_idx=idx)
        else:
            self._draw_record(rec)
        self._finish_render(force_bottom=mine)
