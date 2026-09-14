# -*- coding: utf-8 -*-
"""
crypto_layer.py — LAN 패킷용 최소 인증-암호화 계층 (표준 라이브러리만 사용, 외부 패키지 없음)

배경: 기존 프로토콜은 UDP로 평문 JSON을 그대로 주고받아, 같은 네트워크의 누구나
패킷을 스니핑해 대화 내용을 읽거나, 임의의 "name" 필드로 상대를 사칭(스푸핑)할 수
있었다. 이 모듈은 사전 공유키(secret.key) 기반으로:
  1) 기밀성: HMAC-SHA256을 PRF로 쓰는 카운터 모드 스트림 암호로 본문을 암호화
  2) 무결성/인증: HMAC-SHA256 태그로 변조·위조 패킷을 검증 전 폐기(encrypt-then-MAC)
를 제공한다. AES 같은 표준 블록 암호가 stdlib에 없어 "군사급"은 아니지만,
패시브 스니핑과 임의 위조·사칭을 막는다는 목적은 충분히 달성한다.

키 배포는 기존 배포 방식(폴더를 통째로 복사해 상대 PC에서 실행)에 맞춘다 —
누구든 최초 실행 시 secret.key가 없으면 새로 생성하고, 그 폴더를 그대로 복사해
동료에게 배포하면 모두가 같은 키를 공유하게 된다.
"""
import hashlib
import hmac
import os
import secrets
import struct
import threading
import time

KEY_FILENAME = "secret.key"
_MAGIC_V2 = b"\x02"
_MAGIC_LEN = 1
_TS_LEN = 8
_NONCE_LEN = 12
_TAG_LEN = 32
_HEADER_LEN = _MAGIC_LEN + _TS_LEN + _NONCE_LEN + _TAG_LEN  # 1 + 8 + 12 + 32 = 53 bytes

_MAGIC_BLOB = b"\xb1"  # 저장소(파일) 암호화 전용 매직 — 네트워크 패킷(_MAGIC_V2)과 구분
_BLOB_HEADER_LEN = _MAGIC_LEN + _NONCE_LEN + _TAG_LEN  # 1 + 12 + 32 = 45 bytes

MAX_CLOCK_SKEW = 120.0   # 허용 시계 오차 (초 단위, 2분)
_PRUNE_INTERVAL = 30.0   # 캐시 정리 주기 (초)

_replay_cache = {}       # tag (bytes) -> float (received_time)
_replay_lock = threading.Lock()
_last_prune_time = 0.0


def _prune_replay_cache_locked(now):
    global _last_prune_time
    if now - _last_prune_time < _PRUNE_INTERVAL:
        return
    _last_prune_time = now
    cutoff = now - MAX_CLOCK_SKEW
    expired = [t for t, rec_time in _replay_cache.items()
               if rec_time < cutoff or rec_time > now + MAX_CLOCK_SKEW]
    for t in expired:
        _replay_cache.pop(t, None)


def reset_replay_cache():
    """테스트 또는 디버깅용 캐시 초기화."""
    global _last_prune_time
    with _replay_lock:
        _replay_cache.clear()
        _last_prune_time = 0.0


def _key_path(base_dir):
    return os.path.join(base_dir, KEY_FILENAME)


def load_or_create_key(base_dir):
    """secret.key가 있으면 그대로 쓰고, 없으면 새로 만들어 저장한다.

    같은 폴더를 복사해 배포하는 기존 방식과 맞물려, 먼저 실행한 PC가 만든 키가
    폴더 복사를 통해 모든 동료 PC에 자동으로 퍼진다(사용자가 키를 직접 입력할
    필요 없음). 주의: 서로 다른 PC에서 "동시에 최초 실행"하면 각자 다른 무작위
    키를 만들어버려 서로 대화가 안 된다 — 반드시 한 PC에서 먼저 실행해 키를
    만든 뒤, secret.key를 포함한 폴더 전체를 나머지 PC에 복사해야 한다."""
    path = _key_path(base_dir)
    try:
        with open(path, "rb") as f:
            raw = f.read().strip()
        key = bytes.fromhex(raw.decode("ascii"))
        if len(key) == 32:
            return key
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    try:
        # 임시 파일에 먼저 쓰고 os.replace로 원자적 치환 — 같은 PC에서 두 인스턴스가
        # 동시에 처음 켜져도(테스트 환경 등) 절반만 쓰인 파일을 서로 읽는 일이 없다.
        tmp_path = path + f".tmp{os.getpid()}"
        with open(tmp_path, "wb") as f:
            f.write(key.hex().encode("ascii"))
        os.replace(tmp_path, path)
    except OSError:
        pass
    return key


def _keystream(key, nonce, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _xor_bytes(a, b):
    """a, b(같은 길이) XOR — 큰 버퍼(파일 청크·아바타 이미지 등)에서 바이트 단위
    파이썬 루프보다 훨씬 빠른 big-int XOR 트릭(내부적으로 C 구현)을 사용한다."""
    n = len(a)
    if n == 0:
        return b""
    ia = int.from_bytes(a, "big")
    ib = int.from_bytes(b, "big")
    return (ia ^ ib).to_bytes(n, "big")


def encrypt(key, plaintext):
    """plaintext(bytes) -> magic(1B) + ts(8B) + nonce(12B) + hmac tag(32B) + ciphertext(bytes)."""
    magic = _MAGIC_V2
    now_ms = int(time.time() * 1000)
    ts_bytes = struct.pack(">Q", now_ms)
    nonce = secrets.token_bytes(_NONCE_LEN)
    ks = _keystream(key, nonce, len(plaintext))
    ciphertext = _xor_bytes(plaintext, ks)
    tag = hmac.new(key, magic + ts_bytes + nonce + ciphertext, hashlib.sha256).digest()
    return magic + ts_bytes + nonce + tag + ciphertext


def decrypt(key, blob, max_skew=MAX_CLOCK_SKEW):
    """검증 실패(위조/변조/키 불일치/재전송 공격/시간 만료) 시 None. 성공 시 원문 bytes.
    실패 사유까지 알고 싶으면 decrypt_with_reason()을 대신 쓴다."""
    plain, _reason = decrypt_with_reason(key, blob, max_skew)
    return plain


def decrypt_with_reason(key, blob, max_skew=MAX_CLOCK_SKEW):
    """decrypt()와 동일하게 검증하되, 실패 시 사유 문자열도 함께 돌려준다:
    "bad_format"(길이/매직 불일치 — 구버전 패킷이나 쓰레기 데이터), "clock_skew"(시계
    오차 초과), "bad_tag"(키 불일치·변조·스푸핑), "replay"(재전송 공격 차단).
    성공 시 (plaintext, None). 이 사유는 네트워크로 응답하거나 상대에게 알리는 데
    쓰지 않는다 — 오직 로컬 debug.log 진단용이다(정보 노출 없이 운영자만 원인 확인)."""
    if not blob or len(blob) < _HEADER_LEN:
        return None, "bad_format"
    magic = blob[:_MAGIC_LEN]
    if magic != _MAGIC_V2:
        return None, "bad_format"
    ts_bytes = blob[_MAGIC_LEN:_MAGIC_LEN + _TS_LEN]
    nonce = blob[_MAGIC_LEN + _TS_LEN:_MAGIC_LEN + _TS_LEN + _NONCE_LEN]
    tag = blob[_MAGIC_LEN + _TS_LEN + _NONCE_LEN:_HEADER_LEN]
    ciphertext = blob[_HEADER_LEN:]

    # 1. 타임스탬프 유효성 검사 (시계 오차 윈도우)
    ts_ms = struct.unpack(">Q", ts_bytes)[0]
    pkt_time = ts_ms / 1000.0
    now = time.time()
    if abs(now - pkt_time) > max_skew:
        return None, "clock_skew"

    # 2. HMAC 무결성/인증 검증 (변조 및 타임스탬프 조작 방어)
    expected = hmac.new(key, magic + ts_bytes + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        return None, "bad_tag"

    # 3. 재전송(Replay) 공격 방어 (슬라이딩 윈도우 태그 중복 검사)
    with _replay_lock:
        _prune_replay_cache_locked(now)
        if tag in _replay_cache:
            return None, "replay"  # 이미 처리된 패킷 — 재전송 공격 차단!
        _replay_cache[tag] = now

    # 4. 복호화
    ks = _keystream(key, nonce, len(ciphertext))
    return _xor_bytes(ciphertext, ks), None


def encrypt_blob(key, plaintext):
    """디스크 저장(대화 로그 등)용 인증 암호화. encrypt()/decrypt()는 네트워크 패킷
    전제로 타임스탬프 만료(시계 오차 검사)와 재전송 방어 캐시가 있는데, 저장된 파일은
    몇 달 뒤에도, 같은 내용을 몇 번을 다시 읽어도 항상 복호화돼야 하므로 그 두 가지가
    오히려 방해가 된다(오래된 레코드는 "시계 오차 초과"로, 두 번째 읽기는 "재전송
    공격"으로 오판해 복호화가 실패해버림). 그래서 매직 바이트를 달리하고 타임스탬프·
    재전송 캐시 없이 nonce+HMAC 인증만 쓰는 별도 포맷을 둔다."""
    nonce = secrets.token_bytes(_NONCE_LEN)
    ks = _keystream(key, nonce, len(plaintext))
    ciphertext = _xor_bytes(plaintext, ks)
    tag = hmac.new(key, _MAGIC_BLOB + nonce + ciphertext, hashlib.sha256).digest()
    return _MAGIC_BLOB + nonce + tag + ciphertext


def decrypt_blob(key, blob):
    """실패(길이 부족/매직 불일치/키 불일치·변조) 시 None, 성공 시 원문 bytes."""
    if not blob or len(blob) < _BLOB_HEADER_LEN:
        return None
    if blob[:_MAGIC_LEN] != _MAGIC_BLOB:
        return None
    nonce = blob[_MAGIC_LEN:_MAGIC_LEN + _NONCE_LEN]
    tag = blob[_MAGIC_LEN + _NONCE_LEN:_BLOB_HEADER_LEN]
    ciphertext = blob[_BLOB_HEADER_LEN:]
    expected = hmac.new(key, _MAGIC_BLOB + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        return None
    ks = _keystream(key, nonce, len(ciphertext))
    return _xor_bytes(ciphertext, ks)
