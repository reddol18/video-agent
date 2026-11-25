#!/usr/bin/env python3
"""TTS 대본의 아라비아 숫자·영문 약어를 한글 발음으로 치환한다.

배경
----
TTS 엔진이 "9,115" "PER" "V자" 같은 표기를 자주 잘못 읽는다. 대본에는
처음부터 한글 발음으로 쓰는 게 원칙이고, 이 스크립트는 이미 숫자로
쓰인 기존 대본을 사후 교정할 때 쓴다.

★ 반드시 API 경유 ★
씬에디터 백엔드(uvicorn)가 프로젝트 state를 메모리에 캐시한다.
data/{project}/state.json 을 직접 수정하면 서버는 그 변경을 모르고,
다음 저장 때 자기 메모리 사본으로 통째로 덮어써서 편집이 전부 날아간다.
(2026-08-15 fundedu-regime-vshape에서 실제 발생)
그래서 이 스크립트는 PATCH /api/projects/{id}/scenes/{scene_id} 만 쓴다.

사용법
------
    python3 scripts/tts_korean_numerals.py <project_id> [--dry-run]

    # 백엔드가 떠 있어야 한다 (uvicorn web.backend.main:app --port 8000)

수사 계열 원칙
--------------
  개수·배수 → 고유어  : 15개=열다섯 개, 12배=열두 배
  기간·연도 → 한자어  : 34년=삼십사년, 1989년=천구백팔십구년
  월        → 관용    : 6월=유월, 10월=시월
  퍼센트    → 한자어  : 31퍼센트=삼십일 퍼센트
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

BASE = 'http://localhost:8000/api/projects'

# ── 범용 규칙 ────────────────────────────────────────────────────
# 어느 대본에나 적용되는 약어·기호. 긴 것 먼저(부분매칭 방지).
GLOBAL_RULES: list[tuple[str, str]] = [
    # 증권·경제 약어
    ('S&P500', '에스앤피 오백'),
    ('S&P', '에스앤피'),
    ('SK하이닉스', '에스케이하이닉스'),
    ('KOSPI', '코스피'),
    ('ETF', '이티에프'),
    ('GDP', '지디피'),
    ('PER', '피이알'),      # 피이알 (퍼이알 아님 — 2026-08-16 확정)
    ('PBR', '피비알'),
    ('ROE', '알오이'),
    ('EPS', '이피에스'),
    ('PEG', '피이지'),
    ('CEO', '씨이오'),
    ('IPO', '아이피오'),
    ('AI', '에이아이'),
    # 차트 모양
    ('V자', '브이자'),
    ('L자', '엘자'),
    ('U자', '유자'),
    ('W자', '더블유자'),
    # 월 — 유월·시월은 관용 발음
    ('6월', '유월'),
    ('10월', '시월'),
]

# ── 프로젝트별 규칙 ──────────────────────────────────────────────
# 문맥에 따라 고유어/한자어가 갈리므로 자동 변환하지 않고 명시한다.
PROJECT_RULES: dict[str, list[tuple[str, str]]] = {
    'fundedu-regime-vshape': [
        ('V가 L로', '브이가 엘로'),
        # 연도
        ('1929년', '천구백이십구년'), ('1989년', '천구백팔십구년'),
        ('2007년', '이천칠년'), ('2015년', '이천십오년'),
        ('2024년', '이천이십사년'), ('중국 2015랑', '중국 이천십오년이랑'),
        # 퍼센트
        ('150퍼센트', '백오십 퍼센트'), ('221퍼센트', '이백이십일 퍼센트'),
        ('89퍼센트', '팔십구 퍼센트'), ('70퍼센트', '칠십 퍼센트'),
        ('60퍼센트', '육십 퍼센트'), ('50퍼센트', '오십 퍼센트'),
        ('34퍼센트', '삼십사 퍼센트'), ('31퍼센트', '삼십일 퍼센트'),
        ('12퍼센트', '십이 퍼센트'), ('10퍼센트', '십 퍼센트'),
        # 금액·지수
        ('38조', '삼십팔조'), ('9,115', '구천백십오'),
        # 기간 — 한자어
        ('100년치', '백년치'), ('34년', '삼십사년'), ('30년', '삼십년'),
        ('19년째', '십구년째'), ('10년', '십년'), ('7년', '칠년'),
        ('5개월', '오개월'), ('5년에', '오년에'), ('4년에', '사년에'),
        ('1년에', '일년에'), ('2차', '이차'),
        # 개수·배수 — 고유어
        ('15개', '열다섯 개'), ('7개', '일곱 개'), ('5개', '다섯 개'),
        ('3개', '세 개'), ('19배', '열아홉 배'), ('12배', '열두 배'),
        ('3배', '세 배'), ('6에서 7배', '육에서 칠배'),
    ],
}


def convert(text: str, rules: list[tuple[str, str]]) -> str:
    for old, new in rules:
        text = text.replace(old, new)
    return text


def fetch(url: str):
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def patch_scene(pid: str, scene_id: int, payload: dict) -> None:
    req = urllib.request.Request(
        f'{BASE}/{pid}/scenes/{scene_id}',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='PATCH')
    with urllib.request.urlopen(req) as r:
        if r.status != 200:
            raise RuntimeError(f'PATCH 실패 scene_id={scene_id}: {r.status}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('project_id')
    ap.add_argument('--dry-run', action='store_true',
                    help='치환 결과만 출력하고 서버에 쓰지 않는다')
    args = ap.parse_args()
    pid = args.project_id

    rules = GLOBAL_RULES + PROJECT_RULES.get(pid, [])

    try:
        proj = fetch(f'{BASE}/{pid}')
    except urllib.error.URLError as e:
        print(f'백엔드에 연결할 수 없습니다 ({BASE}): {e}', file=sys.stderr)
        print('uvicorn web.backend.main:app --port 8000 이 떠 있는지 확인하세요.',
              file=sys.stderr)
        return 2

    changed = 0
    for sc in proj['scenes']:
        old = sc.get('narration_text') or ''
        if not old:
            continue
        new = convert(old, rules)
        if new == old:
            continue
        sid = sc['scene_id']
        if args.dry_run:
            print(f'[dry-run] scene_id={sid} ({sc["title"]})\n  {new}\n')
        else:
            patch_scene(pid, sid, {'narration_text': new})
            print(f'PATCH scene_id={sid} ({sc["title"]}) OK')
        changed += 1

    print(f'\n=== {changed}개 씬 {"치환 예정" if args.dry_run else "갱신"} ===')
    if args.dry_run:
        return 0

    # 서버에서 다시 읽어 검증 — 메모리에 실제로 반영됐는지 확인
    leftover = 0
    for sc in fetch(f'{BASE}/{pid}')['scenes']:
        t = sc.get('narration_text') or ''
        if not t:
            continue
        found = re.findall(r'[0-9A-Za-z&]+', t)
        if found:
            print(f'!! 잔여 scene_id={sc["scene_id"]}: {found}')
            leftover += 1
    if leftover:
        print(f'검증 실패: {leftover}개 씬에 숫자·영문이 남았습니다. '
              f'PROJECT_RULES에 규칙을 추가하세요.')
        return 1
    print('검증 통과: 숫자·영문 잔여 없음')
    return 0


if __name__ == '__main__':
    sys.exit(main())
