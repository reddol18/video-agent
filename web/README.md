# video-agent 웹 편집기

## 실행 방법

터미널 두 개를 열어서 각각 실행하세요.

**백엔드 (포트 8000):**
```bash
cd /Users/jkon/Projects/video-agent
uv run uvicorn web.backend.main:app --reload --port 8000
```

**프론트엔드 (포트 5173):**
```bash
cd /Users/jkon/Projects/video-agent/web/frontend
npm run dev
```

브라우저에서 `http://localhost:5173` 접속

## 초기 세팅 흐름

1. 프로젝트 이름 입력
2. footage 폴더 절대 경로 입력
3. 대본 텍스트 입력 (기존 시나리오 파일 내용 그대로 붙여넣기 가능)
4. 매칭 방식 선택:
   - **Gemini · 파일명**: AI가 파일명과 대본 비교 (빠름)
   - **Gemini · 내용 분석**: AI가 영상 내용 분석 (정확, 시간 소요)
   - **키워드 · 파일명**: AI 없이 단어 유사도로 매칭 (무료)
   - **수동 배치**: footage 썸네일을 드래그해서 각 씬에 배치

모든 씬이 매칭되어야 편집 화면으로 진입 가능합니다.

## 편집 화면

- **좌측 미디어 브라우저**: footage 파일 썸네일, 드래그해서 씬 카드에 드롭
- **우측 씬 카드**: 대본 수정 시 TTS 자동 재생성 (0.8초 디바운스)
- **이펙트 뱃지 클릭**: 씬별 이펙트 타입 변경 (clip/kenburns/edit/sync 등)
- **▶ 버튼**: 씬별 TTS 미리 듣기
- **렌더링 버튼**: 기존 파이프라인(Phase 4~6) 실행
