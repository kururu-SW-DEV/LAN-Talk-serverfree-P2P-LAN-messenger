# -*- coding: utf-8 -*-
"""chat_search.py — 대화방 내 인라인 검색(Ctrl+F) 및 하이라이트 제어 믹스인.
app.py에서 분리됨 (유지보수 및 모듈화를 위한 리팩토링)."""
import tkinter as tk
from constants import C_SEARCH_HL
import stickers


class ChatSearchMixin:
    """대화방 내 인라인 검색(Ctrl+F) 믹스인."""

    def _append_active_record(self, rec):
        """새 메시지·파일·스티커가 대화방에 추가될 때 이 헬퍼로 등록하면,
        검색창이 열려 있고 검색어가 입력된 상태에서도 방금 도착한 레코드가
        검색 결과에 바로 반영된다. 예전에는 _active_chat_records만 append되고
        _search_matches는 그대로라, 검색 중 새 메시지가 오면 다음/이전 이동
        버튼이 그 메시지를 못 찾거나 낡은 인덱스로 스크롤될 수 있었다."""
        if getattr(self, "_active_chat_records", None) is None:
            return
        self._active_chat_records.append(rec)
        if hasattr(self, "search_bar") and self.search_bar.winfo_ismapped():
            q = self.search_bar.query_var.get()
            if q:
                self._on_search_query(q)

    def _open_search(self):
        if not hasattr(self, "search_bar") or not self.current:
            return
        self.search_bar.pack(fill="x", side="top", before=self.chat_wrap)
        self.search_bar.focus()

    def _close_search(self):
        if hasattr(self, "search_bar"):
            self.search_bar.pack_forget()
        self._search_matches = []
        self._search_cur_idx = -1
        self.entry.focus_set()

    def _on_search_query(self, q):
        q = (q or "").strip().lower()
        if not q or not getattr(self, "_active_chat_records", None):
            self._search_matches = []
            self._search_cur_idx = -1
            if hasattr(self, "search_bar"):
                self.search_bar.set_counts(0, 0)
            return
        matches = []
        for idx, rec in enumerate(self._active_chat_records):
            if rec.get("kind") == "sticker":
                # 스티커는 rec에 "text"/"fname"이 없어(sticker_id만 있음)
                # 검색에서 항상 빠지고 있었다 — 스티커 이름("축하","감사" 등)으로
                # 검색할 수 있도록 name을 검색 대상 텍스트로 사용한다.
                info = stickers.STICKERS.get(rec.get("sticker_id"))
                txt = " ".join([info.get("name", "")] + info.get("keywords", [])).lower() if info else ""
            else:
                txt = (rec.get("text") or rec.get("fname") or "").lower()
            if q in txt:
                matches.append(idx)
        self._search_matches = matches
        if matches:
            self._search_cur_idx = len(matches) - 1
            if hasattr(self, "search_bar"):
                self.search_bar.set_counts(self._search_cur_idx + 1, len(matches))
            self._scroll_to_match(matches[self._search_cur_idx])
        else:
            self._search_cur_idx = -1
            if hasattr(self, "search_bar"):
                self.search_bar.set_counts(0, 0)

    def _on_search_prev(self):
        if not getattr(self, "_search_matches", None):
            return
        self._search_cur_idx = (self._search_cur_idx - 1) % len(self._search_matches)
        if hasattr(self, "search_bar"):
            self.search_bar.set_counts(self._search_cur_idx + 1, len(self._search_matches))
        self._scroll_to_match(self._search_matches[self._search_cur_idx])

    def _on_search_next(self):
        if not getattr(self, "_search_matches", None):
            return
        self._search_cur_idx = (self._search_cur_idx + 1) % len(self._search_matches)
        if hasattr(self, "search_bar"):
            self.search_bar.set_counts(self._search_cur_idx + 1, len(self._search_matches))
        self._scroll_to_match(self._search_matches[self._search_cur_idx])

    def _scroll_to_match(self, rec_idx):
        if not hasattr(self, "_record_y_positions"):
            return
        target_y = self._record_y_positions.get(rec_idx)
        if target_y is None:
            return
        total_h = max(self._chat_y, 1)
        ratio = max(0.0, min(1.0, (target_y - 60) / total_h))
        self.chat.yview_moveto(ratio)
        self._update_scroll_btn()
        self.root.after(50, lambda: self._flash_highlight(rec_idx))

    def _flash_highlight(self, rec_idx):
        """검색으로 이동한 말풍선을 잠깐 테두리와 액센트 바로 강조 표시한다."""
        try:
            self.chat.delete("search_hl")
        except tk.TclError:
            return
        top = self._record_y_positions.get(rec_idx)
        bottom = self._record_y_end.get(rec_idx)
        if top is None or bottom is None:
            return
        w = self._chat_width()
        # 말풍선 내용을 가리지 않도록 fill=""(투명) 테두리와 좌측 액센트 바로 시인성 극대화
        rect = self.chat.create_rectangle(4, top - 4, w - 4, bottom + 4,
                                          fill="", outline="#f59e0b", width=2, tags="search_hl")
        self.chat.create_rectangle(4, top - 4, 8, bottom + 4,
                                   fill="#f59e0b", outline="", tags="search_hl")
        self.root.after(1400, lambda: self._clear_highlight(rect))

    def _clear_highlight(self, rect_id=None):
        try:
            if rect_id:
                self.chat.delete(rect_id)
            self.chat.delete("search_hl")
        except tk.TclError:
            pass
