# video-agent 클라우드 비용 분석

> 조사 기준일: 2026-05-27 / 환율은 USD 기준 표기.
> 본 문서는 영상 1개당 평균 비용 산정 → 월 N건 운영 시 총비용 추정 → 추천 프로바이더 조합 결정을 위한 근거 자료입니다.
> 단가는 변동되므로 `src/cost_estimator.py`의 `pricing.json`을 통해 코드 수정 없이 갱신할 수 있습니다.

---

## 1. 워크로드 특성

| 항목 | 가정값 | 비고 |
|---|---|---|
| 영상 1편 길이 | 5분 (300초) | 김감독 평균 케이스 |
| 씬 개수 | 30개 | 줄 단위 분리 가정 |
| 나레이션 글자수 | 1,500자 | 씬당 50자 × 30 |
| 시나리오 텍스트 | 2KB | Gemini input |
| Footage 입력 | 1.5GB (mp4 다수) | Phase 2 인덱싱 대상 |
| 최종 mp4 출력 | 200MB | Phase 6 결과 |
| 동시 사용자 | 10~100 | 초기 가정 |
| 처리 패턴 | burst (수십 초~수 분 CPU 90%+) | 평소 idle |

---

## 2. 단가표 (2026-05 기준)

### 2.1 Compute

| Provider | Instance | vCPU | RAM | $/시간 (On-demand) | $/시간 (Spot/Reserved) | $/월 (730h) |
|---|---|---|---|---|---|---|
| **Hetzner** | **CCX23** | **4 (전용)** | **16GB** | **$0.0363** | n/a | **$26.5** ✅ |
| Hetzner | CCX13 | 2 (전용) | 8GB | $0.0231 | n/a | $13.5 |
| Hetzner | CCX33 | 8 (전용) | 32GB | $0.0719 | n/a | $52.5 |
| AWS | t4g.medium (ARM) | 2 | 4GB | $0.0336 | ~$0.010 | $24.5 |
| AWS | c7g.large (ARM) | 2 | 4GB | $0.0725 | ~$0.022 | $52.9 |
| AWS | c7g.xlarge (ARM) | 4 | 8GB | $0.145 | ~$0.044 | $105.9 |
| GCP | e2-medium | 2 (공유) | 4GB | $0.0335 | ~$0.010 | $24.5 |
| GCP | c4a-standard-2 (Axion ARM) | 2 | 8GB | ~$0.083 | ~$0.029 | ~$60 |
| Azure | D2as_v5 | 2 | 8GB | $0.086 | ~$0.025 | $62.8 |
| Vultr | HF 2vCPU/4GB | 2 | 4GB | $0.036 | n/a | $24 |
| Fly.io | shared-cpu-2x (2GB) | 2 (공유) | 2GB | $0.0191 | -40% | $13.94 |

### 2.2 Object Storage + Egress

| Provider | Storage $/GB·월 | Egress $/GB | 무료 egress | 비고 |
|---|---|---|---|---|
| **Cloudflare R2** | **$0.015** | **$0** | **무제한** ✅ | egress 0원이 결정적 |
| Wasabi | $0.0069 | $0 | 사용량 ≤ 저장량 조건 | 최저 스토리지 |
| Backblaze B2 | $0.006 | $0.01 (저장량×3 초과) | Cloudflare 경유 시 전량 무료 | B2+CF 조합 강력 |
| AWS S3 Standard | $0.023 | $0.09 (10TB까지) | 첫 100GB/월 | CloudFront로 일부 절감 |
| GCP Cloud Storage | $0.020 | $0.12 (1TB까지) | 첫 100GB/월 | egress 최고가 |
| Azure Blob Hot | $0.018 | ~$0.0875 | 첫 100GB/월 | - |

### 2.3 외부 API

| API | 단위 | 단가 | 무료 할당 |
|---|---|---|---|
| **Google TTS Chirp 3 HD** | 1M chars | **$30** | 월 1M chars 무료 |
| Google TTS Standard | 1M chars | $4 | 월 4M chars 무료 |
| Google TTS Studio | 1M chars | $160 | - |
| **Gemini 2.5 Flash (input)** | 1M tokens | **$0.30** | - |
| **Gemini 2.5 Flash (output)** | 1M tokens | **$2.50** | - |
| Gemini 2.5 Flash Batch | input/output | $0.15 / $1.25 | 50% 할인 |

---

## 3. Phase별 비용 모델

### 영상 1편(5분, 30씬, footage 1.5GB) 기준 — Hetzner CCX23 + R2 가정

| Phase | 외부 API 호출 | CPU 시간 | API 비용 | Compute 비용 | 누적 |
|---|---|---|---|---|---|
| 1. 시나리오 파서 | Gemini 1회 (in 2k + out 3k tokens) | ~1s | $0.0083 | $0.00001 | $0.008 |
| 2. footage 인덱서 | Gemini × 청크수 N (in 8k + out 5k) | ffmpeg trim ~10s × N | $0.013 × N | $0.0001 × N | 청크 10개면 $0.13 |
| 3. 매칭 | Gemini 1회 (in 10k + out 2k) | ~1s | $0.008 | $0.00001 | +$0.008 |
| 4. 나레이션 (TTS) | Chirp HD 1500 chars | ~5s | $0.045 | $0.00005 | +$0.045 |
| 5. 클립 편집 | - | ~120s (5분 영상 × 0.4) | - | $0.0012 | +$0.0012 |
| 6. 최종 합성 | - | ~60s | - | $0.0006 | +$0.0006 |
| Egress (R2) | - | - | $0 (R2) | - | +$0 |
| **합계** | | | **~$0.21** | **~$0.001** | **약 $0.21/편** |

> ⚠ Phase 2 비용은 footage 청크 수에 비례. 영상이 길고 인덱싱 청크가 많을수록 증가.
> ⚠ Phase 5·6 CPU 시간은 사용 효과(블러/모자이크/오버레이)에 따라 2~5배 변동.

### 영상 1편이 S3+CloudFront로 다운로드되는 경우(같은 영상 5번 다운로드 가정)

| 항목 | R2 | S3 (CF 첫 1TB 무료 후) |
|---|---|---|
| 출력 200MB × 5회 | $0 | $0.085 (CF), 첫 1TB 이후 |
| 저장 30일 | $0.003 | $0.0046 |

---

## 4. 월 비용 시나리오

영상 100건/월, 5분 평균, footage 100GB 저장 가정.

| 항목 | Hetzner CCX23 + R2 | AWS c7g.large + S3 + CloudFront | GCP c4a-2 + GCS + CDN |
|---|---|---|---|
| Compute (730h) | $26.5 | $52.9 (또는 Savings -30%: $37) | ~$60 |
| Object Storage 100GB | $1.5 | $2.3 | $2.0 |
| Egress (영상 100건 × 200MB × 평균 3회 다운 = 60GB) | $0 | $5.4 (CF 첫 1TB 무료라 사실상 $0) | $7.2 |
| Gemini × 100 | $2.1 | $2.1 | $2.1 |
| TTS × 100 (150k chars/월) | $0 (1M chars 무료 분에 포함) | $0 | $0 |
| **월 합계** | **~$30** ✅ | **~$45 (CF 무료 한도 내)** | **~$71** |

### 4.1 영상 100건 → 1000건/월로 늘었을 때

영상 1000건이면 footage 누적 1TB+, 다운로드 600GB.

| 항목 | Hetzner+R2 | AWS+CF | GCP+CDN |
|---|---|---|---|
| Compute | +1대 추가 = $53 | c7g.xlarge = $74 | c4a-4 = $121 |
| Storage 1TB | $15 | $23 | $20 |
| Egress 600GB | $0 | $0 (CF 첫 1TB) | $72 |
| TTS 1.5M chars | $15 | $15 | $15 |
| Gemini × 1000 | $21 | $21 | $21 |
| **월 합계** | **~$104** ✅ | **~$133** | **~$249** |

---

## 5. 추천 조합

### 1) **최저가 — Hetzner CCX23 + Cloudflare R2 + Cloudflare CDN: 월 ~$30~110**

**장점**
- 전용 vCPU 보장(c7g보다 컨텍스트 스위칭 없음) → ffmpeg 풀스피드
- R2 egress 0원으로 다운로드 폭증해도 비용 평탄
- DNS·CDN·R2·Workers를 Cloudflare 한 곳에서 관리

**단점**
- us/asia 리전 부재 → 한국 사용자 200ms+ 지연
- 관리형 서비스 부족 — DB·로그·모니터링 직접 구축
- 결제 단위가 1개월 (시간 단위 미세 조절 어려움)

**적합 대상**: 가격 최우선 + EU 리전 허용 + 직접 운영 가능 팀.

### 2) **균형형 — AWS c7g.large(Savings 1y) + S3 + CloudFront: 월 ~$45~135**

**장점**
- ap-northeast-2(서울) 리전 + CloudFront 한국 PoP → 사용자 지연 최저
- Savings Plans 1년 약정으로 30%, Spot으로 60%+ 절감
- IAM·CloudWatch·SQS 등 풍부한 보조 서비스

**단점**
- S3 egress가 CloudFront 캐시 미스 시 누적, 트래픽 폭증에 약함
- 학습곡선 + 콘솔 복잡도
- CloudFront 무료 1TB 초과 시 $0.085/GB

**적합 대상**: 한국 사용자 중심 + 운영 안정성 우선.

### 3) **올인원 GCP — c4a-standard-2 + Cloud Storage + Cloud CDN: 월 ~$71~250**

**장점**
- Gemini·TTS와 같은 GCP 네트워크 → 내부 transfer 무료/저렴
- Vertex AI 통합 시 추후 모델 교체 용이
- ARM Axion으로 가성비 우수(c4a)

**단점**
- Egress $0.12/GB로 3대 중 최고
- 가격 변동 잦음 (2026-05-01 peering 인상)
- 외부 CDN(Cloudflare) 앞단 권장 → 운영 복잡

**적합 대상**: 이미 GCP에 잠겨 있거나 Gemini 호출 비중 매우 높음.

---

## 6. 결정 (초안)

**1차 선택: Hetzner CCX23 + Cloudflare R2 + Cloudflare CDN**

근거:
1. 영상 1편당 비용 **$0.21** — 외부 API가 대부분.
2. Compute 월정액 $26.5로 사용자 100명 burst 워크로드까지 커버.
3. Egress 0원이 영상 다운로드 트래픽 폭증 시 가장 안전.
4. 추후 한국 사용자 비중 증가 시 AWS Seoul 리전 추가 가능 (point-of-presence 모델).

**대안: AWS c7g.large + S3 + CloudFront**

- 한국 사용자 비중 50%+ 되면 전환 고려.
- CloudFront 첫 1TB/월 무료로 초기엔 R2와 비슷한 비용.

---

## 7. cost_estimator 모듈 단가 매핑

`src/cost_estimator/pricing.json`에서 관리:

```json
{
  "provider": "hetzner_r2",
  "compute": {
    "vcpu_hour_usd": 0.00909
  },
  "storage": {
    "per_gb_month_usd": 0.015,
    "egress_per_gb_usd": 0.0
  },
  "api": {
    "gemini_2_5_flash_input_per_1m_tokens": 0.30,
    "gemini_2_5_flash_output_per_1m_tokens": 2.50,
    "tts_chirp_hd_per_1m_chars": 30.0,
    "tts_free_chars_per_month": 1000000
  }
}
```

프로바이더를 바꾸려면 JSON만 교체. 코드는 동일.

---

## 8. 참고 출처

- AWS EC2 Pricing — https://aws.amazon.com/ec2/pricing/on-demand/
- Hetzner Cloud General Purpose — https://www.hetzner.com/cloud/general-purpose
- Cloudflare R2 Pricing — https://leanopstech.com/blog/cloudflare-r2-pricing-2026/
- Google TTS Pricing — https://cloud.google.com/text-to-speech/pricing
- Gemini API Pricing — https://ai.google.dev/gemini-api/docs/pricing
- Vantage instance pricing — https://instances.vantage.sh/
- LeanOps 2026 storage comparison — https://leanopstech.com/blog/cloud-storage-pricing-comparison-2026/
