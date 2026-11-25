# 테스트 가이드

> 작성일: 2026-03-12

테스트는 4개 마일스톤으로 나뉩니다. 각 마일스톤은 독립적으로 실행 가능하며, 이전 마일스톤의 산출물을 재사용합니다.

---

## 사전 준비

### 1. uv 설치

`uv`는 이 프로젝트를 실행하는 데 필요한 Python 환경 관리 도구입니다.
터미널을 열고 아래 명령어를 붙여넣어 실행해주세요 (최초 1회).

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

설치가 끝나면 터미널을 **완전히 닫았다가 다시 열어주세요.** (환경변수 반영을 위해 필요합니다.)

### 2. 프로젝트 의존성 설치

터미널에서 프로젝트 폴더로 이동한 뒤 아래 명령어를 실행합니다.

```bash
cd video-agent
uv sync
```

> `uv sync`는 이 프로젝트가 필요로 하는 Python 라이브러리들을 자동으로 설치합니다. 인터넷 연결이 필요합니다.

### 3. .env 파일 확인

`.env` 파일이 이미 만들어져 있습니다. 아래 두 값이 채워져 있는지 확인해주세요.

```
GOOGLE_API_KEY=...          ← Gemini API 키
GOOGLE_APPLICATION_CREDENTIALS=./gcp_key.json   ← GCP 인증 파일 경로
```

> 키 발급 방법은 `docs/credentials-guide.md` 참고

### 4. FFmpeg 확인

```bash
ffmpeg -version
```

명령어를 실행했을 때 버전 정보가 나오면 정상입니다.
아무것도 안 나오거나 오류가 나오면 아래 명령어로 설치하세요.

```bash
brew install ffmpeg
```

> `brew`가 없으면 https://brew.sh 에서 먼저 설치해주세요.

---

## M1 — 시나리오 파싱 테스트

**필요한 것:** Gemini API 키만 있으면 됩니다. 영상 파일 불필요.

**확인 목표:**
- 시나리오 텍스트가 씬 단위로 올바르게 분해되는가
- 나레이션 문장이 자연스러운가
- scene_plan.json이 정상 생성되는가

### 실행

> 앞으로 나오는 `uv run python -m ...` 명령어는 모두 같은 패턴입니다.
> "이 프로젝트 환경으로 Python을 실행해줘" 라는 의미입니다. `uv run` 앞에 항상 붙인다고 기억하면 됩니다.

터미널에서 프로젝트 폴더(`video-agent/`) 안에 있는지 확인한 뒤 실행하세요.

```bash
# 제공된 샘플 시나리오로 테스트
uv run python -m src.main --scenario sample_scenario.txt --footage footage/
```

Phase 1이 끝나면 자동으로 멈추고 확인을 요청합니다.
- `y` → 다음 단계 진행
- `n` → 여기서 중단 (scene_plan.json은 저장된 상태)

### Phase 1만 단독 실행

```bash
uv run python -m src.scenario_parser sample_scenario.txt
```

### 확인할 산출물

```
cache/scene_plan.json
```

에디터에서 열어 아래 항목을 확인하세요:
- `scenes` 배열의 씬 수가 시나리오 내용과 맞는지
- 각 씬의 `description`이 시각적으로 구체적인지
- `narration_text`가 자연스러운 한국어 문장인지
- `duration_hint_sec` 합산이 목표 영상 길이와 맞는지

### 예상 소요 시간

약 15~30초 (Gemini API 1회 호출)

---

## M2 — 영상 인덱싱 + 클립 매칭 테스트

**필요한 것:** M1 완료 + Gemini API 키 + 재료 영상 파일

**확인 목표:**
- 재료 영상이 청크 단위로 분석되는가
- 씬과 클립이 의미적으로 맞게 매칭되는가
- 캐시가 정상 동작하는가 (재실행 시 재분석 생략)

### 재료 영상 준비

```
footage/ 폴더에 영상 파일을 넣어주세요.
지원 형식: mp4, mov, avi, mkv, m4v
```

### 실행

```bash
# M1이 완료된 상태에서 Phase 2부터 재실행
uv run python -m src.main --scenario sample_scenario.txt --footage footage/ --force-phase 2
```

또는 각 Phase 단독 실행:

```bash
# Phase 2만
uv run python -m src.footage_indexer footage/

# Phase 3만
uv run python -m src.clip_matcher
```

### 확인할 산출물

```
cache/footage_index.json   — 청크별 분석 결과
cache/clip_selection.json  — 씬별 선택 클립
```

#### 클립 미리보기 (핵심 기능)

일치도가 낮은 씬(⚠️ 표시)은 반드시 미리보기로 확인하세요.

```bash
# 씬 2번 클립 미리보기
uv run python -m src.main --preview 2
```

영상 플레이어가 자동으로 열립니다.

### 캐시 동작 확인

```bash
# 동일 footage로 재실행 → "캐시 사용" 메시지 출력, 재분석 없음
uv run python -m src.footage_indexer footage/
```

### 예상 소요 시간

- 10분짜리 영상 1개 기준: 약 15~25분 (무료 티어 4초 간격)
- 재실행(캐시 히트): 수 초

---

## M3 — 완성본 영상 생성 테스트

**필요한 것:** M2 완료 + Google Cloud TTS 키

**확인 목표:**
- 나레이션 음성이 자연스러운가
- 나레이션이 씬 타이밍에 맞게 배치되는가
- 자막(SBV) 타이밍이 정확한가
- 최종 영상(오디오+영상 싱크)이 올바른가

### 실행

```bash
# M2가 완료된 상태에서 Phase 4부터 실행
uv run python -m src.main --scenario sample_scenario.txt --footage footage/ --force-phase 4
```

또는 각 Phase 단독 실행:

```bash
# Phase 4-A: 나레이션 생성
uv run python -m src.narration_generator

# Phase 4-B: 자막 생성
uv run python -m src.sbv_generator

# Phase 6: 최종 합성
uv run python -m src.final_composer
```

### 확인할 산출물

```
output/narration/narration_scene_001.wav  — 씬별 나레이션 WAV
output/narration/narration_timeline.json  — 나레이션 타이밍 정보
output/subtitles.sbv                      — 자막 파일
output/final_output.mp4                   — 최종 완성본
```

### 최종 영상 확인 체크리스트

```
□ 영상이 씬 순서대로 재생되는가
□ 나레이션 음성이 자연스러운가
□ 나레이션이 해당 씬 영상 구간 안에서 시작하는가
□ 나레이션과 영상 사이 침묵 구간이 3초 이상 있는 씬은 없는가
□ 자막 타이밍이 나레이션과 맞는가
```

### 예상 소요 시간

- 나레이션 생성: 씬당 약 5~10초
- 최종 합성: 약 30초~1분

---

## M4 — 자연어 편집 테스트 (Phase 7)

**필요한 것:** M3 완료

### 실행

```bash
uv run python -m src.main --edit
```

### 테스트 시나리오 예시

| 입력 | 기대 동작 |
|------|---------|
| `3번 씬 나레이션을 '시장의 아침이 열립니다'로 바꿔줘` | scene_plan.json 수정 → Phase 4~6 재실행 |
| `씬 사이에 페이드 트랜지션 넣어줘` | FFmpegPatch 생성 → Phase 6 재실행 |
| `3번 씬을 삭제해줘` | 씬 삭제 → Phase 5~6 재실행 |
| `배경음악 볼륨을 0.3으로 키워줘` | audio_volume 파라미터 수정 → Phase 6 재실행 |

### 롤백 테스트

```bash
# 편집 후 직전 상태로 되돌리기
uv run python -m src.main --rollback
```

---

## 문제 발생 시

### 자주 발생하는 오류

| 증상 | 원인 | 해결 방법 |
|------|------|---------|
| `GOOGLE_API_KEY 환경변수가 없어요` | .env 파일 누락 또는 키 미입력 | .env 파일 확인 |
| `429 Too Many Requests` | Gemini 무료 티어 한도 초과 | 자동 재시도됨. GEMINI_REQUEST_INTERVAL_SEC 값을 높여볼 것 |
| `ffprobe 실패` | FFmpeg 미설치 | `brew install ffmpeg` |
| `서비스 계정 권한 오류 (403)` | 서비스 계정 역할 오류 | `Cloud Text-to-Speech User` 역할인지 확인 |
| `footage/ 폴더에 영상 파일이 없어요` | footage 폴더 비어있음 | footage/ 폴더에 영상 파일 추가 |

### 특정 Phase만 재실행

```bash
# 3번부터 다시
uv run python -m src.main --scenario sample_scenario.txt --footage footage/ --force-phase 3
```

### 캐시 초기화 후 전체 재실행

```bash
rm -rf cache/ output/
uv run python -m src.main --scenario sample_scenario.txt --footage footage/
```
