### 1. 배경 및 목적
- AI가 자동 생성한 영상의 편집점(컷 타이밍), 나레이션 배치를 전문 편집 도구(Final Cut Pro, DaVinci Resolve 등)로 이관하여 미세 조정 및 고도화 편집을 수행하기 위함.
- 프로젝트 전체 타임라인 정보를 표준 XML 형식으로 제공하여 사용자의 수동 편집 경험을 극대화.

### 2. 주요 구현 계획
#### A. FCPXML v1.11 표준 파일 생성
- `cache/clip_selection.json`과 `output/narration/narration_timeline.json`의 타임라인 데이터를 XML로 변환.
- `footage/` 폴더의 원본 영상과 `output/narration/`의 나레이션 오디오를 `<asset>`으로 등록.
- 씬별 제목(`title`)과 설명(`description`)을 마커(Marker)나 키워드로 포함하여 편집 편의성 증대.

#### B. 타임코드 및 좌표 변환
- FFmpeg의 초 단위(seconds)를 FCPXML의 분수형 타임값(e.g., `100/24000s`)으로 변환하는 유틸리티 구현.
- 9:16 숏폼 생성 시 설정된 크롭 정보(Transform)를 XML에 반영하여 편집기에서도 그대로 유지되도록 구현.

### 3. 기술적 변경 사항
- `src/fcpxml_generator.py` 신규 추가: XML 생성 핵심 로직 담당.
- `src/main.py`: 최종 합성(Phase 6) 이후 또는 별도 플래그(`--export-fcp`)를 통해 XML 생성 단계 추가.
- `pyproject.toml`: XML 파싱 및 생성을 위한 `lxml` 등 관련 라이브러리 의존성 확인.

### 4. 예상 워크플로우
- 전체 영상 합성 완료 → FCPXML 자동 생성 → 사용자가 FCPXML 파일을 Final Cut Pro에서 열기 → 원본 소스 연결(Relink) 후 즉시 정밀 편집 시작.
