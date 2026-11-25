# 골든 테스트 가이드 (#305, #318, #319, #320)

> 차원 매트릭스(`aspect × footage_speed × fit × footage_type`) 18개 대표 케이스를 실제
> ffmpeg로 렌더링하여 **첫 프레임 + 중간 프레임** PNG를 사전 승인된 "골든 이미지"와
> 픽셀 단위로 비교한다. 이 테스트의 목적은 fit-vs-speed(2026-05-27 d97386e) 류의
> 회귀 버그가 머지된 뒤에야 발견되는 패턴을 PR 단계에서 차단하는 것이다.

## 어디에 있나

```
tests/golden/
├── _generate_fixtures.py   # 5초 mp4 + PNG 픽스처 생성기 (멱등)
├── conftest.py             # --update-golden 옵션 + 자동 픽스처 생성 + CI 가드(#320)
├── test_render_parity.py   # 18 케이스 매트릭스 테스트 (첫/중간 프레임 비교)
├── fixtures/               # clip_5s.mp4, image_landscape.png (git에 포함)
├── expected/               # 26장의 골든 PNG — clip 18장(t0+tmid), image 8장(t0) (git에 포함)
├── _working/               # 매 실행마다 actual PNG (gitignore)
└── _failures/              # 비교 실패 시 actual/expected/diff (gitignore)
```

> **프레임 추출 정책** (#318)
> - **clip** 케이스: 첫 프레임(`__t0.png`) + 중간 프레임(`__tmid.png`) 2장.
>   중간 프레임은 `duration_sec/2` 시점(출력 타임라인 기준)에서 추출되며,
>   setpts/playback_rate 가 실제로 시간축에 반영되는지를 픽셀로 검증한다.
> - **image** 케이스: 첫 프레임(`__t0.png`) 1장. 정지 소스 + Ken Burns 시작점.

## 실행 방법

### 로컬

```bash
# 모든 골든 테스트 실행 (~3초)
.venv/bin/python -m pytest tests/golden/ -v

# 특정 케이스만
.venv/bin/python -m pytest tests/golden/ -k "9x16__speed0p25__cover" -v
```

### CI

`.github/workflows/test.yml` 에서 PR 마다 자동 실행된다. 실패 시
`tests/golden/_failures/` 가 `golden-failures` 라는 아티팩트로 업로드되어 다운로드 후
시각 검토가 가능하다.

**CI 환경에서의 자동 시드 차단 (#320)**

CI 환경(`CI=true` 환경변수가 설정된 환경, 예: GitHub Actions 기본값)에서는
expected PNG 가 누락된 경우 자동 시드를 **거부**하고 명확한 에러로 실패한다.
이는 다음과 같은 시나리오에서 회귀 가드가 우회되는 것을 방지한다.

- 누군가 실수로 `git rm tests/golden/expected/foo.png` → CI 통과 후 머지
- 새 케이스를 매트릭스에 추가했지만 expected 갱신을 깜빡함 → CI 통과
- 픽스처(`clip_5s.mp4`) 갱신 후 expected 갱신을 깜빡함 → CI 통과

CI 가 expected 누락을 발견하면 다음 에러로 실패한다:

```
[tmid] expected PNG 누락(CI 자동 시드 금지): {경로}
  로컬에서 `pytest tests/golden/ --update-golden` 으로 골든을
  생성하고 시각 확인 후 커밋하세요.
```

로컬 개발 환경(`CI` 미설정 또는 falsy)에서는 기존처럼 자동 시드 + 워닝 동작을
유지하여 새 케이스/픽스처 도입의 마찰을 줄인다.

## 차원 매트릭스

```python
DIMENSIONS = {
    "aspect":        ["16:9", "9:16", "1:1"],
    "footage_speed": [0.25, 1.0, 2.0],
    "fit":           ["contain", "cover", "stretch"],
    "footage_type":  ["clip", "image"],
}
# 전체 54 조합 → 쌍별 직교(pairwise) 18개로 축약
```

18 케이스는 그리디 알고리즘으로 모든 2-way 짝(=45개)을 9 케이스로 커버하고, 나머지 9
케이스로 3-way 트리플의 절반 이상을 추가로 커버한다. 결정론적이라 실행마다 같은
순서·같은 ID로 나온다.

케이스 이름 형식: `{aspect}__speed{N}__{fit}__{type}`
예) `9x16__speed0p25__cover__clip`

`test_case_selection_covers_all_pairs` 메타 테스트가 모든 짝 커버를 보장한다.

## 골든 갱신 흐름

의도된 렌더 변경(예: scale 알고리즘 개선, 새 fit 모드 추가)이 있어 골든 PNG가
바뀌어야 한다면:

```bash
# 1. 변경 사항을 반영한 코드로 골든 PNG를 모두 새로 생성
.venv/bin/python -m pytest tests/golden/ --update-golden

# 2. git diff 로 변경된 expected/*.png 를 시각 확인
#    macOS: open tests/golden/expected/*.png
#    Linux: xdg-open tests/golden/expected/*.png

# 3. 커밋
git add tests/golden/expected/
git commit -m "chore(golden): update expected images for {reason}"
```

**중요**: `--update-golden` 은 검증을 *건너뛴다*. 의도된 변경임을 PR 리뷰어가 시각적으로
확인해야 하며, 실수로 회귀가 골든에 박혀 회귀를 못 잡게 되지 않도록 작성자가
diff 이미지를 PR 설명에 첨부할 것을 권장한다.

## 실패 디버깅

### 실패 메시지 읽기

```
Failed: 골든 비교 실패: case=16x9__speed1p0__cover__clip (1 frame(s))
[tmid] 골든 PNG 불일치: 16x9__speed1p0__cover__clip__tmid.png
  mean_diff_ratio=0.1106 (threshold=0.0200)
  max_diff=255/255  size_match=True
  ssim=0.4521 (source=scikit-image, 통과 판정엔 사용 안 함 — 보조 지표)
  실패 자료: /Users/.../tests/golden/_failures/16x9__speed1p0__cover__clip__tmid/
```

- `mean_diff_ratio`: 픽셀 평균 차이 비율(0~1). 0.02 이하면 통과.
- `max_diff`: 채널 최대 차이(0~255). 노이즈성 차이가 있어도 평균만 낮으면 통과.
- `size_match`: 해상도 일치 여부. False면 fit 매핑이 캔버스 크기를 깬 것.
- `ssim`: 구조적 유사도(0~1). **보조 지표**로 기록되며 통과/실패 판정에는
  사용되지 않는다(#319). 픽셀 평균(`mean_diff_ratio`)은 낮은데 SSIM 이 낮으면
  *구조적 변형*(예: 블러, 안티앨리어싱, 색 시프트) 회귀의 신호일 가능성이 높다.
- `source`: SSIM 산출 방법 — `scikit-image` 가 설치돼 있으면 표준 SSIM,
  미설치 환경에서는 `fallback-mean` 단순 구현이 동작한다.
- 프레임 태그(`[t0]` / `[tmid]`): 어느 프레임에서 깨졌는지를 명시. `t0` 만 깨지면
  fit/scale/crop/pad 관련 회귀, `tmid` 만 깨지면 setpts/playback_rate 회귀일
  가능성이 높다(#318).

### 실패 자료 확인

`_failures/{case_id}/` 폴더에 3종 PNG가 저장된다:

- `actual.png`   — 현재 코드가 생성한 첫 프레임
- `expected.png` — 골든(이전에 승인된 첫 프레임)
- `diff.png`    — 두 이미지 차이를 10배 강조(어두운 영역=같음, 밝은 영역=다름)

### 흔한 실패 패턴

| 패턴 | 원인 | 대응 |
|------|------|------|
| 모든 9:16 케이스가 깨짐 | fit=contain의 자동 회전 휴리스틱이 잘못 발동 | `_needs_rotation` 로직 확인 |
| cover 케이스만 깨짐 | 빌더의 `_compute_fit_geometry` cover 분기 변경 | 변경이 의도된 것이면 골든 갱신 |
| speed=0.25 케이스가 깨짐 | setpts/atempo 매핑이 시간축 외에 공간에도 영향 | fit/speed 분리 회귀 — 즉시 수정 |
| image 케이스가 모두 깨짐 | Ken Burns zoompan 식 변경 | builder의 `zoom_from/zoom_to` 또는 매퍼의 `image_layer_to_kenburns_vf` 확인 |
| clip 케이스 `__tmid` 만 깨짐, `__t0` 통과 | setpts/playback_rate 매핑 회귀 (시간축 처리 누락) | `video_layer_to_vf` 의 `setpts={1/rate}*PTS` 분기 확인(#318) |
| 픽셀 평균은 낮은데 SSIM 만 낮음 | 구조적 회귀(약한 블러, 안티앨리어싱 변경, 색 시프트) | scale/crop 알고리즘 변경 여부 + 인코더 옵션 확인(#319) |

## 픽스처 영상 갱신

`tests/golden/fixtures/clip_5s.mp4` 와 `image_landscape.png` 는 `_generate_fixtures.py`
가 만든다. 픽스처를 갱신하려면:

```bash
# 강제 재생성
.venv/bin/python tests/golden/_generate_fixtures.py --force

# 모든 골든도 같이 갱신 (픽스처 변경 시 expected가 무효해지므로 필수)
.venv/bin/python -m pytest tests/golden/ --update-golden
```

픽스처가 결정론적이라 같은 ffmpeg 버전에서는 같은 픽셀이 나온다. ffmpeg 버전이
크게 바뀌면 골든도 같이 흔들릴 수 있으니 CI 워크플로우에서 사용하는 ffmpeg 버전을
고정하는 것을 권장한다.

## 새 케이스 추가

`tests/golden/test_render_parity.py` 의 `DIMENSIONS` 에 값을 추가하면 자동으로
조합이 늘어난다. 단, `select_pairwise_cases(target=18)` 의 target도 비례해서 늘려야
의미가 있다.

새 차원/값을 추가한 뒤(**로컬에서만** 실행):

1. `.venv/bin/python -m pytest tests/golden/ --update-golden` 으로 새 케이스의 골든을 생성
   - clip 케이스는 `{case_id}__t0.png` + `{case_id}__tmid.png` 두 장 자동 생성(#318).
   - image 케이스는 `{case_id}__t0.png` 한 장 자동 생성.
2. `expected/*.png` 시각 확인
3. 커밋

> **CI 환경에서는 자동 시드가 차단**되므로(#320) expected PNG 를 커밋하지 않으면
> CI 가 실패한다. 새 케이스 추가는 반드시 로컬에서 골든을 생성하고 *함께* 커밋해야 한다.

## 비교 알고리즘 상세

`compare_images` 가 PIL `ImageChops.difference` 로 채널별 절대 차이를 구하고, 모든
픽셀의 채널별 평균을 (R+G+B)/3 으로 합산한 뒤 255로 나누어 0~1 비율로 환산한다.
H.264 양자화로 인한 노이즈성 픽셀 차이는 보통 0.5% 이내라 임계값 2% 안에 안전하게
들어온다. 의도된 작은 변경(예: 안티앨리어싱 개선)도 통과시키려는 의도다.

### SSIM 보조 지표 (#319)

`compare_images` 는 SSIM(Structural Similarity Index) 점수를 함께 산출하여
`CompareResult.ssim` 필드와 실패 메시지에 기록한다. 단, **통과/실패 판정에는
사용하지 않는다** (이슈 본문에 "보조 지표"로 명시).

- **구현 우선순위**: `scikit-image` 의 `structural_similarity` (channel_axis=-1)
  을 우선 사용. 미설치 환경에서는 휘도(Y) 단일 윈도우 기반 폴백 구현으로 전환.
- **CI 환경**: `scikit-image` 가 `pytest` 설치 단계에 포함되어 정확한 SSIM 값을 산출.
- **로컬 환경**: `scikit-image` 가 없어도 폴백 구현이 동작하므로 의존성 추가 부담 없음.
- **의미**: SSIM 은 픽셀 평균 차이로 잡기 어려운 *구조적 회귀*(약한 블러, 미세한
  색 시프트, 안티앨리어싱 변화 등)의 진단 신호로 쓰인다.
  - `mean_diff_ratio` 가 0.02 를 초과해 실패한 경우, SSIM 점수도 함께 보면
    회귀의 성격(픽셀 노이즈 vs 구조적 변형)을 빠르게 식별할 수 있다.
  - 픽셀 평균은 낮은데 SSIM 이 낮은 패턴은 구조적 회귀의 강한 신호.
- **임계값으로 사용하지 않는 이유**: 폴백 구현과 scikit-image 구현이 같은 입력에
  대해 약간 다른 값을 내므로 임계값 비교는 신뢰성이 떨어진다. 환경에 무관한
  *기록* 용도로만 사용하고, 통과 판정은 픽셀 평균 차이로만 유지한다.
