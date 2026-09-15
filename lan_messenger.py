# -*- coding: utf-8 -*-
"""
LAN Talk — 서버 없는 P2P LAN 메신저 (v5.1)
- 감지: UDP 브로드캐스트(50707) 3초 간격 + 수신 시 즉시 맞응답
- 전송: UDP 유니캐스트 + 암호화(HMAC 인증) + 수신 확인(ACK) + 최대 3회 재전송
- 같은 서브넷: 자동 발견. 다른 세그먼트: [IP 직접 추가]로 상대 IP 등록
- 단체대화방: 서버 없이 각자 알고 있는 멤버에게 개별 유니캐스트로 팬아웃(가십 전파)
- Python 표준 라이브러리만 사용 (외부 패키지 설치 불필요)

이 파일은 실행 진입점(entry point)만 담당한다. 실제 구현은 유지보수를 위해
아래 파일들로 분리되어 있으며(v5.1 최초 분리, v6.12에서 app.py를 믹스인 단위로
추가 분리), 모두 이 폴더 안에 함께 배포된다:
  constants.py      - 색상/폰트/레이아웃/포트 등 전역 상수
  netutils.py       - 시간 포맷, 로컬 IP 캐시, 아바타 이미지 처리 등 순수 헬퍼
  canvas_utils.py   - 둥근 모서리 캔버스 그리기 헬퍼
  crypto_layer.py   - 패킷 암호화·인증(HMAC) 계층 + 저장소(로그 파일) 암호화
  applog.py         - 조용히 삼켜지던 예외를 파일로 남기는 디버그 로그
  winapi.py         - Windows 트레이 알림·다크 타이틀바 (ctypes)
  engine.py         - 네트워크 계층 (Engine 클래스)
  widgets.py        - 재사용 커스텀 위젯 (SplitterHandle, ScrollBottomButton 등)
  dnd_handler.py    - App 믹스인: 드래그앤드롭·트레이 알림 클릭(WH_GETMESSAGE 훅)
  chat_search.py    - App 믹스인: 대화방 내 검색(Ctrl+F) 이동/하이라이트
  chat_renderer.py  - App 믹스인: 채팅 캔버스 렌더링(말풍선·라벨·자동 폭파 배지 등)
  dialogs.py        - App 믹스인: 각종 다이얼로그(그룹 관리·카테고리·설정 등)
  app.py            - GUI 진입 클래스 (App = 위 믹스인들을 합친 최종 클래스)
"""
import argparse
import multiprocessing
import tkinter as tk

from app import App
from engine import Engine
from constants import DEFAULT_MAX_FILE_SIZE
from netutils import korea_time_str, parse_target, safe_name  # noqa: F401 - 하위 호환 재노출(기존 테스트 스크립트용)
from winapi import ensure_dpi_awareness


def main():
    # tk.Tk()로 첫 창을 만들기 전에 반드시 먼저 호출해야 한다 — Windows가 이 프로세스를
    # DPI 인식으로 등록하는 시점이 "첫 창이 뜨기 전"으로 고정돼 있다. 이걸 안 하면 배율
    # 100%가 아닌 화면에서 Windows가 창 전체를 사후에 비트맵으로 확대해버려서, IME
    # 한글 조합 중인 글자만 그 확대 배율만큼 다르게(대개 더 크게) 보이는 문제가 생긴다.
    ensure_dpi_awareness()
    ap = argparse.ArgumentParser(description="LAN Talk - 서버 없는 P2P (UDP 알고리즘)")
    ap.add_argument("--name", default=None, help="표시 이름 (기본: Windows 사용자 계정)")
    ap.add_argument("--port", type=int, default=None,
                     help="사용 포트 (기본: [＋]-[포트 번호 변경]으로 저장해둔 값, 없으면 50707)")
    ap.add_argument("--datadir", default=None, help="설정/대화 기록 저장 폴더")
    ap.add_argument("--peers", default="", help="직접 추가할 IP 목록 (콤마 구분, ip:port)")
    args = ap.parse_args()
    extra = [s for s in (args.peers or "").split(",") if s.strip()] or None
    root = tk.Tk()
    App(root, args, extra)
    root.mainloop()


if __name__ == "__main__":
    # PyInstaller onefile exe에서 대용량 파일 청크 암호화·복호화를 여러 CPU
    # 코어로 나눠 처리하려고 crypto_layer.py가 멀티프로세싱(ProcessPoolExecutor)을
    # 쓴다. Windows에서 빌드된 frozen exe는 이 한 줄이 없으면 새로 띄우는 워커
    # 프로세스마다 전체 앱을 처음부터 다시 실행하려 들어(자식이 또 자식을 낳는
    # 무한 증식) 반드시 다른 어떤 코드보다도 먼저 호출해야 한다(공식 문서 권장
    # 위치 그대로 — main() 호출보다도 앞).
    multiprocessing.freeze_support()
    main()
