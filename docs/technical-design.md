# 영상 자동 편집 에이전트 — 기술 설계 문서

> 작성일: 2026-03-12
> 담당: 이팀장 (기술설계 / 리뷰)

---

## 1. 제품 개요

텍스트 시나리오 개요를 입력받아, 사전 제출된 재료 영상(footage)을 자동으로 분석·편집하고, 나레이션과 자막을 부착해 완성본 영상을 출력하는 AI 기반 자동 편집 파이프라인.

### 핵심 요구사항

| # | 요구사항 |
|---|---------|
| 1 | 텍스트 시나리오 개요 → 씬(Scene) 단위 구조화 |
| 2 | 재료 영상을 Gemini 2.5 Flash로 분석하여 씬별 후보 클립 추출 |
| 3 | 씬 순서에 맞게 클립을 이어붙여 무음 편집본 생성 |
| 4 | Google Cloud Chirp TTS로 나레이션 음성 생성 및 합성 |
| 5 | 나레이션 타이밍 기반 SBV 자막 파일 생성 |
| 6 | 영상 + 나레이션 + 자막을 최종 합성하여 완성본 출력 |

---

## 2. 시스템 아키텍처 전체도

```
┌─────────────────────────────────────────────────────────────┐
│                      사용자 인터페이스 (CLI)                   │
│     시나리오 텍스트 파일 경로 + 재료 영상 폴더 경로 입력          │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
          ┌────────────────────────────────┐
          │   Phase 1. 시나리오 파서         │
          │   시나리오 텍스트 → ScenePlan[]  │
          └────────────────┬───────────────┘
                           │ ScenePlan[]
              ┌────────────┴────────────┐
              ▼                         ▼
┌─────────────────────┐   ┌─────────────────────────────────┐
│ Phase 2. 재료 영상   │   │  Phase 3. 클립 매칭              │
│ 인덱서              │──▶│  ScenePlan × FootageIndex        │
│ footage → 청크 분석  │   │  → 씬별 최적 클립 선정           │
│ → FootageIndex      │   └────────────────┬────────────────┘
└─────────────────────┘                    │ ClipSelection[]
                                           ▼
                              ┌────────────────────────┐
                              ▼                          ▼
             ┌──────────────────────┐   ┌──────────────────────────┐
             │  Phase 4. 나레이션    │   │  Phase 4. SBV 자막 생성   │
             │  생성기 (Chirp TTS)   │   │  나레이션 타임스탬프        │
             │  → narration_*.wav   │   │  → final_output.sbv         │
             │  + NarrationTimeline │   └──────────────────────────┘
             └──────────┬───────────┘
                        │
                        ▼
                          ┌─────────────────────────────────┐
                          │  Phase 5. 클립 편집기             │
                          │  ClipSelection[] → 커팅 + concat │
                          │  → muted_output.mp4             │
                          └────────────────┬────────────────┘
                                           │ muted_output.mp4
                        │
                        ▼
             ┌──────────────────────┐
             │  Phase 6. 최종 합성기  │
             │  muted + narration    │
             │  → final_output.mp4  │
             └──────────────────────┘
```

---

## 3. 데이터 스키마 정의

파이프라인 전 단계에 걸쳐 JSON 형식의 중간 산출물이 다음 단계의 입력이 된다.

### 3.1 ScenePlan (시나리오 파서 출력)

```
ScenePlan {
  scene_id          : int          // 씬 순번 (1부터 시작)
  title             : string       // 씬 제목 (짧은 레이블)
  description       : string       // 씬 내용 설명 (시각적 장면 위주)
  mood              : string       // 감정·분위기 키워드 (예: "긴장감, 빠른 전개")
  keywords          : string[]     // 매칭에 사용할 핵심 명사
  duration_hint_sec : int          // 권장 영상 길이 (초)
  narration_text    : string       // 이 씬에 붙을 나레이션 스크립트
}
```

### 3.2 FootageChunk (재료 영상 인덱서 출력)

```
FootageChunk {
  chunk_id      : string    // "{파일명}_{시작초:04d}" 형식
  source_file   : string    // 원본 재료 영상 파일 경로
  start_sec     : float     // 클립 시작 시각 (초)
  end_sec       : float     // 클립 종료 시각 (초)
  description   : string    // Gemini가 생성한 장면 설명
  mood          : string    // 감정·분위기 키워드
  keywords      : string[]  // 장면 핵심 키워드
  quality_score : float     // 0.0~1.0 (화질, 흔들림, 노출 평가)
  has_audio     : bool      // 원본 오디오 포함 여부
}
```

### 3.3 ClipSelection (클립 매칭 출력)

```
ClipSelection {
  scene_id      : int     // 대응하는 ScenePlan.scene_id
  chunk_id      : string  // 선택된 FootageChunk.chunk_id
  trim_start    : float   // 청크 내 실제 사용 시작 시각 (초)
  trim_end      : float   // 청크 내 실제 사용 종료 시각 (초)
  match_score   : float   // 매칭 점수
  match_reason  : string  // Gemini가 설명한 선택 근거
}
```

### 3.4 NarrationEntry (나레이션 생성기 출력)

```
NarrationEntry {
  scene_id       : int     // 대응하는 ScenePlan.scene_id
  narration_text : string  // TTS에 입력한 텍스트
  audio_file     : string  // 생성된 WAV 파일 경로
  duration_sec   : float   // 오디오 길이 (초)
  start_sec      : float   // 최종 영상 기준 나레이션 시작 시각
  end_sec        : float   // 최종 영상 기준 나레이션 종료 시각
}
```

---

## 4. 단계별 상세 설계

---

### Phase 1 — 시나리오 파서

**목적:** 자유 형식의 시나리오 텍스트를 구조화된 ScenePlan 배열로 변환

#### 입출력

- 입력: 시나리오 텍스트 파일 (`.txt`, 자유 형식)
- 출력: `scene_plan.json` (ScenePlan[])

#### 처리 흐름

```
1. 시나리오 텍스트 읽기
2. Gemini 2.5 Flash에 전달
   - 시스템 프롬프트: "영상 편집 어시스턴트. 시나리오를 씬 단위로 분해하라."
   - 사용자 프롬프트: 시나리오 텍스트 전문 + ScenePlan JSON 스키마 + 출력 예시
   - 응답 형식: JSON (response_mime_type: "application/json" 지정)
3. 응답 JSON 파싱 및 유효성 검사
   - 필수 필드 누락 여부 확인
   - duration_hint_sec 최소값(3초) 보정
4. scene_plan.json 저장
```

#### 설계 포인트

- **씬 분해 기준:** Gemini 프롬프트에 "장소 전환, 시간 전환, 감정 전환이 있으면 씬을 나눠라"고 명시
- **나레이션 텍스트 생성:** 씬 description을 바탕으로 Gemini가 자연스러운 나레이션 문장을 직접 작성하도록 프롬프트에 포함. 별도 단계로 분리하지 않음
- **duration_hint_sec 산출 방식:** 나레이션 텍스트의 글자 수 기반으로 Gemini가 추정 (한국어 평균 발화 속도 약 5~6자/초 기준). 추후 실제 TTS 길이로 보정
- **멱등성(Idempotency):** 동일한 시나리오 텍스트로 재실행 시 기존 `scene_plan.json`이 있으면 파싱을 건너뜀 (파일 해시 비교)

#### 유효성 검사 규칙

| 조건 | 처리 |
|------|------|
| scene_id가 연속하지 않음 | 자동 재번호 부여 |
| narration_text가 비어있음 | description을 그대로 사용 |
| duration_hint_sec < 3 | 3초로 강제 보정 |
| keywords가 비어있음 | description에서 명사 추출 재시도 |

---

### Phase 2 — 재료 영상 인덱서

**목적:** 제출된 재료 영상들을 청크로 분할하고 Gemini로 분석하여 검색 가능한 인덱스 생성

#### 입출력

- 입력: `footage/` 폴더 내 모든 영상 파일 (mp4, mov, avi 등)
- 출력: `cache/footage_index.json` (FootageChunk[])

#### 처리 흐름

```
1. footage/ 폴더 스캔 → 영상 파일 목록 수집
2. 파일별:
   a. FFprobe로 영상 메타데이터 추출 (총 길이, 해상도, 프레임레이트, 오디오 유무)
   b. 기존 캐시에 동일 파일이 있으면 건너뜀 (파일명 + 수정일시 기준)
   c. FFmpeg으로 청크 분할 (기본 10초, 오버랩 2초)
   d. 각 청크를 Gemini File API로 업로드
   e. Gemini 분석 요청 → FootageChunk 메타데이터 반환
   f. 분석 완료된 청크 파일 삭제 (임시 파일 정리)
3. 전체 FootageChunk[] → footage_index.json 저장
```

#### 청크 분할 전략

```
영상 길이 T, 청크 길이 C=10초, 오버랩 O=2초 일 때:
  청크 시작 시각: 0, 8, 16, 24, ... (C-O 간격)
  마지막 청크: T-(C-O) 이후 잔여 구간

오버랩을 두는 이유:
  - 씬 경계가 청크 경계와 겹치는 경우 핵심 장면이 잘릴 수 있음
  - 오버랩 구간에서 더 나은 매칭 클립 선정 가능
```

#### Gemini 분석 프롬프트 설계

Gemini에게 각 청크 영상과 함께 아래 정보를 요청:

```
[분석 요청 항목]
- description : 장면을 한국어 2문장으로 묘사. 인물, 배경, 행동을 포함할 것
- mood        : 분위기를 나타내는 형용사 3개 이하 (예: "고요함, 차분함, 새벽")
- keywords    : 검색에 사용할 명사 5개 이하
- quality_score :
    1.0 = 흔들림 없음, 노출 적절, 피사체 명확
    0.5 = 약간의 흔들림 또는 노출 불균형
    0.0 = 심한 흔들림, 블러, 노출 불량
- has_audio   : 의미 있는 현장음 또는 대화 포함 여부 (true/false)
```

#### 캐싱 전략

- `footage_index.json`에 `source_file`, `file_mtime`, `file_size`를 함께 저장
- 재실행 시 파일 수정일시와 크기가 동일하면 해당 파일의 청크는 재분석하지 않음
- 새 파일 추가 시 해당 파일만 증분 인덱싱

#### Gemini 전송 방식: 인라인 우선, File API 폴백

청크 전송 전에 FFmpeg으로 해상도와 비트레이트를 낮춰 20MB 이하로 맞추는 것을 우선한다. File API는 인라인 전송이 불가능한 경우에만 사용한다.

```
전송 방식 결정 흐름:
  1. 청크 영상을 스케일다운 트랜스코딩
       - 해상도: 480p (854×480) 로 축소
       - 영상 비트레이트: 500kbps
       - 오디오: 64kbps mono 유지 (현장음·대화가 장면 이해에 도움)
  2. 트랜스코딩 후 파일 크기 측정
       - 크기 < 20MB → 인라인(base64) 전송
       - 크기 >= 20MB → File API 업로드 후 전송, 분석 완료 후 즉시 삭제
  3. 트랜스코딩된 임시 파일은 전송 직후 삭제
```

**스케일다운 기준 근거:**
- 10초 / 480p / 500kbps 영상 + 64kbps 오디오 기준 예상 크기: 약 0.7MB → 인라인으로 충분
- 30초 장면이어도 약 2.1MB → 인라인 범위 내
- Gemini 영상 분석은 시각적 내용 파악이 목적이므로 480p로도 충분한 품질
- 오디오를 함께 전송하면 현장 대사, 현장음을 시각 정보와 결합해 장면 설명 정확도 향상
- File API는 업로드 지연과 관리 오버헤드가 추가되므로 꼭 필요한 경우에만 사용

#### API 호출 속도 제한 (Rate Limit) 대응

Gemini 무료 쿼터를 초과하지 않도록 청크 분석 요청 사이에 일정 간격을 둔다.

```
호출 간격 전략:
  - 기본 대기: 청크 1개 분석 완료 후 다음 요청 전 N초 대기
  - N 값은 Gemini 무료 티어 RPM(Requests Per Minute) 기준으로 산출
      예) 무료 티어 15 RPM → 60 / 15 = 4초 간격
  - 429 (Too Many Requests) 응답 수신 시:
      지수 백오프(Exponential Backoff) 적용
      1차 재시도: 10초 후, 2차: 20초 후, 3차: 40초 후
      3회 실패 시 해당 청크를 건너뛰고 pipeline_state.json에 실패 기록
  - 설정값(대기 초, 최대 재시도 횟수)은 .env로 외부화하여
    유료 티어 전환 시 코드 수정 없이 조정 가능
```

---

### Phase 3 — 클립 매칭

**목적:** ScenePlan[]과 FootageIndex를 비교하여 씬별 최적 클립을 선정

#### 입출력

- 입력: `scene_plan.json`, `cache/footage_index.json`
- 출력: `cache/clip_selection.json` (ClipSelection[])

#### 매칭 방식: LLM-as-Ranker

단순 키워드 일치 대신, Gemini에게 씬 설명과 footage 후보 목록을 함께 제시하여 직접 랭킹하도록 요청한다.

```
[매칭 프롬프트 구성]
- 씬 설명: ScenePlan.description + mood + keywords
- 후보 클립 목록: FootageChunk.description + mood + keywords + quality_score (상위 N개)
- 요청: "이 씬에 가장 어울리는 클립을 순서대로 3개 골라라. 이유도 함께 설명하라."
```

#### 후보 클립 사전 필터링 (LLM 호출 전)

Gemini 호출 비용 절감을 위해 전체 FootageChunk 중 일부만 후보로 넘긴다.

```
필터 기준:
  1. quality_score >= 0.5 인 청크만 포함
  2. 이미 다른 씬에서 1순위로 선정된 청크는 제외 (중복 사용 방지)
  3. 남은 후보를 ScenePlan.keywords와 단순 문자열 교집합으로 정렬
  4. 상위 20개만 Gemini에 전달
```

#### 씬 길이 조정 로직

선정된 청크의 실제 길이(trim_end - trim_start)와 ScenePlan.duration_hint_sec이 다를 수 있다.

```
조정 우선순위:
  1. 청크 길이 >= duration_hint_sec : 청크 앞부분부터 duration_hint_sec만큼 커팅
  2. 청크 길이 < duration_hint_sec  : 같은 footage에서 인접 청크를 이어붙여 길이 충족
  3. 그래도 부족하면              : 해당 씬의 2순위 후보 클립으로 대체
```

#### 중복 사용 정책

동일 청크를 여러 씬에서 중복 사용하는 것은 기본적으로 허용하지 않는다. 단, footage가 절대적으로 부족한 경우(후보 클립이 씬 수보다 적을 때) 예외적으로 중복 허용하되 `clip_selection.json`에 `is_reused: true` 플래그를 기록한다.

---

### Phase 4 — 나레이션 생성기 & SBV 자막 생성기

두 기능은 같은 타임스탬프 데이터를 공유하므로 하나의 단계로 묶어 처리한다.

**실행 순서 주의:** Phase 4(TTS 먼저) → Phase 5(영상 편집) 순으로 실행. TTS 음성 길이를 먼저 확정해야 Phase 5에서 클립 길이를 TTS에 맞게 조정할 수 있다.

---

### Phase 5 — 클립 편집기

**목적:** ClipSelection[]에 따라 클립을 정밀 커팅하고 하나의 무음 영상으로 연결

#### 입출력

- 입력: `cache/clip_selection.json`, 원본 재료 영상 파일들
- 출력: `output/clips/` (씬별 커팅 클립), `output/muted_output.mp4`

#### 처리 흐름

```
1. ClipSelection[]을 scene_id 순으로 정렬
2. 씬별로:
   a. 원본 영상에서 trim_start ~ trim_end 구간 커팅
   b. 전처리 필터 적용:
      - 해상도 통일: 1920×1080 (letterbox/pillarbox 패딩)
      - 프레임레이트 통일: 30fps
      - 코덱: H.264 (libx264, CRF 23)
      - 오디오 제거 (an 옵션)
   c. output/clips/scene_{scene_id:03d}.mp4 저장
3. 전체 클립을 FFmpeg concat demuxer로 연결
4. output/muted_output.mp4 저장
5. 씬별 영상 시작/종료 타임스탬프를 timeline.json에 기록
```

#### timeline.json 스키마

이후 나레이션 타이밍 배치에 사용된다.

```
SceneTimestamp {
  scene_id   : int    // 씬 번호
  start_sec  : float  // muted_output.mp4 기준 시작 시각
  end_sec    : float  // muted_output.mp4 기준 종료 시각
}
```

#### 전처리 필터 설계 이유

| 문제 | 필터 |
|------|------|
| 해상도 혼재 (세로 영상, 4K 등) | scale + pad 필터로 1920×1080 강제 통일 |
| 프레임레이트 혼재 (24/30/60fps) | fps=30 필터 |
| concat 시 AV sync 어긋남 | 오디오를 완전히 제거하고 나레이션 단계에서 새로 추가 |

#### 트랜지션 (선택 옵션)

- 기본: 트랜지션 없음 (단순 cut)
- 옵션으로 페이드인/아웃 적용 가능 (`fade` 필터, 기본 0.3초)
- 복잡한 크로스디졸브는 1단계에서는 미구현. 추후 Phase 6에서 검토

---

#### 4-A. 나레이션 생성기

**사용 API:** Google Cloud Text-to-Speech — Chirp HD 모델

##### 나레이션 타이밍 배치 전략

각 씬의 나레이션은 해당 씬 영상 구간 내에서 재생되어야 한다.

```
씬 영상 구간: timeline.scene_start_sec ~ timeline.scene_end_sec
나레이션 시작 시각: scene_start_sec + padding (기본 0.5초)
나레이션 종료 예상: narration_start + TTS 오디오 길이

문제 발생 조건: 나레이션 종료 시각 > scene_end_sec
대응:
  1. TTS speaking_rate를 높여 재생성 (1.0 → 1.2 → 1.4 순으로 시도)
  2. 그래도 넘치면 씬 영상 길이를 나레이션 길이에 맞춰 늘림
     (해당 클립의 뒷부분을 더 포함하거나, 동일 씬 2순위 클립 병합)
  3. narration_timeline.json에 실제 start_sec, end_sec, speaking_rate 기록
```

##### TTS 파라미터

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| language_code | ko-KR | 한국어 |
| voice_name | ko-KR-Chirp-HD-D | Chirp HD, 남성 D |
| audio_encoding | LINEAR16 (WAV) | 후처리에 유리한 비압축 포맷 |
| sample_rate_hertz | 24000 | Chirp HD 기본값 |
| speaking_rate | 1.0 | 1.0이 기본, 0.75~2.0 범위 |

> **Chirp HD 선택 이유:** Chirp(기본)에 비해 자연스러운 억양과 감정 표현이 우수하여 나레이션 품질에 직결됨. 비용은 약 2배이나 완성도 우선.

---

#### 5-B. SBV 자막 생성기

**SBV(SubViewer) 포맷 규격:**

```
{H}:{MM}:{SS}.{mmm},{H}:{MM}:{SS}.{mmm}
자막 텍스트

{H}:{MM}:{SS}.{mmm},{H}:{MM}:{SS}.{mmm}
자막 텍스트
```

- 블록 사이는 빈 줄 하나로 구분
- 파일 인코딩: UTF-8

##### 자막 텍스트 분할 전략

나레이션 스크립트가 길 경우 한 블록에 전체를 넣으면 가독성이 떨어진다.

```
분할 기준:
  - 나레이션 텍스트를 문장 부호(., ?, !, —) 기준으로 분리
  - 분리된 문장마다 별도 SBV 블록 생성
  - 각 블록의 start_sec/end_sec은 전체 나레이션 오디오 길이를
    문장 비율(글자 수 기준)로 나누어 배분
  - 글자 수 < 20자인 짧은 문장은 다음 문장과 병합
```

##### SBV 파일 저장 위치

`output/final_output.sbv` — YouTube, Vimeo 등 주요 플랫폼에서 직접 업로드 가능한 형식.

---

### Phase 6 — 최종 합성기

**목적:** 무음 편집본 + 씬별 나레이션 오디오 → 최종 완성본 영상

#### 입출력

- 입력: `output/muted_output.mp4`, `output/narration/narration_timeline.json`, `output/narration/narration_scene_*.wav`
- 출력: `output/final_output.mp4`

#### 처리 흐름

```
1. narration_timeline.json에서 각 씬의 start_sec 읽기
2. FFmpeg filter_complex 구성:
   a. 각 WAV 파일에 adelay 필터로 start_sec 시각에 배치
   b. 전체 나레이션 트랙을 amix로 합산
   c. (선택) 배경음악 WAV가 있으면 낮은 볼륨(0.15)으로 추가 믹싱
3. 영상 트랙(muted_output.mp4)과 합산 오디오 트랙 결합
4. 최종 인코딩:
   - 영상: 스트림 복사 (재인코딩 없음, 속도 최적화)
   - 오디오: AAC, 128kbps
5. output/final_output.mp4 저장
```

#### 오디오 레벨 설계

| 트랙 | 볼륨 |
|------|------|
| 나레이션 | 1.0 (기준) |
| 배경음악 (옵션) | 0.15 (나레이션 방해 최소화) |
| 원본 현장음 | 사용하지 않음 (Phase 4에서 제거) |

#### 출력 품질 설정

| 항목 | 값 |
|------|-----|
| 해상도 | 1920×1080 (muted_output과 동일) |
| 영상 코덱 | 스트림 복사 (H.264) |
| 오디오 코덱 | AAC 128kbps |
| 컨테이너 | MP4 |
| 자막 | 별도 .sbv 파일 (영상에 소프트 자막으로 번들하는 것은 선택사항) |

---

## 5. 파이프라인 상태 관리

각 Phase는 독립적으로 실행 가능해야 한다. 중간 실패 시 전체를 재실행하지 않고 실패 단계부터 재시작할 수 있어야 한다.

### 상태 파일: `cache/pipeline_state.json`

```
PipelineState {
  phase1_done : bool   // scene_plan.json 생성 완료
  phase2_done : bool   // footage_index.json 생성 완료
  phase3_done : bool   // clip_selection.json 생성 완료
  phase4_done : bool   // muted_output.mp4 생성 완료
  phase5_done : bool   // narration + sbv 생성 완료
  phase6_done : bool   // final_output.mp4 생성 완료
  last_updated: string // ISO 8601 타임스탬프
}
```

### 강제 재실행 옵션

CLI 실행 시 `--force-phase 2` 옵션으로 특정 단계부터 강제 재실행 가능.

---

## 6. 디렉토리 구조

```
video-agent/
├── docs/
│   └── technical-design.md          # 본 문서
├── src/
│   ├── main.py                      # CLI 진입점 + 파이프라인 오케스트레이터
│   ├── scenario_parser.py           # Phase 1
│   ├── footage_indexer.py           # Phase 2
│   ├── clip_matcher.py              # Phase 3
│   ├── narration_generator.py       # Phase 4-A
│   ├── sbv_generator.py             # Phase 4-B
│   ├── clip_editor.py               # Phase 5
│   ├── final_composer.py            # Phase 6
│   ├── post_editor.py               # Phase 7 — 자연어 후처리 편집기
│   └── utils/
│       ├── ffmpeg_utils.py          # FFmpeg 래퍼 함수 모음
│       ├── gemini_client.py         # Gemini API 클라이언트 래퍼
│       └── file_cache.py            # 파일 해시/수정일 기반 캐시 유틸
├── footage/                         # 재료 영상 입력 폴더
├── output/
│   ├── clips/                       # 씬별 커팅 클립
│   ├── narration/                   # 씬별 나레이션 WAV
│   ├── muted_output.mp4             # Phase 5 산출물
│   ├── final_output.mp4             # 최종 완성본
│   └── final_output.sbv                # 자막 파일
├── cache/
│   ├── footage_index.json           # Phase 2 캐시
│   ├── clip_selection.json          # Phase 3 산출물
│   ├── scene_plan.json              # Phase 1 산출물
│   ├── timeline.json                # Phase 5 산출물 (씬 타임스탬프)
│   ├── pipeline_state.json          # 파이프라인 진행 상태
│   └── edit_plan.json               # Phase 7 편집 계획서 (최근 1회)
├── output/history/                  # Phase 7 롤백용 이전 산출물 백업 (최근 3회)
├── .env                             # API 키 (gitignore)
└── pyproject.toml
```

---

## 7. 기술 스택

| 영역 | 기술 | 선택 이유 |
|------|------|----------|
| 언어 | Python 3.11+ | Google SDK 공식 지원, 생태계 풍부 |
| 영상 처리 | FFmpeg (CLI) | 산업 표준, 모든 코덱/필터 지원 |
| FFmpeg 래퍼 | ffmpeg-python | 파이썬에서 filter_complex 구성 용이 |
| AI 영상 분석 | Gemini 2.5 Flash API | 멀티모달 영상 이해, File API 지원 |
| TTS | Google Cloud TTS (Chirp HD) | 한국어 품질 최우수, 타임스탬프 배치 가능 |
| 오디오 처리 | pydub | WAV 길이 측정, 오디오 메타데이터 추출 |
| 데이터 형식 | JSON (내부), SBV (자막), MP4 (영상) | |
| 패키지 관리 | uv | 빠른 의존성 설치 |
| 설정 관리 | python-dotenv | .env 기반 API 키 관리 |

---

## 4-7. Phase 7 — 자연어 후처리 편집기

**목적:** 완성본 영상 출력 이후, 자연어 문장으로 편집 수정 요청을 받아 처리

#### 입출력

- 입력: 자연어 편집 요청 문장, 기존 캐시 JSON 파일들 (scene_plan, clip_selection, timeline 등)
- 출력: `EditPlan` (편집 계획서) → 사용자 승인 → 수정된 산출물

---

#### 편집 요청 유형 분류

Gemini가 자연어 요청을 받아 가장 먼저 **편집 유형**을 판단하고, 유형에 따라 처리 경로를 분기한다.

```
편집 유형 A — 구조적 편집
  예) "3번 씬을 1번으로 옮겨줘"
      "2번 씬 나레이션을 '시장의 아침이 열립니다'로 바꿔줘"
      "4번 씬을 삭제해줘"
      "5번 씬 영상을 다른 클립으로 교체해줘"

편집 유형 B — 효과 편집
  예) "전체적으로 색감을 좀 더 따뜻하게 해줘"
      "씬 사이에 페이드 트랜지션 넣어줘"
      "배경음악 볼륨 좀 더 키워줘"
      "자막 글자 크기 키워줘"

편집 유형 AB — 복합
  예) "3번 씬 삭제하고 나머지 씬 사이에 트랜지션 추가해줘"
  → A, B 각각으로 분리하여 순서대로 처리
```

---

#### 처리 흐름

```
1. 자연어 요청 입력
2. Gemini에 요청 전달
   - 컨텍스트: scene_plan.json + clip_selection.json + timeline.json 전문
   - 요청: "편집 유형(A/B)을 분류하고 EditPlan을 생성하라"
3. EditPlan 생성 및 출력 (사람이 읽을 수 있는 형태로 출력)
4. 사용자 승인 요청 (y/n)
   - 거절 시: 재요청 또는 취소
   - 승인 시: 편집 실행
5. 유형별 실행:
   - 유형 A → JSON 패치 → 영향받는 Phase만 재실행
   - 유형 B → FFmpegPatch 적용 → Phase 6 (최종 합성)만 재실행
6. 새 산출물 저장 (기존 파일은 .bak으로 백업)
```

---

#### EditPlan 스키마

```
EditPlan {
  request_summary : string        // Gemini가 해석한 요청 요약 (사람이 검토)
  edit_type       : "A" | "B" | "AB"
  patches         : EditPatch[]   // 유형 A: JSON 패치 목록
  ffmpeg_params   : FFmpegPatch[] // 유형 B: FFmpeg 파라미터 목록
  affected_phases : int[]         // 재실행이 필요한 Phase 번호
  estimated_time  : string        // 예상 처리 시간 (예: "약 30초")
}
```

#### EditPatch 스키마 (유형 A — 구조적 편집)

```
EditPatch {
  target_file : string   // 수정할 JSON 파일 (예: "scene_plan.json")
  operation   : "update" | "delete" | "reorder"
  scene_id    : int      // 대상 씬 번호
  field       : string   // 수정할 필드명 (예: "narration_text")
  new_value   : any      // 새 값 (delete 시 null)
}
```

#### FFmpegPatch 스키마 (유형 B — 효과 편집)

```
FFmpegPatch {
  scope       : "global" | "scene"  // 전체 적용 또는 특정 씬에만
  scene_id    : int | null          // scope가 "scene"일 때만 유효
  filter_type : string              // 효과 유형 (예: "color", "fade", "audio_volume")
  parameters  : object              // FFmpeg 필터 파라미터 (key-value)
  description : string              // 사람이 읽을 수 있는 효과 설명
}
```

**FFmpegPatch 예시:**

| 자연어 요청 | filter_type | parameters |
|------------|-------------|------------|
| "색감을 따뜻하게" | color | `{ "curves": "warm", "eq_gamma": 1.1 }` |
| "씬 사이 페이드" | fade | `{ "duration_sec": 0.5 }` |
| "배경음악 볼륨 키워줘" | audio_volume | `{ "target": "bgm", "volume": 0.3 }` |

---

#### 유형 A 재실행 범위 결정

JSON 패치가 어느 Phase의 입력을 수정하느냐에 따라 재실행 범위가 결정된다.

```
수정 대상 파일          → 재실행 Phase
scene_plan.json        → Phase 3 → 4 → 5 → 6
clip_selection.json    → Phase 4 → 5 → 6
timeline.json          → Phase 5 → 6
narration_timeline.json → Phase 5 → 6 (+ SBV 재생성)
```

씬 삭제/추가처럼 timeline 전체가 바뀌는 경우는 Phase 4부터 전체 재실행.

---

#### 백업 전략

편집 실행 전 영향받는 파일을 자동 백업하여 롤백 가능하게 한다.

```
백업 규칙:
  - 수정되는 JSON 파일: {파일명}.bak.{타임스탬프} 로 저장
  - 재생성되는 영상 파일: output/history/{타임스탬프}/ 폴더로 이동
  - 최근 3회분 백업만 유지 (오래된 것부터 자동 삭제)
  - CLI에서 `--rollback` 옵션으로 직전 상태로 복원 가능
```

---

#### 설계 포인트

- **승인 단계 필수:** Gemini가 요청을 잘못 해석했을 때를 대비해 EditPlan을 반드시 사람이 확인한 후 실행. 자동 실행 모드는 제공하지 않음
- **FFmpegPatch 정합성 검증:** Gemini가 생성한 FFmpeg 파라미터는 실행 전 화이트리스트(허용된 filter_type 목록)와 대조하여 알 수 없는 파라미터는 거부
- **복합 요청 처리 순서:** AB 유형은 항상 구조적 편집(A) 먼저, 효과 편집(B) 나중에 실행. 순서가 바뀌면 타임스탬프가 달라져 효과 적용 위치가 어긋날 수 있음

---

## 8. 개발 로드맵

| Phase | 목표 | 주요 검증 포인트 | 산출물 |
|-------|------|----------------|--------|
| 1 | 시나리오 파서 | ScenePlan JSON이 의미적으로 올바르게 분해되는가 | scene_plan.json |
| 2 | 재료 영상 인덱서 | Gemini 분석 결과의 description/keywords 품질, 캐싱 동작 | footage_index.json |
| 3 | 클립 매칭 | 씬과 클립이 시각적으로 맞는가 (수동 리뷰) | clip_selection.json |
| 4 | 클립 편집기 | muted_output.mp4의 씬 순서, 해상도 통일, 타임스탬프 정확성 | muted_output.mp4, timeline.json |
| 5 | 나레이션 + SBV | 나레이션 타이밍이 씬 구간 내에 들어오는가, SBV 포맷 정합성 | narration_*.wav, final_output.sbv |
| 6 | 최종 합성 + 통합 테스트 | 오디오 싱크, 전체 파이프라인 end-to-end 실행 | final_output.mp4 |
| 7 | 자연어 후처리 편집기 | EditPlan 해석 정확도, 재실행 범위 최소화, 롤백 동작 | post_editor.py, EditPlan JSON |

---

## 9. 리스크 관리

### 9.1 리스크 분류 및 우선순위

리스크를 대응 가능성 기준으로 세 그룹으로 분류하고, 구현 우선순위를 부여한다.

| 우선순위 | 리스크 ID | 리스크 | 분류 |
|---------|----------|--------|------|
| P1 | R6 | FFmpeg 처리 실패 | 기술적 해결 가능 |
| P1 | R4 | 나레이션/영상 길이 불일치 | 기술적 해결 가능 |
| P1 | R8 | FFmpegPatch 안전성 | 기술적 해결 가능 |
| P2 | R5 | Gemini API Rate Limit / 비용 | 해결책 있으나 예산 문제 |
| P2 | R2 | 재료 영상 부족 | 해결책 있으나 예산 문제 |
| P3 | R1 | Gemini 씬 분해 품질 | 해결책을 찾기 어려움 |
| P3 | R3 | 클립 매칭 오판 | 해결책을 찾기 어려움 |
| P3 | R7 | EditPlan 오해석 | 해결책을 찾기 어려움 |

> **우선순위 기준:** P1은 파이프라인 안정성에 직결되므로 초기 개발 단계에서 반드시 구현. P2는 운영 단계 진입 전 대비. P3는 자동화의 구조적 한계이므로 기술이 아닌 운영 프로세스로 대응.

---

### 9.2 P1 — 기술적으로 해결 가능한 리스크

#### R6. FFmpeg 처리 실패

**발생 원인:**
- 재료 영상 파일 손상 또는 지원되지 않는 코덱
- 디스크 공간 부족
- 비정상적인 영상 메타데이터 (길이 0, 프레임레이트 누락 등)

**대응 방안:**
```
1. 사전 검증 단계 추가 (Phase 2 진입 전):
   - FFprobe로 모든 재료 영상의 메타데이터 점검
   - 재생 불가 파일은 목록에서 제외하고 경고 출력
   - 디스크 여유 공간이 재료 영상 총 용량의 2배 미만이면 중단

2. FFmpeg 실행 중 예외 처리:
   - 비정상 종료 시 해당 클립만 건너뛰고 pipeline_state.json에 실패 기록
   - 실패한 씬은 clip_selection.json의 2순위 후보로 자동 대체 시도

3. 중간 산출물 정합성 검증:
   - 생성된 클립 파일의 실제 길이를 FFprobe로 재확인
   - 예상 길이와 1초 이상 차이 나면 재처리
```

#### R4. 나레이션/영상 길이 불일치

**발생 원인:**
- Gemini가 추정한 duration_hint_sec와 실제 TTS 길이 괴리
- 나레이션 스크립트가 예상보다 길거나 짧게 생성된 경우

**대응 방안:**
```
나레이션이 씬 구간을 초과하는 경우 (3단계 순차 시도):
  1. speaking_rate 상향 조정 (1.0 → 1.2 → 1.4)
  2. 씬 클립의 뒷부분을 더 포함하여 영상 길이 연장
  3. 2순위 클립과 병합하여 길이 확보

나레이션이 씬 구간보다 짧은 경우:
  - 나레이션 종료 후 영상은 그대로 재생 (침묵 허용)
  - 침묵 구간이 3초 이상이면 나레이션 끝에 0.5초 fadeout 적용
```

#### R8. FFmpegPatch 안전성

**발생 원인:**
- Gemini가 화이트리스트에 없는 FFmpeg 필터를 생성
- 파라미터 값이 허용 범위를 벗어남 (예: volume=999)

**대응 방안:**
```
실행 전 2단계 검증:
  1. filter_type 화이트리스트 대조
       허용 목록: color, fade, audio_volume, brightness, contrast, saturation
       목록 외 filter_type → 즉시 거부, 사용자에게 사유 출력

  2. 파라미터 값 범위 검증
       audio_volume: 0.0 ~ 1.0
       fade duration_sec: 0.1 ~ 2.0
       brightness/contrast/saturation: -1.0 ~ 1.0
       범위 초과 값 → 경계값으로 클리핑 후 사용자에게 보정 사실 고지
```

---

### 9.3 P2 — 해결책은 있으나 예산이 수반되는 리스크

#### R5. Gemini API Rate Limit / 비용

**상황:**
- 무료 티어(15 RPM)에서는 재료 영상이 많을수록 인덱싱 시간이 수십 분~수 시간으로 늘어남
- Gemini API(영상 분석) + Chirp TTS(나레이션) 양쪽 모두 과금 발생
- 재료 영상 규모가 커질수록 유료 전환이 사실상 불가피

**대응 방안:**
```
무료 티어 내 운영 최적화 (비용 없이 할 수 있는 것):
  - footage_index.json 캐싱으로 재분석 최소화
  - 청크 크기를 키워 청크 수 자체를 줄임 (10초 → 15초, 트레이드오프: 매칭 정밀도 저하)
  - quality_score가 낮은 청크는 Gemini 분석 없이 자동 제외

유료 전환 시 고려사항:
  - Gemini 1.5 Flash → 2.5 Flash로 올리기 전에 비용 대비 품질 비교 테스트 권장
  - Chirp HD 대신 Chirp(기본) 사용 시 비용 절반, 품질 저하 감수
  - 월 예산 상한을 .env에 설정하고 예산 초과 시 파이프라인 자동 중단 기능 추가 검토
```

#### R2. 재료 영상 부족

**상황:**
- 시나리오 씬 수에 비해 footage가 절대적으로 부족한 경우
- 특정 키워드(예: "폭설", "해돋이")에 해당하는 footage가 전혀 없는 경우

**대응 방안:**
```
시스템 측면에서 할 수 있는 것:
  - Phase 3 완료 후 매칭 실패 씬 목록을 "footage 부족 경고"로 출력
  - match_score 임계값(예: 0.4) 미만인 씬은 자동 진행하지 않고 사용자에게 확인 요청
  - 중복 클립 사용을 허용하되 clip_selection.json에 is_reused: true 명시

근본적 해결:
  - 추가 촬영 또는 스톡 영상 구매 — 제작 예산과 일정의 문제
  - 시스템이 부족한 footage 유형을 구체적으로 명시하여 촬영 가이드로 활용
```

---

### 9.4 P3 — 자동화의 구조적 한계, 운영 프로세스로 대응

이 그룹의 리스크는 LLM의 비결정성과 미적 판단의 주관성에서 기인한다. 기술적으로 완전히 해소하기 어려우므로, **파이프라인 안에 사람이 개입하는 검토 포인트를 명시적으로 고정**하는 것이 현실적인 대응이다.

#### R1. Gemini 씬 분해 품질

**구조적 원인:**
- 시나리오가 모호하거나 함축적일수록 LLM 출력이 불안정
- 동일한 시나리오를 실행할 때마다 씬 분해 결과가 달라질 수 있음
- downstream 전체(Phase 3~6)가 scene_plan.json에 종속되어 있어 오류 파급력이 가장 큼

**운영 프로세스 대응:**
```
필수 검토 포인트 — Phase 1 완료 직후:
  - scene_plan.json을 사람이 반드시 검토하고 승인한 후 Phase 2 이후 진행
  - CLI가 자동으로 다음 단계로 넘어가지 않고 "scene_plan.json을 확인하고 계속하려면 y를 입력하세요" 프롬프트 출력
  - 검토 항목 체크리스트 제공:
      □ 씬 수가 시나리오 의도와 맞는가
      □ 각 씬의 description이 시각적으로 구체적인가
      □ narration_text가 자연스러운가
      □ duration_hint_sec 합산이 목표 영상 길이와 맞는가
```

#### R3. 클립 매칭 오판

**구조적 원인:**
- "이 씬에 이 클립이 어울리는가"는 본질적으로 감독의 미적 판단 영역
- Gemini가 장면 설명은 잘 해도, 서사적 맥락과 감정의 적합성까지 판단하기는 어려움
- 자동 매칭 결과가 기술적으로는 정합해 보여도 실제로 보면 어색할 수 있음

**운영 프로세스 대응:**
```
필수 검토 포인트 — Phase 3 완료 직후:
  - clip_selection.json과 함께 씬별 매칭 결과 요약 출력
      씬 1: footage_01.mp4 [00:12 ~ 00:22] — match_score: 0.81
      씬 2: footage_03.mp4 [01:04 ~ 01:14] — match_score: 0.63 ⚠️
  - match_score 0.7 미만인 씬은 ⚠️ 표시로 주의 환기
  - Phase 7 (자연어 후처리 편집기)로 특정 씬 클립 교체를 쉽게 할 수 있도록 연계
  - 장기적으로: 매칭 결과에 대한 사용자 피드백을 누적하여 프롬프트 개선에 활용
```

#### R7. EditPlan 오해석

**구조적 원인:**
- "약간", "좀 더", "자연스럽게" 같은 주관적 표현은 정량화 불가
- 복합 요청일수록 Gemini가 의도를 잘못 분리하거나 순서를 바꿀 수 있음
- 사용자가 원하는 결과를 얻을 때까지 반복 시도가 필요할 수 있음

**운영 프로세스 대응:**
```
설계에 이미 반영된 것:
  - EditPlan의 request_summary 필드: Gemini가 요청을 어떻게 해석했는지 사람이 읽을 수 있게 출력
  - 승인 단계 필수: 자동 실행 없음

추가 대응:
  - 주관적 표현에 대해 Gemini가 "이렇게 해석했습니다. 구체적인 수치는 X로 설정했습니다"라고
    명시하도록 프롬프트 설계
  - 수치가 마음에 들지 않으면 "볼륨을 0.25로 바꿔줘"처럼 구체적으로 재요청하도록
    CLI 안내 메시지 제공
  - 반복 요청 이력을 세션 내에서 유지하여 "아까 그것보다 조금 더"도 맥락을 이어 처리
```
