# 영상 자동 편집 에이전트 — 개발 계획서

> 작성일: 2026-03-12
> 담당: 노팀장 (구현)

---

## 1. 프로젝트 이해 요약

- **목적:** 시나리오 텍스트 + 재료 영상 → AI 자동 편집 → 나레이션 + 자막이 붙은 완성본 영상 출력
- **파이프라인:** 7개 Phase 순차 실행, 각 Phase는 독립적으로 재실행 가능
- **핵심 AI:** Gemini 2.5 Flash (영상 분석 + 클립 매칭), Google Cloud Chirp HD TTS (나레이션)
- **최종 사용자:** 김감독 — CLI 비숙련자, 친절한 UI/UX 필수

---

## 2. 디렉토리 구조

```
video-agent/
├── docs/
│   ├── technical-design.md          # 이팀장 기술설계 문서
│   ├── development-plan.md          # 본 문서
│   └── credentials-guide.md        # 클라우드 계정/키 설정 가이드
├── src/
│   ├── main.py                      # CLI 진입점 + 파이프라인 오케스트레이터
│   ├── scenario_parser.py           # Phase 1: 시나리오 파서
│   ├── footage_indexer.py           # Phase 2: 재료 영상 인덱서
│   ├── clip_matcher.py              # Phase 3: 클립 매칭
│   ├── narration_generator.py       # Phase 4-A: 나레이션 생성기
│   ├── sbv_generator.py             # Phase 4-B: SBV 자막 생성기
│   ├── clip_editor.py               # Phase 5: 클립 편집기
│   ├── final_composer.py            # Phase 6: 최종 합성기
│   ├── post_editor.py               # Phase 7: 자연어 후처리 편집기
│   └── utils/
│       ├── ffmpeg_utils.py          # FFmpeg 래퍼 함수 모음
│       ├── gemini_client.py         # Gemini API 클라이언트 래퍼
│       └── file_cache.py            # 파일 해시/수정일 기반 캐시 유틸
├── footage/                         # 재료 영상 입력 폴더
├── output/
│   ├── clips/                       # 씬별 커팅 클립
│   ├── narration/                   # 씬별 나레이션 WAV
│   ├── history/                     # Phase 7 롤백용 이전 산출물 백업 (최근 3회)
│   ├── muted_output.mp4
│   ├── final_output.mp4
│   └── subtitles.sbv
├── cache/
│   ├── scene_plan.json
│   ├── footage_index.json
│   ├── clip_selection.json
│   ├── timeline.json
│   ├── pipeline_state.json
│   └── edit_plan.json
├── .env                             # API 키 (gitignore 필수)
├── .env.example                     # API 키 템플릿 (커밋 가능)
└── pyproject.toml
```

---

## 3. 구현 순서

| 순서 | 작업 | 주요 산출물 | 의존성 |
|------|------|------------|--------|
| 0 | 프로젝트 초기화 | pyproject.toml, .env.example | — |
| 1 | utils 3종 구현 | ffmpeg_utils.py, gemini_client.py, file_cache.py | FFmpeg, Gemini SDK |
| 2 | **Phase 1** 시나리오 파서 | scene_plan.json | gemini_client |
| 3 | **Phase 2** 재료 영상 인덱서 | footage_index.json | ffmpeg_utils, gemini_client, file_cache |
| 4 | **Phase 3** 클립 매칭 | clip_selection.json | gemini_client |
| 5 | **Phase 4** 나레이션 + SBV | narration/*.wav, final_output.sbv | Google Cloud TTS |
| 6 | **Phase 5** 클립 편집기 | muted_output.mp4, timeline.json | ffmpeg_utils |
| 7 | **Phase 6** 최종 합성기 | final_output.mp4 | ffmpeg_utils |
| 8 | **Phase 7** 자연어 후처리 편집기 | post_editor.py | 전 단계 JSON 캐시 |
| 9 | main.py 오케스트레이터 + UX 통합 | 완성본 CLI | 전체 |

---

## 4. 의존성 패키지

```toml
[project]
name = "video-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "google-generativeai",           # Gemini API
    "google-cloud-texttospeech",     # Chirp HD TTS
    "ffmpeg-python",                 # FFmpeg 래퍼
    "pydub",                         # WAV 길이 측정, 오디오 메타데이터
    "rich",                          # 친절한 CLI UX (progress bar, 컬러 출력)
    "python-dotenv",                 # .env 기반 설정 관리
]
```

---

## 5. 김감독을 위한 UX 설계

김감독은 CLI 환경에 익숙하지 않으므로, 모든 출력과 인터랙션에 아래 원칙을 적용한다.

### 5.1 파일 결과물 — 에디터 바로 열기 링크

JSON 파일이 생성될 때마다 Cursor/VS Code에서 바로 열 수 있는 링크를 함께 출력한다.
링크 경로는 실행 시점의 프로젝트 루트를 기준으로 동적 생성한다 (`os.path.abspath()` 사용).

```
✅ scene_plan.json 생성 완료
   📄 에디터에서 열기 → cursor://file/{PROJECT_ROOT}/cache/scene_plan.json
```

### 5.2 영상 미리보기 — 특정 구간 잘라서 플레이어 실행

클립 매칭 결과나 완성본의 특정 구간을 보고 싶다고 하면, FFmpeg으로 해당 구간만 잘라 `/tmp/preview_*.mp4`로 저장 후 macOS `open` 명령으로 기본 영상 플레이어에서 실행한다.

```
🎬 씬 2 미리보기 (footage_03.mp4 [01:04 ~ 01:14])
   잠깐만요, 해당 구간을 잘라내고 있어요...
   ▶ 재생 중: /tmp/preview_scene_002.mp4
```

### 5.3 Phase 완료 — 체크리스트 + 승인 단계

각 Phase 완료 후 사람이 읽을 수 있는 요약과 체크리스트를 출력하고, `y/n` 승인을 받은 후 다음 Phase로 진행한다.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Phase 1 완료 — 시나리오를 5개 씬으로 분해했어요

  씬 1 (8초): 새벽 시장, 상인들이 자리를 펼치는 장면
  씬 2 (12초): 첫 손님과 상인의 대화
  씬 3 (10초): 시장이 활기를 띠는 전경
  씬 4 (7초): 한 할머니가 물건을 고르는 클로즈업
  씬 5 (6초): 해가 뜨며 시장이 환해지는 마무리
  ─────────────────────────────────────
  총 예상 길이: 43초

검토 항목:
  □ 씬 수가 시나리오 의도와 맞나요?
  □ 각 씬 설명이 구체적으로 느껴지나요?
  □ 나레이션 문장이 자연스럽게 읽히나요?
  □ 총 길이가 목표에 맞나요?

  📄 전체 내용 보기 → cursor://file/{PROJECT_ROOT}/cache/scene_plan.json

계속 진행할까요? [y/n]:
```

### 5.4 진행 상황 — 시각적 progress bar

```
Phase 2: 재료 영상 분석 중...
  footage_01.mp4  ████████████████████  100%  (12/12 청크)
  footage_02.mp4  ████████░░░░░░░░░░░░   40%  ( 4/10 청크)  분석 중...
```

### 5.5 오류 출력 — 기술 용어 없이 자연어로

```
❌ 문제가 생겼어요

   footage_02.mp4 파일을 읽을 수 없어요.
   파일이 손상됐거나 지원하지 않는 형식일 수 있어요.

   해결 방법:
   1. footage/ 폴더에 있는 footage_02.mp4 파일을 확인해주세요.
   2. 다른 영상 플레이어에서 정상 재생되는지 확인해주세요.
   3. 문제가 계속되면 파일을 MP4 형식으로 다시 변환해보세요.

   이 파일을 건너뛰고 나머지 파일로만 계속 진행할까요? [y/n]:
```

### 5.6 클립 매칭 결과 — 주의 씬 표시 + 미리보기 링크

```
Phase 3 완료 — 씬별 클립 매칭 결과

  씬 1 ✅  footage_01.mp4  [0:08~0:18]  일치도 92%
  씬 2 ⚠️  footage_03.mp4  [1:04~1:14]  일치도 61%  ← 확인 필요
  씬 3 ✅  footage_02.mp4  [0:24~0:34]  일치도 88%

  ⚠️ 씬 2는 일치도가 낮아요. 어색할 수 있으니 미리 확인해보세요.
     ▶ 씬 2 미리보기 → 'preview 2' 라고 입력하면 바로 재생해드릴게요.
```

### 5.7 자연어 후처리 편집 모드 (Phase 7)

`--edit` 옵션으로 진입하면 대화형 프롬프트가 실행된다. 텍스트 파일이 아닌 터미널 직접 입력 방식이며, 세션 내에서 이전 요청 맥락을 유지한다.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✏️  편집 모드 — 자연어로 수정 요청을 입력하세요
    (종료하려면 q 입력)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

어떻게 수정할까요? → 3번 씬을 삭제하고 씬 사이에 페이드 트랜지션 넣어줘

잠깐만요, 요청을 분석하고 있어요...

─── 편집 계획서 ───────────────────────────
요청 해석: 3번 씬을 삭제하고, 남은 씬 전환마다 0.5초 페이드 트랜지션 적용
편집 유형: AB (구조 변경 + 효과 적용)

변경 내용:
  1. [구조] scene_plan.json에서 씬 3 삭제 → 씬 4, 5 번호 재정렬
  2. [효과] 씬 전환마다 fade 필터 적용 (0.5초)

영향 받는 단계: Phase 5 → 6 재실행
예상 소요 시간: 약 2분
────────────────────────────────────────

이대로 진행할까요? [y/n]: y

  Phase 5 재실행 중... ████████████████████  완료
  Phase 6 재실행 중... ████████████████████  완료

✅ 수정 완료! 결과물: output/final_output.mp4

다른 수정이 필요하면 계속 입력하세요 (종료: q)
어떻게 수정할까요? →
```

---

## 6. 파이프라인 상태 관리

```json
{
  "phase1_done": true,
  "phase2_done": true,
  "phase3_done": false,
  "phase4_done": false,
  "phase5_done": false,
  "phase6_done": false,
  "phase7_done": false,
  "last_updated": "2026-03-12T10:30:00+09:00"
}
```

- 중간 실패 시 완료된 Phase는 건너뛰고 실패 지점부터 재시작
- `--force-phase 2` 옵션으로 특정 단계부터 강제 재실행 가능

---

## 7. CLI 사용법 (최종 형태)

```bash
# 전체 파이프라인 실행
python src/main.py --scenario scenario.txt --footage footage/

# 특정 Phase부터 재실행
python src/main.py --force-phase 3

# 자연어 후처리 편집 모드
python src/main.py --edit

# 직전 상태로 롤백
python src/main.py --rollback

# 특정 씬 미리보기
python src/main.py --preview 2
```
