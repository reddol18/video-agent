/**
 * 이어보기(연속 재생) 적응형 프리페치 깊이 산출 — 순수 함수 모듈.
 *
 * 배경:
 *   기존 이어보기는 현재 씬이 `ended` 된 *뒤에야* 다음 씬의 mp4 를 서버에서 받아와서,
 *   씬 전환마다 빈 시간(캐시 적중 ~0.12s, 미스 ~2.3s + blob/디코딩 시간)이 생겼다.
 *
 * 설계 원칙:
 *   고정 개수(1개/3개)를 박지 않고 "현재 프로젝트 상태"로 필요한 깊이를 산출한다.
 *   - 핵심 부등식: **앞으로 재생될 누적 시간 ≥ 그때까지 받아야 할 누적 로딩 시간**.
 *     프리페치는 서버가 1건씩 직렬 처리(ffmpeg)한다고 보수적으로 가정하므로,
 *     N 개를 미리 받으려면 N × estLoadSec 이 필요하고, 그동안 벌 수 있는 시간은
 *     "현재 씬 잔여 재생시간 + 앞선 씬들의 재생시간"이다.
 *   - 재생 길이는 배속(playbackRate)으로 나눠 실제 벽시계 시간(wall-clock)으로 환산한다.
 *     3배속이면 10초 씬도 3.3초만 벌어 주므로 깊이가 더 깊어져야 한다.
 *   - estLoadSec 은 호출자가 실측 이동평균으로 넘긴다(캐시 적중/미스가 20배 차이라
 *     상수로 박으면 두 환경 중 하나는 반드시 틀린다).
 *
 * 상한(`PREFETCH_MAX_DEPTH = 5`) 근거:
 *   응답 1건이 0.4~2.2MB 이므로 5건이면 최대 ~11MB 의 blob 이 메모리에 상주한다.
 *   브라우저 blob 저장 한도 대비 무시할 수준이면서, 최악(미스 2.3s × 5 = 11.5s)의
 *   선행 로딩을 허용해 짧은 씬 연속 구간도 대부분 덮는다. 그 이상은 서버 ffmpeg
 *   큐만 늘리고 사용자가 씬을 점프하면 전부 버려지므로 이득이 급감한다.
 */

/** 프리페치 깊이 상한 (메모리/서버 부하 보호). */
export const PREFETCH_MAX_DEPTH = 5;

/** 로딩 시간 추정치의 하한/상한 — 실측 이동평균이 튀어도 깊이가 폭주하지 않게 클램프. */
export const MIN_EST_LOAD_SEC = 0.15;
export const MAX_EST_LOAD_SEC = 6.0;

/** 로딩이 이 시간을 넘어서면 "프리페치로는 못 메운다"고 보고 깊이를 1 로 고정한다.
 *
 *  실측(73씬 프로젝트): 캐시 적중 0.12초인데 미스는 12.9 / 27.9 / **72.5초**까지 나온다.
 *  원본이 177MB 인 footage 는 프록시 인코딩만 수십 초다. 이 구간에서 깊이를 늘리면
 *  ① 어차피 씬 재생시간(5~11초)으로는 절대 못 덮고,
 *  ② 서버 ffmpeg 큐만 길어져 정작 사용자가 보고 있는 현재 씬 요청이 뒤로 밀리며,
 *  ③ 사용자가 씬을 점프하면 그 긴 작업이 통째로 버려진다.
 *  그래서 "느릴수록 더 받는다"는 규칙을 여기서 끊고, 다음 1 개만 미리 받는다. */
export const PREFETCH_GIVEUP_SEC = 5.0;

/** 실측 표본이 없을 때 쓰는 초기 추정치. 캐시 적중(0.12s)과 미스(2.3s)의 중간쯤을
 *  보수적으로 잡아, 첫 전환부터 과소 프리페치되지 않게 한다. */
export const DEFAULT_EST_LOAD_SEC = 1.2;

/** blob URL 생성 + <video> 디코딩/버퍼링 등 네트워크 외 고정 오버헤드(초). */
export const DECODE_OVERHEAD_SEC = 0.25;

/** 안전 계수 — 확보 시간이 소요 시간을 "딱" 덮는 수준(1.0)이면 서버 지연·GC·탭 스로틀링이
 *  한 번만 껴도 즉시 공백이 생긴다. 실제로는 전환 시점마다 깊이를 재산출하므로 버퍼가
 *  1개뿐이면 매 전환이 아슬아슬해진다. 2배의 여유를 요구해 버퍼가 항상 한 겹 더 쌓이게 한다.
 *  → 짧은 씬(2s)+캐시 적중(0.37s) 구간에서 깊이가 2 이상으로 올라간다. */
export const SAFETY_FACTOR = 2.0;

export interface PrefetchDepthInput {
  /** 현재 씬 다음부터 순서대로 나열한 씬들의 재생 길이(초, 1배속 기준).
   *  `computeSceneVisualDuration` 결과를 그대로 넣는다. */
  upcomingDurations: readonly number[];
  /** 현재 씬의 남은 재생 시간(초, 1배속 기준). 모르면 0. */
  currentRemainingSec?: number;
  /** 씬 1개를 받아 재생 가능 상태로 만드는 데 걸리는 실측 추정치(초).
   *  호출자가 이동평균으로 누적해 전달. 미지정 시 DEFAULT_EST_LOAD_SEC. */
  estLoadSec?: number;
  /** 이어보기 재생 배속. 1 이 기본. */
  playbackRate?: number;
  /** 깊이 상한. 미지정 시 PREFETCH_MAX_DEPTH. */
  maxDepth?: number;
}

/**
 * 프리페치할 씬 개수를 산출한다.
 *
 * 알고리즘 — "충분해질 때까지만" 받는다:
 *   앞으로 k 번째 씬이 재생되기 시작하는 시점까지 확보되는 벽시계 시간은
 *     lead(k) = (현재 잔여 + 앞선 k-1 개 씬의 재생시간) / rate
 *   이고, 그 씬을 제때 준비하려면 직렬 로딩 기준 k * est 가 필요하다.
 *
 *   깊이는 **lead(k) 가 처음으로 k * est 를 넉넉히 덮는 최소 k**로 정한다.
 *   그 지점을 넘어서면 이미 여유가 충분하므로 더 받아도 전환 공백이 줄지 않고,
 *   서버 ffmpeg 큐와 메모리만 낭비한다(씬 점프 시 전부 버려짐).
 *
 *   즉 긴 씬이 연속되면 1 개에서 바로 조건이 충족되어 깊이 1,
 *   짧은 씬이 연속되면 여러 개를 합쳐야 로딩시간을 덮으므로 깊이가 깊어진다.
 *
 * @returns 0 이상 maxDepth 이하의 정수. 남은 씬이 없으면 0.
 */
export function computePrefetchDepth(input: PrefetchDepthInput): number {
  const {
    upcomingDurations,
    currentRemainingSec = 0,
    estLoadSec,
    playbackRate,
    maxDepth = PREFETCH_MAX_DEPTH,
  } = input;

  const cap = Math.max(0, Math.floor(maxDepth));
  if (cap === 0) return 0;
  if (!upcomingDurations || upcomingDurations.length === 0) return 0;

  const rate =
    typeof playbackRate === "number" && isFinite(playbackRate) && playbackRate > 0
      ? playbackRate
      : 1;

  const rawEst =
    typeof estLoadSec === "number" && isFinite(estLoadSec) && estLoadSec > 0
      ? estLoadSec
      : DEFAULT_EST_LOAD_SEC;
  // 디코딩 오버헤드를 더한 뒤 클램프 — 네트워크가 아무리 빨라도 0 이 될 수는 없다.
  const est = Math.min(
    MAX_EST_LOAD_SEC,
    Math.max(MIN_EST_LOAD_SEC, rawEst + DECODE_OVERHEAD_SEC),
  );

  // 로딩이 너무 비싼 환경(캐시 미스가 지배적)에서는 깊이를 늘려도 공백을 못 메우고
  // 서버 큐만 밀린다. 다음 1 개만 받아 두고 포기한다. (PREFETCH_GIVEUP_SEC 주석 참고)
  if (est >= PREFETCH_GIVEUP_SEC) return Math.min(1, cap, upcomingDurations.length);

  const remaining =
    typeof currentRemainingSec === "number" && isFinite(currentRemainingSec) && currentRemainingSec > 0
      ? currentRemainingSec
      : 0;

  const limit = Math.min(cap, upcomingDurations.length);

  // 확보해야 할 최소 선행 재생시간(벽시계).
  //   깊이를 매 전환마다 재산출하므로, 어느 시점에 봐도 "앞으로 재생될 분량"이
  //   로딩 1회에 SAFETY_FACTOR 배의 여유를 두고 남아 있어야 공백이 안 생긴다.
  //   짧은 씬은 1 개로 이 시간을 못 채우므로 자연히 여러 개를 쌓게 된다.
  let targetLeadSec = est * SAFETY_FACTOR;

  // 현재 씬의 잔여 시간이 로딩 1회분도 안 되면(막 전환된 짧은 씬) 이미 늦은 상태다.
  //   이때는 따라잡기 위해 목표를 한 겹 더 높여 버퍼를 두껍게 만든다.
  if (remaining > 0 && remaining / rate < est) {
    targetLeadSec += est;
  }

  // covered: 프리페치 버퍼가 확보하는 재생시간(벽시계).
  //   현재 씬의 잔여 재생시간(remaining)은 의도적으로 더하지 않는다.
  //   그 시간은 "첫 1 개를 받을 여유"일 뿐 버퍼가 아니며, 이를 예산에 합산하면
  //   긴 씬 직후 구간에서 깊이가 과대 산출됐다가 짧은 씬 구간에 진입하는 순간
  //   버퍼가 비어 버린다. 버퍼 충분성은 오직 "미리 받아 둔 씬들의 재생시간"으로 판단한다.
  let covered = 0;
  for (let k = 1; k <= limit; k++) {
    const d = upcomingDurations[k - 1];
    const safe = typeof d === "number" && isFinite(d) && d > 0 ? d : 0;
    covered += safe / rate;
    // k 개를 미리 받으면 covered 만큼의 재생시간이 버퍼로 확보된다.
    //
    // 멈춤 조건은 두 가지를 동시에 만족해야 한다:
    //  ① covered >= targetLeadSec — 버퍼가 로딩 1회분을 여유 있게 덮는다.
    //  ② 이 씬 1개의 재생시간이 로딩 1회분보다 길다 — 재생 중에 다음 1개를 받아낼 수 있어,
    //     버퍼가 스스로 유지된다(self-sustaining). 이 조건이 없으면 "2초 씬 1개 버퍼"처럼
    //     보기엔 충분해도, 매 전환마다 새 로딩이 제때 못 끝나 버퍼가 결국 고갈된다.
    //     짧은 씬이 연속되는 구간에서 깊이가 2 이상으로 올라가는 건 이 조건 때문이다.
    const selfSustaining = safe / rate >= est;
    if (covered >= targetLeadSec && selfSustaining) return k;
  }

  // 상한까지 가도 목표를 못 채우는 경우(매우 짧은 씬 + 느린 서버) — 상한까지 받는다.
  return limit;
}

/**
 * 로딩 시간 실측 이동평균(EMA) 갱신.
 *
 * 캐시 적중(0.12s)과 미스(2.3s) 사이를 빠르게 따라가야 하므로 alpha 를 크게(0.4) 잡는다.
 * 첫 표본(prev == null)은 그대로 채택.
 */
export function updateLoadEstimate(prev: number | null, sampleSec: number, alpha = 0.4): number {
  const s =
    typeof sampleSec === "number" && isFinite(sampleSec) && sampleSec >= 0 ? sampleSec : 0;
  if (prev == null || !isFinite(prev) || prev <= 0) return Math.max(0, s);
  const a = isFinite(alpha) && alpha > 0 && alpha <= 1 ? alpha : 0.4;
  return prev * (1 - a) + s * a;
}
