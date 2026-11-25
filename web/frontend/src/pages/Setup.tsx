import { useState, useEffect, useRef } from "react";
import { useStore } from "../store";
import { ui } from "../store/ui";
import { createProject, setScenes, updateScene as dbUpdateScene, getProject, updateProject } from "../lib/project";
import { synthesizeTtsAndSave } from "../lib/tts";
// requestProjectDirectory는 서버 모드에서만 사용됨
import type { SceneCard, MatchingMethod, FootageAsset, Project } from "../types";
import ManualMatchPanel from "../components/ManualMatchPanel";
import LocalThumbnail from "../components/LocalThumbnail";
import ServerFootageThumbnail from "../components/ServerFootageThumbnail";
import { getProjectDirectory } from "../lib/fileStore";
import { makeFootageFromFilename } from "../lib/footage";
import { runKeywordMatchClient } from "../lib/clientKeywordMatching";
import { GEMINI_ENABLED } from "../lib/features";
import { getFootage, startMatch, getMatchCostEstimate, wsUrl, getProject as getServerProject, getFootageIndexStatus, listSeries, createSeries, type FootageIndexStatus } from "../api/client";

// ── 매칭 메서드 카드 노출 목록 ─────────────────────────────────────────────
// id는 백엔드 MatchRequest.method Literal과 동일해야 한다 (web/backend/models.py:441).
// keyword_filename(#326): 클라이언트가 rapidfuzz 호환 알고리즘으로 직접 계산하므로
// 백엔드 ffmpeg/Gemini 경로를 우회 → 매칭 단가 0원 + 백엔드 CPU 절감.
//
// #355 후속:
//  - `gemini_content` 카드를 다시 노출. 청크 description/keywords 까지 종합 분석해
//    레이블 기반 매칭(`gemini_filename`)보다 정확하지만 입력 토큰이 약 4배 → 비용도 4배.
//  - Gemini 매칭(aiRequired=true) 은 모두 백엔드 `_do_match` 라우터(`startMatch`)를 통해
//    처리한다. 브라우저 직접 호출(`matchFootageByLabel`) 경로는 제거 — `record_matching`
//    의 by_model 분해에 모델별 누적이 정확히 잡히도록 모든 호출이 백엔드를 거치게 한다.
//  - `gemini_filename` 의 실제 동작은 footage 스튜디오의 레이블을 우선 사용하고,
//    레이블이 없는 자산만 파일명으로 폴백한다. 식별자(id)는 호환성을 위해 그대로 유지.
const METHODS: { id: MatchingMethod; label: string; desc: string; aiRequired: boolean }[] = [
  { id: "keyword_filename", label: "파일명 키워드 매칭 (비용 0원, 빠름)", desc: "씬 설명과 footage 파일명을 비교해 자동 매칭 — AI 미호출이라 비용이 들지 않고 결과가 즉시 나옴", aiRequired: false },
  { id: "gemini_filename", label: "Gemini · 레이블 매칭", desc: "AI가 씬 설명과 footage 레이블을 비교해 가장 어울리는 클립 선택 (레이블 없는 자산은 파일명으로 폴백, 입력 약 233토큰/씬)", aiRequired: true },
  { id: "gemini_content", label: "Gemini · 내용 분석", desc: "AI가 레이블 + 청크의 설명·키워드를 종합 분석해 가장 어울리는 클립 선택 (입력 약 1,200토큰/씬 — 정확하지만 약 5배 비용)", aiRequired: true },
  { id: "label", label: "레이블 매칭 (AI 불필요)", desc: "footage 스튜디오에서 붙인 레이블을 키워드로 자동 매칭 — AI 미호출이라 비용 0원", aiRequired: false },
  { id: "manual", label: "수동 배치", desc: "footage를 직접 선택하여 각 씬에 배치", aiRequired: false },
];

// #355 후속 — Setup Step 3 Gemini 카드에 표시할 모델 셀렉트 옵션.
// Editor.tsx 의 `GEMINI_MODELS` 와 동일한 목록을 유지해야 한다 (pricing.json
// `api.gemini.models` 와도 1:1). 별도 모듈로 분리하면 양쪽에서 import 가능하지만,
// 시점상 두 곳에 정의를 두고 주석으로 동기화 책임을 명시한다.
const GEMINI_MODELS: { value: string; label: string }[] = [
  { value: "gemini-2.5-flash-lite", label: "Gemini 2.5 Flash-Lite (저렴)" },
  { value: "gemini-2.5-flash",      label: "Gemini 2.5 Flash (기본)" },
  { value: "gemini-2.5-pro",        label: "Gemini 2.5 Pro (정확)" },
  { value: "gemini-3-flash-preview", label: "Gemini 3 Flash Preview (실험)" },
];

const DEFAULT_GEMINI_MODEL = "gemini-2.5-flash";

export default function Setup({
  onComplete,
  onBack,
  initialProjectId,
  initialProjectName,
}: {
  onComplete: (projectId: string) => void;
  onBack?: () => void;
  initialProjectId?: string;
  initialProjectName?: string;
}) {
  const {
    setupStep, setSetupStep,
    projectName, setProjectName,
    projectSeries, setProjectSeries,
    scenarioText, setScenarioText,
    matchingMethod, setMatchingMethod,
    isProcessing, setProcessing,
    progress, setProgress,
    project, setProject, updateScene,
  } = useStore();

  const [error, setError] = useState("");
  const [projectId, setProjectId] = useState<string | null>(initialProjectId ?? null);
  // 그룹(시리즈) 선택 후보 목록. 서버 등록 시리즈 이름들. 실패해도 자유 입력은 가능하므로 조용히 무시한다.
  const [seriesOptions, setSeriesOptions] = useState<string[]>([]);
  const [matchStatus, setMatchStatus] = useState<{ matched: number; total: number } | null>(null);
  // 자동 매칭(gemini/label) 완료 후 검토 단계 진입 여부.
  // true면 ManualMatchPanel을 표시해 사용자가 결과를 확인·재배치한 뒤 TTS로 진행한다.
  const [autoMatchReviewing, setAutoMatchReviewing] = useState(false);
  const [studioAssets, setStudioAssets] = useState<FootageAsset[]>([]);
  // 대본 작성 중 참고용으로 클릭 재생할 footage (모달). null 이면 닫힘.
  const [previewAsset, setPreviewAsset] = useState<FootageAsset | null>(null);
  // 시나리오 작성 패널에서 footage 썸네일을 표시하기 위한 로컬 파일 매핑.
  // 서버 footage_dir에 파일이 없는 경우(클라이언트가 IndexedDB 핸들로만 가진 파일) 폴백 용도.
  const [localFootageFileMap, setLocalFootageFileMap] = useState<Map<string, File>>(new Map());

  // #355 후속 — Gemini 매칭 카드 안에서 모델을 즉시 선택할 수 있게 한다.
  // 프로젝트의 `gemini_model` 이 진실의 원천. 카드에서 변경하면 IndexedDB+서버에도 즉시 동기화.
  // 두 Gemini 카드(`gemini_filename`, `gemini_content`)가 같은 모델 상태를 공유한다.
  const geminiModel = (project?.gemini_model as string | undefined) ?? DEFAULT_GEMINI_MODEL;
  // 예상 비용 캐시 — (method, model, sceneCount) 변경 시 API 호출 결과를 보관.
  // 키는 method 별로 분리해 카드마다 자기 비용을 보여준다.
  const [matchCostByMethod, setMatchCostByMethod] = useState<Record<string, { krw: number; usd: number }>>({});
  const matchCostReqIdRef = useRef(0);

  // footage_index.json 존재 여부 — `gemini_content` 카드 활성화 판정.
  // null = 로딩 중(깜빡임 방지 위해 비활성화 적용 안 함).
  // {indexed:false} 이면 청크 description 활용이 0이라 사실상 레이블 매칭과 같아져 카드를 막는다.
  const [indexStatus, setIndexStatus] = useState<FootageIndexStatus | null>(null);

  // 시나리오 초안 자동 저장 키 — 새로고침·이전 화면 이동 후에도 입력 내용을 보존한다.
  // 신규 프로젝트는 "__new__" 키를 쓰고, 기존 프로젝트는 project_id별로 분리.
  const draftKey = `setup.draft.scenario:${initialProjectId ?? "__new__"}`;
  const draftNameKey = `setup.draft.projectName:__new__`;

  // initialProjectId가 있으면 Step 2부터 시작, projectName 초기화.
  // 시나리오 텍스트는 localStorage 초안 > IndexedDB project.scenario_text 순으로 복원.
  useEffect(() => {
    if (initialProjectId) {
      setSetupStep(2);
      if (initialProjectName) setProjectName(initialProjectName);
      getProject(initialProjectId).then((p) => {
        setStudioAssets(p.studio_assets ?? []);
        // 시나리오 텍스트가 store에 비어 있을 때만 복원(사용자가 작업 중인 내용 덮어쓰지 않도록).
        // 우선순위: localStorage 초안 > IndexedDB project.scenario_text > 서버 fetch.
        if (!useStore.getState().scenarioText.trim()) {
          let restored = "";
          try { restored = localStorage.getItem(draftKey) || ""; } catch { /* ignore */ }
          if (!restored && p.scenario_text) restored = p.scenario_text;
          if (restored) {
            setScenarioText(restored);
          } else {
            // IndexedDB에도 없으면 서버 state를 마지막으로 시도.
            fetch(`/api/projects/${initialProjectId}`)
              .then((r) => r.ok ? r.json() : null)
              .then((srv) => {
                if (srv && typeof srv.scenario_text === "string" && srv.scenario_text.trim()
                    && !useStore.getState().scenarioText.trim()) {
                  setScenarioText(srv.scenario_text);
                }
              })
              .catch(() => {});
          }
        }
      }).catch(() => {});
    } else {
      // 신규 프로젝트 — store가 비었으면 localStorage 초안 복원.
      if (!useStore.getState().scenarioText.trim()) {
        try {
          const s = localStorage.getItem(draftKey);
          if (s) setScenarioText(s);
        } catch { /* ignore */ }
      }
      if (!useStore.getState().projectName.trim()) {
        try {
          const n = localStorage.getItem(draftNameKey);
          if (n) setProjectName(n);
        } catch { /* ignore */ }
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 시나리오·프로젝트명 자동 저장 — 입력 즉시 localStorage에 백업.
  useEffect(() => {
    try {
      if (scenarioText.trim()) localStorage.setItem(draftKey, scenarioText);
      else localStorage.removeItem(draftKey);
    } catch { /* ignore */ }
  }, [scenarioText, draftKey]);

  useEffect(() => {
    if (initialProjectId) return; // 기존 프로젝트는 이름이 IndexedDB에 있음
    try {
      if (projectName.trim()) localStorage.setItem(draftNameKey, projectName);
      else localStorage.removeItem(draftNameKey);
    } catch { /* ignore */ }
  }, [projectName, initialProjectId, draftNameKey]);

  // 그룹(시리즈) 후보 목록 로드 — 신규 생성 화면에서만 필요.
  // 실패해도 datalist 가 비는 것뿐이고 직접 입력은 그대로 되므로 조용히 무시한다.
  useEffect(() => {
    if (initialProjectId) return;
    let cancelled = false;
    listSeries()
      .then((list) => {
        if (!cancelled) setSeriesOptions(list.map((s) => s.name).filter(Boolean));
      })
      .catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, [initialProjectId]);

  // footage_index.json 인덱싱 상태 조회 — `gemini_content` 카드 활성화 판정.
  // projectId 변경 시 1회만 호출(가벼운 fs.exists + len 만 검사). 실패 시 미인덱싱으로 폴백.
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    getFootageIndexStatus(projectId)
      .then((s) => { if (!cancelled) setIndexStatus(s); })
      .catch(() => { if (!cancelled) setIndexStatus({ indexed: false, chunk_count: 0 }); });
    return () => { cancelled = true; };
  }, [projectId]);

  // IndexedDB 디렉토리 핸들에서 footage 파일을 File로 변환해 맵 구축.
  // Studio와 동일 패턴 — 권한 부여 안 됐거나 핸들 없으면 빈 맵 유지(서버 URL로 폴백).
  useEffect(() => {
    if (!initialProjectId) return;
    let cancelled = false;
    getProjectDirectory(initialProjectId).then(async (dir) => {
      if (!dir || cancelled) return;
      const map = new Map<string, File>();
      try {
        for await (const [name, entry] of dir.entries()) {
          if (cancelled) return;
          if ((entry as FileSystemHandle).kind !== "file") continue;
          try {
            const file = await (entry as FileSystemFileHandle).getFile();
            map.set(name, file);
          } catch { /* 개별 파일 실패 무시 */ }
        }
      } catch { /* enumeration 실패 무시 */ }
      if (!cancelled) setLocalFootageFileMap(map);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [initialProjectId]);

  // Step 1 → 2
  function handleStep1() {
    if (!projectName.trim()) { setError("프로젝트 이름을 입력하세요"); return; }
    setError("");
    setSetupStep(2);
  }

  // Step 2: 프로젝트 생성(또는 기존 프로젝트 재사용) + 시나리오 파싱
  async function handleStep2() {
    if (!scenarioText.trim()) { setError("대본을 입력하세요"); return; }
    // #292 — 줄이 너무 많거나(>50) 긴 줄(>200자)이 있으면 진행 전 확인.
    const lines = scenarioText.split("\n").map((l) => l.trim()).filter((l) => l.length > 0);
    const longLines = lines.filter((l) => l.length > 200).length;
    if (lines.length > 50 || longLines > 0) {
      const parts: string[] = [];
      if (lines.length > 50) parts.push(`줄(=씬) 수가 ${lines.length}개로 많습니다 (권장 50개 이하).`);
      if (longLines > 0) parts.push(`200자를 초과하는 줄이 ${longLines}개 있습니다. 한 줄이 한 씬이므로 자막이 너무 길어질 수 있습니다.`);
      const ok = await ui.confirm({
        title: "대본 확인",
        message: parts.join("\n") + "\n\n그대로 진행할까요?",
        variant: "warning",
        confirmLabel: "진행",
      });
      if (!ok) return;
    }
    setError("");
    setProcessing(true);
    setProgress({ current: 0, total: 0, message: "처리 중...", phase: "parsing" });
    try {
      let pid: string;
      if (initialProjectId) {
        // footage 스튜디오에서 진입: 기존 프로젝트 재사용
        pid = initialProjectId;
      } else {
        // 신규 프로젝트 생성
        const p = await createProject(projectName, scenarioText, "", projectSeries);
        pid = p.project_id;
        setProjectId(pid);
        // 새 그룹명을 입력했으면 시리즈 저장소에도 등록한다(멱등 — 기존 이름이면 그대로 반환).
        // 이렇게 해야 프로젝트가 0개인 시점에도 홈 화면 그룹 목록/드롭 대상에 나타난다.
        const seriesName = projectSeries.trim();
        if (seriesName) {
          try {
            await createSeries(seriesName);
          } catch { /* 목록 등록 실패해도 프로젝트의 series 값은 이미 반영됨 */ }
        }
        // 생성 완료 — 다음 신규 프로젝트에 이 그룹이 딸려가지 않도록 입력값을 비운다.
        setProjectSeries("");
        // 초안 정리 — 신규 키는 비우고, 프로젝트 키에 시나리오 백업 유지(혹시 모를 사고 대비).
        try {
          localStorage.removeItem(`setup.draft.scenario:__new__`);
          localStorage.removeItem(`setup.draft.projectName:__new__`);
          if (scenarioText.trim()) localStorage.setItem(`setup.draft.scenario:${pid}`, scenarioText);
        } catch { /* ignore */ }
      }

      // 줄 단위 파싱: 각 줄이 하나의 씬
      const lines = scenarioText.split("\n").map(l => l.trim()).filter(l => l.length > 0);
      const scenes: SceneCard[] = lines.map((line, i) => ({
        scene_id: i + 1,
        title: line.slice(0, 30),
        description: "",
        mood: "",
        keywords: [],
        duration_hint_sec: 5.0,
        narration_text: line,
        footage: null,
        match: null,
        tts: null,
        text_overlays: [],
        shape_overlays: [],
      }));
      const saved = await setScenes(pid, scenes);
      setProject(saved);
      setMatchStatus({ matched: 0, total: scenes.length });
      setSetupStep(3);
    } catch (e: unknown) {
      setError((e as Error).message ?? "오류 발생");
    } finally {
      setProcessing(false);
    }
  }

  // #355 후속 — Gemini 카드의 모델/방식/씬 수가 바뀔 때마다 예상 비용 갱신.
  // 카드를 선택만 했다 해도 모든 Gemini 카드의 비용을 보여줘야 비교가 쉬우므로
  // method 별로 한 번씩 호출한다(2회 — gemini_filename / gemini_content).
  useEffect(() => {
    if (!projectId) return;
    const sceneCount = project?.scenes.length ?? matchStatus?.total ?? 0;
    if (sceneCount <= 0) return;
    const reqId = ++matchCostReqIdRef.current;
    const methods = ["gemini_filename", "gemini_content"] as const;
    Promise.all(
      methods.map((m) =>
        getMatchCostEstimate(projectId, m, geminiModel, sceneCount)
          .then((r) => ({ m, krw: r.krw, usd: r.usd }))
          .catch(() => ({ m, krw: 0, usd: 0 })),
      ),
    ).then((rows) => {
      // 최신 요청이 아니면 무시 — race condition 방지.
      if (reqId !== matchCostReqIdRef.current) return;
      const next: Record<string, { krw: number; usd: number }> = {};
      for (const row of rows) next[row.m] = { krw: row.krw, usd: row.usd };
      setMatchCostByMethod(next);
    });
  }, [projectId, geminiModel, project?.scenes.length, matchStatus?.total]);

  // 카드에서 모델 변경 — 낙관적으로 store 갱신 후 IndexedDB+서버 동기화.
  async function handleSelectModel(newModel: string) {
    if (!project || !projectId) return;
    setProject({ ...project, gemini_model: newModel } as Project);
    try {
      await updateProject(projectId, { gemini_model: newModel });
    } catch (e) {
      console.warn("[Setup] gemini_model 저장 실패:", e);
    }
  }

  // Step 3: 자동 매칭만 실행. TTS·완료 진행은 검토 단계 이후 사용자 확인 후 수행.
  async function handleStartMatching() {
    if (!matchingMethod || !projectId || !project) return;
    setError("");
    setProcessing(true);
    try {
      const scenes = project.scenes;
      const assets = project.studio_assets;

      if (matchingMethod === "gemini_filename" || matchingMethod === "gemini_content") {
        // #355 후속 — 백엔드 라우터로 통일. 모델은 카드에서 선택한 값을 그대로 전달.
        // 백엔드는 _do_match → record_matching(model=...) 으로 by_model 분해까지 갱신한다.
        await doBackendGeminiMatch(projectId, matchingMethod, geminiModel);
      } else if (matchingMethod === "label") {
        await doLabelMatch(projectId, scenes, assets);
      } else if (matchingMethod === "keyword_filename") {
        // #326 — UI에서 keyword_filename 선택 시 클라이언트 알고리즘으로 직접 매칭한다.
        // 백엔드 POST /match (method=keyword_filename) 경로는 회귀 대비로 유지되지만
        // UI에서는 호출하지 않는다. 결과는 saveKeywordMatchResult 한 번으로 일괄 저장.
        await doKeywordFilenameClientMatch(projectId, scenes);
      }

      // 매칭 결과를 사용자가 확인·수정할 수 있도록 검토 단계로 전환.
      // 검토 후 'TTS 생성 시작' 버튼에서 TTS·완료 진행.
      // matchStatus 초기화(total만 채워두면 ManualMatchPanel onMatchCountChange가 matched를 갱신).
      setMatchStatus({ matched: 0, total: project.scenes.length });
      setAutoMatchReviewing(true);
    } catch (e: unknown) {
      setError((e as Error).message ?? "오류 발생");
    } finally {
      setProcessing(false);
    }
  }

  // 자동 매칭 검토 단계에서 사용자가 확인 후 TTS 진행.
  async function handleProceedFromReview() {
    if (!projectId || !project) return;
    setError("");
    setProcessing(true);
    try {
      await doGenerateTts(projectId, project.scenes, project.tts_voice);
      onComplete(projectId);
    } catch (e: unknown) {
      setError((e as Error).message ?? "오류 발생");
    } finally {
      setProcessing(false);
    }
  }

  // #355 후속 — 백엔드 라우터 기반 Gemini 매칭.
  // 기존 브라우저 직접 호출(`matchFootageByLabel`)은 백엔드 `record_matching` 를 거치지
  // 못해 CostBadge by_model 분해에 모델별 누적이 잡히지 않았다. 백엔드로 통일하여
  // (a) 모델 선택이 실제 Gemini 호출에 반영되고, (b) 비용도 모델별로 정확히 누적된다.
  async function doBackendGeminiMatch(pid: string, method: "gemini_filename" | "gemini_content", model: string) {
    setProgress({ current: 0, total: project?.scenes.length ?? 0, message: `Gemini 매칭 중... (${model})`, phase: "matching" });

    // 1) 백엔드 매칭 태스크 시작 — model 인자를 함께 전달한다.
    const taskId = await startMatch(pid, method, undefined, model);

    // 2) WS 로 진행률 수신. complete/error 가 오면 Promise 종료.
    await new Promise<void>((resolve) => {
      const ws = new WebSocket(wsUrl(taskId));
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        try { ws.close(); } catch { /* ignore */ }
        resolve();
      };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === "progress") {
            setProgress({
              current: msg.current ?? 0,
              total: msg.total ?? 0,
              message: msg.message ?? "매칭 중...",
              phase: "matching",
            });
          } else if (msg.type === "complete") {
            finish();
          } else if (msg.type === "error" && !("scene_id" in msg)) {
            // 전체 실패 — 메시지만 표시하고 종료.
            console.warn("[matching] 백엔드 매칭 실패:", msg);
            finish();
          }
        } catch { /* ignore */ }
      };
      ws.onclose = finish;
      ws.onerror = finish;
    });

    // 3) 백엔드가 store.update_scene 으로 footage/match 를 반영했으므로
    //    서버에서 프로젝트를 다시 가져와 클라 store + IndexedDB 와 동기화한다.
    try {
      const serverProject = await getServerProject(pid);
      // store 갱신 — Editor 와 동일하게 setProject 로 전체 교체.
      setProject(serverProject);
      // IndexedDB 동기화 — 씬별로 footage 만 반영(다른 필드 보존).
      for (const scene of serverProject.scenes) {
        if (scene.footage) {
          await dbUpdateScene(pid, scene.scene_id, { footage: scene.footage, match: scene.match });
          updateScene(scene.scene_id, { footage: scene.footage, match: scene.match });
        }
      }
    } catch (e) {
      console.warn("[matching] 서버 프로젝트 재동기화 실패:", e);
    }
  }

  // #326 — 클라이언트 keyword_filename 매칭.
  // 백엔드 `_match_keyword`(`web/backend/routers/matching.py:126`)와 1:1 동등한
  // TypeScript 구현 `runKeywordMatchClient`(`web/frontend/src/lib/clientKeywordMatching.ts`)를
  // 호출한다. footage_index.json은 백엔드가 보유 → `GET /api/projects/{id}/footage`로
  // FootageItem(chunks 포함)을 받아 매칭 입력으로 변환. 결과는 클라이언트가 직접
  // store에 반영하지 않고 `saveKeywordMatchResult` 한 번으로 백엔드에 일괄 저장한다
  // (백엔드 store만 정답 — IndexedDB 동기화는 saveKeywordMatchResult 응답 후 별도 처리).
  async function doKeywordFilenameClientMatch(pid: string, scenes: SceneCard[]) {
    setProgress({ current: 0, total: scenes.length, message: "키워드 매칭 중...", phase: "matching" });
    let items;
    try {
      items = await getFootage(pid);
    } catch (e) {
      throw new Error(
        `footage 정보를 가져오지 못했습니다. 먼저 footage 인덱싱을 완료하세요. (${(e as Error).message ?? e})`
      );
    }
    // 진행률 콜백 — 백엔드 매칭과 동일한 컨벤션(current/total/message/phase) 유지.
    const r = await runKeywordMatchClient(pid, scenes, items, (done, total, sceneId) => {
      setProgress({
        current: done,
        total,
        message: `키워드 매칭 중... 씬 ${sceneId} 처리 완료 (${done}/${total})`,
        phase: "matching",
      });
    });

    // 백엔드가 저장한 결과를 클라이언트 store와 IndexedDB에도 반영.
    // saveKeywordMatchResult 응답에는 scene_id별 성공 여부만 들어 있어
    // 실제 footage/match 값은 computed_results에서 가져온다.
    for (const c of r.computed_results) {
      if (!c.result) continue;
      if (!r.updated_scene_ids.includes(c.scene_id)) continue;
      const footage = makeFootageFromFilename(c.result.filename);
      await dbUpdateScene(pid, c.scene_id, { footage });
      updateScene(c.scene_id, { footage });
    }
  }

  async function doLabelMatch(pid: string, scenes: SceneCard[], assets: FootageAsset[]) {
    setProgress({ current: 0, total: scenes.length, message: "레이블 매칭 중...", phase: "matching" });
    let matched = 0;
    for (const scene of scenes) {
      const keywords = [...scene.keywords, ...scene.title.split(" "), ...scene.description.split(" ")]
        .map((k) => k.toLowerCase().trim()).filter(Boolean);
      const best = assets
        .map((a) => {
          const label = a.label.toLowerCase();
          const score = keywords.filter((k) => label.includes(k)).length;
          return { asset: a, score };
        })
        .filter((x) => x.score > 0)
        .sort((a, b) => b.score - a.score)[0];

      if (best) {
        const footage = makeFootageFromFilename(best.asset.filename);
        await dbUpdateScene(pid, scene.scene_id, { footage });
        updateScene(scene.scene_id, { footage });
        matched++;
      }
      setProgress({ current: matched, total: scenes.length, message: `매칭 중... ${matched}/${scenes.length}`, phase: "matching" });
    }
  }

  async function doGenerateTts(pid: string, scenes: SceneCard[], voice?: string) {
    const toGenerate = scenes.filter((s) => s.narration_text?.trim());
    setProgress({ current: 0, total: toGenerate.length, message: "TTS 생성 중...", phase: "tts" });
    for (let i = 0; i < toGenerate.length; i++) {
      const scene = toGenerate[i];
      setProgress({ current: i, total: toGenerate.length, message: `TTS 생성 중... (${i + 1}/${toGenerate.length})`, phase: "tts" });
      const { blobUrl, durationSec } = await synthesizeTtsAndSave(pid, scene.scene_id, scene.narration_text, voice);
      const tts = { audio_url: blobUrl, duration_sec: durationSec, status: "ready" as const, synced: true };
      await dbUpdateScene(pid, scene.scene_id, { tts });
      updateScene(scene.scene_id, { tts });
    }
  }

  // 수동 매칭 완료 후 TTS
  async function handleManualMatchComplete() {
    if (!projectId || !project) return;
    setError("");
    setProcessing(true);
    try {
      await doGenerateTts(projectId, project.scenes, project.tts_voice);
      onComplete(projectId);
    } catch (e: unknown) {
      setError((e as Error).message ?? "오류 발생");
    } finally {
      setProcessing(false);
    }
  }

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <div style={styles.topBar}>
          <button
            style={styles.topBackBtn}
            onClick={() => initialProjectId ? onBack?.() : onBack?.()}
            title={initialProjectId ? "이전 화면으로" : "홈으로"}
          >
            ← {initialProjectId ? "이전" : "홈"}
          </button>
        </div>
        <h1 style={styles.title}>🎬 video-agent 편집기</h1>

        {/* #292 — 단계 헤더 클릭 시 뒤 단계로 이동(앞으로는 차단). manual/review 중 등
            특수 진행 상태에서는 자동 매칭 검토 패널을 깨지 않도록 클릭 이동을 막는다. */}
        <StepIndicator
          current={setupStep}
          onGoStep={(s) => {
            if (s < setupStep && !isProcessing && !autoMatchReviewing) {
              setError("");
              setSetupStep(s as 1 | 2 | 3);
            }
          }}
        />

        {error && <div style={styles.error}>{error}</div>}

        {setupStep === 1 && (
          <div style={styles.step}>
            <h2>1. 프로젝트 이름</h2>
            {/* #292 — placeholder 외에 무엇을 입력하는지 설명/예시 추가 */}
            <p style={styles.hint}>
              영상을 구분할 이름입니다. 나중에 홈 화면 목록에 이 이름으로 표시됩니다. (예: "고양이 간식 장난감 리뷰")
            </p>
            <input
              style={styles.input}
              placeholder="예: 고양이 간식 장난감"
              value={projectName}
              onChange={(e) => setProjectName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleStep1()}
            />
            {/* 프로젝트 그룹(시리즈) — 선택 사항. 기존 그룹을 고르거나 새 이름을 직접 입력한다.
                홈 화면에서 이 그룹으로 묶여 표시된다. 비워두면 "시리즈 미편성". */}
            <h2 style={{ marginTop: 24 }}>프로젝트 그룹 <span style={{ fontSize: 14, fontWeight: 400, color: "#888" }}>(선택)</span></h2>
            <p style={styles.hint}>
              연재물이면 같은 그룹으로 묶어두면 홈 화면에서 함께 표시됩니다. 목록에서 고르거나 새 이름을 입력하세요. 비워두면 "시리즈 미편성"입니다.
            </p>
            <input
              style={styles.input}
              list="setup-series-options"
              placeholder="예: AI펀드매니저 (비워두면 미편성)"
              value={projectSeries}
              onChange={(e) => setProjectSeries(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleStep1()}
            />
            <datalist id="setup-series-options">
              {seriesOptions.map((s) => <option key={s} value={s} />)}
            </datalist>
            <div style={styles.row}>
              {onBack && <button style={styles.btnSecondary} onClick={onBack}>← 홈</button>}
              <button style={styles.btn} onClick={handleStep1}>다음 →</button>
            </div>
          </div>
        )}

        {setupStep === 2 && (
          <div style={styles.step}>
            <h2>2. 대본 입력</h2>
            <p style={styles.hint}>각 줄이 하나의 씬으로 처리됩니다. 매칭 방식은 다음 단계에서 선택합니다.</p>
            {/* #292 — 줄 수 = 씬 수 실시간 카운터 */}
            {(() => {
              const lineCount = scenarioText.split("\n").map((l) => l.trim()).filter((l) => l.length > 0).length;
              const longLines = scenarioText.split("\n").filter((l) => l.trim().length > 200).length;
              return (
                <div style={styles.sceneCounter}>
                  현재 <b style={{ color: "#fff" }}>{lineCount}</b>줄 → <b style={{ color: "#818cf8" }}>{lineCount}개 씬</b> 생성 예정
                  {lineCount > 50 && <span style={styles.counterWarn}> · 씬이 너무 많습니다 ({lineCount}개, 권장 50개 이하)</span>}
                  {longLines > 0 && <span style={styles.counterWarn}> · 200자 초과 줄 {longLines}개</span>}
                </div>
              );
            })()}
            <div style={styles.step2Layout}>
              <textarea
                style={{ ...styles.textarea, flex: 1, whiteSpace: "nowrap", overflowX: "auto" }}
                placeholder={"[1] 어흥이가 아빠와 무언가를 조립하고 있습니다.\n[2] 조립이 완료된 건가요? 어흥이가 신나합니다.\n[3] 완성된 장난감으로 신나게 노는 어흥이"}
                value={scenarioText}
                onChange={(e) => setScenarioText(e.target.value)}
                rows={12}
                wrap="off"
              />
              {initialProjectId && studioAssets.filter((a) => a.label?.trim()).length > 0 && (
                <div style={styles.labelPanel}>
                  <div style={styles.labelPanelTitle}>footage 레이블</div>
                  {studioAssets
                    .filter((a) => a.label?.trim())
                    // 레이블 텍스트 기준 정렬 — 시나리오 작성 시 날짜/번호 순으로 찾기 쉽게.
                    // numeric:true 로 "01_cover (2)" < "01_cover (10)" 자연 정렬.
                    .slice()
                    .sort((a, b) =>
                      (a.label || "").localeCompare(b.label || "", undefined, { numeric: true })
                    )
                    .map((a) => {
                      const localFile = localFootageFileMap.get(a.filename);
                      return (
                        <div
                          key={a.asset_id}
                          style={{ ...styles.labelItem, cursor: "pointer" }}
                          title="클릭하면 영상을 재생합니다"
                          onClick={() => setPreviewAsset(a)}
                        >
                          {localFile ? (
                            <LocalThumbnail file={localFile} alt={a.label} style={styles.labelThumb} />
                          ) : (
                            // #307: 서버 푸티지도 브라우저에서 추출 + IndexedDB 캐시 (백엔드 ffmpeg 절감).
                            <ServerFootageThumbnail
                              projectId={initialProjectId ?? ""}
                              filename={a.filename}
                              assetType={a.asset_type}
                              alt={a.label}
                              style={styles.labelThumb}
                            />
                          )}
                          <span style={styles.labelText}>{a.label}</span>
                        </div>
                      );
                    })}
                </div>
              )}
            </div>
            <div style={styles.row}>
              <button
                style={styles.btnSecondary}
                onClick={() => initialProjectId ? onBack?.() : setSetupStep(1)}
              >
                ← 이전
              </button>
              <button style={styles.btn} onClick={handleStep2} disabled={isProcessing}>
                {isProcessing ? "처리 중..." : "다음 →"}
              </button>
            </div>
          </div>
        )}

        {setupStep === 3 && !isProcessing && (
          <div style={styles.step}>
            <h2>3. footage 매칭 방식 선택</h2>
            <p style={styles.hint}>총 {matchStatus?.total ?? 0}개 씬 — 매칭 방식을 선택하고 시작하세요.</p>
            <div style={styles.methodGrid}>
              {/* Gemini 매칭 카드는 기본 숨김(lib/features.ts) — 매칭은 Claude Code 가 대신한다. */}
              {METHODS.filter((m) => GEMINI_ENABLED || !m.aiRequired).map((m) => {
                // #355 후속 — Gemini 카드에만 모델 셀렉트 + 예상 비용 라벨 표시.
                const isGemini = m.id === "gemini_filename" || m.id === "gemini_content";
                const cost = matchCostByMethod[m.id];
                // `gemini_content` 만 인덱싱이 필수. 인덱싱 없으면 청크 description 활용이 0이라
                // 사실상 `gemini_filename`(레이블 매칭)과 결과가 같아져 추가 비용만 소모됨.
                // indexStatus가 아직 null인 동안(로딩 중)에는 비활성화 적용 안 함 — 깜빡임 방지.
                const requiresIndex = m.id === "gemini_content";
                const disabled = requiresIndex && indexStatus !== null && !indexStatus.indexed;
                const selected = !disabled && matchingMethod === m.id;
                return (
                  <div
                    key={m.id}
                    style={{
                      ...styles.methodCard,
                      ...(selected ? styles.methodCardSelected : {}),
                      ...(disabled ? styles.methodCardDisabled : {}),
                    }}
                    onClick={disabled ? undefined : () => setMatchingMethod(m.id)}
                  >
                    <div style={styles.methodLabel}>{m.label}</div>
                    <div style={styles.methodDesc}>{m.desc}</div>
                    {m.aiRequired && <span style={styles.aiBadge}>AI</span>}
                    {isGemini && (
                      <div style={styles.methodFooter}>
                        {/* 모델 선택 — 카드 클릭(=select)으로 모달 진입 막기 위해 stopPropagation. */}
                        <select
                          style={styles.modelSelect}
                          value={geminiModel}
                          onChange={(e) => {
                            e.stopPropagation();
                            void handleSelectModel(e.target.value);
                          }}
                          onClick={(e) => e.stopPropagation()}
                          title="이 매칭에 사용할 Gemini 모델"
                          disabled={disabled}
                        >
                          {GEMINI_MODELS.map((g) => (
                            <option key={g.value} value={g.value}>{g.label}</option>
                          ))}
                        </select>
                        <div style={styles.costLabel} title={cost ? `약 $${cost.usd.toFixed(4)} USD` : ""}>
                          예상 약 <b>{cost ? cost.krw.toLocaleString() : "—"}</b>원
                        </div>
                      </div>
                    )}
                    {disabled && (
                      <div style={styles.disabledNotice}>
                        ⚠ 먼저 footage 스튜디오에서 AI 인덱싱을 완료하세요.
                        <br />
                        <span style={styles.disabledHint}>현재 청크 수: {indexStatus?.chunk_count ?? 0}</span>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            {(matchingMethod === "manual" || autoMatchReviewing) && project && (
              <>
                {autoMatchReviewing && (
                  <div style={styles.reviewBanner}>
                    ✓ 자동 매칭이 완료되었습니다. 결과를 확인하고 필요하면 수정한 뒤 TTS 생성을 시작하세요.
                  </div>
                )}
                <ManualMatchPanel
                  project={project}
                  footageDir=""
                  onAllMatched={() => setMatchStatus((prev) => prev ?? { matched: 0, total: 0 })}
                  onMatchCountChange={(matched) =>
                    setMatchStatus((prev) => prev ? { ...prev, matched } : null)
                  }
                />
                <div style={styles.row}>
                  <button
                    style={styles.btnSecondary}
                    onClick={() => {
                      if (autoMatchReviewing) setAutoMatchReviewing(false);
                      else setSetupStep(2);
                    }}
                  >
                    ← 이전
                  </button>
                  <button
                    style={styles.btn}
                    onClick={autoMatchReviewing ? handleProceedFromReview : handleManualMatchComplete}
                    disabled={
                      !matchStatus ||
                      matchStatus.matched < matchStatus.total ||
                      matchStatus.total === 0
                    }
                  >
                    TTS 생성 시작 →
                  </button>
                </div>
              </>
            )}

            {matchingMethod && matchingMethod !== "manual" && !autoMatchReviewing && (
              <div style={styles.row}>
                <button style={styles.btnSecondary} onClick={() => setSetupStep(2)}>← 이전</button>
                {/* gemini_content 선택 후 indexStatus가 false로 도착한 경우
                    카드는 disabled로 가지만 matchingMethod state는 그대로 남아 있음.
                    혼란을 피하기 위해 선택은 유지하되 매칭 시작 버튼만 막는다. */}
                <button
                  style={styles.btn}
                  onClick={handleStartMatching}
                  disabled={
                    matchingMethod === "gemini_content" &&
                    indexStatus !== null &&
                    !indexStatus.indexed
                  }
                  title={
                    matchingMethod === "gemini_content" && indexStatus !== null && !indexStatus.indexed
                      ? "footage 인덱싱이 필요합니다 — footage 스튜디오에서 AI 인덱싱을 먼저 완료하세요"
                      : undefined
                  }
                >
                  매칭 시작 →
                </button>
              </div>
            )}
          </div>
        )}

        {isProcessing && (
          <ProgressPanel phase={progress.phase} current={progress.current} total={progress.total} message={progress.message} />
        )}
      </div>

      {/* footage 재생 모달 — 대본 작성 중 참고용. 바탕 클릭으로 닫힘. */}
      {previewAsset && initialProjectId && (
        <div style={styles.previewOverlay} onClick={() => setPreviewAsset(null)}>
          <div style={styles.previewModal} onClick={(e) => e.stopPropagation()}>
            <div style={styles.previewModalHeader}>
              <span style={styles.previewModalTitle}>{previewAsset.label || previewAsset.filename}</span>
              <button style={styles.previewModalClose} onClick={() => setPreviewAsset(null)} title="닫기">✕</button>
            </div>
            {previewAsset.asset_type === "image" ? (
              <img
                src={`/api/projects/${initialProjectId}/footage/${encodeURIComponent(previewAsset.filename)}/stream`}
                alt={previewAsset.filename}
                style={styles.previewModalMedia}
              />
            ) : (
              <video
                key={previewAsset.asset_id}
                src={`/api/projects/${initialProjectId}/footage/${encodeURIComponent(previewAsset.filename)}/stream`}
                style={styles.previewModalMedia}
                controls
                autoPlay
                preload="metadata"
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// #292 — Stepper 강화:
//  - 완료된 단계(번호 < current): ✓ 체크마크 + 진한 색 + 클릭하여 돌아가기 가능
//  - 현재 단계: 굵게 + 강조색 테두리
//  - 미진행 단계: 회색
function StepIndicator({ current, onGoStep }: { current: number; onGoStep?: (step: number) => void }) {
  const steps = ["프로젝트명", "대본 입력", "매칭 방식"];
  return (
    <div style={styles.stepIndicator}>
      {steps.map((s, i) => {
        const stepNum = i + 1;
        const done = stepNum < current;
        const active = stepNum === current;
        const clickable = done && !!onGoStep;
        return (
          <div key={i} style={styles.stepCol}>
            <div
              style={{
                ...styles.stepCircle,
                ...(done ? styles.stepCircleDone : {}),
                ...(active ? styles.stepCircleActive : {}),
                cursor: clickable ? "pointer" : "default",
              }}
              onClick={() => clickable && onGoStep!(stepNum)}
              title={clickable ? `${s} 단계로 돌아가기` : undefined}
            >
              {done ? "✓" : stepNum}
            </div>
            <div
              style={{
                ...styles.stepLabel,
                color: active ? "#fff" : done ? "#a5b4fc" : "#555",
                fontWeight: active ? 700 : 500,
                cursor: clickable ? "pointer" : "default",
              }}
              onClick={() => clickable && onGoStep!(stepNum)}
            >
              {s}
            </div>
            {i < steps.length - 1 && (
              <div style={{ ...styles.stepConnector, background: stepNum < current ? "#6366f1" : "#2a2a2a" }} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function ProgressPanel({ phase, current, total, message }: {
  phase: string; current: number; total: number; message: string;
}) {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  const phaseLabel: Record<string, string> = {
    parsing: "시나리오 파싱",
    matching: "씬 매칭",
    tts: "TTS 생성",
  };
  return (
    <div style={styles.progressBox}>
      <div style={styles.progressPhase}>{phaseLabel[phase] ?? phase}</div>
      <div style={styles.progressBar}>
        <div style={{ ...styles.progressFill, width: `${pct}%` }} />
      </div>
      <div style={styles.progressMsg}>{message || "처리 중..."} {total > 0 && `(${current}/${total})`}</div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: { minHeight: "100vh", background: "#0f0f0f", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 },
  card: { background: "#1a1a1a", borderRadius: 16, padding: 40, width: "100%", maxWidth: 640, boxShadow: "0 4px 40px rgba(0,0,0,0.6)" },
  topBar: { display: "flex", justifyContent: "flex-start", marginBottom: 12 },
  topBackBtn: { padding: "6px 14px", background: "#252525", color: "#aaa", border: "1px solid #333", borderRadius: 6, fontSize: 13, cursor: "pointer" },
  reviewBanner: { padding: "10px 14px", background: "#1e293b", color: "#93c5fd", border: "1px solid #334155", borderRadius: 8, fontSize: 13, marginBottom: 8 },
  title: { color: "#fff", marginBottom: 32, fontSize: 28 },
  step: { display: "flex", flexDirection: "column", gap: 16 },
  input: { padding: "12px 16px", borderRadius: 8, border: "1px solid #333", background: "#111", color: "#fff", fontSize: 16, width: "100%", boxSizing: "border-box" },
  textarea: { padding: "12px 16px", borderRadius: 8, border: "1px solid #333", background: "#111", color: "#fff", fontSize: 14, width: "100%", boxSizing: "border-box", fontFamily: "monospace", resize: "vertical" },
  btn: { padding: "12px 28px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 8, fontSize: 16, cursor: "pointer", alignSelf: "flex-end" },
  btnSecondary: { padding: "12px 28px", background: "#333", color: "#aaa", border: "none", borderRadius: 8, fontSize: 16, cursor: "pointer" },
  row: { display: "flex", justifyContent: "space-between", marginTop: 8 },
  hint: { color: "#888", fontSize: 14, margin: 0 },
  error: { background: "#3b0e0e", color: "#f87171", padding: "12px 16px", borderRadius: 8, marginBottom: 16 },
  methodGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 },
  methodCard: { position: "relative", padding: 16, border: "2px solid #333", borderRadius: 10, cursor: "pointer", transition: "border-color 0.15s" },
  methodCardSelected: { borderColor: "#6366f1", background: "#1e1b4b" },
  // 인덱싱 미완료로 비활성화된 카드 — 선택 불가.
  methodCardDisabled: { opacity: 0.5, cursor: "not-allowed", background: "#181818" },
  disabledNotice: { marginTop: 8, padding: "8px 10px", background: "#3a2e0d", border: "1px solid #5a4a1a", borderRadius: 6, fontSize: 12, color: "#e5c07b" },
  disabledHint: { color: "#a08a4a", fontSize: 11 },
  methodLabel: { color: "#fff", fontWeight: 600, marginBottom: 4, fontSize: 14 },
  methodDesc: { color: "#888", fontSize: 12 },
  aiBadge: { position: "absolute", top: 10, right: 10, background: "#4f46e5", color: "#fff", fontSize: 10, padding: "2px 6px", borderRadius: 4 },
  // #355 후속 — Gemini 카드 하단 모델 셀렉트 + 비용 라벨.
  methodFooter: { display: "flex", flexDirection: "column", gap: 6, marginTop: 10, paddingTop: 10, borderTop: "1px solid #2a2a2a" },
  modelSelect: { padding: "6px 8px", background: "#111", color: "#fff", border: "1px solid #333", borderRadius: 4, fontSize: 12 },
  costLabel: { fontSize: 12, color: "#a5b4fc" },
  stepIndicator: { display: "flex", gap: 0, marginBottom: 32, justifyContent: "space-between" },
  // #292 — 강화된 stepper
  stepCol: { position: "relative", display: "flex", flexDirection: "column", alignItems: "center", flex: 1 },
  stepCircle: { width: 32, height: 32, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, fontWeight: 700, background: "#1a1a1a", border: "2px solid #2a2a2a", color: "#555", zIndex: 1 },
  stepCircleDone: { background: "#312e81", border: "2px solid #6366f1", color: "#c7d2fe" },
  stepCircleActive: { background: "#1e1b4b", border: "2px solid #818cf8", color: "#fff", boxShadow: "0 0 0 4px rgba(129,140,248,0.18)" },
  stepLabel: { fontSize: 11, marginTop: 6 },
  stepConnector: { position: "absolute", top: 15, left: "calc(50% + 20px)", right: "calc(-50% + 20px)", height: 2 },
  sceneCounter: { fontSize: 12, color: "#999", background: "#111", border: "1px solid #2a2a2a", borderRadius: 6, padding: "6px 10px" },
  counterWarn: { color: "#fbbf24" },
  progressBox: { marginTop: 24, display: "flex", flexDirection: "column", gap: 10 },
  progressPhase: { color: "#6366f1", fontWeight: 600, fontSize: 14 },
  progressBar: { height: 8, background: "#333", borderRadius: 4, overflow: "hidden" },
  progressFill: { height: "100%", background: "#6366f1", transition: "width 0.3s", borderRadius: 4 },
  progressMsg: { color: "#888", fontSize: 13 },
  step2Layout: { display: "flex", gap: 12, alignItems: "flex-start" },
  labelPanel: { width: 220, flexShrink: 0, background: "#111", border: "1px solid #2a2a2a", borderRadius: 8, padding: "12px 10px", display: "flex", flexDirection: "column", gap: 6, maxHeight: 480, overflowY: "auto" as const },
  labelPanelTitle: { color: "#6366f1", fontSize: 11, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: 0.5, marginBottom: 4 },
  labelItem: { color: "#ccc", fontSize: 12, padding: "4px 6px", background: "#1a1a1a", borderRadius: 4, display: "flex", alignItems: "center", gap: 8 },
  labelThumb: { width: 56, height: 32, objectFit: "cover" as const, borderRadius: 3, background: "#222", flexShrink: 0 },
  labelText: { wordBreak: "break-all" as const, minWidth: 0 },
  previewOverlay: { position: "fixed" as const, inset: 0, background: "rgba(0,0,0,0.75)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000, padding: 24 },
  previewModal: { background: "#181818", border: "1px solid #333", borderRadius: 8, padding: 12, maxWidth: "90vw", maxHeight: "90vh", display: "flex", flexDirection: "column" as const, gap: 8 },
  previewModalHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 },
  previewModalTitle: { color: "#eee", fontSize: 13, fontWeight: 600, wordBreak: "break-all" as const },
  previewModalClose: { background: "transparent", color: "#999", border: "none", fontSize: 16, cursor: "pointer", padding: "0 4px", flexShrink: 0 },
  previewModalMedia: { maxWidth: "86vw", maxHeight: "78vh", objectFit: "contain" as const, borderRadius: 4, background: "#000" },
};
