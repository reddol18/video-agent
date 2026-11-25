# 클라우드 서비스 계정 및 API 키 설정 가이드

> 작성일: 2026-03-12

이 프로젝트를 실행하려면 두 가지 클라우드 서비스의 API 접근 권한이 필요합니다.

---

## 필요한 서비스 요약

| 서비스 | 용도 | 과금 방식 |
|--------|------|-----------|
| **Google AI Studio (Gemini API)** | 영상 분석, 시나리오 파싱, 클립 매칭 | 무료 티어 있음 (15 RPM) |
| **Google Cloud Text-to-Speech** | 나레이션 음성 생성 (Chirp HD) | 무료 크레딧 후 과금 |

---

## 1. Gemini API 키

### 용도
- Phase 1: 시나리오 텍스트 → 씬 분해
- Phase 2: 재료 영상 청크 분석 (설명, 키워드, 분위기 추출)
- Phase 3: 씬과 클립 매칭 판단
- Phase 7: 자연어 편집 요청 해석

### 발급 방법

1. **Google AI Studio** 접속
   - https://aistudio.google.com 로 이동
   - Google 계정으로 로그인

2. **API 키 생성**
   - 왼쪽 메뉴에서 `Get API key` 클릭
   - `Create API key` 버튼 클릭
   - 새 프로젝트 생성 또는 기존 프로젝트 선택
   - 생성된 키 복사 (다시 볼 수 없으니 즉시 저장)

3. **.env 파일에 입력**
   ```
   GOOGLE_API_KEY=여기에_복사한_키_붙여넣기
   ```
   > `google-generativeai` SDK는 `GOOGLE_API_KEY`를 기본값으로 읽습니다. `GEMINI_API_KEY`로 설정하면 SDK가 키를 인식하지 못하니 반드시 `GOOGLE_API_KEY`를 사용하세요.

### 무료 티어 한계

| 항목 | 무료 한도 |
|------|-----------|
| 분당 요청 수 (RPM) | 15회 |
| 일일 요청 수 | 1,500회 |
| 컨텍스트 길이 | 100만 토큰 |

> **참고:** 재료 영상이 많을수록 분석 시간이 길어집니다. 10분짜리 영상 1개 기준으로 약 60개 청크가 생성되며, 무료 티어에서는 분석에 약 15~20분 소요됩니다.

---

## 2. Google Cloud Text-to-Speech (Chirp HD)

### 용도
- Phase 5: 씬별 나레이션 텍스트 → 음성 파일(WAV) 생성
- 사용 음성: `ko-KR-Chirp-HD-D` (한국어, 남성, 자연스러운 억양)

### 발급 방법

#### 2-1. Google Cloud 프로젝트 설정

1. **Google Cloud Console** 접속
   - https://console.cloud.google.com 로 이동
   - Google 계정으로 로그인

2. **새 프로젝트 생성** (또는 기존 프로젝트 사용)
   - 상단 프로젝트 선택 드롭다운 클릭
   - `새 프로젝트` 클릭
   - 프로젝트 이름 입력 (예: `video-agent`)
   - `만들기` 클릭

3. **Cloud Text-to-Speech API 활성화**
   - 상단 검색창에 `Cloud Text-to-Speech API` 검색
   - `사용 설정` 버튼 클릭

#### 2-2. 서비스 계정 키 생성

1. **IAM 및 관리자** → **서비스 계정** 이동
2. `서비스 계정 만들기` 클릭
3. 이름 입력 (예: `video-agent-tts`)
4. 역할 선택: `Cloud Text-to-Speech User` (최소 권한 원칙, 권장)
   > ⚠️ `Cloud Text-to-Speech 서비스 에이전트`는 Google 내부 시스템용이라 외부 앱에서 호출 시 권한 오류(403)가 발생합니다. 반드시 `Cloud Text-to-Speech User`를 선택하세요.
5. `완료` 클릭
6. 생성된 서비스 계정 클릭 → `키` 탭 → `키 추가` → `JSON` 선택
7. 다운로드된 JSON 파일을 프로젝트 루트에 저장

   ```
   video-agent/
   └── gcp-credentials.json    ← 이 파일 (gitignore에 추가 필수!)
   ```

8. **.env 파일에 경로 입력**
   ```
   GOOGLE_APPLICATION_CREDENTIALS=./gcp-credentials.json
   ```

### 무료 크레딧 및 과금

| 항목 | 내용 |
|------|------|
| 신규 가입 크레딧 | $300 (90일 유효) |
| Chirp HD 요금 | 100만 자 당 약 $16 |
| Chirp (기본) 요금 | 100만 자 당 약 $8 |
| 나레이션 예상 사용량 | 5분짜리 영상 기준 약 1,000~2,000자 |

> **참고:** 5분짜리 완성본 기준 Chirp HD TTS 비용은 약 $0.02~0.04 수준입니다. 무료 크레딧 내에서 수백 편 제작이 가능합니다.

---

## 3. .env 파일 최종 형태

프로젝트 루트에 `.env` 파일을 만들고 아래 내용을 채워주세요.

```bash
# ─── Gemini API ───────────────────────────────────────────
GOOGLE_API_KEY=AIza...여기에_Gemini_API_키

# ─── Google Cloud TTS ─────────────────────────────────────
GOOGLE_APPLICATION_CREDENTIALS=./gcp-credentials.json

# ─── Gemini 호출 설정 ─────────────────────────────────────
# 무료 티어: 4 / 유료 티어: 1 또는 0
GEMINI_REQUEST_INTERVAL_SEC=4
GEMINI_MAX_RETRY=3

# ─── TTS 설정 ──────────────────────────────────────────────
TTS_VOICE_NAME=ko-KR-Chirp-HD-D
TTS_SPEAKING_RATE=1.0

# ─── 파이프라인 설정 ───────────────────────────────────────
CHUNK_DURATION_SEC=10
CHUNK_OVERLAP_SEC=2
MIN_QUALITY_SCORE=0.5
```

---

## 4. .gitignore 필수 항목

아래 항목이 `.gitignore`에 반드시 포함되어야 합니다. API 키가 외부에 유출되면 즉시 악용될 수 있습니다.

```gitignore
.env
gcp-credentials.json
*.json.bak.*
output/
cache/
footage/
```

> `cache/` 전체를 제외하는 것은 1인 개발 기준 결정사항입니다. 협업이 생기면 `cache/footage_index.json` 공유 여부를 재검토하세요.

---

## 5. 준비 완료 체크리스트

```
□ Google 계정이 있다
□ Google AI Studio에서 Gemini API 키를 발급받았다
□ Google Cloud 프로젝트를 만들었다
□ Cloud Text-to-Speech API를 활성화했다
□ 서비스 계정 JSON 키를 다운로드했다
□ .env 파일에 두 키를 모두 입력했다
□ .gitignore에 .env와 gcp-credentials.json을 추가했다
□ footage/ 폴더에 재료 영상을 넣었다
□ 시나리오 텍스트 파일(.txt)을 준비했다
```

모든 항목이 체크되면 `python src/main.py --scenario 시나리오.txt --footage footage/` 로 시작할 수 있습니다.
