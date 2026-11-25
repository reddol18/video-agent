"""모의 클라우드 비용 계산 모듈.

각 Phase의 실제 사용량(시간/바이트/글자/토큰)을 받아 USD 비용을 추정한다.
단가는 `pricing.json`에서 로드 — 코드 수정 없이 프로바이더 변경 가능.

Phase별 누적 비용은 Project.cost_estimate 필드에 저장되고,
프론트엔드 Editor 상단 배지와 렌더 완료 모달에 표시된다.

기본 단가 출처: docs/cost-analysis.md (2026-05-27 기준 Hetzner CCX23 + Cloudflare R2).
"""
from __future__ import annotations
import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


_PRICING_PATH = Path(__file__).parent / "pricing.json"
_CACHED_PRICING: Optional[dict] = None


def load_pricing(force_reload: bool = False) -> dict:
    """단가 JSON을 로드. 첫 호출 시 캐싱."""
    global _CACHED_PRICING
    if _CACHED_PRICING is None or force_reload:
        with open(_PRICING_PATH, "r", encoding="utf-8") as f:
            _CACHED_PRICING = json.load(f)
    return _CACHED_PRICING


# ── #355 모델별 단가 헬퍼 ───────────────────────────────────────────────────

def get_gemini_default_model() -> str:
    """pricing.json 의 default_model 반환 — 모델 미지정 시 폴백용."""
    p = load_pricing()
    g = p.get("api", {}).get("gemini", {})
    return g.get("default_model") or g.get("model") or "gemini-2.5-flash"


def get_gemini_model_pricing(model: Optional[str] = None) -> dict:
    """주어진 model 의 단가 dict 를 반환. 누락 시 default_model, 그래도 없으면 평탄 키 폴백.

    구버전 pricing.json(평탄 키만 있던 시절)을 읽어도 동작하도록 호환을 유지한다.

    반환 dict 키:
      - input_per_1m_tokens_usd
      - output_per_1m_tokens_usd
      - cached_input_per_1m_tokens_usd
      - cache_storage_per_1m_tokens_hour_usd
      - chars_per_token_korean / english (평탄 키)
      - video_tokens_per_sec
    """
    p = load_pricing()
    g = p.get("api", {}).get("gemini", {})
    models = g.get("models") or {}
    default = g.get("default_model") or g.get("model") or "gemini-2.5-flash"
    key = model or default
    entry = models.get(key)
    if entry is None:
        entry = models.get(default) or {}
    # 평탄 키와 합쳐서 한 dict 로 만든다 — chars_per_token_*, video_tokens_per_sec 등은
    # 모델별이 아닌 공용 메타이므로 g 에서 가져온다.
    merged = dict(g)  # 평탄 키 포함
    for k, v in (entry or {}).items():
        merged[k] = v
    return merged


# ── 단위 변환 헬퍼 ──────────────────────────────────────────────────────────

def estimate_korean_tokens(text: str) -> int:
    """한글 텍스트의 Gemini 토큰 수 추정. 평균 1.5 chars/token."""
    if not text:
        return 0
    p = load_pricing()
    chars_per_token = p["api"]["gemini"]["chars_per_token_korean"]
    return math.ceil(len(text) / chars_per_token)


def estimate_english_tokens(text: str) -> int:
    if not text:
        return 0
    p = load_pricing()
    chars_per_token = p["api"]["gemini"]["chars_per_token_english"]
    return math.ceil(len(text) / chars_per_token)


def estimate_mixed_tokens(text: str) -> int:
    """한글/영문 혼용 — 보수적으로 한글 비율 가정 (1.5 chars/token)."""
    return estimate_korean_tokens(text)


# ── Phase별 추정 함수 ───────────────────────────────────────────────────────

def estimate_gemini_text(input_chars: int, output_chars: int, model: Optional[str] = None) -> float:
    """Gemini 텍스트 호출 비용 (USD).

    실제 토큰 수를 모르므로 글자 수로 추정. 한글 비율 가정.
    `model` 미지정 시 pricing.json default_model 단가를 사용한다.
    """
    g = get_gemini_model_pricing(model)
    chars_per_token = g["chars_per_token_korean"]
    in_tokens = input_chars / chars_per_token
    out_tokens = output_chars / chars_per_token
    return (
        in_tokens / 1_000_000 * g["input_per_1m_tokens_usd"]
        + out_tokens / 1_000_000 * g["output_per_1m_tokens_usd"]
    )


def estimate_gemini_video(
    video_duration_sec: float,
    output_chars: int,
    prompt_chars: int = 0,
    model: Optional[str] = None,
) -> float:
    """Gemini 비디오 호출 비용 (USD).

    Google 공식: 비디오는 ~295 tokens/sec.
    `model` 미지정 시 pricing.json default_model 단가를 사용한다.
    """
    g = get_gemini_model_pricing(model)
    video_tokens = video_duration_sec * g["video_tokens_per_sec"]
    prompt_tokens = prompt_chars / g["chars_per_token_korean"]
    in_tokens = video_tokens + prompt_tokens
    out_tokens = output_chars / g["chars_per_token_korean"]
    return (
        in_tokens / 1_000_000 * g["input_per_1m_tokens_usd"]
        + out_tokens / 1_000_000 * g["output_per_1m_tokens_usd"]
    )


def estimate_tts(chars: int, used_free_chars_this_month: int = 0) -> float:
    """Google TTS Chirp HD 비용 (USD).

    월 1M chars 무료 할당 차감 후 청구분만 계산.
    """
    p = load_pricing()
    t = p["api"]["tts"]
    free_remaining = max(0, t["free_chars_per_month"] - used_free_chars_this_month)
    billable = max(0, chars - free_remaining)
    return billable / 1_000_000 * t["per_1m_chars_usd"]


def estimate_typecast_credits(chars: int, used_free_this_month: int = 0) -> dict:
    """Typecast TTS 크레딧/비용 추정.

    1 크레딧 = 1 글자(공백 포함). 월 무료 크레딧(기본 30,000) 차감 후 청구분만 USD 환산.

    Args:
        chars: 합성할 총 글자 수(크레딧 = 글자 수).
        used_free_this_month: 이번 달 이미 소진한 무료 크레딧(차감 기준).

    Returns:
        {
          "credits": 총 소요 크레딧,
          "free_covered": bool (무료 한도 내 전액 커버 여부),
          "billable_credits": 무료 차감 후 청구 크레딧,
          "usd": 청구 크레딧의 USD 환산,
        }
    """
    p = load_pricing()
    t = p.get("api", {}).get("typecast", {})
    credits_per_char = t.get("credits_per_char", 1)
    free_per_month = t.get("free_credits_per_month", 30000)
    usd_per_1k = t.get("usd_per_1k_credits", 0.075)

    credits = max(0, int(chars)) * credits_per_char
    free_remaining = max(0, free_per_month - max(0, used_free_this_month))
    billable = max(0, credits - free_remaining)
    usd = billable / 1000.0 * usd_per_1k
    return {
        "credits": credits,
        "free_covered": billable == 0,
        "billable_credits": billable,
        "usd": usd,
    }


def estimate_compute(cpu_seconds: float) -> float:
    """vCPU·초 단위 컴퓨트 비용 (USD).

    Hetzner CCX23 4vCPU = $26.5/월 = $0.00909/vCPU·시간 = $0.00000253/vCPU·초.
    """
    p = load_pricing()
    c = p["compute"]
    vcpu_hour = cpu_seconds / 3600
    return vcpu_hour * c["vcpu_hour_usd"]


def estimate_storage(bytes_size: int, days: float = 30.0) -> float:
    """오브젝트 스토리지 보관 비용 (USD)."""
    p = load_pricing()
    s = p["storage"]
    gb = bytes_size / (1024 ** 3)
    months = days / 30.0
    return gb * s["per_gb_month_usd"] * months


def estimate_egress(bytes_size: int) -> float:
    """다운로드 egress 비용 (USD).

    Cloudflare R2 기본 $0. 다른 프로바이더는 pricing.json에서 변경.
    """
    p = load_pricing()
    s = p["storage"]
    gb = bytes_size / (1024 ** 3)
    return gb * s["egress_per_gb_usd"]


# ── 데이터 클래스 ───────────────────────────────────────────────────────────

@dataclass
class PhaseCost:
    """단일 Phase의 사용량 + 비용 기록."""
    phase: str
    api_usd: float = 0.0
    compute_usd: float = 0.0
    storage_usd: float = 0.0
    egress_usd: float = 0.0
    # 사용량 (디버그/감사용)
    gemini_input_chars: int = 0
    gemini_output_chars: int = 0
    gemini_video_sec: float = 0.0
    tts_chars: int = 0
    cpu_seconds: float = 0.0
    bytes_stored: int = 0
    bytes_egress: int = 0
    duration_sec: float = 0.0
    # ── #355 — 어떤 Gemini 모델로 발생한 비용인지 표시 ────────────────────
    # 빈 문자열이면 모델 정보가 없는 항목(=Gemini API 와 무관한 phase 또는
    # 구버전 누적 데이터). _add 는 (phase, gemini_model) 조합으로 합산한다.
    gemini_model: str = ""

    @property
    def total_usd(self) -> float:
        return self.api_usd + self.compute_usd + self.storage_usd + self.egress_usd

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_usd"] = self.total_usd
        return d


@dataclass
class CostBreakdown:
    """프로젝트 전체 비용 누적 기록."""
    phases: list[PhaseCost] = field(default_factory=list)
    provider: str = "hetzner_r2"
    currency: str = "USD"
    pricing_updated_at: str = ""

    @property
    def total_usd(self) -> float:
        return sum(p.total_usd for p in self.phases)

    @property
    def api_usd(self) -> float:
        return sum(p.api_usd for p in self.phases)

    @property
    def compute_usd(self) -> float:
        return sum(p.compute_usd for p in self.phases)

    @property
    def storage_usd(self) -> float:
        return sum(p.storage_usd for p in self.phases)

    @property
    def egress_usd(self) -> float:
        return sum(p.egress_usd for p in self.phases)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "currency": self.currency,
            "pricing_updated_at": self.pricing_updated_at,
            "total_usd": self.total_usd,
            "api_usd": self.api_usd,
            "compute_usd": self.compute_usd,
            "storage_usd": self.storage_usd,
            "egress_usd": self.egress_usd,
            "phases": [p.to_dict() for p in self.phases],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CostBreakdown":
        if not data:
            return cls()
        # PhaseCost 가 모르는 키(예: 미래 확장 필드)는 조용히 무시 — 회귀 안전.
        # 구버전 데이터는 gemini_model 키가 없을 수 있는데, dataclass 기본값이 ""로
        # 채워주므로 그대로 통과한다(`getattr(...) or ""` 약속과 일관).
        allowed = set(PhaseCost.__dataclass_fields__.keys())
        phases: list[PhaseCost] = []
        for p in data.get("phases", []):
            kwargs = {k: v for k, v in p.items() if k in allowed}
            phases.append(PhaseCost(**kwargs))
        return cls(
            phases=phases,
            provider=data.get("provider", "hetzner_r2"),
            currency=data.get("currency", "USD"),
            pricing_updated_at=data.get("pricing_updated_at", ""),
        )


# ── 고수준 API: Phase별 한 줄 기록 ──────────────────────────────────────────

class CostEstimator:
    """프로젝트 단위로 Phase별 비용을 누적 기록한다.

    사용 예:
        est = CostEstimator()
        est.record_scenario_parse(scenario_text="...", output_chars=3000, cpu_seconds=1.0)
        est.record_tts(chars=1500, cpu_seconds=5.0)
        ...
        breakdown = est.breakdown
    """

    def __init__(self, existing: Optional[CostBreakdown] = None):
        p = load_pricing()
        self.breakdown = existing or CostBreakdown(
            provider=p.get("provider", "hetzner_r2"),
            currency=p.get("currency", "USD"),
            pricing_updated_at=p.get("updated_at", ""),
        )
        # TTS 월 무료 한도 추적 (한 프로젝트 단위에서는 0으로 간주)
        self._tts_used_this_month = 0

    def _add(self, phase_cost: PhaseCost) -> None:
        # 같은 (phase, gemini_model) 조합이 이미 있으면 누적, 없으면 새 행으로 추가.
        # gemini_model 이 빈 문자열인 항목(Gemini 무관 phase)은 phase 이름만으로 합산되어
        # 기존 데이터(model 필드 없음)와 호환된다.
        target_model = phase_cost.gemini_model or ""
        existing = next(
            (
                p for p in self.breakdown.phases
                if p.phase == phase_cost.phase
                and (getattr(p, "gemini_model", "") or "") == target_model
            ),
            None,
        )
        if existing:
            existing.api_usd += phase_cost.api_usd
            existing.compute_usd += phase_cost.compute_usd
            existing.storage_usd += phase_cost.storage_usd
            existing.egress_usd += phase_cost.egress_usd
            existing.gemini_input_chars += phase_cost.gemini_input_chars
            existing.gemini_output_chars += phase_cost.gemini_output_chars
            existing.gemini_video_sec += phase_cost.gemini_video_sec
            existing.tts_chars += phase_cost.tts_chars
            existing.cpu_seconds += phase_cost.cpu_seconds
            existing.bytes_stored += phase_cost.bytes_stored
            existing.bytes_egress += phase_cost.bytes_egress
            existing.duration_sec += phase_cost.duration_sec
        else:
            self.breakdown.phases.append(phase_cost)

    def record_scenario_parse(
        self, scenario_text: str, output_chars: int, cpu_seconds: float = 1.0
    ) -> PhaseCost:
        api = estimate_gemini_text(len(scenario_text), output_chars)
        compute = estimate_compute(cpu_seconds)
        pc = PhaseCost(
            phase="1_scenario_parse",
            api_usd=api,
            compute_usd=compute,
            gemini_input_chars=len(scenario_text),
            gemini_output_chars=output_chars,
            cpu_seconds=cpu_seconds,
        )
        self._add(pc)
        return pc

    def record_footage_index_chunk(
        self, video_duration_sec: float, output_chars: int,
        prompt_chars: int = 0, cpu_seconds: float = 10.0,
        *, model: Optional[str] = None,
    ) -> PhaseCost:
        """footage 청크 1개 — 여러 번 호출하면 자동 누적.

        `model` 인자(2026-06 추가): 어떤 Gemini 모델로 인덱싱했는지 함께 기록한다.
        None 이면 pricing.json default_model 단가로 폴백. `_add()` 는
        (phase, gemini_model) 조합으로 합산하므로 by_model 분해에 그대로 노출된다.

        파일=청크 정책(2026-06)으로 `video_duration_sec` 는 파일 전체 길이가 되며,
        Gemini 청구는 영상 길이 × 295 tok/sec 기반이므로 호출 횟수만 줄어들고
        비용 단위 계산은 그대로 유효하다.
        """
        api = estimate_gemini_video(video_duration_sec, output_chars, prompt_chars, model=model)
        compute = estimate_compute(cpu_seconds)
        model_label = model or get_gemini_default_model()
        pc = PhaseCost(
            phase="2_footage_index",
            api_usd=api,
            compute_usd=compute,
            gemini_video_sec=video_duration_sec,
            gemini_input_chars=prompt_chars,
            gemini_output_chars=output_chars,
            cpu_seconds=cpu_seconds,
            duration_sec=video_duration_sec,
            gemini_model=model_label,
        )
        self._add(pc)
        return pc

    def record_matching(
        self,
        input_chars: int,
        output_chars: int,
        cpu_seconds: float = 1.0,
        *,
        model: Optional[str] = None,
    ) -> PhaseCost:
        """Phase 3 매칭 비용 기록.

        #355 후속 — `model` 인자로 어떤 Gemini 모델로 호출했는지 함께 기록한다.
        None 이면 pricing.json `default_model` 단가로 폴백한다. 같은 phase 라도
        모델이 다르면 `_add()` 가 분리해서 합산하므로 GET /api/projects/{id}/cost
        의 `by_model` 분해에 모델별 누적이 그대로 노출된다.

        기존 호출부(model 미전달)는 회귀 없이 그대로 동작 — 기본값 None → 평탄 키
        단가 폴백.
        """
        api = estimate_gemini_text(input_chars, output_chars, model=model)
        compute = estimate_compute(cpu_seconds)
        model_label = model or get_gemini_default_model()
        pc = PhaseCost(
            phase="3_matching",
            api_usd=api,
            compute_usd=compute,
            gemini_input_chars=input_chars,
            gemini_output_chars=output_chars,
            cpu_seconds=cpu_seconds,
            gemini_model=model_label,
        )
        self._add(pc)
        return pc

    def record_tts(
        self, chars: int, cpu_seconds: float = 0.0
    ) -> PhaseCost:
        api = estimate_tts(chars, used_free_chars_this_month=self._tts_used_this_month)
        compute = estimate_compute(cpu_seconds)
        self._tts_used_this_month += chars
        pc = PhaseCost(
            phase="4_narration",
            api_usd=api,
            compute_usd=compute,
            tts_chars=chars,
            cpu_seconds=cpu_seconds,
        )
        self._add(pc)
        return pc

    def record_clip_edit(self, cpu_seconds: float, output_bytes: int = 0) -> PhaseCost:
        compute = estimate_compute(cpu_seconds)
        storage = estimate_storage(output_bytes, days=30)
        pc = PhaseCost(
            phase="5_clip_edit",
            compute_usd=compute,
            storage_usd=storage,
            cpu_seconds=cpu_seconds,
            bytes_stored=output_bytes,
        )
        self._add(pc)
        return pc

    def record_final_compose(self, cpu_seconds: float, output_bytes: int = 0) -> PhaseCost:
        compute = estimate_compute(cpu_seconds)
        storage = estimate_storage(output_bytes, days=30)
        pc = PhaseCost(
            phase="6_final_compose",
            compute_usd=compute,
            storage_usd=storage,
            cpu_seconds=cpu_seconds,
            bytes_stored=output_bytes,
        )
        self._add(pc)
        return pc

    def record_nl_edit(
        self,
        input_chars: int = 0,
        output_chars: int = 0,
        cpu_seconds: float = 0.5,
        *,
        cached_input_tokens: int = 0,
        regular_input_tokens: int = 0,
        output_tokens: int = 0,
        cache_storage_tokens: int = 0,
        cache_storage_seconds: float = 0.0,
        video_duration_sec: float = 0.0,
        model: Optional[str] = None,
    ) -> PhaseCost:
        """자연어 편집 Phase 7 — Gemini 2.5 Flash 1회 호출 비용.

        `nl_editor.generate_nl_edit` 호출 후 호출 비용을 누적 기록한다.

        토큰 기반 정확 계산(#317 컨텍스트 캐싱) 우선 — meta 에 토큰 수가 있으면 그쪽을
        사용하고, 글자수(`input_chars`/`output_chars`) 는 호환용으로만 남긴다.

        인자:
        - `input_chars`/`output_chars`: 토큰 카운트가 없을 때의 폴백(기존 호환).
        - `cached_input_tokens`: 컨텍스트 캐시 적용분 입력 토큰. 별도 `7_nl_edit_cached_input`
          Phase 로 기록되어 절감 효과를 시각화한다.
        - `regular_input_tokens`: 일반 입력 토큰. `7_nl_edit` Phase 에 기록.
        - `output_tokens`: 출력 토큰. `7_nl_edit` Phase 에 기록.
        - `cache_storage_tokens`/`cache_storage_seconds`: 캐시 저장 비용 산정용. 0 이면 미기록.

        반환: 일반 호출 Phase (`7_nl_edit`) 의 `PhaseCost`. 캐시 입력/저장은 별도 Phase 로
        자동 누적된다.

        #355 — `model` 인자로 어느 Gemini 모델로 발생한 호출인지 기록한다. None 이면
        pricing.json 의 `default_model` 단가로 폴백한다. 같은 phase 라도 모델이 다르면
        `_add()` 가 분리해서 합산하므로 by_model 집계에 그대로 활용 가능하다.
        """
        # 모델별 단가 — model 미지정이면 default_model 단가.
        g = get_gemini_model_pricing(model)
        model_label = model or get_gemini_default_model()

        has_tokens = (cached_input_tokens or regular_input_tokens or output_tokens) > 0
        if has_tokens:
            # 정확 — 토큰 기반.
            regular_api = (
                regular_input_tokens / 1_000_000 * g["input_per_1m_tokens_usd"]
                + output_tokens / 1_000_000 * g["output_per_1m_tokens_usd"]
            )
            cached_api = (
                cached_input_tokens / 1_000_000 * g.get("cached_input_per_1m_tokens_usd", 0.03)
            )
        else:
            # 폴백 — 글자수. estimate_gemini_text 에도 model 을 그대로 전달.
            regular_api = estimate_gemini_text(input_chars, output_chars, model=model)
            cached_api = 0.0

        # 영상 입력 토큰 — 자동 씬 분할/자동 오버레이 검출 시 비디오를 inline으로 보냄.
        # Google 공식 환산(295 tok/s) 기반으로 입력 토큰 비용을 추가 누적.
        # (이전에는 비디오 토큰이 meta 에서 누락되어 비용이 약 1/12 로 과소 계상됨.)
        if video_duration_sec and video_duration_sec > 0:
            video_tokens = float(video_duration_sec) * g["video_tokens_per_sec"]
            video_api = video_tokens / 1_000_000 * g["input_per_1m_tokens_usd"]
            regular_api += video_api

        compute = estimate_compute(cpu_seconds)
        pc = PhaseCost(
            phase="7_nl_edit",
            api_usd=regular_api,
            compute_usd=compute,
            gemini_input_chars=input_chars,
            gemini_output_chars=output_chars,
            gemini_video_sec=float(video_duration_sec or 0.0),
            cpu_seconds=cpu_seconds,
            gemini_model=model_label,
        )
        self._add(pc)

        # 캐시 적용 입력 — 별도 Phase 로 시각화. 모델별 단가가 다르므로 model 도 함께 기록.
        if cached_api > 0 or cached_input_tokens > 0:
            self._add(PhaseCost(
                phase="7_nl_edit_cached_input",
                api_usd=cached_api,
                gemini_input_chars=0,
                gemini_output_chars=0,
                gemini_model=model_label,
            ))

        # 캐시 저장 비용 — 시간당 단가, 호출 1회의 점유 시간 기반.
        if cache_storage_tokens > 0 and cache_storage_seconds > 0:
            storage_usd = (
                cache_storage_tokens
                / 1_000_000
                * g.get("cache_storage_per_1m_tokens_hour_usd", 1.0)
                * (cache_storage_seconds / 3600.0)
            )
            if storage_usd > 0:
                self._add(PhaseCost(
                    phase="7_nl_edit_cache_storage",
                    api_usd=storage_usd,
                    duration_sec=cache_storage_seconds,
                    gemini_model=model_label,
                ))

        return pc

    def record_intent_classify(
        self,
        input_chars: int = 0,
        output_chars: int = 0,
        cpu_seconds: float = 0.1,
        *,
        cached_input_tokens: int = 0,
        regular_input_tokens: int = 0,
        output_tokens: int = 0,
        model: Optional[str] = None,
    ) -> PhaseCost:
        """자연어 편집 의도 분류(`7_nl_edit_classify`) Phase — LLM 1회 호출 비용.

        의도 분류는 텍스트만 보내므로 매우 저렴(Flash-Lite 기준 약 $0.00002 ≈ 0.03원).
        영상 첨부 호출(split/overlay detector) 과 분리해서 누적해야 절감 효과를 시각화할 수
        있어 별도 phase 로 기록한다.

        토큰 정보가 있으면 정확 계산, 없으면 글자수 폴백.
        """
        g = get_gemini_model_pricing(model)
        model_label = model or get_gemini_default_model()

        has_tokens = (cached_input_tokens or regular_input_tokens or output_tokens) > 0
        if has_tokens:
            api = (
                cached_input_tokens / 1_000_000 * g.get("cached_input_per_1m_tokens_usd", 0.03)
                + regular_input_tokens / 1_000_000 * g["input_per_1m_tokens_usd"]
                + output_tokens / 1_000_000 * g["output_per_1m_tokens_usd"]
            )
        else:
            api = estimate_gemini_text(input_chars, output_chars, model=model)

        compute = estimate_compute(cpu_seconds)
        pc = PhaseCost(
            phase="7_nl_edit_classify",
            api_usd=api,
            compute_usd=compute,
            gemini_input_chars=input_chars,
            gemini_output_chars=output_chars,
            cpu_seconds=cpu_seconds,
            gemini_model=model_label,
        )
        self._add(pc)
        return pc

    def record_egress(self, bytes_size: int) -> PhaseCost:
        egress = estimate_egress(bytes_size)
        pc = PhaseCost(
            phase="7_egress",
            egress_usd=egress,
            bytes_egress=bytes_size,
        )
        self._add(pc)
        return pc

    def record_bgm_render(self, cpu_seconds: float, output_bytes: int = 0) -> PhaseCost:
        """BGM 후처리 합성 — ffmpeg amix + AAC 인코딩."""
        compute = estimate_compute(cpu_seconds)
        storage = estimate_storage(output_bytes, days=30)
        pc = PhaseCost(
            phase="9_bgm_render",
            compute_usd=compute,
            storage_usd=storage,
            cpu_seconds=cpu_seconds,
            bytes_stored=output_bytes,
        )
        self._add(pc)
        return pc

    def record_transcode(self, cpu_seconds: float, output_bytes: int = 0,
                          phase_label: str = "10_transcode") -> PhaseCost:
        """업로드된 파일의 ffmpeg 변환 (footage upload, recording, presentation 등)."""
        compute = estimate_compute(cpu_seconds)
        storage = estimate_storage(output_bytes, days=30)
        pc = PhaseCost(
            phase=phase_label,
            compute_usd=compute,
            storage_usd=storage,
            cpu_seconds=cpu_seconds,
            bytes_stored=output_bytes,
        )
        self._add(pc)
        return pc

    def record_scene_preview(self, cpu_seconds: float, output_bytes: int = 0,
                              cache_hit: bool = False) -> PhaseCost:
        """씬 미리보기 1회 — Editor에서 사용자가 ▶ 갱신을 누를 때마다 호출 가능.

        cache_hit=True면 ffmpeg가 안 돌아 cpu_seconds는 무시. egress(다운로드)만 기록.
        """
        compute = 0.0 if cache_hit else estimate_compute(cpu_seconds)
        egress = estimate_egress(output_bytes)
        pc = PhaseCost(
            phase="8_scene_preview",
            compute_usd=compute,
            egress_usd=egress,
            cpu_seconds=0.0 if cache_hit else cpu_seconds,
            bytes_egress=output_bytes,
        )
        self._add(pc)
        return pc


# ── 사전 추정 (실제 실행 전 예상치) ─────────────────────────────────────────

def preview_video_cost(
    scenes: int,
    avg_narration_chars_per_scene: int,
    avg_output_duration_sec: float,
    footage_total_duration_sec: float = 0.0,
    footage_chunks: int = 1,
    tts_provider: str = "google",
    include_gemini: bool = True,
) -> dict:
    """렌더링 시작 전 영상 1편의 예상 비용을 미리 추정.

    ``include_gemini`` 가 False 면 Gemini 단계(1 시나리오 파싱 · 2 인덱싱 · 3 매칭)를
    더하지 않는다. 이 단계들은 이제 Claude Code 가 편집 코멘트로 대신하므로
    웹 API(cost.py)는 항상 False 로 호출한다.

    각 Phase의 평균값을 가정해서 어림잡는다. 실제 비용은 record_* 호출 결과로 정확해진다.

    ``tts_provider`` 가 ``typecast`` 면 Phase 4 를 Google TTS USD 대신 크레딧 기반으로
    추정하고, 반환 dict 에 ``tts_credits`` / ``tts_credits_usd`` / ``tts_free_covered``
    필드를 추가한다. (Phase 4 의 api_usd 는 typecast usd 로 대체된다.)
    """
    est = CostEstimator()
    if include_gemini:
        # Phase 1 — 시나리오 파싱: 씬당 약 50자, 출력 씬당 200자(JSON)
        scenario_chars = scenes * avg_narration_chars_per_scene
        est.record_scenario_parse(
            scenario_text="x" * scenario_chars,
            output_chars=scenes * 200,
            cpu_seconds=1.0,
        )
        # Phase 2 — footage 인덱싱: 청크당 평균 길이
        if footage_chunks > 0 and footage_total_duration_sec > 0:
            avg_chunk_sec = footage_total_duration_sec / footage_chunks
            for _ in range(footage_chunks):
                est.record_footage_index_chunk(
                    video_duration_sec=avg_chunk_sec,
                    output_chars=500,
                    prompt_chars=1000,
                    cpu_seconds=10.0,
                )
        # Phase 3 — 매칭
        est.record_matching(input_chars=10_000, output_chars=2_000, cpu_seconds=1.0)
    # Phase 4 — TTS
    total_tts_chars = scenes * avg_narration_chars_per_scene
    # Typecast 크레딧 예상은 provider 와 무관하게 항상 계산한다.
    # 사용자가 지금 Google 목소리를 쓰고 있어도 "이 나레이션을 Typecast 로 만들면
    # 몇 크레딧" 을 미리 보고 전환을 판단할 수 있어야 하기 때문(현재 provider 가
    # google 이라고 크레딧 예상을 숨기면 정작 전환 판단에 필요한 정보가 사라진다).
    typecast_info = estimate_typecast_credits(total_tts_chars)
    if tts_provider == "typecast":
        # Google 무료 한도 대신 Typecast 크레딧 기반. Phase 4 의 api_usd 는
        # typecast usd 로 직접 기록한다(compute 는 동일하게 반영).
        est._add(PhaseCost(
            phase="4_narration",
            api_usd=typecast_info["usd"],
            compute_usd=estimate_compute(scenes * 0.5),
            tts_chars=total_tts_chars,
            cpu_seconds=scenes * 0.5,
        ))
    else:
        est.record_tts(chars=total_tts_chars, cpu_seconds=scenes * 0.5)
    # Phase 5 — 클립 편집: 영상 1초당 ~0.4s CPU
    p = load_pricing()
    edit_cpu = avg_output_duration_sec * p["compute"]["ffmpeg_speed_factor"]
    est.record_clip_edit(cpu_seconds=edit_cpu, output_bytes=int(avg_output_duration_sec * 500_000))
    # Phase 6 — 최종 합성: 영상 1초당 ~0.2s CPU
    compose_cpu = avg_output_duration_sec * 0.2
    est.record_final_compose(cpu_seconds=compose_cpu, output_bytes=int(avg_output_duration_sec * 800_000))
    result = est.breakdown.to_dict()
    result["tts_provider"] = tts_provider
    # 크레딧 예상은 provider 무관하게 항상 노출 — 프론트가 "Typecast 로 만들면 N 크레딧"
    # 을 Google 프로젝트에서도 보여줄 수 있게 한다.
    result["tts_credits"] = typecast_info["credits"]
    result["tts_credits_usd"] = typecast_info["usd"]
    result["tts_free_covered"] = typecast_info["free_covered"]
    result["tts_billable_credits"] = typecast_info["billable_credits"]
    return result
