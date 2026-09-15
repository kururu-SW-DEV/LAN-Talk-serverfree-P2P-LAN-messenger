# -*- coding: utf-8 -*-
"""engine.py — 네트워크 계층(UDP 발견/전송/파일전송/그룹/영속화). GUI와는 이벤트 큐로만 통신.
lan_messenger.py에서 분리됨 (유지보수를 위해 여러 파일로 분할, v5.1).

v5.1 변경: 모든 패킷을 crypto_layer로 암호화·인증(HMAC)해서 주고받는다 — 평문
스니핑과 이름(name) 필드 위조(스푸핑)를 막는다. secret.key가 없는 상대(구버전,
또는 아직 키가 복사되지 않은 PC)의 패킷은 검증에 실패해 조용히 버려진다."""
import base64
import collections
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import time
import uuid
import sys

from constants import *  # noqa: F401,F403 - 타이밍/크기 상수 전체 사용
from netutils import (app_dir, default_datadir, default_name, local_ips, parse_target,
                       safe_name, sanitize_chat_text, make_circular_avatar_png)
import crypto_layer
import applog
import winapi
import stickers

class Engine:
    """네트워크 계층. GUI와 이벤트 큐로만 주고받는다."""

    def __init__(self, name, port=None, datadir=None, extra_peers=None, on_event=None,
                 instance_id=None):
        self.datadir = datadir or default_datadir()
        self.logdir = os.path.join(self.datadir, "logs")
        self.settings_path = os.path.join(self.datadir, "settings.json")
        self.groups_path = os.path.join(self.datadir, "groups.json")
        os.makedirs(self.logdir, exist_ok=True)
        applog.init(self.datadir)
        # 포트 우선순위: 실행 인자(--port)로 명시한 값 > 지난번 [포트 번호 변경]으로
        # 저장해둔 값 > 기본값(50707). 실행 인자를 명시하지 않으면(None) 저장된
        # 포트를 그대로 써서, 한 번 바꿔두면 --port 없이 실행해도 유지된다.
        self.port = int(port) if port is not None else self._clamp_port(
            self._read_settings().get("port"))
        # 이름 우선순위: 실행 인자로 준 이름 > 지난번 저장된 표시 이름 > 시스템 계정명.
        # 저장된 이름을 여기서 읽지 않으면 재시작할 때마다 set_name()으로 바꾼
        # 이름이 default_name()으로 되돌아가 버린다.
        self.name = (name or self._read_settings().get("name") or default_name())
        self.max_file_size = self._clamp_file_size_mb(
            self._read_settings().get("max_file_size_mb")) * 1024 * 1024
        self.always_on_top = bool(self._read_settings().get("always_on_top", False))
        self.notify_sound_enabled = bool(self._read_settings().get("notify_sound_enabled", True))
        # 사전 공유키 — app_dir()(소스 폴더)에 둔다: 이 폴더를 통째로 복사해 배포하는
        # 기존 방식 그대로, 최초 실행 PC가 만든 키가 복사를 통해 모든 동료에게 퍼진다.
        self._crypto_key = crypto_layer.load_or_create_key(app_dir())
        self.on_event = on_event or (lambda ev: None)
        self.instance_id = instance_id or uuid.uuid4().hex[:10]
        self.peers = {}                 # (ip, port) -> {name, ip, port, last, static}
        self.plock = threading.Lock()
        self.groups = {}                # gid -> {name, members:set[(ip,port)], left:set[(ip,port)], created}
        self.left_groups = set()        # {gid} — 내가 나간 그룹 ID 집합
        self.glock = threading.RLock()
        self.pending = {}               # msg_id -> threading.Event (ACK 대기)
        self.pending_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.settings_lock = threading.RLock()
        self.hidden = set()             # {("dm",ip,port), ("grp",gid)} — 목록에서 숨긴 대화
        self.aliases = {}               # "dm:ip:port" -> custom alias (사용자 지정 별칭)
        self.contact_categories = {}    # "dm:ip:port" -> 친구 카테고리(폴더) 이름
        self.pinned_notices = {}        # "dm:ip:port"/"grp:gid" -> {"mid","text","sender","ts"} (카톡 공지 기능)
        self.transfers_in = {}          # fid -> {name,size,total,chunks:{seq:bytes},from,target}
        self.tlock = threading.Lock()
        # 완료된 파일을 다운로드 폴더로 옮길 때 "빈 이름 찾기(_unique_path)"와
        # 실제 이동(shutil.move) 사이에 다른 전송이 끼어들면 같은 파일명을 서로
        # 다른 두 파일이 골라버릴 수 있다(TOCTOU) — 이 구간만 직렬화해서 막는다.
        self._save_path_lock = threading.Lock()
        self.partial_dir = os.path.join(self.datadir, "partial")  # 이어받기용 미완성 파일(.part)
        os.makedirs(self.partial_dir, exist_ok=True)
        self._partial_index_path = os.path.join(self.partial_dir, "index.json")
        self._partial_index = {}        # resume_key -> {"part": 파일명, "next_seq": N, "total": N}
        self._resume_info = {}          # (fid, ip, port)(발신 시도) -> 상대가 알려온 resume_from
        self._load_partial_index()
        # read_ack/gread_ack가 한꺼번에 여러 개 몰려올 때(안 읽은 메시지가 N개 있던 방을
        # 열면 N개가 연속으로 옴)마다 로그 파일 전체를 읽고 다시 쓰면 대화가 많이 쌓인
        # 방일수록 I/O가 튀어 반응이 느려진다. 짧은 시간 동안 들어온 요청을 모아뒀다가
        # 한 번만 반영(batch flush)한다.
        self._read_ack_pending = {}     # (ip,port) -> {mid,...} (None이 들어있으면 "전체 읽음")
        self._read_ack_timer = {}       # (ip,port) -> threading.Timer
        self._gread_ack_pending = {}    # gid -> [(ip,port,mid), ...]
        self._gread_ack_timer = {}      # gid -> threading.Timer
        self._read_ack_lock = threading.Lock()
        # 자동 폭파(self-destruct) 타이머 — 상대가 "읽은" 시점부터 카운트다운 시작.
        # 내가 받은 메시지를 "읽은" 시점은 send_read_ack()를 부르는 바로 그 순간(대화방을
        # 열거나, 열려 있는 상태에서 새 메시지가 온 순간)이고, 내가 보낸 메시지를 상대가
        # "읽은" 시점은 상대로부터 read_ack가 도착하는 순간이다 — 둘 다 이미 있는 read_ack
        # 흐름에 얹어서 처리한다(양쪽 다 read_ack 배치 처리와 같은 lock/타이머 재사용).
        self._burn_start_pending = {}   # (ip,port) -> {mid,...} (내가 받은 메시지 쪽)
        self._burn_start_timer = {}     # (ip,port) -> threading.Timer
        self._burn_candidates = set()   # 아직 안 지워진 burn_deadline이 있을 수 있는 key 집합
        self._last_record_cache = {}    # ("dm",ip,port) or ("grp",gid) -> last log record dict (O(1) preview)
        self._my_ips = set(local_ips()) # 로컬 IP 캐시 (시스템 콜/DNS 반복 부하 방지)
        self._stop = threading.Event()
        self.avatardir = os.path.join(self.datadir, "avatars")
        os.makedirs(self.avatardir, exist_ok=True)
        self.my_avatar_hash = ""
        self.my_avatar_b64 = ""
        self._avatar_req_debounce = {}  # (ip, port, av_hash) -> last_ts
        self._clock_skew_debounce = {}  # ip -> last_logged_ts (시계 오차 로그 스팸 방지)
        # 위 두 딕셔너리는 수신 스레드(_recv_loop)가 쓰고 하트비트 스레드(_presence_loop
        # -> _prune)가 순회하며 만료 항목을 지운다 — 락 없이 두면 순회 도중 다른
        # 스레드가 키를 넣어 "dictionary changed size during iteration"으로 죽을 수 있다.
        self._debounce_lock = threading.Lock()
        self._init_static(extra_peers)
        self._load_groups()
        self._load_hidden()
        self._load_aliases()
        self._load_history_peers()
        self._load_categories()
        self._load_pinned_notices()
        self._load_my_avatar()
        self.outbox_path = os.path.join(self.datadir, "outbox.json")
        self.outbox_lock = threading.Lock()
        self.outbox = {}                # "ip:port" -> list of [{"pkt": ..., "ts": ...}]
        self._flushing_targets = set()  # "ip:port" 병렬 중복 flush 방지
        self._seen_mids = collections.OrderedDict()  # 최근 수신한 메시지 ID (중복 처리 방지)
        self.seen_lock = threading.Lock()
        self._load_outbox()
        self.download_dir = self._load_download_dir()
        self.sock = None
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Windows 전용: 상대 포트 미개방 ICMP(WSAECONNRESET 10054)를 오류로 보고하지 않게 함.
            # 이 설정이 없으면 오프라인 상대에게 보낸 1패킷이 수신 스레드를 조용히 죽인다.
            if sys.platform.startswith("win"):
                try:
                    import ctypes
                    in_buf = ctypes.c_ulong(0)
                    cb = ctypes.c_ulong(0)
                    ctypes.windll.ws2_32.WSAIoctl(
                        self.sock.fileno(), 0x9800000C,
                        ctypes.byref(in_buf), ctypes.sizeof(in_buf),
                        None, 0, ctypes.byref(cb), None, None
                    )
                except Exception:
                    pass
            self.sock.settimeout(0.5)
            self.sock.bind(("", self.port))
        except OSError as e:
            applog.log("socket_bind", e, detail=f"port={self.port}")
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            raise
        threading.Thread(target=self._presence_loop, daemon=True, name="presence").start()
        threading.Thread(target=self._recv_loop, daemon=True, name="recv").start()
        threading.Thread(target=self._migrate_plaintext_logs, daemon=True, name="log_migrate").start()

    @staticmethod
    def _atomic_write_text(path, text):
        """임시 파일에 먼저 쓰고 os.replace로 원자적 치환한다. 기존에는 open(path, "w")로
        바로 덮어써서, 쓰는 도중 프로세스가 강제 종료되면(정전, 강제 종료 등) 파일이
        절반만 쓰인 채로 남거나 0바이트가 되어 대화 기록/설정이 통째로 유실될 위험이
        있었다. crypto_layer.load_or_create_key()가 이미 쓰던 방식과 동일하게 맞춘다."""
        # pid만 쓰면 같은 프로세스 안의 서로 다른 스레드가 동시에 같은 path를
        # 저장할 때 임시 파일명이 겹쳐 PermissionError(WinError 32)가 날 수 있어
        # 스레드 id도 함께 섞는다.
        tmp_path = path + f".tmp{os.getpid()}_{threading.get_ident()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)

    # ---------- 대화 로그 파일 암호화(저장소 보호) ----------
    # 대화 내용은 네트워크 전송 중에는 이미 crypto_layer로 암호화되지만, 디스크에 저장된
    # data/logs/*.jsonl은 지금까지 평문 JSON 그대로였다 — 자리를 비운 PC의 폴더를 열면
    # 대화가 그대로 보일 수 있었다. 이미 동료 간 통신에 쓰는 secret.key를 그대로 재사용해
    # (사용자 조작 없이 투명하게) 로그 한 줄 한 줄을 암호화한다. 같은 LAN Talk를 쓰는
    # 동료가 secret.key를 갖고 있으면 이론적으로 복호화가 가능하지만, 그 폴더를 복사해오지
    # 않은 제3자가 자리 비운 PC를 열어보는 상황은 완벽히 막는다.
    def _enc_log_line(self, rec):
        plain = json.dumps(rec, ensure_ascii=False).encode("utf-8")
        blob = crypto_layer.encrypt_blob(self._crypto_key, plain)
        return base64.b64encode(blob).decode("ascii") + "\n"

    def _dec_log_line(self, line):
        rec, _was_plain = self._dec_log_line_detailed(line)
        return rec

    def _dec_log_line_detailed(self, line):
        """반환: (레코드 dict 또는 None, 이번 줄이 아직 암호화되기 전의 구버전 평문이었는지).
        두 번째 값은 마이그레이션(평문이 하나라도 섞여 있으면 파일 전체를 재암호화) 판단에
        쓰인다."""
        line = line.strip()
        if not line:
            return None, False
        try:
            blob = base64.b64decode(line, validate=True)
            plain = crypto_layer.decrypt_blob(self._crypto_key, blob)
            if plain is not None:
                rec = json.loads(plain.decode("utf-8"))
                return (rec if isinstance(rec, dict) else None), False
        except Exception:
            pass
        # 아직 암호화 적용 전(v6.8 이전)에 만들어진 평문 로그와의 하위 호환
        try:
            rec = json.loads(line)
            return (rec if isinstance(rec, dict) else None), True
        except ValueError:
            return None, False

    @staticmethod
    def _key_from_log_filename(fname):
        if not fname.endswith(".jsonl"):
            return None
        if fname.startswith("group_"):
            return ("grp", fname[6:-6])
        base = fname[:-6]
        if "_" not in base:
            return None
        parts = base.split("_", 1)
        try:
            return ("dm", parts[0], int(parts[1]))
        except (TypeError, ValueError):
            return None

    def _migrate_one_log_file(self, path):
        """파일 하나를 읽어 평문 줄이 하나라도 섞여 있으면 전체를 암호화해 다시 쓴다.
        겸사겸사 이미 만료 대기 중인 자동 폭파(burn_deadline) 레코드가 있는지도 확인해
        둔다 — 앱을 재시작해도 예약된 타이머(_burn_candidates)가 이어지도록."""
        key = self._key_from_log_filename(os.path.basename(path))
        has_pending_burn = False
        with self.log_lock:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_lines = [l for l in f if l.strip()]
            except OSError:
                return
            recs = []
            needs_migration = False
            for line in raw_lines:
                rec, was_plain = self._dec_log_line_detailed(line)
                if rec is None:
                    continue
                if was_plain:
                    needs_migration = True
                if rec.get("burn_deadline"):
                    has_pending_burn = True
                recs.append(rec)
            if needs_migration and recs:
                try:
                    text = "".join(self._enc_log_line(r) for r in recs)
                    self._atomic_write_text(path, text)
                except OSError as e:
                    applog.log("migrate_log", e, detail=path)
        # log_lock을 놓은 뒤에 _read_ack_lock을 잡는다 — 이 파일의 다른 모든
        # 지점은 이 두 락을 절대 중첩해서 잡지 않는 관례를 지키고 있는데, 여기만
        # log_lock 안에서 _read_ack_lock을 잡고 있어 향후 반대 순서로 두 락을
        # 잡는 코드가 추가되면 교착 상태(deadlock) 위험이 될 수 있었다.
        if key and has_pending_burn:
            with self._read_ack_lock:
                self._burn_candidates.add(key)

    def _migrate_plaintext_logs(self):
        """앱 시작 시 한 번, data/logs 폴더의 기존 평문 대화 로그를 전부 암호화 형식으로
        전환한다. 파일이 많거나 크면 시간이 걸릴 수 있어 백그라운드 스레드에서 실행하며,
        읽기/쓰기는 어차피 암호화 여부와 무관하게 항상 정상 동작하므로(위 _dec_log_line이
        평문도 함께 읽음) 이 마이그레이션이 끝나기 전에 앱을 써도 안전하다."""
        try:
            fnames = os.listdir(self.logdir)
        except OSError:
            return
        for fname in fnames:
            if fname.endswith(".jsonl"):
                self._migrate_one_log_file(os.path.join(self.logdir, fname))

    # ---------- 자동 폭파(self-destruct) 타이머 ----------
    def _sweep_burns(self):
        """_prune()에서 주기적으로(PRESENCE_INTERVAL마다) 호출 — burn_deadline이 지난
        레코드가 있을 수 있는 대화방(_burn_candidates)만 골라서 확인한다. 대부분의
        대화방은 자동 폭파를 안 쓰므로 이 집합은 보통 비어 있어 거의 아무 일도 안 한다."""
        with self._read_ack_lock:
            candidates = list(self._burn_candidates)
        now = time.time()
        for key in candidates:
            if key[0] == "dm":
                path = self._log_path(key[1], key[2])
            elif key[0] == "grp":
                path = self._group_log_path(key[1])
            else:
                with self._read_ack_lock:
                    self._burn_candidates.discard(key)
                continue
            self._sweep_one_burn_file(key, path, now)

    def _sweep_one_burn_file(self, key, path, now):
        if not os.path.exists(path):
            with self._read_ack_lock:
                self._burn_candidates.discard(key)
            return
        changed = False
        burned_mids = []
        with self.log_lock:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_lines = [l for l in f if l.strip()]
            except OSError:
                return
            kept = []
            still_pending = False
            for line in raw_lines:
                rec = self._dec_log_line(line)
                if rec is None:
                    continue
                deadline = rec.get("burn_deadline")
                if deadline:
                    if now >= deadline:
                        burned_mids.append(rec.get("mid"))
                        changed = True
                        continue  # 삭제 — kept에 넣지 않음
                    still_pending = True
                kept.append(rec)
            if changed:
                try:
                    text = "".join(self._enc_log_line(r) for r in kept)
                    self._atomic_write_text(path, text)
                    if kept:
                        self._last_record_cache[key] = kept[-1]
                    else:
                        self._last_record_cache.pop(key, None)
                except OSError as e:
                    applog.log("burn_sweep", e, detail=path)
                    return
        with self._read_ack_lock:
            if not still_pending:
                self._burn_candidates.discard(key)
        if changed:
            if burned_mids:
                # 상단에 고정해둔 공지가 방금 자동 폭파로 삭제된 메시지였다면, 공지
                # 배너도 같이 해제한다 — 안 그러면 본문은 영구 삭제됐는데 평문 공지만
                # settings.json에 계속 남는다.
                notice = self.get_pinned_notice(key)
                if notice and notice.get("mid") in burned_mids:
                    self.clear_pinned_notice(key)
            self._emit({"ev": "burned", "key": key, "mids": burned_mids})

    def _queue_burn_start(self, ip, port, mid):
        """내가 받은 메시지를 "읽은"(=send_read_ack를 부른) 시점 — 그 메시지에
        burn_sec이 걸려 있으면 여기서부터 카운트다운을 시작해야 한다."""
        key = (ip, port)
        with self._read_ack_lock:
            pending = self._burn_start_pending.setdefault(key, set())
            pending.add(mid or None)
            timer = self._burn_start_timer.get(key)
            if timer is None or not timer.is_alive():
                t = threading.Timer(0.3, self._flush_burn_start, args=(key,))
                t.daemon = True
                self._burn_start_timer[key] = t
                t.start()

    def _flush_burn_start(self, key):
        ip, port = key
        with self._read_ack_lock:
            mids = self._burn_start_pending.pop(key, None)
            self._burn_start_timer.pop(key, None)
        if not mids:
            return
        mark_all = None in mids
        specific = {m for m in mids if m}
        path = self._log_path(ip, port)
        if not os.path.exists(path):
            return
        with self.log_lock:
            updated = False
            has_pending = False
            recs = []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        r = self._dec_log_line(line)
                        if r is None:
                            continue
                        if r.get("dir") == "in" and r.get("burn_sec") and not r.get("burn_deadline"):
                            if mark_all or r.get("mid") in specific:
                                r["burn_deadline"] = time.time() + r["burn_sec"]
                                updated = True
                        if r.get("burn_deadline"):
                            has_pending = True
                        recs.append(r)
                if updated:
                    text = "".join(self._enc_log_line(r) for r in recs)
                    self._atomic_write_text(path, text)
                    if recs:
                        self._last_record_cache[("dm", ip, port)] = recs[-1]
            except OSError as e:
                applog.log("burn_start", e, detail=f"{ip}:{port}")
                return
        if has_pending:
            with self._read_ack_lock:
                self._burn_candidates.add(("dm", ip, port))

    # ---------- 설정(직접 추가 IP) ----------
    def _read_settings(self):
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _update_settings(self, mutate):
        """settings.json을 읽→콜백으로 수정→쓰기까지 잠금 하나로 묶는다.
        (정적 IP·숨김 목록·다운로드 폴더 등 여러 항목이 같은 파일을 공유하므로
        동시 쓰기로 서로 덮어쓰지 않게 방지)"""
        with self.settings_lock:
            cfg = self._read_settings()
            mutate(cfg)
            try:
                self._atomic_write_text(self.settings_path, json.dumps(cfg, ensure_ascii=False, indent=2))
            except OSError as e:
                applog.log("update_settings", e, detail=self.settings_path)

    def _save_static(self):
        self._update_settings(
            lambda cfg: cfg.__setitem__("static_targets", [f"{ip}:{p}" for ip, p in sorted(self.static)]))

    def _load_hidden(self):
        raw = self._read_settings().get("hidden") or []
        keys = set()
        for s in raw:
            k = self._str_to_key(s)
            if k:
                keys.add(k)
        self.hidden = keys

    def _save_hidden(self):
        self._update_settings(
            lambda cfg: cfg.__setitem__("hidden", sorted(self._key_to_str(k) for k in self.hidden)))

    def _load_aliases(self):
        raw = self._read_settings().get("aliases") or {}
        if isinstance(raw, dict):
            self.aliases = {str(k): str(v).strip() for k, v in raw.items() if str(v).strip()}
        else:
            self.aliases = {}

    def _save_aliases(self):
        self._update_settings(
            lambda cfg: cfg.__setitem__("aliases", dict(self.aliases)))

    def get_alias(self, key):
        if not key:
            return None
        return self.aliases.get(self._key_to_str(key))

    def set_alias(self, key, alias):
        kstr = self._key_to_str(key)
        alias = sanitize_chat_text(str(alias or "")).strip()[:60]
        with self.settings_lock:
            if alias:
                self.aliases[kstr] = alias
            else:
                self.aliases.pop(kstr, None)
        self._save_aliases()
        self._emit({"ev": "peer"})

    # ---------- 친구 카테고리(폴더) 분류 ----------
    def _load_categories(self):
        raw = self._read_settings().get("contact_categories") or {}
        if isinstance(raw, dict):
            self.contact_categories = {str(k): str(v).strip() for k, v in raw.items() if str(v).strip()}
        else:
            self.contact_categories = {}

    def _save_categories(self):
        self._update_settings(
            lambda cfg: cfg.__setitem__("contact_categories", dict(self.contact_categories)))

    def get_category(self, key):
        if not key or key[0] != "dm":
            return ""
        with self.settings_lock:
            return self.contact_categories.get(self._key_to_str(key), "")

    def set_category(self, key, category):
        kstr = self._key_to_str(key)
        category = sanitize_chat_text(str(category or "")).strip()[:40]
        with self.settings_lock:
            if category:
                self.contact_categories[kstr] = category
            else:
                self.contact_categories.pop(kstr, None)
        self._save_categories()
        self._emit({"ev": "peer"})

    def list_categories(self):
        """현재 실제로 쓰이고 있는 카테고리 이름들(가나다순)."""
        with self.settings_lock:
            return sorted({v for v in self.contact_categories.values() if v})

    # ---------- 최근 사용 스티커 ----------
    # (v6.25까지는 "최근 사용 이모지"였으나, 이모지 피커 자체를 스티커 전용으로
    # 바꾸면서 이 자리도 스티커 기준으로 교체했다. 저장 키만 "recent_stickers"로
    # 바뀌었을 뿐 패턴은 기존 정적 IP 목록·최근 이모지와 동일 — 항상 앞으로
    # 재정렬, 최대 24개.)
    def get_recent_stickers(self):
        raw = self._read_settings().get("recent_stickers") or []
        return [x for x in raw if isinstance(x, str) and x in stickers.STICKERS][:24]

    def add_recent_sticker(self, sticker_id):
        sticker_id = str(sticker_id or "").strip()
        if not sticker_id or sticker_id not in stickers.STICKERS:
            return

        def _mutate(cfg):
            lst = [x for x in (cfg.get("recent_stickers") or [])
                  if isinstance(x, str) and x != sticker_id]
            lst.insert(0, sticker_id)
            cfg["recent_stickers"] = lst[:24]

        self._update_settings(_mutate)

    # ---------- 대화방 공지사항(카카오톡 스타일 고정 메시지) ----------
    def _load_pinned_notices(self):
        raw = self._read_settings().get("pinned_notices") or {}
        self.pinned_notices = raw if isinstance(raw, dict) else {}

    def _save_pinned_notices(self):
        self._update_settings(
            lambda cfg: cfg.__setitem__("pinned_notices", dict(self.pinned_notices)))

    def get_pinned_notice(self, key):
        if not key:
            return None
        with self.settings_lock:
            return self.pinned_notices.get(self._key_to_str(key))

    def set_pinned_notice(self, key, mid, text, sender, ts=None, _from_network=False):
        """메시지 하나를 그 대화방의 공지로 지정한다. 1:1이든 그룹이든 상대(들)에게도
        똑같이 전파되어(카톡 단체방 공지와 동일한 동작) 모두 같은 공지를 본다."""
        notice = {
            "mid": str(mid or ""),
            "text": sanitize_chat_text(str(text or ""))[:MAX_LEN],
            "sender": sanitize_chat_text(str(sender or ""))[:60],
            "ts": float(ts) if ts is not None else time.time(),
        }
        with self.settings_lock:
            self.pinned_notices[self._key_to_str(key)] = notice
        self._save_pinned_notices()
        if not _from_network:
            self._broadcast_pin_notice(key, notice)
        self._emit({"ev": "pinned", "key": key})
        return notice

    def clear_pinned_notice(self, key, _from_network=False):
        kstr = self._key_to_str(key)
        with self.settings_lock:
            existed = self.pinned_notices.pop(kstr, None) is not None
        if existed:
            self._save_pinned_notices()
        if not _from_network:
            self._broadcast_pin_notice(key, None)
        self._emit({"ev": "pinned", "key": key})

    def _broadcast_pin_notice(self, key, notice):
        """공지 지정/해제를 1:1 상대 또는 그룹 멤버 전원에게 전파한다. 실패해도 앱이
        멈추면 안 되므로 기존 신뢰 전송(_send_reliable)에 실패 시 아웃박스 재시도를
        그대로 태운다 — 다른 그룹 브로드캐스트(send_group_message 등)와 동일한 패턴."""
        base = {"type": "pin_notice" if notice else "unpin_notice",
                "tid": self.instance_id, "port": self.port}
        if notice:
            base.update(mid=notice["mid"], text=notice["text"], sender=notice["sender"], ts=notice["ts"])
        if key[0] == "dm":
            ip, port = key[1], key[2]
            pkt = dict(base, id=uuid.uuid4().hex)
            def _done(ok, ip=ip, port=port, pkt=pkt):
                if not ok:
                    self._enqueue_outbox(ip, port, pkt)
            self._send_reliable(pkt, ip, port, on_done=_done)
        else:
            gid = key[1]
            base["gid"] = gid
            with self.glock:
                g = self.groups.get(gid)
                members = list(g["members"]) if g else []
            for (ip, port) in members:
                if self._is_self(ip, port):
                    continue
                pkt = dict(base, id=uuid.uuid4().hex)
                def _done(ok, ip=ip, port=port, pkt=pkt):
                    if not ok:
                        self._enqueue_outbox(ip, port, pkt)
                self._send_reliable(pkt, ip, port, on_done=_done)

    # ---------- 프로필 사진 (아바타) ----------
    def _load_my_avatar(self):
        av_hash = str(self._read_settings().get("my_avatar_hash") or "").strip()
        if av_hash:
            av_path = os.path.join(self.avatardir, f"{av_hash}.png")
            if os.path.exists(av_path):
                try:
                    with open(av_path, "rb") as f:
                        data = f.read()
                    self.my_avatar_hash = av_hash
                    self.my_avatar_b64 = base64.b64encode(data).decode("ascii")
                    return
                except Exception:
                    pass
        self.my_avatar_hash = ""
        self.my_avatar_b64 = ""

    def set_my_avatar(self, file_path_or_none):
        """프로필 사진을 등록하거나(file_path_or_none) 기본값으로 복원(None)."""
        if not file_path_or_none:
            self.my_avatar_hash = ""
            self.my_avatar_b64 = ""
            self._update_settings(lambda cfg: cfg.pop("my_avatar_hash", None))
            self._announce_avatar_update()
            self._emit({"ev": "my_avatar", "av": ""})
            return True
        try:
            png_bytes = make_circular_avatar_png(file_path_or_none, size=80)
            if not png_bytes or len(png_bytes) > 200 * 1024:
                return False
            av_hash = hashlib.sha1(png_bytes).hexdigest()[:12]
            av_path = os.path.join(self.avatardir, f"{av_hash}.png")
            with open(av_path, "wb") as f:
                f.write(png_bytes)
            self.my_avatar_hash = av_hash
            self.my_avatar_b64 = base64.b64encode(png_bytes).decode("ascii")
            self._update_settings(lambda cfg: cfg.__setitem__("my_avatar_hash", av_hash))
            self._announce_avatar_update()
            self._emit({"ev": "my_avatar", "av": av_hash})
            return True
        except Exception:
            return False

    def _save_received_avatar(self, b64_data):
        """상대가 보낸 아바타(base64 PNG)를 검증 후 저장한다. 파일명은 절대
        상대가 주장한 av_hash를 그대로 쓰지 않고, 우리가 직접 복호화한 바이트로
        SHA1을 계산해 만든다 — 그래야 (1) 악의적인 peer가 "../../..." 같은 값을
        av_hash로 보내 avatardir 밖에 파일을 쓰게 만드는 경로 탈출을 막고,
        (2) 임의의 바이트를 남의 av_hash로 속여 캐시를 오염시키는 것도 막는다
        (파일명이 항상 그 내용의 진짜 해시이므로). 성공 시 계산된 해시(12자 hex),
        실패 시 빈 문자열을 반환한다."""
        try:
            png_bytes = base64.b64decode(b64_data)
        except Exception:
            return ""
        if not png_bytes or len(png_bytes) > 150 * 1024:
            return ""
        av_hash = hashlib.sha1(png_bytes).hexdigest()[:12]
        av_path = os.path.join(self.avatardir, f"{av_hash}.png")
        try:
            with open(av_path, "wb") as f:
                f.write(png_bytes)
        except OSError as e:
            applog.log("save_avatar", e, detail=av_hash)
            return ""
        return av_hash

    def _announce_avatar_update(self):
        threading.Thread(target=self._announce, daemon=True).start()
        with self.plock:
            peers_list = list(self.peers.keys())
        pkt = {
            "type": "avatar_update",
            "id": uuid.uuid4().hex,
            "tid": self.instance_id,
            "port": self.port,
            "av": self.my_avatar_hash,
            "data": self.my_avatar_b64,
        }
        for (ip, port) in peers_list:
            self._send_dict(pkt, ip, port)

    def _check_fetch_avatar(self, ip, port, av_hash):
        # av_hash는 presence/메시지 패킷에서 그대로 온 값이라, 파일 경로 조립 전에
        # 우리가 실제로 만드는 형식(12자리 소문자 16진수)인지 확인해 "../.."류
        # 값으로 avatardir 밖의 파일 존재 여부를 캐는 것을 막는다.
        if not av_hash or not re.fullmatch(r"[0-9a-f]{12}", av_hash):
            return
        av_path = os.path.join(self.avatardir, f"{av_hash}.png")
        if os.path.exists(av_path):
            return
        now = time.time()
        deb_key = (ip, port, av_hash)
        with self._debounce_lock:
            if now - self._avatar_req_debounce.get(deb_key, 0) < 10.0:
                return
            self._avatar_req_debounce[deb_key] = now
        pkt = {
            "type": "avatar_req",
            "id": uuid.uuid4().hex,
            "tid": self.instance_id,
            "port": self.port,
            "av": av_hash,
        }
        self._send_dict(pkt, ip, port)

    @staticmethod
    def _key_to_str(key):
        return f"dm:{key[1]}:{key[2]}" if key[0] == "dm" else f"grp:{key[1]}"

    @staticmethod
    def _str_to_key(s):
        parts = str(s).split(":")
        if len(parts) == 3 and parts[0] == "dm":
            try:
                return ("dm", parts[1], int(parts[2]))
            except ValueError:
                return None
        if len(parts) == 2 and parts[0] == "grp":
            return ("grp", parts[1])
        return None

    def hide_key(self, key):
        self.hidden.add(key)
        self._save_hidden()

    def unhide_key(self, key):
        if key not in self.hidden:
            return False
        self.hidden.discard(key)
        self._save_hidden()
        return True

    def unhide_all(self):
        if not self.hidden:
            return False
        self.hidden = set()
        self._save_hidden()
        return True

    def _load_download_dir(self):
        d = self._read_settings().get("download_dir")
        return d if isinstance(d, str) and d else os.path.join(self.datadir, "downloads")

    def set_download_dir(self, path):
        self.download_dir = path
        self._update_settings(lambda cfg: cfg.__setitem__("download_dir", path))

    def _init_static(self, extra):
        targets = set()
        src = extra if extra is not None else (self._read_settings().get("static_targets") or [])
        for item in src:
            try:
                targets.add(parse_target(item))
            except ValueError:
                pass
        self.static = targets
        if extra is not None:
            self._save_static()

    def set_static_targets(self, new_set):
        self.static = set(new_set)
        self._save_static()
        with self.plock:
            for key in self.static:
                if key not in self.peers:
                    ip, port = key
                    self.peers[key] = {"name": ip, "ip": ip, "port": port,
                                       "last": 0, "static": True}
        self._emit({"ev": "peer"})

    def _load_history_peers(self):
        """기존 1:1 대화 기록이 있는 상대는 앱 재기동 후에도 대화 목록에 유지되도록
        logdir 내 파일 목록을 스캔하여 self.peers에 기본 등록한다 (last=0: 오프라인 상태)."""
        if not os.path.isdir(self.logdir):
            return
        try:
            for fn in os.listdir(self.logdir):
                if fn.startswith("grp_") or fn.startswith("group_") or not fn.endswith(".jsonl"):
                    continue
                base = fn[:-6]
                if "_" not in base:
                    continue
                parts = base.rsplit("_", 1)
                ip = parts[0]
                try:
                    port = int(parts[1])
                except ValueError:
                    continue
                key = (ip, port)
                with self.plock:
                    if key not in self.peers:
                        self.peers[key] = {
                            "name": self.get_alias(("dm", ip, port)) or ip,
                            "ip": ip,
                            "port": port,
                            "last": 0,
                            "static": False,
                        }
        except Exception:
            pass

    # ---------- 파일 전송 이어받기(resume) 인덱스 ----------
    def _load_partial_index(self):
        try:
            with open(self._partial_index_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        self._partial_index = raw if isinstance(raw, dict) else {}
        # 인덱스엔 있는데 실제 .part 파일이 없으면(수동 삭제 등) 고아 항목 정리
        changed = False
        for key in list(self._partial_index.keys()):
            part_path = os.path.join(self.partial_dir, self._partial_index[key].get("part", ""))
            if not os.path.exists(part_path):
                del self._partial_index[key]
                changed = True
        if changed:
            self._save_partial_index()

    def _save_partial_index(self):
        with self.tlock:
            try:
                with open(self._partial_index_path, "w", encoding="utf-8") as f:
                    json.dump(self._partial_index, f, ensure_ascii=False, indent=2)
            except OSError as e:
                applog.log("save_partial_index", e)

    @staticmethod
    def _resume_key(target, ip, port, name, size):
        target_desc = f"grp:{target[1]}" if target[0] == "grp" else "dm"
        return f"{target_desc}:{ip}:{port}:{name}:{size}"

    # ---------- 오프라인 아웃박스 (재전송 큐) ----------
    def _load_outbox(self):
        try:
            with open(self.outbox_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            self.outbox = raw
        else:
            self.outbox = {}

    def _save_outbox(self):
        with self.outbox_lock:
            try:
                with open(self.outbox_path, "w", encoding="utf-8") as f:
                    json.dump(self.outbox, f, ensure_ascii=False, indent=2)
            except OSError as e:
                applog.log("save_outbox", e)

    def _enqueue_outbox(self, ip, port, pkt):
        target_k = f"{ip}:{port}"
        with self.outbox_lock:
            q = self.outbox.setdefault(target_k, [])
            mid = pkt.get("id")
            if not any(item.get("pkt", {}).get("id") == mid for item in q):
                q.append({"pkt": pkt, "ts": time.time()})
                if len(q) > 100:
                    del q[0]
        self._save_outbox()

    def _flush_outbox(self, ip, port):
        target_k = f"{ip}:{port}"
        with self.outbox_lock:
            if target_k in self._flushing_targets:
                return
            items = list(self.outbox.get(target_k, []))
            if not items:
                return
            self._flushing_targets.add(target_k)
        def worker():
            try:
                sent_ids = set()
                for item in items:
                    pkt = item.get("pkt")
                    if not pkt:
                        continue
                    mid = pkt.get("id")
                    if time.time() - item.get("ts", time.time()) > 86400:
                        if mid:
                            sent_ids.add(mid)
                        continue
                    ok = self._send_reliable_wait(pkt, ip, port)
                    if ok:
                        if mid:
                            sent_ids.add(mid)
                        if pkt.get("type") == "group_msg":
                            self._emit({"ev": "gsent", "gid": pkt.get("gid"), "ip": ip, "port": port, "ok": True, "id": pkt.get("mid") or mid})
                        elif pkt.get("type") == "group_leave":
                            pass
                        else:
                            self._emit({"ev": "sent", "id": mid, "ok": True, "ip": ip, "port": port})
                    else:
                        # 한 건이 실패했는데 계속 다음 건을 시도하면(상대가 방금
                        # 또 오프라인이 된 경우가 흔함), 뒤에 있던 메시지가 먼저
                        # 도착해 대화 순서가 뒤바뀔 수 있다. 순서를 지키기 위해
                        # 여기서 멈추고, 남은 항목은 다음 flush(상대가 다시
                        # 온라인이 됐을 때) 때 처음부터 이어서 재시도한다.
                        break
                with self.outbox_lock:
                    if sent_ids:
                        cur = self.outbox.get(target_k, [])
                        filtered = [it for it in cur if it.get("pkt", {}).get("id") not in sent_ids]
                        if filtered:
                            self.outbox[target_k] = filtered
                        else:
                            self.outbox.pop(target_k, None)
                self._save_outbox()
            finally:
                with self.outbox_lock:
                    self._flushing_targets.discard(target_k)
        threading.Thread(target=worker, daemon=True).start()

    # ---------- 읽음 확인 & 그룹 퇴장 ----------
    def send_read_ack(self, ip, port, mid=""):
        pkt = {"type": "read_ack", "mid": mid, "port": self.port, "tid": self.instance_id}
        self._send_dict(pkt, ip, int(port))
        # "읽음"을 알리는 이 시점이 곧 자동 폭파 타이머의 시작점이기도 하다.
        self._queue_burn_start(ip, int(port), mid)

    def send_group_read_ack(self, gid, mid=""):
        with self.glock:
            g = self.groups.get(gid)
            if not g:
                return
            members = list(g["members"])
        pkt = {"type": "gread_ack", "gid": gid, "mid": mid, "port": self.port, "tid": self.instance_id}
        for (ip, port) in members:
            if not self._is_self(ip, port):
                self._send_dict(pkt, ip, port)

    def leave_group(self, gid):
        with self.glock:
            g = self.groups.get(gid)
            if not g:
                return False
            members = set(g["members"])
            name = g["name"]
            del self.groups[gid]
            self.left_groups.add(gid)
        self._save_groups()
        self.hidden.discard(("grp", gid))
        self._save_hidden()
        self._append_group_log(gid, "시스템", f"{self.name} 님이 대화방을 나갔습니다.", True, is_system=True)
        for ip, port in members:
            pkt_id = uuid.uuid4().hex
            pkt = {"type": "group_leave", "id": pkt_id, "gid": gid, "port": self.port, "name": self.name,
                   "tid": self.instance_id}
            def _make_done(target_ip, target_port, packet):
                def _done(ok):
                    if not ok:
                        self._enqueue_outbox(target_ip, target_port, packet)
                return _done
            self._send_reliable(pkt, ip, port, on_done=_make_done(ip, port, pkt))
        self._emit({"ev": "group", "gid": gid, "left": True})
        return True

    # ---------- 그룹 영속화 ----------
    def _load_groups(self):
        try:
            with open(self.groups_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        left_groups = set()
        if "groups" in raw and isinstance(raw["groups"], dict):
            g_dict = raw["groups"]
            left_groups = set(raw.get("left_groups") or [])
        else:
            g_dict = raw
        groups = {}
        for gid, g in g_dict.items():
            if not isinstance(g, dict):
                continue
            members = set()
            for m in (g.get("members") or []):
                try:
                    members.add((str(m[0]), int(m[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            left = set()
            for m in (g.get("left") or []):
                try:
                    left.add((str(m[0]), int(m[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            groups[gid] = {"name": str(g.get("name") or "그룹")[:60],
                           "members": members, "left": left,
                           "created": g.get("created") or time.time()}
        self.groups = groups
        self.left_groups = left_groups

    def _save_groups(self):
        with self.glock:
            raw = {
                "groups": {
                    gid: {"name": g["name"],
                          "members": [[ip, port] for ip, port in sorted(g["members"])],
                          "left": [[ip, port] for ip, port in sorted(g.get("left", set()))],
                          "created": g["created"]}
                    for gid, g in self.groups.items()
                },
                "left_groups": sorted(self.left_groups)
            }
        try:
            self._atomic_write_text(self.groups_path, json.dumps(raw, ensure_ascii=False, indent=2))
        except OSError:
            pass

    def _is_self(self, ip, port):
        # 포트가 다르면 무조건 타인. 포트가 같으면 실제 로컬 IP와 일치하거나(정상 배포 환경),
        # 127.0.0.1(같은 머신에서 포트만 다르게 띄우는 루프백 테스트 환경)일 때 자기 자신으로 간주.
        if int(port) != self.port:
            return False
        if ip == "127.0.0.1" or ip in self._my_ips:
            return True
        cur_ips = set(local_ips())
        self._my_ips = cur_ips
        return ip in cur_ips

    # ---------- 패킷 ----------
    def _presence_packet(self):
        idle = winapi.get_idle_seconds() if hasattr(winapi, "get_idle_seconds") else 0.0
        status = "away" if idle >= 300.0 else "online"
        pkt = {"type": "presence", "id": self.instance_id, "tid": self.instance_id,
               "name": self.name, "port": self.port, "status": status}
        if self.my_avatar_hash:
            pkt["av"] = self.my_avatar_hash
        return pkt

    def _send_dict(self, pkt, ip, port):
        try:
            plain = json.dumps(pkt, ensure_ascii=False).encode("utf-8")
            blob = crypto_layer.encrypt(self._crypto_key, plain)
            self.sock.sendto(blob, (ip, int(port)))
        except OSError as e:
            applog.log("send_dict", e, detail=f"{ip}:{port} type={pkt.get('type')}")

    def _announce(self):
        # DEFAULT_PORT으로 고정하면, --port로 기본값과 다른 포트를 골라 쓰는
        # 인스턴스(예: 사무실 전체가 50708로 통일해 쓰는 경우, 또는 한 PC에서
        # 여러 인스턴스를 테스트하는 경우)는 서로 브로드캐스트를 들을 수 없었다
        # — 어차피 자기 자신이 듣는 포트로 쏴야 서로 발견이 가능하므로 self.port로
        # 보낸다.
        self._send_dict(self._presence_packet(), "255.255.255.255", self.port)

    def set_name(self, name):
        name = sanitize_chat_text(str(name or "")).strip()[:60]
        if not name:
            return
        self.name = name
        self._update_settings(lambda cfg: cfg.__setitem__("name", name))
        threading.Thread(target=self._announce, daemon=True).start()

    @staticmethod
    def _clamp_file_size_mb(mb):
        """설정값을 1~MAX_FILE_SIZE_LIMIT_MB(1024, 1GB) 사이로 강제한다.
        저장된 값이 없거나(최초 실행) 손상됐으면 기본값(20MB)으로 되돌린다."""
        try:
            mb = int(mb)
        except (TypeError, ValueError):
            return DEFAULT_MAX_FILE_SIZE // (1024 * 1024)
        return max(1, min(MAX_FILE_SIZE_LIMIT_MB, mb))

    def set_max_file_size_mb(self, mb):
        """파일 전송·다운로드 최대 크기를 MB 단위로 바꾸고 저장한다(1~1024MB)."""
        mb = self._clamp_file_size_mb(mb)
        self.max_file_size = mb * 1024 * 1024
        self._update_settings(lambda cfg: cfg.__setitem__("max_file_size_mb", mb))
        return mb

    @staticmethod
    def _clamp_port(port):
        """설정값을 1~65535 사이로 강제한다. 저장된 값이 없거나(최초 실행)
        손상됐으면 기본값(DEFAULT_PORT)으로 되돌린다."""
        try:
            port = int(port)
        except (TypeError, ValueError):
            return DEFAULT_PORT
        if not (1 <= port <= 65535):
            return DEFAULT_PORT
        return port

    def set_port(self, port):
        """포트 번호를 저장한다(1~65535). 이미 열려 있는 소켓은 이 포트로 다시
        묶이지 않으므로(수신 스레드가 계속 옛 소켓을 쓰고 있음), 변경 사항은
        앱을 재시작해야 실제로 적용된다 — 호출 쪽에서 재시작 안내를 띄운다."""
        port = self._clamp_port(port)
        self._update_settings(lambda cfg: cfg.__setitem__("port", port))
        return port

    def set_always_on_top(self, enabled):
        """창을 항상 다른 창 위에 표시할지 저장한다(재시작해도 유지)."""
        enabled = bool(enabled)
        self.always_on_top = enabled
        self._update_settings(lambda cfg: cfg.__setitem__("always_on_top", enabled))
        return enabled

    def set_notify_sound_enabled(self, enabled):
        """새 메시지 알림음(띵동)을 켜고 끈다(재시작해도 유지). 꺼도 작업표시줄
        깜빡임·트레이 토스트 자체는 그대로 뜬다 — 소리만 없어진다."""
        enabled = bool(enabled)
        self.notify_sound_enabled = enabled
        self._update_settings(lambda cfg: cfg.__setitem__("notify_sound_enabled", enabled))
        return enabled

    # ---------- 루프 ----------
    def _presence_loop(self):
        while not self._stop.is_set():
            with self.plock:
                for key in self.static:
                    if key not in self.peers:
                        ip, port = key
                        self.peers[key] = {"name": ip, "ip": ip, "port": port,
                                           "last": 0, "static": True}
            pkt = self._presence_packet()
            self._send_dict(pkt, "255.255.255.255", self.port)
            with self.plock:
                statics = [k for k, v in self.peers.items() if v.get("static")]
            for (ip, port) in statics:
                self._send_dict(pkt, ip, port)
            self._prune()
            self._stop.wait(PRESENCE_INTERVAL)

    def get_group(self, gid):
        """스레드 안전한 그룹 정보 조회(복사본 반환)."""
        with self.glock:
            g = self.groups.get(gid)
            if not g:
                return None
            return {"name": g["name"], "members": set(g.get("members", ())),
                    "left": set(g.get("left", ())), "created": g.get("created", 0)}

    def get_peer(self, ip_or_key, port=None):
        """스레드 안전한 피어 정보 조회(복사본 반환)."""
        if isinstance(ip_or_key, tuple):
            key = (ip_or_key[0], ip_or_key[1]) if len(ip_or_key) == 2 else (ip_or_key[1], ip_or_key[2])
        else:
            key = (ip_or_key, int(port))
        with self.plock:
            p = self.peers.get(key)
            return dict(p) if p else None

    def _prune(self):
        self._sweep_burns()
        now = time.time()
        cutoff = now - PEER_TIMEOUT
        changed = False
        with self.plock:
            candidates = [k for k, v in self.peers.items()
                          if not v.get("static") and v.get("last", 0) < cutoff]
        # 디스크 I/O(대화 로그 존재 확인)는 락 바깥에서 수행하여 타 스레드 블로킹 방지
        to_delete = []
        for key in candidates:
            if not os.path.exists(self._log_path(key[0], key[1])):
                to_delete.append(key)
        if to_delete:
            with self.plock:
                for key in to_delete:
                    v = self.peers.get(key)
                    if v and not v.get("static") and v.get("last", 0) < cutoff:
                        del self.peers[key]
                        changed = True
        if changed:
            self._emit({"ev": "peer"})

        # 미완료 파일 전송 세션(120초 경과) 메모리 정리
        with self.tlock:
            expired_fids = [fid for fid, tr in self.transfers_in.items()
                            if now - tr.get("ts", now) > 120.0]
            for fid in expired_fids:
                del self.transfers_in[fid]

        # 이어받기용 .part 조각 파일 중, 일주일 넘게 이어받기 시도가 없던 것은
        # 디스크에 무한히 쌓이지 않도록 정리한다(메모리상의 transfers_in과는 별개 —
        # 이건 재시도를 위해 디스크에 남겨두는 진짜 이어받기 상태다).
        stale_cutoff = now - 7 * 24 * 3600
        with self.tlock:
            stale_keys = [k for k, v in self._partial_index.items()
                          if v.get("ts", 0) < stale_cutoff]
            for k in stale_keys:
                part_name = self._partial_index[k].get("part", "")
                part_path = os.path.join(self.partial_dir, part_name) if part_name else None
                if part_path and os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass
                del self._partial_index[k]
            # 인덱스에서 누락된 7일 이상 경과한 고아 .part 파일도 디스크에서 함께 정리
            try:
                for fn in os.listdir(self.partial_dir):
                    if fn.endswith(".part"):
                        fp = os.path.join(self.partial_dir, fn)
                        if os.path.isfile(fp) and os.path.getmtime(fp) < stale_cutoff:
                            try:
                                os.remove(fp)
                            except OSError:
                                pass
            except OSError:
                pass
        if stale_keys:
            self._save_partial_index()

        # 아바타 요청/시계 오차 디바운스 엔트리 정리 — 수신 스레드가 같은 순간에
        # 새 키를 써넣을 수 있으므로 같은 락으로 보호해야 순회 중 크기가 바뀌어
        # RuntimeError로 죽는 일이 없다.
        with self._debounce_lock:
            expired_deb = [k for k, ts in self._avatar_req_debounce.items()
                           if now - ts > 60.0]
            for k in expired_deb:
                del self._avatar_req_debounce[k]

            expired_skew = [k for k, ts in self._clock_skew_debounce.items()
                            if now - ts > 600.0]
            for k in expired_skew:
                del self._clock_skew_debounce[k]

    def _recv_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as e:
                if self._stop.is_set() or self.sock is None or self.sock.fileno() < 0:
                    break
                if isinstance(e, ConnectionResetError) or getattr(e, "winerror", None) == 10054:
                    continue
                applog.log("recv_loop", e)
                continue
            try:
                self._handle(data, addr)
            except Exception as e:  # 수신 스레드는 절대 죽으면 안 됨 — 원인만 남기고 계속
                applog.log("handle", e, detail=f"from={addr}")

    def _handle(self, data, addr):
        # 암호화·HMAC 검증 실패(키 불일치/변조/스푸핑 시도/구버전 평문 패킷)는 상대에게는
        # 조용히 버린다 — 기존 "형식이 이상한 패킷은 무시" 동작과 동일한 결로, 앱이
        # 죽지도 않고 신뢰할 수 없는 패킷을 처리하지도 않는다. 단, "시계 오차"만은
        # 정상적인 상대와도 벌어질 수 있는 문제라(도메인/NTP 미동기화 사내 PC 등),
        # 원인 진단이 가능하도록 로컬 debug.log에만 남긴다(IP당 5분에 한 번으로 제한).
        plain, reason = crypto_layer.decrypt_with_reason(self._crypto_key, data)
        if plain is None:
            if reason == "clock_skew":
                ip = addr[0]
                now = time.time()
                with self._debounce_lock:
                    should_log = now - self._clock_skew_debounce.get(ip, 0) >= 300.0
                    if should_log:
                        self._clock_skew_debounce[ip] = now
                if should_log:
                    applog.log("decrypt_clock_skew", detail=f"from={ip} — 상대 PC와 시스템 시계가 2분 이상 차이납니다")
            return
        try:
            d = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(d, dict):
            return
        dtype = d.get("type")
        ip = addr[0]

        if dtype == "ack":
            with self.pending_lock:
                e = self.pending.get(str(d.get("id") or ""))
                if e is not None:
                    e.set()
            return

        if d.get("tid") == self.instance_id:      # 내가 보낸 것의 루프백
            return

        if dtype == "presence":
            try:
                port = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                return
            ip = addr[0]
            key = (ip, port)
            with self.plock:
                existed = key in self.peers
            av_hash = str(d.get("av") or "").strip()
            status = str(d.get("status") or "online").strip()
            self._upsert_peer(ip, port, str(d.get("name") or "?")[:60], avatar_hash=av_hash, status=status)
            if not existed:
                # 새로 발견한 동료에게 즉시 맞응답 → 양쪽 모두 빠르게 목록 등록
                self._send_dict(self._presence_packet(), ip, port)
            self._flush_outbox(ip, port)
            return

        if dtype == "msg":
            ip = addr[0]
            mid = str(d.get("id") or "")
            self._send_dict({"type": "ack", "id": mid,
                             "tid": self.instance_id}, ip, addr[1])
            if mid:
                with self.seen_lock:
                    if mid in self._seen_mids:
                        return
                    self._seen_mids[mid] = time.time()
                    if len(self._seen_mids) > 1000:
                        self._seen_mids.popitem(last=False)
            try:
                port = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                port = DEFAULT_PORT
            name = sanitize_chat_text(str(d.get("name") or "?"))[:60]
            text = sanitize_chat_text(str(d.get("text") or ""))[:MAX_LEN]
            sticker_id = str(d.get("sticker_id") or "").strip() or None
            if not text and not sticker_id:
                return
            av_hash = str(d.get("av") or "").strip()
            reply = d.get("reply")
            try:
                burn_sec = max(0, int(d.get("burn_sec") or 0))
            except (TypeError, ValueError):
                burn_sec = 0
            is_mention = bool(self.name and f"@{self.name}" in text)
            self._upsert_peer(ip, port, name, avatar_hash=av_hash)
            self._append_log(ip, port, "in", text, av=av_hash, reply=reply, mid=mid,
                             burn_sec=burn_sec, sticker_id=sticker_id)
            self._emit({"ev": "msg", "peer": (ip, port), "name": name,
                        "text": text, "ts": time.time(), "av": av_hash, "reply": reply, "mid": mid,
                        "is_mention": is_mention, "burn_sec": burn_sec, "sticker_id": sticker_id})
            return

        if dtype == "group_invite":
            ip = addr[0]
            self._send_dict({"type": "ack", "id": str(d.get("id") or ""),
                             "tid": self.instance_id}, ip, addr[1])
            gid = str(d.get("gid") or "")
            if not gid:
                return
            with self.glock:
                if gid in self.left_groups:
                    return
            name = str(d.get("name") or "그룹")[:60]
            members = self._parse_members(d.get("members"))
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = DEFAULT_PORT
            members.add((ip, sport))
            with self.glock:
                g = self.groups.get(gid)
                if g:
                    # 초대장에 명시된 멤버는 정식으로 재초대된 멤버이므로 left 목록에서 제외
                    g.setdefault("left", set()).difference_update(members)
            sender_name = str(d.get("sender_name") or "").strip()
            if sender_name:
                self._upsert_peer(ip, sport, sender_name)
            # 이력 병합을 먼저 끝낸 뒤 그룹을 등록한다 — 순서를 반대로 하면
            # "gid가 groups에 보이는 시점"과 "이력 파일 기록이 끝난 시점" 사이에
            # 미세한 틈이 생겨, 그 틈에 이력을 읽으면 방금 합류한 그룹인데
            # 스냅샷이 비어 보이는 경합이 생긴다(실측으로 발견).
            hist = d.get("history") or []
            hist_changed = False
            if isinstance(hist, list) and hist:
                hist_changed = self._merge_group_history(gid, hist)
            changed = self._upsert_group(gid, name, members) or hist_changed
            if changed:
                self._emit({"ev": "group", "gid": gid})
            return

        if dtype == "group_msg":
            ip = addr[0]
            pkt_id = str(d.get("id") or "")
            self._send_dict({"type": "ack", "id": pkt_id,
                             "tid": self.instance_id}, ip, addr[1])
            gid = str(d.get("gid") or "")
            if not gid:
                return
            with self.glock:
                if gid in self.left_groups:
                    return
            mid = str(d.get("mid") or d.get("id") or "")
            if mid:
                with self.seen_lock:
                    if mid in self._seen_mids:
                        return
                    self._seen_mids[mid] = time.time()
                    if len(self._seen_mids) > 1000:
                        self._seen_mids.popitem(last=False)
            name = sanitize_chat_text(str(d.get("name") or "?"))[:60]
            text = sanitize_chat_text(str(d.get("text") or ""))[:MAX_LEN]
            sticker_id = str(d.get("sticker_id") or "").strip() or None
            if not text and not sticker_id:
                return
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = DEFAULT_PORT
            members = self._parse_members(d.get("members"))
            members.add((ip, sport))
            av_hash = str(d.get("av") or "").strip()
            reply = d.get("reply")
            try:
                burn_sec = max(0, int(d.get("burn_sec") or 0))
            except (TypeError, ValueError):
                burn_sec = 0
            burn_deadline = d.get("burn_deadline")
            if burn_sec and not burn_deadline:
                burn_deadline = time.time() + burn_sec
            try:
                burn_deadline = float(burn_deadline) if burn_deadline else None
            except (ValueError, TypeError):
                burn_deadline = None
            if name and name != "?":
                self._upsert_peer(ip, sport, name, avatar_hash=av_hash)
            with self.glock:
                existing = self.groups.get(gid)
                fallback_name = existing["name"] if existing else (name + " 그룹")
            self._upsert_group(gid, fallback_name, members)
            who = {"ip": ip, "port": sport, "name": name}
            mid = str(d.get("mid") or d.get("id") or "")
            is_mention = bool(self.name and f"@{self.name}" in text)
            self._append_group_log(gid, name, text, False, av=av_hash, reply=reply, mid=mid,
                                   is_mention=is_mention, burn_sec=burn_sec, burn_deadline=burn_deadline,
                                   sticker_id=sticker_id)
            self._emit({"ev": "gmsg", "gid": gid, "who": who, "text": text, "ts": time.time(),
                        "av": av_hash, "reply": reply, "mid": mid, "is_mention": is_mention,
                        "burn_sec": burn_sec, "sticker_id": sticker_id})
            return

        if dtype == "read_ack":
            ip = addr[0]
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = addr[1]
            mid = str(d.get("mid") or "")
            self._mark_read_in_log(ip, sport, mid)
            self._emit({"ev": "read_ack", "peer": (ip, sport), "mid": mid})
            return

        if dtype == "gread_ack":
            ip = addr[0]
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = addr[1]
            gid = str(d.get("gid") or "")
            mid = str(d.get("mid") or "")
            if gid:
                self._mark_group_read_in_log(gid, ip, sport, mid)
                self._emit({"ev": "gread_ack", "gid": gid, "peer": (ip, sport), "mid": mid})
            return

        if dtype == "group_leave":
            ip = addr[0]
            self._send_dict({"type": "ack", "id": str(d.get("id") or ""),
                             "tid": self.instance_id}, ip, addr[1])
            gid = str(d.get("gid") or "")
            if not gid:
                return
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = addr[1]
            name = str(d.get("name") or "그룹원")
            with self.glock:
                g = self.groups.get(gid)
                if g:
                    g.setdefault("left", set()).add((ip, sport))
                    g["members"].discard((ip, sport))
            self._save_groups()
            self._append_group_log(gid, "시스템", f"{name} 님이 대화방을 나갔습니다.", False, is_system=True)
            self._emit({"ev": "group", "gid": gid, "leave": (ip, sport), "name": name})
            return

        if dtype in ("pin_notice", "unpin_notice"):
            ip = addr[0]
            self._send_dict({"type": "ack", "id": str(d.get("id") or ""),
                             "tid": self.instance_id}, ip, addr[1])
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = addr[1]
            gid = d.get("gid")
            key = ("grp", str(gid)) if gid else ("dm", ip, sport)
            if dtype == "pin_notice":
                self.set_pinned_notice(key, d.get("mid"), d.get("text"), d.get("sender"),
                                       ts=d.get("ts"), _from_network=True)
            else:
                self.clear_pinned_notice(key, _from_network=True)
            return

        if dtype == "avatar_req":
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = addr[1]
            req_av = str(d.get("av") or "")
            if self.my_avatar_hash and (not req_av or req_av == self.my_avatar_hash):
                resp = {
                    "type": "avatar_resp",
                    "id": str(d.get("id") or ""),
                    "tid": self.instance_id,
                    "port": self.port,
                    "av": self.my_avatar_hash,
                    "data": self.my_avatar_b64,
                }
                self._send_dict(resp, ip, sport)
            return

        if dtype == "avatar_resp":
            b64_data = str(d.get("data") or "")
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = addr[1]
            if b64_data:
                av_hash = self._save_received_avatar(b64_data)
                if av_hash:
                    with self.plock:
                        if (ip, sport) in self.peers:
                            self.peers[(ip, sport)]["av"] = av_hash
                    av_path = os.path.join(self.avatardir, f"{av_hash}.png")
                    self._emit({"ev": "avatar", "peer": (ip, sport), "av": av_hash, "path": av_path})
            return

        if dtype == "avatar_update":
            b64_data = str(d.get("data") or "")
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = addr[1]
            av_hash = self._save_received_avatar(b64_data) if b64_data else ""
            with self.plock:
                if (ip, sport) in self.peers:
                    self.peers[(ip, sport)]["av"] = av_hash
            self._emit({"ev": "avatar", "peer": (ip, sport), "av": av_hash})
            return

        if dtype == "file_offer":
            ip = addr[0]
            self._send_dict({"type": "ack", "id": str(d.get("id") or ""),
                             "tid": self.instance_id}, ip, addr[1])
            fid = str(d.get("fid") or "")
            if not fid:
                return
            try:
                size = int(d.get("size") or 0)
                total = max(1, int(d.get("total") or 1))
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                return
            if size < 0 or size > self.max_file_size:
                return
            max_chunks = (self.max_file_size // FILE_CHUNK_SIZE) + 10
            if total > max_chunks:
                return
            name = str(d.get("name") or "파일")[:200]
            gid = d.get("gid")
            target = ("grp", str(gid)) if gid else ("dm", ip, sport)
            resume_key = self._resume_key(target, ip, sport, name, size)

            with self.tlock:
                entry = self._partial_index.get(resume_key)
                resume_from = 0
                part_name = entry.get("part") if entry else None
                if entry and entry.get("total") == total and part_name:
                    part_path_existing = os.path.join(self.partial_dir, part_name)
                    if os.path.exists(part_path_existing):
                        # 인덱스의 next_seq는 I/O를 줄이려고 8조각에 한 번만 디스크에
                        # 저장되므로(should_persist), 죽는 타이밍에 따라 실제 파일이
                        # 그보다 더 앞서 있을 수 있다 — next_seq는 "이어받기 가능한
                        # 대상이 있다"는 힌트로만 쓰고, 실제 재개 지점은 매 조각마다
                        # append로만 쓰인 파일의 진짜 크기에서 역산해야 정확하다
                        # (실측으로 발견 — next_seq를 그대로 믿으면 파일이 더 앞서
                        # 있는 정상 상황도 "깨졌다"고 오판해 처음부터 다시 받았다).
                        actual_size = os.path.getsize(part_path_existing)
                        if actual_size == size and total > 0:
                            # 이미 전부 수신된 경우라도 마지막 1개 청크(total - 1)는 발신자가
                            # 다시 보내게 하여 수신 측의 _finish_incoming_file 완료 파이프라인이 정상 발화되도록 보장
                            resume_from = max(0, total - 1)
                        elif actual_size < size and actual_size % FILE_CHUNK_SIZE == 0:
                            resume_from = min(actual_size // FILE_CHUNK_SIZE, max(0, total - 1))
                if not part_name:
                    part_name = hashlib.sha1(resume_key.encode("utf-8")).hexdigest()[:20] + ".part"
                part_path = os.path.join(self.partial_dir, part_name)
                if resume_from == 0:
                    try:
                        open(part_path, "wb").close()  # 새로 시작 — 기존 조각은 못 믿으니 초기화
                    except OSError as e:
                        applog.log("partial_create", e, detail=name)
                        return
                else:
                    try:
                        # 재개 지점 이후의 잔여 바이트가 남아있지 않도록 정확한 청크 경계로 truncate
                        with open(part_path, "r+b") as f:
                            f.truncate(resume_from * FILE_CHUNK_SIZE)
                    except OSError:
                        resume_from = 0
                        try:
                            open(part_path, "wb").close()
                        except OSError as e:
                            applog.log("partial_create", e, detail=name)
                            return
                self.transfers_in[fid] = {
                    "name": name, "size": size, "total": total, "from": (ip, sport),
                    "target": target, "ts": time.time(), "resume_key": resume_key,
                    "part_path": part_path, "next_seq": resume_from,
                }
                self._partial_index[resume_key] = {"part": part_name, "next_seq": resume_from,
                                                    "total": total, "ts": time.time()}
            self._save_partial_index()
            self._send_dict({"type": "file_offer_ack", "id": uuid.uuid4().hex,
                             "tid": self.instance_id, "fid": fid, "resume_from": resume_from}, ip, sport)
            return

        if dtype == "file_offer_ack":
            ip = addr[0]
            try:
                sport = int(d.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                sport = addr[1]
            fid = str(d.get("fid") or "")
            try:
                resume_from = max(0, int(d.get("resume_from") or 0))
            except (TypeError, ValueError):
                resume_from = 0
            if fid:
                # 키에 상대(ip,port)를 포함해야 한다 — fid 하나만 키로 쓰면 그룹 전송 시
                # 멤버들이 거의 동시에 file_offer_ack를 보낼 때 서로의 resume_from이
                # 덮어씌워져 엉뚱한 지점부터 이어보내는 경합(race)이 생길 수 있었다.
                with self.tlock:
                    self._resume_info[(fid, ip, sport)] = resume_from
            return

        if dtype == "file_chunk":
            ip = addr[0]
            self._send_dict({"type": "ack", "id": str(d.get("id") or ""),
                             "tid": self.instance_id}, ip, addr[1])
            fid = str(d.get("fid") or "")
            with self.tlock:
                tr = self.transfers_in.get(fid)
            if tr is None:
                return
            try:
                seq = int(d.get("seq"))
                chunk = base64.b64decode(d.get("data") or "")
            except (TypeError, ValueError):
                return
            if len(chunk) > FILE_CHUNK_SIZE * 2:
                return
            done = False
            with self.tlock:
                if seq != tr["next_seq"]:
                    # 이미 기록한 조각의 중복 재전송(ACK 유실 등) 또는 순서 이탈 —
                    # ack는 이미 보냈으니 디스크에 다시 쓰지 않고 조용히 무시한다.
                    return
                try:
                    with open(tr["part_path"], "ab") as f:
                        f.write(chunk)
                except OSError as e:
                    applog.log("partial_write", e, detail=tr["name"])
                    return
                tr["next_seq"] += 1
                idx_entry = self._partial_index.get(tr["resume_key"])
                if idx_entry is not None:
                    idx_entry["next_seq"] = tr["next_seq"]
                    idx_entry["ts"] = time.time()
                if tr["next_seq"] >= tr["total"]:
                    done = True
                should_persist = done or (tr["next_seq"] % 8 == 0)
            if should_persist:
                self._save_partial_index()
            if done:
                self._finish_incoming_file(fid, tr)
            return

    def _parse_members(self, raw):
        members = set()
        for m in (raw or []):
            try:
                ip, port = str(m[0]), int(m[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not self._is_self(ip, port):
                members.add((ip, port))
                if len(m) >= 3 and m[2]:
                    pname = str(m[2]).strip()
                    if pname and pname != "?":
                        self._upsert_peer(ip, port, pname)
        return members

    # ---------- 동료 목록 ----------
    def _upsert_peer(self, ip, port, name, avatar_hash=None, status=None):
        # presence 패킷의 이름은 (메시지 본문과 달리) 그동안 살균을 안 거쳤다 — 상대가
        # 이형 문자 선택자(U+FE0F)가 섞인 이름을 쓰면, 사이드바·멘션 자동완성 등 이름이
        # 그대로 노출되는 모든 곳에서 폰트 두부 상자가 재현된다(실측 확인). 여기서
        # 한 번에 막아두면 이름을 저장/전파하는 모든 경로(presence·group_invite 등)가
        # 다 이 함수를 거치므로 한 곳만 고치면 된다.
        name = sanitize_chat_text(str(name or ""))[:60]
        key = (ip, int(port))
        changed = False
        with self.plock:
            p = self.peers.get(key)
            if p is None:
                self.peers[key] = {"name": name or ip, "ip": ip, "port": key[1],
                                   "last": time.time(), "static": key in self.static,
                                   "av": avatar_hash or "", "status": status or "online"}
                changed = True
            else:
                if key in self.static:
                    p["static"] = True
                if name and name != "?":
                    if p["name"] != name:
                        p["name"] = name
                        changed = True
                if avatar_hash is not None and p.get("av") != avatar_hash:
                    p["av"] = avatar_hash
                    changed = True
                if status is not None and p.get("status") != status:
                    p["status"] = status
                    changed = True
                p["last"] = time.time()
        if changed:
            self._emit({"ev": "peer"})
        if avatar_hash:
            self._check_fetch_avatar(ip, int(port), avatar_hash)
        return changed

    # ---------- 그룹 ----------
    def _upsert_group(self, gid, name, members):
        name = sanitize_chat_text(str(name or ""))[:60]  # 이름 필드도 살균(peer 이름과 동일 이유)
        with self.glock:
            if gid in self.left_groups:
                return False
        members = {(ip, port) for ip, port in members if not self._is_self(ip, port)}
        changed = False
        with self.glock:
            g = self.groups.get(gid)
            if g is None:
                self.groups[gid] = {"name": name or "그룹", "members": set(members),
                                    "left": set(), "created": time.time()}
                changed = True
            else:
                if name and g["name"] != name:
                    g["name"] = name
                    changed = True
                left = g.get("left", set())
                effective = members - left
                union = g["members"] | effective
                if union != g["members"]:
                    g["members"] = union
                    changed = True
        if changed:
            self._save_groups()
        return changed

    def create_group(self, name, member_keys):
        gid = uuid.uuid4().hex[:12]
        name = sanitize_chat_text(str(name or "그룹")).strip()[:60] or "그룹"
        members = {(ip, port) for ip, port in member_keys if not self._is_self(ip, port)}
        with self.glock:
            self.left_groups.discard(gid)
            self.groups[gid] = {"name": name, "members": members, "left": set(), "created": time.time()}
        self._save_groups()
        self._emit({"ev": "group", "gid": gid})
        self._broadcast_group_invite(gid)
        return gid

    def add_group_members(self, gid, new_keys):
        new_set = {(ip, port) for ip, port in new_keys if not self._is_self(ip, port)}
        with self.glock:
            g = self.groups.get(gid)
            if g is None:
                return False
            g.setdefault("left", set()).difference_update(new_set)
            g["members"] |= new_set
        self._save_groups()
        self._emit({"ev": "group", "gid": gid})
        self._broadcast_group_invite(gid)
        return True

    def rename_group(self, gid, new_name):
        new_name = sanitize_chat_text(str(new_name or "그룹")).strip()[:60] or "그룹"
        with self.glock:
            g = self.groups.get(gid)
            if g is None:
                return False
            g["name"] = new_name
        self._save_groups()
        self._emit({"ev": "group", "gid": gid})
        self._broadcast_group_invite(gid)
        return True

    def _broadcast_group_invite(self, gid):
        with self.glock:
            g = self.groups.get(gid)
            if g is None:
                return
            name = g["name"]
            members = set(g["members"])
        hist = self._recent_group_history(gid, GROUP_HISTORY_SNAPSHOT)
        with self.plock:
            pnames = {k: v.get("name", "") for k, v in self.peers.items()}
        member_payload = [[ip, port, pnames.get((ip, port), "")] for ip, port in members]
        for (ip, port) in members:
            pkt = {"type": "group_invite", "id": uuid.uuid4().hex, "tid": self.instance_id,
                   "gid": gid, "name": name, "port": self.port, "sender_name": self.name,
                   "members": member_payload, "history": hist}
            self._send_reliable(pkt, ip, port)

    def send_group_message(self, gid, text, reply=None, burn_sec=0, sticker_id=None):
        if sticker_id:
            sticker_id = str(sticker_id)
            text = ""
        else:
            text = sanitize_chat_text(text or "").strip()
            if not text:
                return None
            if len(text) > MAX_LEN:
                text = text[:MAX_LEN]
        try:
            burn_sec = max(0, int(burn_sec or 0))
        except (TypeError, ValueError):
            burn_sec = 0
        burn_deadline = (time.time() + burn_sec) if burn_sec else None
        with self.glock:
            g = self.groups.get(gid)
            if g is None:
                return None
            members = set(g["members"])
        mid = uuid.uuid4().hex
        unread_count = len(members)
        self._append_group_log(gid, self.name, text, True, reply=reply, mid=mid,
                               unread_count=unread_count, burn_sec=burn_sec,
                               burn_deadline=burn_deadline, sticker_id=sticker_id)
        with self.plock:
            pnames = {k: v.get("name", "") for k, v in self.peers.items()}
        member_payload = [[ip, port, pnames.get((ip, port), "")] for ip, port in members]
        for (ip, port) in members:
            pkt_id = uuid.uuid4().hex
            pkt = {"type": "group_msg", "id": pkt_id, "mid": mid, "tid": self.instance_id,
                   "gid": gid, "name": self.name, "text": text, "port": self.port,
                   "members": member_payload}
            if reply:
                pkt["reply"] = reply
            if burn_sec:
                pkt["burn_sec"] = burn_sec
                pkt["burn_deadline"] = burn_deadline
            if sticker_id:
                pkt["sticker_id"] = sticker_id
            def _make_done(target_ip, target_port, packet):
                def _done(ok):
                    if not ok:
                        self._enqueue_outbox(target_ip, target_port, packet)
                    self._emit({"ev": "gsent", "gid": gid, "ip": target_ip, "port": target_port, "ok": ok, "id": mid})
                return _done
            self._send_reliable(pkt, ip, port, on_done=_make_done(ip, port, pkt))
        return mid

    # ---------- 전송(공용 신뢰 전송 워커) ----------
    def _send_reliable_wait(self, pkt, ip, port):
        """ack를 기다렸다가(최대 3회 재전송) 성공 여부를 돌려주는 블로킹 버전.
        파일 청크처럼 "이전 것이 도착한 뒤 다음 것을 보내야" 하는 순차 전송에 쓰인다."""
        mid = pkt["id"]
        ack = threading.Event()
        with self.pending_lock:
            self.pending[mid] = ack
        ok = False
        for _ in range(MSG_RETRY_MAX):
            self._send_dict(pkt, ip, port)
            if ack.wait(RETRY_WAIT):
                ok = True
                break
        with self.pending_lock:
            self.pending.pop(mid, None)
        return ok

    def _send_reliable(self, pkt, ip, port, on_done=None):
        """백그라운드 스레드에서 _send_reliable_wait를 실행하는 논블로킹 버전."""
        def worker():
            ok = self._send_reliable_wait(pkt, ip, port)
            if on_done:
                on_done(ok)

        threading.Thread(target=worker, daemon=True).start()
        return pkt["id"]

    def send_message(self, ip, port, text, reply=None, burn_sec=0, sticker_id=None):
        if sticker_id:
            # 스티커는 본문 텍스트가 없다 — 빈 텍스트라고 무시되지 않도록 일반
            # 텍스트 정제/빈 값 검사를 건너뛴다.
            sticker_id = str(sticker_id)
            text = ""
        else:
            text = sanitize_chat_text(text or "").strip()
            if not text:
                return None
            if len(text) > MAX_LEN:
                text = text[:MAX_LEN]
        try:
            burn_sec = max(0, int(burn_sec or 0))
        except (TypeError, ValueError):
            burn_sec = 0
        mid = uuid.uuid4().hex
        pkt = {"type": "msg", "id": mid, "tid": self.instance_id, "name": self.name,
               "text": text, "port": self.port}
        if reply:
            pkt["reply"] = reply
        if burn_sec:
            pkt["burn_sec"] = burn_sec
        if sticker_id:
            pkt["sticker_id"] = sticker_id
        self._append_log(ip, int(port), "out", text, reply=reply, mid=mid, unread=True,
                         burn_sec=burn_sec, sticker_id=sticker_id)
        def _done(ok):
            if not ok:
                self._enqueue_outbox(ip, port, pkt)
            self._emit({"ev": "sent", "id": mid, "ok": ok, "ip": ip, "port": port})
        self._send_reliable(pkt, ip, port, on_done=_done)
        return mid

    # ---------- 파일 전송 ----------
    def send_file(self, target, path):
        """target = ("dm", ip, port) 또는 ("grp", gid). 성공 시 fid, 실패 시 예외/None."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        if size > self.max_file_size:
            raise ValueError(f"파일이 너무 큽니다. 최대 {self.max_file_size // (1024 * 1024)}MB까지 보낼 수 있습니다.")
        fid = uuid.uuid4().hex
        name = os.path.basename(path)
        total = max(1, (size + FILE_CHUNK_SIZE - 1) // FILE_CHUNK_SIZE)
        is_image = os.path.splitext(name)[1].lower() in ALL_IMAGE_EXTS

        if target[0] == "dm":
            _, ip, port = target
            members = [(ip, int(port))]
            self._append_log_file(ip, int(port), "out", name, size, path, is_image)
        else:
            _, gid = target
            with self.glock:
                g = self.groups.get(gid)
                members = list(g["members"]) if g else []
            self._append_group_log_file(gid, self.name, True, name, size, path, is_image)

        threading.Thread(target=self._file_send_worker,
                         args=(fid, path, name, total, target, members),
                         daemon=True).start()
        return fid

    def _wait_resume_from(self, fid, ip, port, timeout=2.0):
        """상대가 file_offer_ack로 알려주는 resume_from을 잠깐 기다린다. 응답이 없으면
        (구버전 상대이거나 패킷 유실) 0부터 — 즉 지금까지의 동작과 동일하게 — 보낸다."""
        key = (fid, ip, port)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.tlock:
                if key in self._resume_info:
                    return self._resume_info.pop(key)
            time.sleep(0.05)
        return 0

    def _file_send_to_member(self, fid, path, name, total, gid, ip, port, progress_agg=None):
        """progress_agg가 주어지면(그룹 전송, 멤버별 스레드가 병렬로 도는 경우)
        각자의 진행 상황을 합산한 값을 emit해, 서로 다른 멤버 스레드가 같은 fid로
        진행률 이벤트를 쏘면서 화면의 진행률 바가 80%↔10% 사이를 오가며 튀는
        현상을 막는다. None이면(1:1 전송) 기존과 동일하게 이 멤버 하나만의
        seq/total을 그대로 emit한다."""
        def _emit_progress(seq, resumed=False):
            if progress_agg is None:
                ev = {"ev": "file_progress", "fid": fid, "sent": seq, "total": total}
                if resumed:
                    ev["resumed"] = True
                self._emit(ev)
                return
            done, lock, member_count = progress_agg
            with lock:
                done[(ip, port)] = seq
                agg_sent = sum(done.values())
            self._emit({"ev": "file_progress", "fid": fid, "sent": agg_sent,
                       "total": total * member_count})

        offer = {"type": "file_offer", "id": uuid.uuid4().hex, "tid": self.instance_id,
                 "fid": fid, "name": name, "size": os.path.getsize(path) if os.path.exists(path) else 0,
                 "total": total, "port": self.port}
        if gid:
            offer["gid"] = gid
        if not self._send_reliable_wait(offer, ip, port):
            return False
        # 상대가 이 파일을 이미 부분적으로(혹은 전부) 갖고 있으면 그만큼 건너뛴다
        # (이어받기) — 예전에 끊긴 전송을 다시 시도할 때 처음부터 다시 안 보내도 됨.
        resume_from = self._wait_resume_from(fid, ip, port)
        if resume_from:
            _emit_progress(resume_from, resumed=True)
        ok = True
        try:
            with open(path, "rb") as f:
                seq = resume_from
                file_size = os.path.getsize(path) if os.path.exists(path) else 0
                if file_size == 0:
                    if resume_from >= total:
                        return True  # 상대가 이미 다 받음(빈 파일)
                    pkt = {"type": "file_chunk", "id": uuid.uuid4().hex, "tid": self.instance_id,
                           "fid": fid, "seq": 0, "data": "", "port": self.port}
                    if gid:
                        pkt["gid"] = gid
                    if not self._send_reliable_wait(pkt, ip, port):
                        ok = False
                    _emit_progress(1)
                else:
                    if resume_from >= total:
                        return True  # 상대가 이미 전부 받음
                    if seq > 0:
                        f.seek(seq * FILE_CHUNK_SIZE)
                    while True:
                        chunk = f.read(FILE_CHUNK_SIZE)
                        if not chunk:
                            break
                        pkt = {"type": "file_chunk", "id": uuid.uuid4().hex, "tid": self.instance_id,
                              "fid": fid, "seq": seq,
                              "data": base64.b64encode(chunk).decode("ascii"), "port": self.port}
                        if gid:
                            pkt["gid"] = gid
                        if not self._send_reliable_wait(pkt, ip, port):
                            ok = False
                            break
                        seq += 1
                        _emit_progress(seq)
        except OSError as e:
            applog.log("file_send", e, detail=f"{path} -> {ip}:{port}")
            ok = False
        return ok

    def _file_send_worker(self, fid, path, name, total, target, members):
        gid = target[1] if target[0] == "grp" else None
        if not members:
            self._emit({"ev": "file_sent", "fid": fid, "ok": False})
            return
        sent_count = None
        if len(members) == 1:
            ip, port = members[0]
            ok_all = self._file_send_to_member(fid, path, name, total, gid, ip, port)
        else:
            # 그룹 파일 전송 — 예전에는 멤버 1명씩 순서대로 처음부터 끝까지 다 보낸 뒤
            # 다음 멤버로 넘어갔다(5명 그룹에 20MB면 사실상 80MB를 순차로 보내는 셈이라
            # 오래 걸림). 서로 독립된 상대이므로 동시에 병렬로 보내도 안전하다.
            results = []
            results_lock = threading.Lock()
            progress_agg = ({}, threading.Lock(), len(members))

            def _run(ip, port):
                ok = self._file_send_to_member(fid, path, name, total, gid, ip, port,
                                               progress_agg=progress_agg)
                with results_lock:
                    results.append(ok)

            threads = [threading.Thread(target=_run, args=(ip, port), daemon=True)
                      for (ip, port) in members]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # 전원 성공(all)만 성공으로 치면, 10명 중 9명이 정상 수신했어도
            # "전송 실패"로 표시돼 이미 받은 사람들에게 불필요하게 재전송을
            # 유도했다 — 한 명이라도 받았으면 성공으로 보고하되, 몇 명이
            # 받았는지도 같이 넘겨 GUI가 부분 성공을 구분해 보여줄 수 있게 한다.
            sent_count = sum(1 for r in results if r)
            ok_all = sent_count > 0
        self._emit({"ev": "file_sent", "fid": fid, "ok": ok_all,
                    "sent_count": sent_count, "total_count": len(members)})

    def _unique_path(self, path):
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        n = 1
        while True:
            cand = f"{base} ({n}){ext}"
            if not os.path.exists(cand):
                return cand
            n += 1

    def _finish_incoming_file(self, fid, tr):
        """조각들은 이미 도착하는 대로 tr['part_path']에 순서대로 이어 써뒀으므로
        (이어받기를 위한 구조 변경), 여기서는 그 파일을 최종 위치로 옮기기만 하면
        된다 — 예전처럼 메모리에 전체를 들고 있다가 한 번에 쓰지 않는다."""
        with self.tlock:
            self.transfers_in.pop(fid, None)
            self._partial_index.pop(tr.get("resume_key"), None)
        self._save_partial_index()
        part_path = tr.get("part_path")
        name = tr["name"]
        if not part_path or not os.path.exists(part_path):
            return
        actual_size = os.path.getsize(part_path)
        if actual_size != tr["size"] or actual_size > self.max_file_size:
            applog.log("file_recv_size_mismatch", detail=f"{name} expected={tr['size']} actual={actual_size}")
            try:
                os.remove(part_path)
            except OSError:
                pass
            return
        is_image = os.path.splitext(name)[1].lower() in ALL_IMAGE_EXTS
        try:
            os.makedirs(self.download_dir, exist_ok=True)
            with self._save_path_lock:
                save_path = self._unique_path(os.path.join(self.download_dir, safe_name(name)))
                shutil.move(part_path, save_path)
        except OSError as e:
            applog.log("file_recv_save", e, detail=name)
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError:
                pass
            return
        target = tr["target"]
        ip, sport = tr["from"]
        if target[0] == "dm":
            self._append_log_file(ip, sport, "in", name, actual_size, save_path, is_image)
            self._emit({"ev": "file_recv", "peer": (ip, sport), "path": save_path, "name": name,
                       "size": actual_size, "is_image": is_image, "ts": time.time()})
        else:
            gid = target[1]
            with self.plock:
                p = self.peers.get((ip, sport))
            sender_name = (p or {}).get("name") or ip
            self._append_group_log_file(gid, sender_name, False, name, actual_size, save_path, is_image)
            self._emit({"ev": "gfile_recv", "gid": gid,
                       "who": {"ip": ip, "port": sport, "name": sender_name},
                       "path": save_path, "name": name, "size": actual_size,
                       "is_image": is_image, "ts": time.time()})

    # ---------- 대화 기록(1:1) ----------
    def _log_path(self, ip, port):
        # 이름이 아니라 IP+포트로 기록 — 표시 이름 변경과 무관하게 대화가 하나의 파일에 누적
        return os.path.join(self.logdir, f"{ip}_{int(port)}.jsonl")

    def load_history(self, ip, port):
        path = self._log_path(ip, port)
        out = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        rec = self._dec_log_line(line)
                        if isinstance(rec, dict) and ("text" in rec or "kind" in rec):
                            out.append(rec)
            except OSError:
                pass
        if out:
            with self.log_lock:
                self._last_record_cache[("dm", ip, port)] = out[-1]
        return out

    def _append_log(self, ip, port, direction, text, av="", reply=None, mid=None, unread=False,
                    burn_sec=0, sticker_id=None):
        rec = {"dir": direction, "text": text[:MAX_LEN], "ts": time.time()}
        if av:
            rec["av"] = av
        if reply:
            rec["reply"] = reply
        if mid:
            rec["mid"] = mid
        if unread:
            rec["unread"] = True
        if burn_sec:
            rec["burn_sec"] = burn_sec
        if sticker_id:
            rec["kind"] = "sticker"
            rec["sticker_id"] = sticker_id
        try:
            with self.log_lock, open(self._log_path(ip, port), "a", encoding="utf-8") as f:
                f.write(self._enc_log_line(rec))
                self._last_record_cache[("dm", ip, port)] = rec
        except OSError as e:
            applog.log("append_log", e, detail=f"{ip}:{port}")

    def _mark_read_in_log(self, ip, port, mid=""):
        """read_ack 수신 즉시 반영하지 않고 짧게 모아서(batch) 처리한다 — 안 읽은
        메시지가 N개 있던 방을 열면 read_ack가 N번 연속으로 도착하는데, 그때마다
        대화 로그 파일 전체를 읽고 다시 쓰면 대화가 많이 쌓인 방일수록 log_lock을
        오래 붙잡아 반응이 느려진다."""
        key = (ip, port)
        with self._read_ack_lock:
            pending = self._read_ack_pending.setdefault(key, set())
            pending.add(mid or None)  # None = "전체 읽음 처리"(mid 미지정)
            timer = self._read_ack_timer.get(key)
            if timer is None or not timer.is_alive():
                t = threading.Timer(0.3, self._flush_read_ack, args=(key,))
                t.daemon = True
                self._read_ack_timer[key] = t
                t.start()

    def _flush_read_ack(self, key):
        ip, port = key
        with self._read_ack_lock:
            mids = self._read_ack_pending.pop(key, None)
            self._read_ack_timer.pop(key, None)
        if not mids:
            return
        mark_all = None in mids
        specific = {m for m in mids if m}
        path = self._log_path(ip, port)
        if not os.path.exists(path):
            return
        with self.log_lock:
            updated = False
            has_pending_burn = False
            recs = []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        r = self._dec_log_line(line)
                        if r is None:
                            continue
                        if r.get("dir") == "out" and r.get("unread"):
                            if mark_all or r.get("mid") in specific:
                                r["unread"] = False
                                updated = True
                                # 상대가 방금 이 메시지를 읽었다 — burn_sec이 걸려
                                # 있으면 여기서부터 자동 폭파 카운트다운을 시작한다.
                                if r.get("burn_sec") and not r.get("burn_deadline"):
                                    r["burn_deadline"] = time.time() + r["burn_sec"]
                        if r.get("burn_deadline"):
                            has_pending_burn = True
                        recs.append(r)
                if updated:
                    text = "".join(self._enc_log_line(r) for r in recs)
                    self._atomic_write_text(path, text)
                    if recs:
                        self._last_record_cache[("dm", ip, port)] = recs[-1]
            except OSError as e:
                applog.log("mark_read_in_log", e, detail=f"{ip}:{port}")
                return
        if has_pending_burn:
            with self._read_ack_lock:
                self._burn_candidates.add(("dm", ip, port))
        if updated:
            self._emit({"ev": "read_ack", "peer": (ip, port), "flush": True})

    def _append_log_file(self, ip, port, direction, name, size, path, is_image):
        rec = {"dir": direction, "kind": "file", "name": name, "size": size,
              "path": path, "is_image": bool(is_image), "ts": time.time()}
        try:
            with self.log_lock, open(self._log_path(ip, port), "a", encoding="utf-8") as f:
                f.write(self._enc_log_line(rec))
                self._last_record_cache[("dm", ip, port)] = rec
        except OSError:
            pass

    # ---------- 대화 기록(그룹) ----------
    def _group_log_path(self, gid):
        return os.path.join(self.logdir, f"group_{safe_name(gid)}.jsonl")

    def load_group_history(self, gid):
        path = self._group_log_path(gid)
        out = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        rec = self._dec_log_line(line)
                        if isinstance(rec, dict) and ("text" in rec or "kind" in rec):
                            out.append(rec)
            except OSError:
                pass
        if out:
            with self.log_lock:
                self._last_record_cache[("grp", gid)] = out[-1]
        return out

    def _append_group_log(self, gid, name, text, mine, av="", reply=None, is_system=False, mid=None,
                          unread_count=0, is_mention=False, burn_sec=0, burn_deadline=None, sticker_id=None):
        rec = {"name": name, "mine": bool(mine), "text": text[:MAX_LEN], "ts": time.time()}
        if mid:
            rec["mid"] = mid
        if mine and not is_system:
            rec["unread_count"] = unread_count
            rec["initial_unread"] = unread_count
            rec["readers"] = []
        if is_mention:
            rec["is_mention"] = True
        if av:
            rec["av"] = av
        if reply:
            rec["reply"] = reply
        if is_system:
            rec["is_system"] = True
        if burn_sec:
            rec["burn_sec"] = burn_sec
        if burn_deadline:
            rec["burn_deadline"] = burn_deadline
            with self._read_ack_lock:
                self._burn_candidates.add(("grp", gid))
        if sticker_id:
            rec["kind"] = "sticker"
            rec["sticker_id"] = sticker_id
        try:
            with self.log_lock, open(self._group_log_path(gid), "a", encoding="utf-8") as f:
                f.write(self._enc_log_line(rec))
                self._last_record_cache[("grp", gid)] = rec
        except OSError as e:
            applog.log("append_group_log", e, detail=gid)

    def _append_group_log_file(self, gid, name, mine, fname, size, path, is_image):
        rec = {"name": name, "mine": bool(mine), "kind": "file", "fname": fname, "size": size,
              "path": path, "is_image": bool(is_image), "ts": time.time()}
        try:
            with self.log_lock, open(self._group_log_path(gid), "a", encoding="utf-8") as f:
                f.write(self._enc_log_line(rec))
                self._last_record_cache[("grp", gid)] = rec
        except OSError:
            pass

    def _recent_group_history(self, gid, n):
        # 초대 스냅샷은 일반 텍스트 대화만 포함
        # (파일·스티커는 텍스트가 없어 스냅샷 형식과 안 맞아 제외, 자동 폭파 메시지는
        # 보안상 새 멤버에게 유출되지 않도록 제외)
        recs = [r for r in self.load_group_history(gid)
                if r.get("kind") not in ("file", "sticker") and not r.get("burn_sec")][-n:]
        return [{"name": r.get("name") or "?", "text": r.get("text", ""), "ts": r.get("ts")}
                for r in recs]

    def _merge_group_history(self, gid, hist):
        existing = self.load_group_history(gid)
        seen = {(r.get("name"), r.get("text"), round(r.get("ts") or 0, 1)) for r in existing}
        path = self._group_log_path(gid)
        added = False
        try:
            with self.log_lock, open(path, "a", encoding="utf-8") as f:
                for entry in hist:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name") or "?")[:60]
                    text = str(entry.get("text") or "")[:MAX_LEN]
                    ts = entry.get("ts") or time.time()
                    if not text:
                        continue
                    sig = (name, text, round(ts, 1))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    rec = {"name": name, "mine": False, "text": text, "ts": ts}
                    f.write(self._enc_log_line(rec))
                    added = True
        except OSError:
            pass
        if added:
            self._last_record_cache.pop(("grp", gid), None)
        return added

    def delete_history(self, key):
        if not key:
            return False
        if key[0] == "dm":
            _, ip, port = key
            path = self._log_path(ip, port)
        elif key[0] == "grp":
            _, gid = key
            path = self._group_log_path(gid)
        else:
            return False
        with self.log_lock:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            self._last_record_cache.pop(key, None)
        # 기록을 통째로 지운 이상 그 방을 가리키던 공지(있었다면)도 같이 해제한다
        # (안 그러면 메시지는 없는데 공지 배너만 계속 settings.json에 남는다).
        if self.get_pinned_notice(key):
            self.clear_pinned_notice(key)
        self._emit({"ev": "history_deleted", "key": key})
        return True

    def get_last_history_record(self, key):
        """대화 목록 미리보기(마지막 1줄)를 위한 초고속 O(1) 메모리 캐시 조회.
        캐시 미스 시 디스크 파일의 끝 8KB만 탐색하여 역방향으로 마지막 1줄만 파싱한다."""
        if not key:
            return None
        with self.log_lock:
            if key in self._last_record_cache:
                return self._last_record_cache[key]
            if key[0] == "dm":
                path = self._log_path(key[1], key[2])
            elif key[0] == "grp":
                path = self._group_log_path(key[1])
            else:
                return None
            last_rec = self._read_last_record_from_file(path)
            self._last_record_cache[key] = last_rec
            return last_rec

    def _read_last_record_from_file(self, path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 8192)
                f.seek(size - read_size)
                chunk = f.read(read_size).decode("utf-8", "replace")
                lines = [l for l in chunk.splitlines() if l.strip()]
                for l in reversed(lines):
                    rec = self._dec_log_line(l)
                    if isinstance(rec, dict) and ("text" in rec or "kind" in rec):
                        return rec
        except OSError:
            pass
        return None

    def _mark_group_read_in_log(self, gid, ip, port, mid=""):
        """gread_ack도 read_ack와 같은 이유로 짧게 모아뒀다가 한 번에 반영한다."""
        reader_k = f"{ip}:{port}"
        with self._read_ack_lock:
            pending = self._gread_ack_pending.setdefault(gid, [])
            pending.append((reader_k, mid or None))
            timer = self._gread_ack_timer.get(gid)
            if timer is None or not timer.is_alive():
                t = threading.Timer(0.3, self._flush_gread_ack, args=(gid,))
                t.daemon = True
                self._gread_ack_timer[gid] = t
                t.start()

    def _flush_gread_ack(self, gid):
        with self._read_ack_lock:
            entries = self._gread_ack_pending.pop(gid, None)
            self._gread_ack_timer.pop(gid, None)
        if not entries:
            return
        path = self._group_log_path(gid)
        if not os.path.exists(path):
            return
        with self.log_lock:
            updated = False
            recs = []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        r = self._dec_log_line(line)
                        if r is None:
                            continue
                        if r.get("mine") and not r.get("is_system") and r.get("unread_count", 0) > 0:
                            readers = r.setdefault("readers", [])
                            for reader_k, mid in entries:
                                if mid and r.get("mid") != mid:
                                    continue
                                if reader_k not in readers:
                                    readers.append(reader_k)
                                    init_cnt = r.get("initial_unread", len(readers))
                                    r["unread_count"] = max(0, init_cnt - len(readers))
                                    updated = True
                        recs.append(r)
                if updated:
                    text = "".join(self._enc_log_line(r) for r in recs)
                    self._atomic_write_text(path, text)
                    if recs:
                        self._last_record_cache[("grp", gid)] = recs[-1]
            except OSError as e:
                applog.log("mark_group_read", e, detail=gid)
                return
        if updated:
            self._emit({"ev": "gread_ack", "gid": gid, "flush": True})

    def search_all_logs(self, query, limit=50):
        query = (query or "").strip().lower()
        if not query:
            return []
        results = []
        try:
            fnames = os.listdir(self.logdir)
            # 최근에 수정된(=최근에 대화한) 방부터 훑는다. 예전에는 os.listdir 순서 그대로
            # 훑다가 limit이 차면 그 즉시 검색을 끝내버려서, 최근 대화가 뒤쪽 파일에 있으면
            # 검색 결과에서 통째로 누락되고 오래된 대화만 나오는 문제가 있었다.
            files = sorted(fnames, key=lambda fn: os.path.getmtime(os.path.join(self.logdir, fn))
                           if os.path.exists(os.path.join(self.logdir, fn)) else 0, reverse=True)
        except OSError:
            return []

        with self.plock:
            peers_map = {k: v.get("name") or k[0] for k, v in self.peers.items()}
        with self.glock:
            groups_map = {gid: g.get("name") or "그룹" for gid, g in self.groups.items()}

        with self.log_lock:
            for fname in files:
                if not fname.endswith(".jsonl"):
                    continue
                fpath = os.path.join(self.logdir, fname)
                if fname.startswith("group_"):
                    gid = fname[6:-6]
                    key = ("grp", gid)
                    room_name = groups_map.get(gid, "그룹")
                    is_grp = True
                else:
                    base = fname[:-6]
                    if "_" not in base:
                        continue
                    parts = base.split("_", 1)
                    try:
                        ip, sport = parts[0], int(parts[1])
                    except ValueError:
                        continue
                    key = ("dm", ip, sport)
                    alias = self.get_alias(key)
                    room_name = alias or peers_map.get((ip, sport), ip)
                    is_grp = False

                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        rec_idx = 0
                        for line in f:
                            r = self._dec_log_line(line)
                            if not (isinstance(r, dict) and ("text" in r or "kind" in r)):
                                continue
                            kind = r.get("kind")
                            if kind == "sticker":
                                # 스티커는 본문 텍스트가 없어("") 이름/키워드로 검색 가능하게
                                # 하고, 화면에는 "[이모티콘] 이름"으로 보여준다. (예전에는
                                # text가 비어 있으면 발신자 이름(name)까지 본문으로 오인해서
                                # 사람 이름을 검색하면 그 사람이 보낸 스티커가 엉뚱하게
                                # 매칭되는 오탐이 있었다 — 이제 "name"을 본문으로 취급하지
                                # 않는다.)
                                meta = stickers.STICKERS.get(r.get("sticker_id"), {})
                                match_txt = " ".join([meta.get("name", "")] + meta.get("keywords", []))
                                disp_txt = f"[이모티콘] {meta.get('name', '이모티콘')}"
                            elif kind == "file":
                                # 그룹 파일은 "fname"이 실제 파일명("name"은 발신자),
                                # DM 파일은 "fname"이 없고 "name" 자체가 파일명이다.
                                match_txt = r.get("fname") or r.get("name") or ""
                                disp_txt = match_txt
                            else:
                                match_txt = r.get("text") or ""
                                disp_txt = match_txt
                            if query in match_txt.lower():
                                sender = r.get("name") or ("나" if (r.get("mine") or r.get("dir") == "out") else room_name)
                                results.append({
                                    "key": key,
                                    "room_name": room_name,
                                    "sender": sender,
                                    "text": disp_txt,
                                    "ts": r.get("ts", 0),
                                    "rec_idx": rec_idx,
                                    "mid": r.get("mid", ""),
                                    "is_grp": is_grp
                                })
                            rec_idx += 1
                except OSError:
                    pass
        return sorted(results, key=lambda x: x["ts"], reverse=True)[:limit]

    def stop(self):
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        crypto_layer.shutdown_crypto_pool()

    def _emit(self, ev):
        try:
            self.on_event(ev)
        except Exception as e:
            # 이 지점은 엔진 스레드가 GUI로 알리는 모든 이벤트(메시지 수신, 피어
            # 상태, 아바타 갱신 등)가 지나가는 유일한 통로다 — 여기서 예외를
            # 조용히 삼키면 원인 파악 없이 이벤트 하나가 통째로 유실된다.
            applog.log("emit", e, detail=str(ev.get("ev")))


