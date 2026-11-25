import { useState, useRef, useEffect } from "react";
import type { FootageAsset, FootageMarker, CropRect } from "../types";
import {
  updateAssetLabel as apiUpdateAssetLabel,
  updateAssetCrop,
  setDefaultCrop as apiSetDefaultCrop,
  deleteAsset as apiDeleteAsset,
  addStudioAssets,
  dedupeStudioAssets,
  getProject,
} from "../lib/project";
import {
  saveAsset,
  getCachedFileUrl,
  getProjectDirectory,
  requestProjectDirectory,
} from "../lib/fileStore";
import { isBrowserMode } from "../lib/processingMode";
import { GEMINI_ENABLED } from "../lib/features";
import { trimVideo, cropVideo } from "../lib/ffmpeg";
import { v4 as uuidv4 } from "uuid";
import { ui } from "../store/ui";
import {
  api,
  copyFootageFrom,
  syncStudioAssetFile,
  checkFootageConflicts,
  indexFootage,
  getIndexCostEstimate,
  getFootage,
  wsUrl,
  type FootageConflict,
  type FootageConflictPolicy,
} from "../api/client";
import FootageConflictDialog from "../components/FootageConflictDialog";
import { updateProject } from "../lib/project";
import LocalThumbnail from "../components/LocalThumbnail";
import ServerFootageThumbnail from "../components/ServerFootageThumbnail";
// #380 — RecordTab 을 ScreenRecorderDialog 로 추출. Studio 는 inline 모드로 동일 UX 유지.
//        VideoTimeline / TimelineSegment / tls 는 AssetsTab 의 구간 분할 UI 에서도 재사용한다.
import ScreenRecorderDialog, {
  VideoTimeline,
  tls,
  type TimelineSegment,
} from "../components/ScreenRecorderDialog";
// 구간 분할 보조 — interval 초 간격 프레임을 띠로 보여주고 클릭 시 seek.
import FilmstripBar from "../components/FilmstripBar";
// 구간 분할 보조 — 장면 설명을 텍스트로 검색해 클릭하면 그 시각으로 seek.
import SceneSearchPanel, { type SceneSearchPick } from "../components/SceneSearchPanel";
import MarkerCommentBar from "../components/MarkerCommentBar";

// 2026-06 — Setup/Editor 의 GEMINI_MODELS 목록과 동기화 유지(주석 책임).
const STUDIO_GEMINI_MODELS: { value: string; label: string }[] = [
  { value: "gemini-2.5-flash-lite", label: "Flash-Lite (저렴)" },
  { value: "gemini-2.5-flash",      label: "Flash (기본)" },
  { value: "gemini-2.5-pro",        label: "Pro (정확)" },
];
const STUDIO_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash";

type Tab = "record" | "presentation" | "assets";

// footage 스튜디오 간 복사/붙여넣기 클립보드 (localStorage 직렬화).
type StudioClipboardEntry = {
  source_project_id: string;
  asset_id: string;
  filename: string;
  label: string;
  asset_type: FootageAsset["asset_type"];
};

const STUDIO_CLIPBOARD_KEY = "studio_footage_clipboard";

// ── 자산 일괄 삭제 (다중 선택) ─────────────────────────────────────────────
// studio_assets 목록에서만 제거하고 footage 폴더의 실제 파일은 남긴다.
// 씬이 아직 참조 중인 파일이 있으면 scene_referenced_count 로 알려주므로
// 호출부가 사용자에게 경고할 수 있다.
interface BulkDeleteAssetsResult {
  deleted: number;
  not_found: string[];
  scene_referenced_count: number;
  scene_referenced_filenames: string[];
  files_kept: boolean;
}

const bulkDeleteStudioAssets = (projectId: string, assetIds: string[]) =>
  api
    .post<BulkDeleteAssetsResult>(
      `/api/projects/${projectId}/studio/assets/bulk-delete`,
      { asset_ids: assetIds },
    )
    .then((r) => r.data);

interface Props {
  projectId: string;
  projectName: string;
  onGoToScenario: () => void;
  onBack: () => void;
  initialAssetId?: string;
}

export default function Studio({ projectId, projectName, onGoToScenario, onBack, initialAssetId }: Props) {
  const [tab, setTab] = useState<Tab>(initialAssetId ? "assets" : "record");
  const [assets, setAssets] = useState<FootageAsset[]>([]);
  const [loadingAssets, setLoadingAssets] = useState(false);
  const [defaultCrop, setDefaultCrop] = useState<CropRect | null>(null);
  const [cropProcessing, setCropProcessing] = useState<string | null>(null);
  const [cropProgress, setCropProgress] = useState(0);
  // 현재 선택된 폴더 핸들 이름
  const [directoryName, setDirectoryName] = useState<string | null>(null);
  // footage 복사 클립보드 — 다른 프로젝트 스튜디오로 붙여넣기. localStorage로 프로젝트 간 유지.
  const [clipboard, setClipboard] = useState<StudioClipboardEntry | null>(() => {
    try {
      const raw = localStorage.getItem(STUDIO_CLIPBOARD_KEY);
      return raw ? (JSON.parse(raw) as StudioClipboardEntry) : null;
    } catch {
      return null;
    }
  });
  const [pasting, setPasting] = useState(false);
  // 기존 footage_dir 에서 가져오기 / 파일 선택 가져오기 진행 표시.
  //   파일이 크거나 여러개면 서버 업로드에 수초~수분 걸릴 수 있어 UI 피드백 필수.
  const [importProgress, setImportProgress] = useState<
    { current: number; total: number; filename: string } | null
  >(null);
  // 파일명 충돌 해결 다이얼로그 — 사용자가 결정할 때까지 업로드를 붙잡아 둔다.
  // `resolve` 는 다이얼로그의 선택 결과를 대기 중인 import 흐름으로 넘기는 콜백.
  const [conflictPrompt, setConflictPrompt] = useState<{
    conflicts: FootageConflict[];
    cleanCount: number;
    resolve: (d: Record<string, FootageConflictPolicy> | null) => void;
  } | null>(null);

  /**
   * 충돌 파일이 있으면 다이얼로그를 띄우고 사용자의 결정을 기다린다.
   * 충돌이 없으면 즉시 빈 결정으로 통과 — 사용자를 불필요하게 붙잡지 않는다.
   * 취소 시 null 을 반환해 호출부가 가져오기 전체를 중단하게 한다.
   */
  async function askConflictDecisions(
    names: string[],
  ): Promise<Record<string, FootageConflictPolicy> | null> {
    let conflicts: FootageConflict[] = [];
    try {
      conflicts = await checkFootageConflicts(projectId, names);
    } catch (err) {
      // 검사 실패 시 기존 동작(skip)으로 안전하게 진행 — 가져오기를 막지는 않는다.
      console.warn("[import] 충돌 검사 실패 — skip 정책으로 진행", err);
      return {};
    }
    if (conflicts.length === 0) return {};
    return new Promise((resolve) => {
      setConflictPrompt({
        conflicts,
        cleanCount: names.length - conflicts.length,
        resolve: (d) => { setConflictPrompt(null); resolve(d); },
      });
    });
  }

  // 초기 assets + 디렉토리 상태 로드
  useEffect(() => {
    setLoadingAssets(true);
    getProject(projectId)
      .then((p) => {
        setAssets(p.studio_assets);
        setDefaultCrop(p.default_crop ?? null);
      })
      .catch(() => {})
      .finally(() => setLoadingAssets(false));
    getProjectDirectory(projectId).then((h) => {
      setDirectoryName(h ? h.name : null);
    });
  }, [projectId]);

  async function handleChangeDirectory() {
    try {
      const handle = await requestProjectDirectory(projectId);
      setDirectoryName(handle.name);
      // assets 새로고침
      setLoadingAssets(true);
      const p = await getProject(projectId);
      setAssets(p.studio_assets);
      setDefaultCrop(p.default_crop ?? null);
      setLoadingAssets(false);
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        console.error("폴더 변경 실패:", e);
      }
    }
  }

  function handleAssetsUpdated(newAssets: FootageAsset[]) {
    setAssets((prev) => [...prev, ...newAssets]);
    setTab("assets");
  }

  // footage 항목을 클립보드에 복사 — 다른 프로젝트 스튜디오에서 붙여넣기 위해 기록.
  function handleCopyAsset(asset: FootageAsset) {
    const entry: StudioClipboardEntry = {
      source_project_id: projectId,
      asset_id: asset.asset_id,
      filename: asset.filename,
      label: asset.label,
      asset_type: asset.asset_type,
    };
    try {
      localStorage.setItem(STUDIO_CLIPBOARD_KEY, JSON.stringify(entry));
    } catch { /* ignore */ }
    setClipboard(entry);
    ui.success(`"${asset.label || asset.filename}" 복사됨 — 다른 프로젝트 스튜디오에서 붙여넣기 하세요`);
  }

  // 클립보드의 footage를 이 프로젝트로 복사한다.
  // 서버가 소스 footage_dir→타겟 footage_dir로 실제 파일을 복사·등록한다(서버→서버).
  // 파일이 서버 footage_dir에 들어가므로 에디터 미리보기/렌더링과 일관된다.
  //
  // #381 — 이전에는 `copyFootageFrom`(백엔드가 이미 studio_assets 에 등록 완료) 직후
  // 프론트가 `addStudioAssets` 로 또 등록해 동일 asset_id 가 2번 들어가는 버그가 있었다.
  // 이제 서버 등록은 백엔드에 일임하고, 프론트는 `getProject` 로 서버 최신본을 받아
  // IndexedDB·UI 상태를 동기화한다(getProject 가 dedup 도 함께 적용).
  async function handlePasteAsset() {
    if (!clipboard || pasting) return;
    setPasting(true);
    try {
      const newAsset = await copyFootageFrom(projectId, {
        source_project_id: clipboard.source_project_id,
        asset_id: clipboard.asset_id,
      });
      // 백엔드가 이미 studio_assets 에 신규 항목을 추가했으므로 서버에서 최신본을 받아 반영.
      const p = await getProject(projectId);
      setAssets(p.studio_assets);
      setTab("assets");
      ui.success(`"${newAsset.label || newAsset.filename}" 붙여넣기 완료`);
    } catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 404) {
        // 원본 프로젝트/파일이 사라진 stale 클립보드 — 비운다.
        try { localStorage.removeItem(STUDIO_CLIPBOARD_KEY); } catch { /* ignore */ }
        setClipboard(null);
        ui.error("원본 footage를 찾을 수 없습니다. 클립보드를 비웠습니다.");
      } else {
        console.error("[paste] 붙여넣기 실패:", e);
        ui.error(`붙여넣기 실패: ${(e as Error).message || e}`);
      }
    } finally {
      setPasting(false);
    }
  }

  async function handleDeleteAsset(assetId: string) {
    const ok = await ui.confirm({
      title: "목록에서 제거",
      message: "이 항목을 목록에서 제거하시겠습니까? (실제 파일은 유지됩니다)",
      variant: "warning",
      confirmLabel: "제거",
    });
    if (!ok) return;
    await apiDeleteAsset(projectId, assetId);
    setAssets((prev) => prev.filter((a) => a.asset_id !== assetId));
  }

  async function handleDeleteAllAssets() {
    if (assets.length === 0) return;
    const ok = await ui.confirm({
      title: "전체 목록에서 제거",
      message: `등록된 ${assets.length}개 자산을 모두 목록에서 제거하시겠습니까? (footage_dir 실제 파일은 유지됩니다)`,
      variant: "warning",
      confirmLabel: "전체 제거",
    });
    if (!ok) return;
    // updateProject 로 studio_assets=[] 통째 저장 — 개별 DELETE 반복 대비 단일 요청.
    const { updateProject } = await import("../lib/project");
    await updateProject(projectId, { studio_assets: [] });
    setAssets([]);
    ui.success("모든 자산을 목록에서 제거했습니다.");
  }

  async function handleLabelChange(assetId: string, label: string) {
    await apiUpdateAssetLabel(projectId, assetId, label);
    setAssets((prev) => prev.map((a) => (a.asset_id === assetId ? { ...a, label } : a)));
  }

  async function handleCropChange(assetId: string, crop: CropRect | null) {
    const asset = assets.find((a) => a.asset_id === assetId);
    if (isBrowserMode() && crop && asset) {
      setCropProcessing(assetId);
      try {
        const srcBlob = await (await fetch(await getCachedFileUrl(projectId, asset.filename))).blob();
        const croppedBlob = await cropVideo(srcBlob, crop, (p) => setCropProgress(Math.round(p * 100)));
        const newAsset = await saveAsset(projectId, croppedBlob,
          `${asset.filename.replace(/\.[^.]+$/, "")}_cropped.mp4`, asset.label, "split");
        const p = await addStudioAssets(projectId, [newAsset]);
        setAssets(p.studio_assets);
      } finally {
        setCropProcessing(null);
        setCropProgress(0);
      }
      return;
    }
    // 서버 모드: 메타데이터만 저장
    const updated = await updateAssetCrop(projectId, assetId, crop);
    const updatedAsset = (updated as unknown as { studio_assets?: FootageAsset[] }).studio_assets?.find((a) => a.asset_id === assetId);
    if (updatedAsset) setAssets((prev) => prev.map((a) => (a.asset_id === assetId ? updatedAsset : a)));
  }

  async function handleSetDefaultCrop(crop: CropRect | null, applyToAll: boolean) {
    const p = await apiSetDefaultCrop(projectId, crop, applyToAll);
    setDefaultCrop(p.default_crop ?? null);
    if (applyToAll) setAssets(p.studio_assets);
  }

  async function handleImportExisting() {
    try {
      const dir = await getProjectDirectory(projectId) ?? await requestProjectDirectory(projectId);
      setDirectoryName(dir.name);
      const now = new Date().toISOString();
      const VIDEO_EXTS = new Set([".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"]);
      const IMAGE_EXTS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp"]);
      // #289/#290 — 오디오 자산 자동 분류 (BGM/SFX 공용)
      const AUDIO_EXTS = new Set([".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"]);
      // 1단계: 업로드 대상 파일 목록 먼저 수집 (진행률 total 확정용).
      const candidates: { name: string; handle: FileSystemFileHandle; assetType: FootageAsset["asset_type"] }[] = [];
      for await (const [name, handle] of dir.entries()) {
        const ext = name.match(/\.[^.]+$/)?.[0].toLowerCase() ?? "";
        if (!VIDEO_EXTS.has(ext) && !IMAGE_EXTS.has(ext) && !AUDIO_EXTS.has(ext)) continue;
        if ((handle as FileSystemHandle).kind !== "file") continue;
        const asset_type: FootageAsset["asset_type"] = VIDEO_EXTS.has(ext)
          ? "video"
          : AUDIO_EXTS.has(ext)
            ? "audio"
            : "image";
        candidates.push({ name, handle: handle as FileSystemFileHandle, assetType: asset_type });
      }
      if (candidates.length === 0) { ui.info("가져올 수 있는 파일이 없습니다(형식 미지원)."); return; }
      // 2단계: 충돌 확인 — 사용자가 취소하면 아무것도 가져오지 않는다.
      const decisions = await askConflictDecisions(candidates.map((c) => c.name));
      if (decisions === null) { ui.info("가져오기를 취소했습니다."); return; }
      // 3단계: 순차 업로드 + 진행률 갱신.
      setImportProgress({ current: 0, total: candidates.length, filename: candidates[0].name });
      // 업로드 필터가 아니라 자산 카드 중복 방지용 — 덮어쓴 파일은 카드를 새로 만들지 않는다.
      const existingNames = new Set(assets.map((a) => a.filename));
      const newAssets: FootageAsset[] = [];
      let syncFailed = 0;
      let skipped = 0;
      try {
        for (let i = 0; i < candidates.length; i++) {
          const { name, handle, assetType } = candidates[i];
          const policy = decisions[name] ?? "skip";
          if (policy === "skip" && decisions[name]) { skipped++; continue; }
          setImportProgress({ current: i, total: candidates.length, filename: name });
          // 서버가 정한 최종 파일명을 그대로 등록해야 `(2)` 파일이 목록에 보인다.
          let savedName = name;
          try {
            const file = await handle.getFile();
            const res = await syncStudioAssetFile(projectId, file, name, policy);
            if (res.skipped) { skipped++; continue; }
            savedName = res.filename;
          } catch (err) {
            syncFailed++;
            console.warn(`[import] 서버 동기화 실패(로컬에는 있음): ${name}`, err);
          }
          // 덮어쓰기는 파일만 교체 — 같은 이름의 카드가 이미 있으면 새로 만들지 않는다.
          if (existingNames.has(savedName)) continue;
          existingNames.add(savedName);
          newAssets.push({
            asset_id: uuidv4(),
            filename: savedName,
            label: "",
            asset_type: assetType,
            source: "imported",
            created_at: now,
            crop: null,
          });
        }
        setImportProgress({ current: candidates.length, total: candidates.length, filename: "등록 중…" });
        if (newAssets.length > 0) {
          const p = await addStudioAssets(projectId, newAssets);
          setAssets(p.studio_assets);
        }
      } finally {
        setImportProgress(null);
      }
      const skipNote = skipped > 0 ? ` (${skipped}개 건너뜀)` : "";
      if (syncFailed > 0) {
        ui.error(`${newAssets.length}개 가져왔으나 ${syncFailed}개는 서버 저장 실패 — 미리보기가 안 될 수 있습니다.`);
      } else if (newAssets.length === 0) {
        ui.info(`가져온 파일이 없습니다${skipNote}.`);
      } else {
        ui.success(`${newAssets.length}개 파일을 가져왔습니다.${skipNote}`);
      }
    } catch (e) {
      setImportProgress(null);
      if ((e as Error).name !== "AbortError") ui.error("가져오기에 실패했습니다.");
    }
  }

  // 파일 선택 가져오기 — <input type="file" multiple>로 사용자가 고른 파일만 등록.
  // 서버 footage_dir에 같은 이름의 파일이 있으면 충돌 다이얼로그로 사용자에게 묻는다
  // (둘 다 유지 / 덮어쓰기 / 건너뛰기). 예전처럼 조용히 무시하지 않는다.
  async function handleImportFiles(files: FileList | File[]) {
    const arr = Array.from(files);
    if (arr.length === 0) return;
    const VIDEO_EXTS = new Set([".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"]);
    const IMAGE_EXTS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp"]);
    const AUDIO_EXTS = new Set([".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"]);
    const now = new Date().toISOString();
    // 지원 형식만 남긴다 — 중복 여부로는 더 이상 거르지 않는다(충돌은 사용자에게 질문).
    const targets = arr.filter((f) => {
      const ext = f.name.match(/\.[^.]+$/)?.[0].toLowerCase() ?? "";
      return VIDEO_EXTS.has(ext) || IMAGE_EXTS.has(ext) || AUDIO_EXTS.has(ext);
    });
    const skippedUnknown = arr.length - targets.length;
    if (targets.length === 0) {
      ui.info(`지원하지 않는 파일 형식 ${skippedUnknown}개 — 가져오지 못했습니다.`);
      return;
    }
    const decisions = await askConflictDecisions(targets.map((f) => f.name));
    if (decisions === null) { ui.info("가져오기를 취소했습니다."); return; }

    // 업로드 필터가 아니라 자산 카드 중복 방지용 — 덮어쓴 파일은 카드를 새로 만들지 않는다.
    const existingNames = new Set(assets.map((a) => a.filename));
    const newAssets: FootageAsset[] = [];
    let syncFailed = 0;
    let skipped = 0;
    setImportProgress({ current: 0, total: targets.length, filename: targets[0].name });
    try {
      for (let i = 0; i < targets.length; i++) {
        const file = targets[i];
        const name = file.name;
        const ext = name.match(/\.[^.]+$/)?.[0].toLowerCase() ?? "";
        const asset_type: FootageAsset["asset_type"] = VIDEO_EXTS.has(ext)
          ? "video"
          : AUDIO_EXTS.has(ext)
            ? "audio"
            : "image";
        // 충돌 목록에 없던 파일은 decisions 에 키가 없다 — 그대로 저장(정책 무관).
        const policy = decisions[name] ?? "skip";
        if (policy === "skip" && decisions[name]) { skipped++; continue; }
        setImportProgress({ current: i, total: targets.length, filename: name });
        let savedName = name;
        try {
          const res = await syncStudioAssetFile(projectId, file, name, policy);
          // 서버가 skip 했으면 파일이 안 올라간 것 — "가져왔다"고 표시하면 거짓말이 된다.
          if (res.skipped) { skipped++; continue; }
          savedName = res.filename;  // rename 시 `이름 (2).mp4`
        } catch (err) {
          syncFailed++;
          console.warn(`[import-files] 서버 업로드 실패: ${name}`, err);
          continue; // 서버 저장 실패면 등록도 안 함(미리보기 불가)
        }
        // 덮어쓰기는 파일 내용만 교체 — 같은 이름의 자산 카드가 이미 있으면
        // 새 카드를 만들지 않는다(dedupe 는 asset_id 기준이라 중복 카드가 생긴다).
        if (existingNames.has(savedName)) continue;
        existingNames.add(savedName);
        newAssets.push({
          asset_id: uuidv4(),
          filename: savedName,
          label: "",
          asset_type,
          source: "imported",
          created_at: now,
          crop: null,
        });
      }
      setImportProgress({ current: targets.length, total: targets.length, filename: "등록 중…" });
      if (newAssets.length > 0) {
        const p = await addStudioAssets(projectId, newAssets);
        setAssets(p.studio_assets);
      }
    } finally {
      setImportProgress(null);
    }
    const failNote = syncFailed > 0 ? ` (${syncFailed}개 업로드 실패 제외)` : "";
    const skipNote = skipped > 0 ? ` (${skipped}개 건너뜀)` : "";
    if (newAssets.length === 0) {
      ui.info(`가져온 파일이 없습니다.${skipNote}${failNote}`);
    } else {
      ui.success(`${newAssets.length}개 파일을 가져왔습니다.${skipNote}${failNote}`);
    }
  }

  return (
    <div style={s.root}>
      {/* Header */}
      <div style={s.header}>
        <button style={s.backBtn} onClick={onBack}>← 홈</button>
        <div style={s.headerCenter}>
          <span style={s.headerTitle}>footage 스튜디오</span>
          <span style={s.headerSub}>{projectName}</span>
        </div>
        <button style={s.goBtn} onClick={onGoToScenario}>
          시나리오 작성으로 이동 →
        </button>
      </div>

      {/* footage 폴더 표시 / 변경 */}
      <div style={s.dirBar}>
        <span style={s.dirLabel}>footage 폴더</span>
        <span style={s.dirPath}>{directoryName ?? "폴더 미설정"}</span>
        <button style={s.dirEditBtn} onClick={handleChangeDirectory}>
          폴더 변경
        </button>
      </div>

      {/* Tabs */}
      <div style={s.tabs}>
        {/* #313 — presentation 탭을 활성화. PDF/HTML 을 브라우저에서 직접
            캡처하여 백엔드 Playwright 호출(=비용)을 건너뛴다. */}
        {(["record", "presentation", "assets"] as Tab[]).map((t) => (
          <button
            key={t}
            style={{ ...s.tab, ...(tab === t ? s.tabActive : {}) }}
            onClick={() => setTab(t)}
          >
            {t === "record" && "화면 녹화"}
            {t === "presentation" && "프레젠테이션"}
            {t === "assets" && `footage 목록 (${assets.length})`}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div style={s.content}>
        {tab === "record" && (
          // #380 — RecordTab 을 ScreenRecorderDialog 로 추출. 탭 내부 표시는 inline 모드를 사용해
          //        기존 UX (탭 안에 그대로 펼침)를 그대로 유지한다.
          <ScreenRecorderDialog
            open
            mode="inline"
            projectId={projectId}
            onClose={() => { /* 탭 모드: 별도 닫기 없음 */ }}
            onSaved={handleAssetsUpdated}
          />
        )}
        {tab === "presentation" && (
          <PresentationTab
            projectId={projectId}
            onAssetsAdded={handleAssetsUpdated}
          />
        )}
        {tab === "assets" && (
          <AssetsTab
            projectId={projectId}
            assets={assets}
            loading={loadingAssets}
            defaultCrop={defaultCrop}
            cropProcessingId={cropProcessing}
            cropProgress={cropProgress}
            onDelete={handleDeleteAsset}
            onLabelChange={handleLabelChange}
            onCropChange={handleCropChange}
            onSetDefaultCrop={handleSetDefaultCrop}
            onImportExisting={handleImportExisting}
            onImportFiles={handleImportFiles}
            onAssetsAdded={handleAssetsUpdated}
            onAssetsReplaced={(a) => setAssets(a)}
            initialAssetId={initialAssetId}
            onCopyAsset={handleCopyAsset}
            onPasteAsset={handlePasteAsset}
            clipboard={clipboard}
            pasting={pasting}
            importProgress={importProgress}
            onDeleteAll={handleDeleteAllAssets}
          />
        )}
      </div>
      {/* 파일명 충돌 해결 — 가져오기 흐름이 사용자의 결정을 기다리는 동안 표시된다. */}
      <FootageConflictDialog
        open={conflictPrompt !== null}
        conflicts={conflictPrompt?.conflicts ?? []}
        cleanCount={conflictPrompt?.cleanCount ?? 0}
        onCancel={() => conflictPrompt?.resolve(null)}
        onResolve={(d) => conflictPrompt?.resolve(d)}
      />
    </div>
  );
}

// ── footage 목록 탭 ─────────────────────────────────────────────────────────

function AssetsTab({
  projectId,
  assets,
  loading,
  defaultCrop,
  cropProcessingId,
  cropProgress,
  onDelete,
  onLabelChange,
  onCropChange,
  onSetDefaultCrop,
  onImportExisting,
  onImportFiles,
  onAssetsAdded,
  onAssetsReplaced,
  initialAssetId,
  onCopyAsset,
  onPasteAsset,
  clipboard,
  pasting,
  importProgress,
  onDeleteAll,
}: {
  projectId: string;
  assets: FootageAsset[];
  loading: boolean;
  defaultCrop: CropRect | null;
  cropProcessingId: string | null;
  cropProgress: number;
  onDelete: (id: string) => void;
  onLabelChange: (id: string, label: string) => void;
  onCropChange: (id: string, crop: CropRect | null) => void;
  onSetDefaultCrop: (crop: CropRect | null, applyToAll: boolean) => void;
  onImportExisting: () => void;
  onImportFiles: (files: FileList | File[]) => Promise<void> | void;
  onAssetsAdded: (a: FootageAsset[]) => void;
  // 목록을 서버 최신본으로 통째 교체 (일괄 삭제·분할 저장 후 동기화용).
  onAssetsReplaced: (a: FootageAsset[]) => void;
  initialAssetId?: string;
  onCopyAsset: (asset: FootageAsset) => void;
  onPasteAsset: () => void;
  clipboard: StudioClipboardEntry | null;
  pasting: boolean;
  importProgress: { current: number; total: number; filename: string } | null;
  onDeleteAll: () => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [editLabel, setEditLabel] = useState("");
  const [splitTarget, setSplitTarget] = useState<FootageAsset | null>(null);
  const [splitSegments, setSplitSegments] = useState<TimelineSegment[]>([]);
  const [splitSaving, setSplitSaving] = useState(false);
  const [splitError, setSplitError] = useState("");
  const [splitUrl, setSplitUrl] = useState<string | null>(null);
  // ── 구간 분할 화면의 라벨·전체 코멘트 편집 ────────────────────────────────
  // 진입한 자산이 바뀔 때만 서버 최신값으로 채운다(입력 중 assets 갱신으로 덮이지 않게).
  const [splitLabel, setSplitLabel] = useState("");
  const [splitNote, setSplitNote] = useState("");
  const [splitInfoSaving, setSplitInfoSaving] = useState(false);
  // ── 프레임 스트립 ────────────────────────────────────────────────────────
  // VideoTimeline 이 <video> 를 내부에서 소유하므로(ref 미노출), 래퍼 div 를 통해
  // 실제 엘리먼트를 찾아 seek 하고 재생 위치를 구독한다. VideoTimeline 은 그대로 둔다.
  const splitPlayerWrapRef = useRef<HTMLDivElement>(null);
  const [splitCurrentTime, setSplitCurrentTime] = useState(0);
  const [splitDuration, setSplitDuration] = useState(0);
  const [cropTarget, setCropTarget] = useState<FootageAsset | null>(null);
  const [cropUrl, setCropUrl] = useState<string | null>(null);

  // ── 인덱싱 (2026-06) ─────────────────────────────────────────────────────
  // 프로젝트의 gemini_model 을 진실의 원천으로 사용. 카드 셀렉트에서 바꾸면 즉시
  // 서버/IndexedDB 동기화하고, estimate API 를 재호출하여 라벨을 갱신한다.
  const [indexGeminiModel, setIndexGeminiModel] = useState<string>(STUDIO_DEFAULT_GEMINI_MODEL);
  const [indexEstimate, setIndexEstimate] = useState<{
    krw: number; usd: number; file_count: number; cached_file_count: number;
    total_duration_sec: number;
  } | null>(null);
  const [indexRunning, setIndexRunning] = useState(false);
  const [indexProgress, setIndexProgress] = useState<{ current: number; total: number; message: string } | null>(null);
  const [indexedCount, setIndexedCount] = useState<number>(0);
  const indexCostReqIdRef = useRef(0);
  // estimate API 호출 중 표시용 플래그. 모델 변경 시 즉시 true → 응답 도착 시 false.
  // 빠르게 모델을 두 번 바꾸면 이전 호출이 도착해도 reqId 가드에 의해 setIndexCostLoading(false)가
  // 실행되지 않으므로, 항상 최신 호출의 응답에서만 로딩이 꺼진다.
  const [indexCostLoading, setIndexCostLoading] = useState(false);

  // 최초 + assets 변경 시 인덱싱된 파일 수도 같이 갱신(=서버 footage chunks 길이는
  // assets 와 무관하므로 estimate API 응답의 cached_file_count 를 그대로 신뢰).
  useEffect(() => {
    let cancelled = false;
    getProject(projectId).then((p) => {
      if (cancelled) return;
      const m = (p as { gemini_model?: string }).gemini_model;
      if (m) setIndexGeminiModel(m);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    const reqId = ++indexCostReqIdRef.current;
    // 새 호출 시작 → 라벨을 즉시 "계산 중…" 으로 전환. 이전 값을 보여주거나
    // 깜빡이는 dash 가 보이지 않도록 한다.
    setIndexCostLoading(true);
    getIndexCostEstimate(projectId, indexGeminiModel)
      .then((r) => {
        if (reqId !== indexCostReqIdRef.current) return;
        setIndexEstimate({
          krw: r.krw, usd: r.usd,
          file_count: r.file_count,
          cached_file_count: r.cached_file_count,
          total_duration_sec: r.total_duration_sec,
        });
        setIndexedCount(r.cached_file_count);
        setIndexCostLoading(false);
      })
      .catch(() => {
        if (reqId !== indexCostReqIdRef.current) return;
        setIndexEstimate(null);
        setIndexCostLoading(false);
      });
  }, [projectId, indexGeminiModel, assets.length]);

  async function handleSelectIndexModel(m: string) {
    setIndexGeminiModel(m);
    try {
      await updateProject(projectId, { gemini_model: m });
    } catch (e) {
      console.warn("[Studio] gemini_model 저장 실패:", e);
    }
  }

  async function handleStartIndexing(force: boolean) {
    if (indexRunning) return;
    setIndexRunning(true);
    setIndexProgress({ current: 0, total: 1, message: "인덱싱 시작..." });
    try {
      const taskId = await indexFootage(projectId, force);
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
              setIndexProgress({
                current: msg.current ?? 0,
                total: msg.total ?? 1,
                message: msg.message ?? "인덱싱 중...",
              });
            } else if (msg.type === "complete") {
              ui.success(`인덱싱 완료: ${msg.summary?.total ?? 0}개 청크`);
              finish();
            } else if (msg.type === "error") {
              ui.error(`인덱싱 실패: ${msg.message ?? msg.code ?? "알 수 없는 오류"}`);
              finish();
            }
          } catch { /* ignore */ }
        };
        ws.onclose = finish;
        ws.onerror = finish;
      });
      // estimate 재호출 — cached_file_count 갱신.
      try {
        const r = await getIndexCostEstimate(projectId, indexGeminiModel);
        setIndexEstimate({
          krw: r.krw, usd: r.usd,
          file_count: r.file_count,
          cached_file_count: r.cached_file_count,
          total_duration_sec: r.total_duration_sec,
        });
        setIndexedCount(r.cached_file_count);
      } catch { /* ignore */ }
    } finally {
      setIndexRunning(false);
      setIndexProgress(null);
    }
  }
  // #272: 로컬 파일명 → File 객체 매핑. AssetThumb의 브라우저 썸네일 생성에 사용.
  //       디렉토리 핸들 확보 시 비동기로 채워진다. 빈 Map이면 origin="local" 분기를 타지 않으므로
  //       기존 서버 API 경로로 폴백되어 회귀 위험은 없다.
  const [localFootageFileMap, setLocalFootageFileMap] = useState<Map<string, File>>(new Map());
  // 2026-06 — 인덱싱 결과(filename → description/keywords) 매핑. 자산 카드에 표시.
  //   • 백엔드 /api/projects/{id}/footage 응답의 첫 청크에서 추출 (파일 1개 = 청크 1개 정책).
  //   • 미인덱싱 파일은 description="" 이므로 카드에 description 블록이 렌더링되지 않는다.
  //   • indexedCount 변경 시(=인덱싱 직후) 자동 재fetch.
  const [chunkInfoMap, setChunkInfoMap] = useState<Map<string, { description: string; keywords: string[] }>>(new Map());
  // #299 DoD 3 — 라벨 미입력 안내 배너 닫힘 상태.
  const [labelBannerDismissed, setLabelBannerDismissed] = useState(false);
  // #299 DoD 4 — 자산 정렬 모드.
  const [sortMode, setSortMode] = useState<"default" | "unlabeled-first">("default");

  // ── 분할 출처 추적 (split_from / has_splits) ─────────────────────────────
  // 두 필드는 백엔드 FootageAsset 에 추가된 신규 선택 필드다. 손으로 관리하는
  // `types/index.ts` 의 FootageAsset 에는 아직 없으므로, 여기서 구조적으로
  // 좁혀 읽는다(값이 없는 기존 자산은 각각 ""/false 로 취급).
  const splitFromOf = (a: FootageAsset): string =>
    typeof (a as { split_from?: unknown }).split_from === "string"
      ? (a as { split_from: string }).split_from
      : "";
  const hasSplitsOf = (a: FootageAsset): boolean =>
    (a as { has_splits?: unknown }).has_splits === true;

  // ── 다중 선택 삭제 ───────────────────────────────────────────────────────
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkDeleting, setBulkDeleting] = useState(false);

  // 목록에서 사라진 자산의 선택 id 가 남아 있어도 무해하다 — 아래 selectedAssets 가
  // 항상 살아있는 assets 와 교집합을 취하므로, 별도 정리 effect 를 두지 않는다.
  function toggleSelected(assetId: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  }

  // #299 — 라벨이 비어있는 자산 수.
  const unlabeledCount = assets.filter((a) => !a.label).length;
  // #299 DoD 4 — 정렬 적용된 표시용 목록 (원본 assets 는 보존).
  const displayedAssets =
    sortMode === "unlabeled-first"
      ? [...assets].sort((a, b) => Number(!!a.label) - Number(!!b.label))
      : assets;

  // 실제 선택된 자산 — 선택 id 와 살아있는 assets 의 교집합. 삭제 후 남은
  // 유령 id 는 여기서 자연히 걸러지므로 화면 숫자와 요청 건수가 항상 일치한다.
  const selectedAssets = assets.filter((a) => selectedIds.has(a.asset_id));
  // 전체선택/해제는 **현재 표시 중인 목록** 기준으로 동작한다.
  const allDisplayedSelected =
    displayedAssets.length > 0 && displayedAssets.every((a) => selectedIds.has(a.asset_id));
  // "분할당한 원본만 선택" 의 대상 수 — 0 이면 버튼을 숨긴다.
  const splitParentCount = assets.filter(hasSplitsOf).length;

  function toggleSelectAllDisplayed() {
    setSelectedIds((prev) => {
      if (allDisplayedSelected) {
        const next = new Set(prev);
        for (const a of displayedAssets) next.delete(a.asset_id);
        return next;
      }
      const next = new Set(prev);
      for (const a of displayedAssets) next.add(a.asset_id);
      return next;
    });
  }

  // 사용자의 실제 목적 — 구간분할의 재료로 쓰인 원본만 한 번에 고른다.
  function selectSplitParentsOnly() {
    setSelectedIds(new Set(assets.filter(hasSplitsOf).map((a) => a.asset_id)));
  }

  async function handleBulkDelete() {
    if (bulkDeleting) return;
    const ids = selectedAssets.map((a) => a.asset_id);
    if (ids.length === 0) return;

    const ok = await ui.confirm({
      title: `선택한 ${ids.length}개 자산 제거`,
      message:
        `선택한 ${ids.length}개 자산을 목록에서 제거하시겠습니까?\n` +
        "목록에서만 제거되며 footage 폴더의 파일은 남습니다.",
      variant: "warning",
      confirmLabel: `${ids.length}개 제거`,
    });
    if (!ok) return;

    setBulkDeleting(true);
    try {
      const res = await bulkDeleteStudioAssets(projectId, ids);
      // 서버가 진실의 원천 — 삭제 후 최신 목록을 받아 IndexedDB·UI 를 동기화한다.
      // (서버가 분할 원본의 has_splits 를 함께 정리하므로 로컬 필터만으로는 어긋난다.)
      const p = await getProject(projectId);
      onAssetsReplaced(p.studio_assets);
      setSelectedIds(new Set());
      if (res.scene_referenced_count > 0) {
        ui.error(
          `${res.deleted}개 제거됨 — 다만 씬 ${res.scene_referenced_count}개가 아직 이 파일들을 ` +
          "참조 중입니다(파일은 남아 있어 렌더는 계속 동작합니다).",
        );
      } else {
        ui.success(`${res.deleted}개 자산을 목록에서 제거했습니다 (파일은 유지).`);
      }
    } catch (e) {
      ui.error(`선택 삭제 실패: ${(e as Error).message || e}`);
    } finally {
      setBulkDeleting(false);
    }
  }

  // #272: 디렉토리 핸들에서 모든 파일을 File로 변환해 맵 구축.
  useEffect(() => {
    let cancelled = false;
    getProjectDirectory(projectId).then(async (dir) => {
      if (!dir || cancelled) return;
      const map = new Map<string, File>();
      try {
        for await (const [name, entry] of dir.entries()) {
          if (cancelled) return;
          if ((entry as FileSystemHandle).kind !== "file") continue;
          try {
            const file = await (entry as FileSystemFileHandle).getFile();
            map.set(name, file);
          } catch {
            // 개별 파일 실패는 무시
          }
        }
      } catch {
        // enumeration 실패 무시
      }
      if (!cancelled) setLocalFootageFileMap(map);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [projectId]);

  // 2026-06 — 인덱싱된 청크 정보(description/keywords) 로드.
  //   • indexedCount 가 변경되면 자동 재fetch → 인덱싱 완료 직후 카드가 바로 갱신.
  //   • assets.length 도 의존성에 포함하여 자산이 새로 추가/삭제될 때도 동기화.
  //   • 응답 실패 시 빈 Map 유지(미인덱싱 상태로 표시).
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    getFootage(projectId)
      .then((items) => {
        if (cancelled) return;
        const map = new Map<string, { description: string; keywords: string[] }>();
        for (const it of items) {
          // 파일 1개 = 청크 1개 정책. 첫 청크의 description/keywords 만 사용.
          const c = (it.chunks ?? [])[0];
          if (!c) continue;
          const desc = (c.description ?? "").trim();
          const kws = Array.isArray(c.keywords) ? c.keywords.filter((k): k is string => typeof k === "string" && !!k.trim()) : [];
          if (!desc && kws.length === 0) continue;
          map.set(it.filename, { description: desc, keywords: kws });
        }
        setChunkInfoMap(map);
      })
      .catch((e) => {
        // 미인덱싱·네트워크 오류 모두 빈 Map → 카드 description 블록 미표시(회귀 안전).
        console.warn("[Studio] getFootage 실패(인덱싱 정보 미표시):", e);
      });
    return () => { cancelled = true; };
  }, [projectId, indexedCount, assets.length]);

  // initialAssetId가 있으면 해당 에셋을 구간분할 편집 화면으로 바로 진입
  useEffect(() => {
    if (!initialAssetId || assets.length === 0) return;
    const target = assets.find((a) => a.asset_id === initialAssetId);
    if (target) { setSplitTarget(target); setSplitSegments([]); }
  }, [initialAssetId, assets]);

  useEffect(() => {
    if (!splitTarget) { setSplitUrl(null); return; }
    getCachedFileUrl(projectId, splitTarget.filename).then(setSplitUrl).catch(() => setSplitUrl(null));
  }, [splitTarget, projectId]);

  useEffect(() => {
    if (!cropTarget) { setCropUrl(null); return; }
    getCachedFileUrl(projectId, cropTarget.filename).then(setCropUrl).catch(() => setCropUrl(null));
  }, [cropTarget, projectId]);

  // 분할 화면의 <video> 재생 위치를 구독 — 프레임 스트립 하이라이트용.
  // 플레이어는 splitUrl 이 준비된 뒤에 마운트되므로 splitUrl 을 의존성에 둔다.
  useEffect(() => {
    if (!splitTarget || !splitUrl) return;
    const vid = splitPlayerWrapRef.current?.querySelector("video");
    if (!vid) return;
    // 새 영상이 붙으면 재생 위치는 0 에서 시작하므로, 구독만 걸고 리셋은
    // loadedmetadata 콜백에 맡긴다(effect 본문 setState 는 cascading render 유발).
    const onTime = () => setSplitCurrentTime(vid.currentTime);
    // 지점 코멘트 띠의 가로 비율 계산용 길이. 메타 로드 전엔 NaN 이라 0 으로 둔다.
    const onMeta = () => {
      onTime();
      setSplitDuration(Number.isFinite(vid.duration) ? vid.duration : 0);
    };
    if (vid.readyState >= 1) onMeta();
    vid.addEventListener("timeupdate", onTime);
    vid.addEventListener("seeked", onTime);
    vid.addEventListener("loadedmetadata", onMeta);
    vid.addEventListener("durationchange", onMeta);
    return () => {
      vid.removeEventListener("timeupdate", onTime);
      vid.removeEventListener("seeked", onTime);
      vid.removeEventListener("loadedmetadata", onMeta);
      vid.removeEventListener("durationchange", onMeta);
    };
  }, [splitTarget, splitUrl]);

  // 스트립 썸네일 클릭 → 플레이어를 해당 시각으로 이동(핵심 동작).
  function seekSplitPlayer(sec: number) {
    const vid = splitPlayerWrapRef.current?.querySelector("video");
    if (!vid) return;
    vid.currentTime = sec;
    setSplitCurrentTime(sec);
  }

  // ── 지점 편집 코멘트 ──────────────────────────────────────────────────────
  // splitTarget 은 진입 시점 스냅샷이므로, 저장 후 최신 마커는 assets(서버 최신본)에서 읽는다.
  // splitTarget 자체를 갱신하면 파일 URL effect 가 다시 돌아 플레이어가 재로딩될 수 있다.
  const splitMarkers: FootageMarker[] = splitTarget
    ? (assets.find((a) => a.asset_id === splitTarget.asset_id)?.markers ?? splitTarget.markers ?? [])
    : [];

  useEffect(() => {
    if (!splitTarget) return;
    const cur = assets.find((a) => a.asset_id === splitTarget.asset_id) ?? splitTarget;
    setSplitLabel(cur.label ?? "");
    setSplitNote(cur.note ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [splitTarget?.asset_id]);

  const splitSavedAsset = splitTarget
    ? (assets.find((a) => a.asset_id === splitTarget.asset_id) ?? splitTarget)
    : null;
  const splitInfoDirty = !!splitSavedAsset
    && (splitLabel !== (splitSavedAsset.label ?? "") || splitNote !== (splitSavedAsset.note ?? ""));

  async function saveSplitInfo() {
    if (!splitTarget) return;
    const targetId = splitTarget.asset_id;
    setSplitInfoSaving(true);
    try {
      // markers 저장과 같은 방식 — 서버 최신본에서 대상 자산의 label/note 만 교체.
      const current = await getProject(projectId);
      const merged = current.studio_assets.map((a) =>
        a.asset_id === targetId ? { ...a, label: splitLabel, note: splitNote } : a,
      );
      const saved = await updateProject(projectId, { studio_assets: merged });
      onAssetsReplaced(saved.studio_assets);
      ui.success("라벨·코멘트를 저장했습니다.");
    } catch (e) {
      ui.error(`저장 실패: ${(e as Error).message || e}`);
    } finally {
      setSplitInfoSaving(false);
    }
  }

  async function closeSplit() {
    if (splitInfoDirty) {
      const ok = await ui.confirm({
        title: "저장하지 않은 변경",
        message: "라벨·코멘트 변경을 저장하지 않았습니다. 그대로 나가시겠습니까?",
        variant: "warning",
        confirmLabel: "나가기",
      });
      if (!ok) return;
    }
    setSplitTarget(null);
    setSplitSegments([]);
  }

  async function saveSplitMarkers(next: FootageMarker[]) {
    if (!splitTarget) return;
    const targetId = splitTarget.asset_id;
    // 다른 탭/작업의 변경을 덮지 않도록 서버 최신본에서 대상 자산의 markers 만 교체한다.
    const current = await getProject(projectId);
    const merged = current.studio_assets.map((a) =>
      a.asset_id === targetId ? { ...a, markers: next } : a,
    );
    const saved = await updateProject(projectId, { studio_assets: merged });
    onAssetsReplaced(saved.studio_assets);
  }

  /**
   * 장면 찾기 패널에서 선택한 구간들을 분할 목록에 **병합**한다.
   * - 기존 수동 구간은 건드리지 않는다.
   * - 같은 (start, end) 가 이미 있으면 건너뛴다(소수점 오차 흡수: 0.05초 이내 동일 취급).
   * - 병합 후 start 오름차순 정렬.
   * 반환값으로 추가/건너뛴 개수를 패널에 알려 사용자 안내 문구를 만든다.
   */
  function addSceneSegments(picks: SceneSearchPick[]): { added: number; skipped: number } {
    // 집계를 updater 안에서 하면 StrictMode 이중 호출에 개수가 부풀려진다.
    // 현재 splitSegments 를 기준으로 **먼저 계산**하고, 결과 배열을 그대로 세팅한다.
    const same = (a: number, b: number) => Math.abs(a - b) < 0.05;
    const next = [...splitSegments];
    let added = 0;
    let skipped = 0;
    for (const p of picks) {
      if (next.some((s) => same(s.start, p.start) && same(s.end, p.end))) {
        skipped++;
        continue;
      }
      next.push({ start: p.start, end: p.end, label: p.label });
      added++;
    }
    next.sort((a, b) => a.start - b.start);
    setSplitSegments(next);
    return { added, skipped };
  }

  function startEdit(asset: FootageAsset) {
    setEditingId(asset.asset_id);
    setEditLabel(asset.label);
  }

  async function saveEdit(assetId: string) {
    await onLabelChange(assetId, editLabel);
    setEditingId(null);
  }

  async function handleSplitSave() {
    if (!splitTarget || splitSegments.length === 0) return;
    setSplitSaving(true);
    setSplitError("");
    try {
      const srcBlob = await (await fetch(await getCachedFileUrl(projectId, splitTarget.filename))).blob();
      const parentId = splitTarget.asset_id;
      const newAssets: FootageAsset[] = [];
      for (const seg of splitSegments) {
        const trimmed = await trimVideo(srcBlob, seg.start, seg.end);
        const base = splitTarget.filename.replace(/\.[^.]+$/, "");
        const name = `${base}_${seg.start.toFixed(1)}-${seg.end.toFixed(1)}.mp4`;
        const asset = await saveAsset(projectId, trimmed, name, seg.label, "split");
        // 조각 범위 안의 지점 코멘트는 조각 기준 시각으로 보정해 함께 넘긴다.
        // (구간이 겹치면 같은 코멘트가 여러 조각에 복사되므로 id 는 새로 발급.)
        const markers: FootageMarker[] = splitMarkers
          .filter((m) => m.time_sec >= seg.start && m.time_sec <= seg.end)
          .map((m) => ({
            ...m,
            marker_id: uuidv4(),
            time_sec: Math.round((m.time_sec - seg.start) * 10) / 10,
          }));
        // 조각이 어느 원본에서 나왔는지 기록 — 나중에 "분할당한 원본만 선택" 의 근거.
        newAssets.push({ ...asset, split_from: parentId, markers } as FootageAsset);
      }
      if (newAssets.length > 0) {
        // 조각 등록과 원본의 has_splits 갱신을 **한 번의 저장**으로 처리한다.
        // 두 번 나누면 중간 실패 시 뱃지와 실제 관계가 어긋나고, 그 사이 다른
        // 탭이 끼어들 틈도 생긴다. dedupe 는 addStudioAssets 와 동일 함수를 쓴다.
        const current = await getProject(projectId);
        const merged = dedupeStudioAssets([...current.studio_assets, ...newAssets]).map((a) =>
          a.asset_id === parentId
            ? ({ ...a, has_splits: true } as FootageAsset)
            : a,
        );
        const saved = await updateProject(projectId, { studio_assets: merged });
        onAssetsReplaced(saved.studio_assets);
      }
      setSplitTarget(null);
      setSplitSegments([]);
    } catch (e) {
      setSplitError(`저장 실패: ${(e as Error).message}`);
    } finally {
      setSplitSaving(false);
    }
  }

  const sourceLabel: Record<string, string> = {
    recorded: "녹화",
    split: "구간분할",
    presentation: "프레젠테이션",
    imported: "가져오기",
  };

  // 크롭 편집 화면
  // #289/#290 — audio 자산은 크롭 대상에서 제외하므로 video/image만 도달한다.
  //              CropEditor.mediaType은 video|image이므로 타입 가드를 통해 좁힌다.
  if (cropTarget && cropTarget.asset_type !== "audio") {
    const cropMediaType: "video" | "image" = cropTarget.asset_type;
    return (
      <div style={s.tabPane}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
          <button style={tls.cancelSmBtn} onClick={() => setCropTarget(null)}>← 목록으로</button>
          <span style={{ color: "#fff", fontWeight: 700 }}>{cropTarget.filename} — 화면 자르기</span>
        </div>
        {!cropUrl && <p style={{ color: "#666" }}>파일 URL 로딩 중...</p>}
        {cropUrl && (
          <CropEditor
            mediaUrl={cropUrl}
            mediaType={cropMediaType}
            initialCrop={cropTarget.crop ?? null}
            defaultCrop={defaultCrop}
            onSave={(crop) => {
              onCropChange(cropTarget.asset_id, crop);
              setCropTarget(null);
            }}
            onSetDefault={(crop, applyToAll) => {
              onSetDefaultCrop(crop, applyToAll);
            }}
            onCancel={() => setCropTarget(null)}
          />
        )}
      </div>
    );
  }

  // 구간 분할 화면
  if (splitTarget) {
    return (
      <div style={s.tabPane}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
          <button style={tls.cancelSmBtn} onClick={closeSplit}>
            ← 목록으로
          </button>
          <span style={{ color: "#fff", fontWeight: 700 }}>{splitTarget.filename} — 구간 분할</span>
        </div>
        {/* 라벨 + 푸티지 전체 코멘트 — 영상을 보면서 바로 기록한다. */}
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: "#aaa", fontSize: 13, width: 72, flexShrink: 0 }}>라벨</span>
            <input
              style={{ ...s.input, flex: 1 }}
              value={splitLabel}
              placeholder="이 푸티지의 라벨"
              onChange={(e) => setSplitLabel(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && splitInfoDirty) saveSplitInfo(); }}
            />
          </div>
          <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
            <span style={{ color: "#aaa", fontSize: 13, width: 72, flexShrink: 0, paddingTop: 6 }}>전체 코멘트</span>
            <textarea
              style={{ ...s.input, flex: 1, minHeight: 64, resize: "vertical", fontFamily: "inherit" }}
              value={splitNote}
              placeholder="영상 전체에 대한 메모 (내용, 쓸 곳, 주의점 등)"
              onChange={(e) => setSplitNote(e.target.value)}
            />
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 8 }}>
            {splitInfoDirty && <span style={{ color: "#facc15", fontSize: 12 }}>저장 안 됨</span>}
            <button
              style={s.saveSmBtn}
              onClick={saveSplitInfo}
              disabled={splitInfoSaving || !splitInfoDirty}
            >
              {splitInfoSaving ? "저장 중..." : "라벨·코멘트 저장"}
            </button>
          </div>
        </div>
        {splitError && <div style={s.errorBox}>{splitError}</div>}
        {!splitUrl && <p style={{ color: "#666" }}>파일 URL 로딩 중...</p>}
        {splitUrl && (
          <div ref={splitPlayerWrapRef}>
            <VideoTimeline
              videoUrl={splitUrl}
              segments={splitSegments}
              onSegmentsChange={setSplitSegments}
            />
          </div>
        )}
        {/* 지점 편집 코멘트 — 특정 시각에 편집 지시를 꽂는다. 원본 자산에 영구 저장. */}
        {splitUrl && splitTarget.asset_type === "video" && (
          <MarkerCommentBar
            duration={splitDuration}
            currentTime={splitCurrentTime}
            markers={splitMarkers}
            onSeek={seekSplitPlayer}
            onChange={saveSplitMarkers}
          />
        )}
        {/* 프레임 스트립 — 영상 자산에만 표시. 실패해도 위 분할 UI 는 그대로 동작한다. */}
        {splitUrl && splitTarget.asset_type === "video" && (
          <FilmstripBar
            projectId={projectId}
            assetId={splitTarget.asset_id}
            currentTime={splitCurrentTime}
            onSeek={seekSplitPlayer}
          />
        )}
        {/* 장면 텍스트 검색 — 스트립과 같은 seek 경로를 공유한다.
            색인 파일이 없으면 패널이 스스로 한 줄만 남기고 비활성화된다. */}
        {splitUrl && splitTarget.asset_type === "video" && (
          <SceneSearchPanel
            projectId={projectId}
            assetId={splitTarget.asset_id}
            currentTime={splitCurrentTime}
            onSeek={seekSplitPlayer}
            onAddSegments={addSceneSegments}
          />
        )}
        <div style={{ ...s.actionRow, marginTop: 20 }}>
          <button style={s.saveBtn} onClick={handleSplitSave} disabled={splitSaving || splitSegments.length === 0}>
            {splitSaving ? "저장 중..." : `${splitSegments.length}개 구간 저장`}
          </button>
          <button style={s.discardBtn} onClick={closeSplit}>
            취소
          </button>
        </div>
      </div>
    );
  }

  return (
    <div style={s.tabPane}>
      {/* #299 DoD 2 — 라벨 영역 hover 시 점선 테두리 + 펜 아이콘 hint. */}
      <style>{`
        .va-label-edit { display: inline-flex; align-items: center; gap: 6px; border: 1px dashed transparent; border-radius: 6px; padding: 2px 6px; margin-left: -6px; transition: border-color 0.12s ease, background 0.12s ease; }
        .va-label-edit:hover { border-color: #6366f1; background: rgba(99,102,241,0.08); }
        .va-label-pen { opacity: 0; color: #a5b4fc; font-size: 12px; transition: opacity 0.12s ease; }
        .va-label-edit:hover .va-label-pen { opacity: 1; }
      `}</style>
      {/* ── 인덱싱 카드 (2026-06) — 파일 1개 = 청크 1개 정책. ─────────────────
          매칭 정확도를 높이려면 footage 를 미리 AI 로 분석해 두는 것이 좋다.
          카드에서 Gemini 모델을 선택하면 예상 비용이 즉시 갱신된다.
          Gemini 기능이라 기본 숨김(lib/features.ts) — 장면 분석은 Claude Code 가 대신한다. */}
      {GEMINI_ENABLED && (
      <div style={s.indexCard}>
        <div style={s.indexCardHeader}>
          <div>
            <div style={s.indexCardTitle}>AI 인덱싱 (선택)</div>
            <div style={s.indexCardSubtitle}>
              footage 를 미리 Gemini 로 분석하면 매칭이 더 정확해집니다 — 파일 1개당 호출 1회.
            </div>
          </div>
          <div style={s.indexCardRight}>
            <select
              style={s.indexModelSelect}
              value={indexGeminiModel}
              onChange={(e) => void handleSelectIndexModel(e.target.value)}
              disabled={indexRunning}
              title="인덱싱에 사용할 Gemini 모델"
            >
              {STUDIO_GEMINI_MODELS.map((g) => (
                <option key={g.value} value={g.value}>{g.label}</option>
              ))}
            </select>
            <div style={s.indexCostLabel} title={indexEstimate ? `약 $${indexEstimate.usd.toFixed(4)} USD` : ""}>
              {indexCostLoading
                ? <span style={s.indexCostLoading}>계산 중…</span>
                : indexEstimate
                  ? <>예상 약 <b>{indexEstimate.krw.toLocaleString()}</b>원</>
                  : <span style={s.indexCostLoading}>—</span>}
            </div>
          </div>
        </div>
        <div style={s.indexCardBody}>
          <div style={s.indexCardStats}>
            영상 <b>{indexEstimate?.file_count ?? 0}</b>개
            {" · "}
            총 <b>{indexEstimate ? Math.round(indexEstimate.total_duration_sec / 60) : 0}</b>분
            {" · "}
            인덱싱됨 <b>{indexedCount}</b>개
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              style={s.indexBtn}
              onClick={() => handleStartIndexing(false)}
              disabled={indexRunning || (indexEstimate?.file_count ?? 0) === 0}
              title="아직 인덱싱되지 않은 영상만 분석"
            >
              {indexRunning ? "인덱싱 중..." : "인덱싱 시작"}
            </button>
            {indexedCount > 0 && !indexRunning && (
              <button
                style={s.indexForceBtn}
                onClick={() => {
                  if (window.confirm("기존 인덱스를 무시하고 모든 영상을 다시 분석합니다. 비용이 다시 발생합니다.\n계속할까요?")) {
                    void handleStartIndexing(true);
                  }
                }}
                title="기존 인덱스를 무시하고 전체 재분석"
              >
                강제 재인덱싱
              </button>
            )}
          </div>
        </div>
        {indexProgress && (
          <div style={s.indexProgressBar}>
            <div
              style={{
                ...s.indexProgressFill,
                width: `${indexProgress.total > 0 ? Math.round((indexProgress.current / indexProgress.total) * 100) : 0}%`,
              }}
            />
            <div style={s.indexProgressMsg}>
              {indexProgress.message} ({indexProgress.current}/{indexProgress.total})
            </div>
          </div>
        )}
      </div>
      )}

      <div style={s.assetsHeader}>
        <span style={{ color: "#aaa" }}>
          총 {assets.length}개
          {selectedAssets.length > 0 && (
            <span style={{ color: "#f87171", marginLeft: 8 }}>({selectedAssets.length}개 선택)</span>
          )}
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {/* ── 다중 선택 삭제 조작부 ─────────────────────────────────────── */}
          {assets.length > 0 && (
            <label style={s.selectAllLabel} title="표시 중인 자산 전체 선택/해제">
              <input
                type="checkbox"
                checked={allDisplayedSelected}
                onChange={toggleSelectAllDisplayed}
                aria-label="전체 선택"
              />
              전체선택
            </label>
          )}
          {splitParentCount > 0 && (
            <button
              style={s.importBtn}
              onClick={selectSplitParentsOnly}
              title="구간분할의 재료로 쓰인 원본만 골라 선택합니다"
            >
              분할당한 원본만 선택 ({splitParentCount})
            </button>
          )}
          {selectedAssets.length > 0 && (
            <button
              style={s.deleteAllBtn}
              onClick={handleBulkDelete}
              disabled={bulkDeleting}
              title="선택한 자산을 목록에서만 제거 (footage 폴더 파일은 유지)"
            >
              {bulkDeleting ? "제거 중…" : `선택 삭제 (${selectedAssets.length})`}
            </button>
          )}
          {/* #299 DoD 4 — 정렬 옵션. */}
          {assets.length > 0 && (
            <select
              style={s.sortSelect}
              value={sortMode}
              onChange={(e) => setSortMode(e.target.value as typeof sortMode)}
              title="자산 정렬"
            >
              <option value="default">정렬: 기본</option>
              <option value="unlabeled-first">정렬: 라벨 미입력 먼저</option>
            </select>
          )}
          {assets.length > 0 && (
            <button
              style={s.importBtn}
              onClick={async () => {
                const lines = displayedAssets.map((a) => a.label || "");
                const text = lines.join("\n");
                try {
                  await navigator.clipboard.writeText(text);
                  const emptyCount = lines.filter((v) => !v).length;
                  ui.success(
                    emptyCount > 0
                      ? `라벨 ${lines.length}개 복사됨 (빈 라벨 ${emptyCount}개 포함)`
                      : `라벨 ${lines.length}개 복사됨`,
                  );
                } catch (e) {
                  console.error("[label-copy] clipboard write failed:", e);
                  ui.error(`복사 실패: ${(e as Error).message || e}`);
                }
              }}
              title="현재 표시 순서대로 라벨을 줄바꿈으로 구분해 클립보드에 복사"
            >
              라벨 복사
            </button>
          )}
          {clipboard && (
            <button
              style={s.pasteBtn}
              onClick={onPasteAsset}
              disabled={pasting}
              title={`복사한 footage를 이 프로젝트로 붙여넣기: ${clipboard.label || clipboard.filename}`}
            >
              {pasting
                ? "붙여넣는 중…"
                : clipboard.source_project_id === projectId
                  ? "📋 붙여넣기 (이 프로젝트)"
                  : `📋 붙여넣기 (${clipboard.label || clipboard.filename})`}
            </button>
          )}
          <button
            style={s.importBtn}
            onClick={() => fileInputRef.current?.click()}
            title="파일을 골라 footage 폴더로 복사 후 등록"
          >
            파일 선택 가져오기
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="video/*,image/*,audio/*"
            style={{ display: "none" }}
            onChange={async (e) => {
              const files = e.target.files;
              if (files && files.length > 0) await onImportFiles(files);
              // 같은 파일 재선택 가능하도록 input value 초기화
              e.target.value = "";
            }}
          />
          <button style={s.importBtn} onClick={onImportExisting}>
            기존 footage_dir에서 가져오기
          </button>
          {assets.length > 0 && (
            <button
              style={s.deleteAllBtn}
              onClick={onDeleteAll}
              title="등록된 모든 자산을 목록에서 제거 (실제 파일은 유지)"
            >
              전체 삭제
            </button>
          )}
        </div>
      </div>

      {importProgress && (
        <div style={s.importProgressBar}>
          <div
            style={{
              ...s.importProgressFill,
              width: `${importProgress.total > 0 ? Math.round((importProgress.current / importProgress.total) * 100) : 0}%`,
            }}
          />
          <div style={s.importProgressMsg}>
            가져오는 중… ({importProgress.current}/{importProgress.total}) {importProgress.filename}
          </div>
        </div>
      )}

      {/* #299 DoD 3 — 라벨 미입력 자산 안내 배너 (닫기 가능). */}
      {unlabeledCount > 0 && !labelBannerDismissed && (
        <div style={s.labelBanner}>
          <span style={{ color: "#f59e0b" }}>⚠</span>
          <span style={{ color: "#e5c07b", flex: 1 }}>
            라벨이 비어있는 자산이 {unlabeledCount}개 있습니다 — 라벨이 없으면 매칭 정확도가 떨어집니다.
          </span>
          {assets.length > 0 && sortMode !== "unlabeled-first" && (
            <button style={s.bannerActionBtn} onClick={() => setSortMode("unlabeled-first")}>
              미입력 먼저 보기
            </button>
          )}
          <button
            style={s.bannerCloseBtn}
            onClick={() => setLabelBannerDismissed(true)}
            title="닫기"
          >
            ×
          </button>
        </div>
      )}

      {defaultCrop && (
        <div style={{
          display: "flex", alignItems: "center", gap: 10,
          background: "#0f1f0f", border: "1px solid #2a4a2a", borderRadius: 8,
          padding: "10px 14px", marginBottom: 16, fontSize: 13,
        }}>
          <span style={{ color: "#4ade80" }}>기본 크롭 적용 중</span>
          <span style={{ color: "#666" }}>
            x:{(defaultCrop.x * 100).toFixed(0)}% y:{(defaultCrop.y * 100).toFixed(0)}%
            {" "}크기:{(defaultCrop.w * 100).toFixed(0)}×{(defaultCrop.h * 100).toFixed(0)}%
          </span>
          <button
            style={{ marginLeft: "auto", ...tls.cancelSmBtn, fontSize: 12 }}
            onClick={() => onSetDefaultCrop(null, false)}
          >
            기본 크롭 제거
          </button>
        </div>
      )}

      {loading && <p style={{ color: "#666", textAlign: "center" }}>불러오는 중...</p>}

      {!loading && assets.length === 0 && (
        <p style={{ color: "#555", textAlign: "center", marginTop: 40 }}>
          footage가 없습니다. 녹화하거나 프레젠테이션을 캡처하세요.
        </p>
      )}

      <div style={s.assetList}>
        {displayedAssets.map((asset) => {
          // #272: 로컬 폴더에 같은 이름의 파일이 있으면 origin="local"로 표시하여
          //  AssetThumb가 서버 API 대신 브라우저 썸네일 생성기를 사용하도록 한다.
          const localFile = localFootageFileMap.get(asset.filename);
          const assetWithLocal: FootageAsset = localFile
            ? { ...asset, origin: "local", localFile }
            : asset;
          // 2026-06 — 인덱싱된 자산이면 description/keywords 를 노출. 미인덱싱이면 undefined → 블록 미렌더.
          const chunkInfo = chunkInfoMap.get(asset.filename);
          const indexedDesc = chunkInfo?.description ?? "";
          const indexedKws = chunkInfo?.keywords ?? [];
          return (
          <div
            key={asset.asset_id}
            style={selectedIds.has(asset.asset_id) ? s.assetCardSelected : s.assetCard}
          >
            <input
              type="checkbox"
              style={s.assetCheckbox}
              checked={selectedIds.has(asset.asset_id)}
              onChange={() => toggleSelected(asset.asset_id)}
              title="선택 삭제 대상으로 고르기"
              aria-label={`${asset.filename} 선택`}
            />
            <AssetThumb projectId={projectId} asset={assetWithLocal} />
            <div style={s.assetInfo}>
              <div style={s.assetFilename}>{asset.filename}</div>
              <div style={s.assetMeta}>
                {/* 분할 출처 뱃지 — 원본(잘림) / 조각 / 그 외는 기존 source 라벨. */}
                {hasSplitsOf(asset) ? (
                  <span style={s.splitParentBadge} title="이 자산을 잘라 조각을 만든 적이 있습니다">
                    원본·잘림
                  </span>
                ) : splitFromOf(asset) ? (
                  <span style={s.splitChildBadge} title="다른 자산에서 구간분할된 조각입니다">
                    분할
                  </span>
                ) : (
                  <span style={s.badge}>{sourceLabel[asset.source] ?? asset.source}</span>
                )}
                <span style={s.badge}>
                  {asset.asset_type === "video" ? "동영상"
                   : asset.asset_type === "audio" ? "오디오"
                   : "이미지"}
                </span>
                {asset.crop && <span style={{ ...s.badge, color: "#4ade80", borderColor: "#2a4a2a" }}>크롭</span>}
                {/* #299 DoD 1 — 라벨 미입력 자산에 노란 경고 배지. */}
                {!asset.label && <span style={s.labelMissingBadge}>⚠ 라벨 미입력</span>}
              </div>
              {/* 2026-06 — 인덱싱된 자산: AI 분석 요약(2줄 clamp). 미인덱싱이면 미표시 → 기존 모양 유지. */}
              {indexedDesc && (
                <div style={s.assetDescription} title={indexedDesc}>
                  <span style={s.assetDescriptionIcon}>🤖</span>
                  <span style={s.assetDescriptionText}>{indexedDesc}</span>
                </div>
              )}
              {indexedKws.length > 0 && (
                <div style={s.assetKeywords}>
                  {indexedKws.map((k) => (
                    <span key={k} style={s.assetKeywordBadge}>{k}</span>
                  ))}
                </div>
              )}
              {/* 푸티지 전체 코멘트(구간 분할 화면에서 입력) — 있으면 2줄 요약 표시. */}
              {asset.note && (
                <div style={s.assetDescription} title={asset.note}>
                  <span style={s.assetDescriptionIcon}>📝</span>
                  <span style={s.assetDescriptionText}>{asset.note}</span>
                </div>
              )}
              {editingId === asset.asset_id ? (
                <div style={s.editRow}>
                  <input
                    style={{ ...s.input, flex: 1 }}
                    value={editLabel}
                    onChange={(e) => setEditLabel(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") saveEdit(asset.asset_id); }}
                    autoFocus
                  />
                  <button style={s.saveSmBtn} onClick={() => saveEdit(asset.asset_id)}>저장</button>
                  <button style={s.cancelSmBtn} onClick={() => setEditingId(null)}>취소</button>
                </div>
              ) : (
                <div
                  className="va-label-edit"
                  style={s.labelDisplay}
                  onClick={() => startEdit(asset)}
                  title="클릭하여 라벨 편집"
                >
                  {asset.label ? (
                    <span style={s.labelValue}>{asset.label}</span>
                  ) : (
                    <span style={s.labelEmpty}>라벨 클릭하여 입력</span>
                  )}
                  {/* #299 DoD 2 — hover 시 펜 아이콘 hint (CSS 로 표시 토글). */}
                  <span className="va-label-pen" style={s.labelPen}>✎</span>
                </div>
              )}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6, flexShrink: 0, alignItems: "flex-end" }}>
              {cropProcessingId === asset.asset_id ? (
                <div style={{ color: "#a5b4fc", fontSize: 12, textAlign: "right" }}>
                  크롭 처리 중... {cropProgress}%
                  <div style={{ height: 3, background: "#333", borderRadius: 2, marginTop: 4, width: 80 }}>
                    <div style={{ height: "100%", background: "#6366f1", borderRadius: 2, width: `${cropProgress}%`, transition: "width 0.2s" }} />
                  </div>
                </div>
              ) : (
                <>
                  {/* #289/#290 — 오디오 자산은 화면 자르기·구간분할 비대상. */}
                  {asset.asset_type !== "audio" && (
                    <button
                      style={s.cropSmBtn}
                      onClick={() => setCropTarget(asset)}
                      title="화면 자르기"
                    >
                      자르기
                    </button>
                  )}
                  {asset.asset_type === "video" && (
                    <button
                      style={s.splitSmBtn}
                      onClick={() => { setSplitTarget(asset); setSplitSegments([]); }}
                      title="구간 분할"
                    >
                      구간분할
                    </button>
                  )}
                  <button
                    style={s.copySmBtn}
                    onClick={() => onCopyAsset(asset)}
                    title="다른 프로젝트로 복사"
                  >
                    복사
                  </button>
                  <button style={s.delBtn} onClick={() => onDelete(asset.asset_id)} title="목록에서 제거">×</button>
                </>
              )}
            </div>
          </div>
          );
        })}
      </div>
    </div>
  );
}

// ── AssetThumb — 로컬 파일 URL에서 썸네일 표시 ─────────────────────────────

function AssetThumb({ projectId, asset }: { projectId: string; asset: FootageAsset }) {
  // #272: 서버 footage_dir에 없는 로컬 파일은 서버 썸네일 API가 404를 반환한다.
  //  → origin === "local" + localFile이 있으면 브라우저에서 첫 프레임 추출 + IndexedDB 캐시.
  // localFile이 아직 로드되기 전이면 placeholder 표시(서버 404 폭주 회피).
  if (asset.origin === "local") {
    if (asset.localFile) {
      return <LocalThumbnail file={asset.localFile} alt={asset.filename} style={s.assetThumb} />;
    }
    return <div style={{ ...s.assetThumb, background: "#1a1a1a" }} aria-label={asset.filename} />;
  }
  // #307: 서버 푸티지도 브라우저에서 추출 + IndexedDB 캐시 (백엔드 ffmpeg spawn 절감).
  // 오디오는 파형 썸네일이 필요하므로 백엔드 fallback로 자동 라우팅된다.
  return (
    <ServerFootageThumbnail
      projectId={projectId}
      filename={asset.filename}
      assetType={asset.asset_type}
      alt={asset.filename}
      style={s.assetThumb}
    />
  );
}

// ── CropEditor ─────────────────────────────────────────────────────────────

function CropEditor({
  mediaUrl,
  mediaType,
  initialCrop,
  defaultCrop,
  onSave,
  onSetDefault,
  onCancel,
}: {
  mediaUrl: string;
  mediaType: "video" | "image";
  initialCrop: CropRect | null;
  defaultCrop: CropRect | null;
  onSave: (crop: CropRect | null) => void;
  onSetDefault: (crop: CropRect | null, applyToAll: boolean) => void;
  onCancel: () => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [crop, setCrop] = useState<CropRect>(
    initialCrop ?? defaultCrop ?? { x: 0, y: 0, w: 1, h: 1 }
  );
  // 드래그 상태: null이면 비활성, 아니면 드래그 시작점(비율)
  const dragRef = useRef<{ startX: number; startY: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  // 컨테이너 내 비율 좌표 계산
  function ratioFromClient(clientX: number, clientY: number): { rx: number; ry: number } {
    const el = containerRef.current;
    if (!el) return { rx: 0, ry: 0 };
    const rect = el.getBoundingClientRect();
    const rx = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const ry = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));
    return { rx, ry };
  }

  function handleMouseDown(e: React.MouseEvent) {
    e.preventDefault();
    const { rx, ry } = ratioFromClient(e.clientX, e.clientY);
    dragRef.current = { startX: rx, startY: ry };
    setCrop({ x: rx, y: ry, w: 0, h: 0 });
    setDragging(true);
  }

  useEffect(() => {
    function onMove(e: MouseEvent) {
      if (!dragRef.current) return;
      const { rx, ry } = ratioFromClient(e.clientX, e.clientY);
      const x = Math.min(dragRef.current.startX, rx);
      const y = Math.min(dragRef.current.startY, ry);
      const w = Math.abs(rx - dragRef.current.startX);
      const h = Math.abs(ry - dragRef.current.startY);
      setCrop({ x, y, w, h });
    }
    function onUp() {
      if (!dragRef.current) return;
      dragRef.current = null;
      setDragging(false);
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  const isNoop = crop.w < 0.01 || crop.h < 0.01 || (crop.x === 0 && crop.y === 0 && crop.w === 1 && crop.h === 1);

  // 선택 영역 바깥 어두운 마스크 (4개 사각형)
  const maskStyle: React.CSSProperties = { position: "absolute", background: "#00000088", pointerEvents: "none" };

  return (
    <div>
      <p style={{ color: "#aaa", fontSize: 13, marginBottom: 12 }}>
        미리보기에서 드래그하여 유지할 영역을 선택하세요. 바깥은 잘립니다.
      </p>

      {/* 미디어 + 오버레이 */}
      <div
        ref={containerRef}
        style={{ position: "relative", display: "inline-block", cursor: "crosshair", userSelect: "none", width: "100%" }}
        onMouseDown={handleMouseDown}
      >
        {mediaType === "image" ? (
          <img src={mediaUrl} style={{ width: "100%", display: "block", borderRadius: 8 }} draggable={false} />
        ) : (
          <video src={mediaUrl} style={{ width: "100%", display: "block", borderRadius: 8 }} muted />
        )}

        {!isNoop && (
          <>
            {/* 위 */}
            <div style={{ ...maskStyle, top: 0, left: 0, right: 0, height: `${crop.y * 100}%` }} />
            {/* 아래 */}
            <div style={{ ...maskStyle, bottom: 0, left: 0, right: 0, top: `${(crop.y + crop.h) * 100}%` }} />
            {/* 왼쪽 */}
            <div style={{ ...maskStyle, top: `${crop.y * 100}%`, height: `${crop.h * 100}%`, left: 0, width: `${crop.x * 100}%` }} />
            {/* 오른쪽 */}
            <div style={{ ...maskStyle, top: `${crop.y * 100}%`, height: `${crop.h * 100}%`, left: `${(crop.x + crop.w) * 100}%`, right: 0 }} />
            {/* 선택 테두리 */}
            <div style={{
              position: "absolute", pointerEvents: "none",
              left: `${crop.x * 100}%`, top: `${crop.y * 100}%`,
              width: `${crop.w * 100}%`, height: `${crop.h * 100}%`,
              border: "2px solid #6366f1", boxSizing: "border-box",
            }}>
              <span style={{
                position: "absolute", top: 4, left: 4,
                background: "#6366f1cc", color: "#fff", fontSize: 11,
                padding: "2px 6px", borderRadius: 4,
              }}>
                {(crop.w * 100).toFixed(0)}×{(crop.h * 100).toFixed(0)}%
              </span>
            </div>
          </>
        )}

        {dragging && isNoop && (
          <div style={{ ...maskStyle, inset: 0, background: "#00000044", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <span style={{ color: "#fff", fontSize: 14 }}>드래그하여 영역 선택</span>
          </div>
        )}
      </div>

      {/* 수치 표시 */}
      {!isNoop && (
        <div style={{ color: "#666", fontSize: 12, marginTop: 6 }}>
          시작: ({(crop.x * 100).toFixed(1)}%, {(crop.y * 100).toFixed(1)}%) —
          크기: {(crop.w * 100).toFixed(1)}% × {(crop.h * 100).toFixed(1)}%
        </div>
      )}

      {/* 액션 버튼 */}
      <div style={{ display: "flex", gap: 10, marginTop: 16, flexWrap: "wrap" }}>
        <button
          style={s.saveBtn}
          disabled={isNoop}
          onClick={() => onSave(isNoop ? null : crop)}
        >
          이 파일에 적용
        </button>
        <button
          style={{ ...s.saveBtn, background: "#059669" }}
          disabled={isNoop}
          onClick={() => {
            onSave(isNoop ? null : crop);
            onSetDefault(isNoop ? null : crop, false);
          }}
        >
          기본값으로 저장
        </button>
        <button
          style={{ ...s.saveBtn, background: "#7c3aed" }}
          disabled={isNoop}
          onClick={() => {
            onSave(isNoop ? null : crop);
            onSetDefault(isNoop ? null : crop, true);
          }}
        >
          기본값 저장 + 전체 적용
        </button>
        {(initialCrop) && (
          <button
            style={{ ...s.discardBtn }}
            onClick={() => onSave(null)}
          >
            크롭 제거
          </button>
        )}
        <button style={s.discardBtn} onClick={onCancel}>취소</button>
      </div>
    </div>
  );
}

// ── Styles ─────────────────────────────────────────────────────────────────

const s: Record<string, React.CSSProperties> = {
  root: { minHeight: "100vh", background: "#0f0f0f", display: "flex", flexDirection: "column" },
  header: {
    display: "flex", alignItems: "center", gap: 16,
    padding: "16px 24px", background: "#141414", borderBottom: "1px solid #222",
    position: "sticky" as const, top: 0, zIndex: 10,
  },
  backBtn: { background: "none", border: "none", color: "#888", cursor: "pointer", fontSize: 14 },
  headerCenter: { flex: 1, display: "flex", flexDirection: "column" as const },
  headerTitle: { color: "#fff", fontWeight: 700, fontSize: 16 },
  headerSub: { color: "#666", fontSize: 12 },
  goBtn: {
    padding: "10px 18px", background: "#6366f1", color: "#fff",
    border: "none", borderRadius: 8, cursor: "pointer", fontSize: 14, fontWeight: 600,
    flexShrink: 0,
  },
  // footage 폴더 바
  dirBar: {
    display: "flex", alignItems: "center", gap: 10,
    padding: "10px 24px", background: "#111", borderBottom: "1px solid #1e1e1e",
  },
  dirLabel: { color: "#555", fontSize: 12, flexShrink: 0 },
  dirPath: { color: "#888", fontSize: 13, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" as const },
  dirEditBtn: {
    padding: "4px 12px", background: "#1e1e2e", color: "#a5b4fc",
    border: "1px solid #2a2a4a", borderRadius: 6, cursor: "pointer", fontSize: 12, flexShrink: 0,
  },
  tabs: { display: "flex", borderBottom: "1px solid #222", background: "#141414" },
  tab: {
    padding: "14px 24px", background: "none", border: "none",
    color: "#666", cursor: "pointer", fontSize: 14,
    borderBottom: "2px solid transparent",
  },
  tabActive: { color: "#fff", borderBottom: "2px solid #6366f1" },
  content: { flex: 1, overflowY: "auto" as const, padding: 24 },
  tabPane: { maxWidth: 860, margin: "0 auto" },
  errorBox: {
    background: "#2a1a1a", border: "1px solid #5a2a2a", color: "#f87171",
    borderRadius: 8, padding: "10px 14px", marginBottom: 16, fontSize: 14,
  },
  recordCenter: { textAlign: "center" as const, padding: "60px 0" },
  bigBtn: {
    padding: "16px 36px", background: "#6366f1", color: "#fff",
    border: "none", borderRadius: 10, fontSize: 16, cursor: "pointer", fontWeight: 600,
  },
  hint: { color: "#666", fontSize: 13, marginTop: 12 },
  recordingIndicator: {
    display: "flex", alignItems: "center", justifyContent: "center",
    gap: 8, color: "#f87171", fontWeight: 700, fontSize: 18, marginBottom: 20,
  },
  dot: {
    display: "inline-block", width: 10, height: 10,
    borderRadius: "50%", background: "#ef4444",
    animation: "pulse 1s infinite",
  },
  saveArea: {},
  preview: { width: "100%", maxHeight: 360, borderRadius: 8, marginBottom: 20, background: "#000" },
  modeRow: { display: "flex", alignItems: "center", gap: 10, marginBottom: 16 },
  modeLabel: { color: "#aaa", fontSize: 14 },
  modeBtn: {
    padding: "7px 18px", background: "#222", color: "#888",
    border: "1px solid #333", borderRadius: 7, cursor: "pointer", fontSize: 13,
  },
  modeBtnActive: { background: "#6366f1", color: "#fff", borderColor: "#6366f1" },
  labelRow: { display: "flex", alignItems: "center", gap: 12, marginBottom: 20 },
  labelText: { color: "#aaa", fontSize: 14, flexShrink: 0 },
  input: {
    background: "#1a1a1a", border: "1px solid #333", borderRadius: 7,
    color: "#fff", padding: "8px 12px", fontSize: 14, outline: "none",
  },
  splitArea: { marginBottom: 20 },
  splitInputRow: { display: "flex", gap: 8, alignItems: "center", marginTop: 8, flexWrap: "wrap" as const },
  addBtn: {
    padding: "8px 14px", background: "#252525", color: "#aaa",
    border: "1px solid #333", borderRadius: 7, cursor: "pointer", fontSize: 13,
  },
  splitList: { marginTop: 12, display: "flex", flexDirection: "column" as const, gap: 6 },
  splitItem: {
    display: "flex", alignItems: "center", gap: 10,
    background: "#1a1a1a", borderRadius: 7, padding: "8px 12px",
  },
  splitRange: { color: "#6366f1", fontSize: 13, minWidth: 100 },
  splitLbl: { color: "#aaa", fontSize: 13, flex: 1 },
  removeBtn: { background: "none", border: "none", color: "#888", cursor: "pointer", fontSize: 18 },
  actionRow: { display: "flex", gap: 10 },
  saveBtn: {
    padding: "12px 28px", background: "#6366f1", color: "#fff",
    border: "none", borderRadius: 8, cursor: "pointer", fontSize: 15, fontWeight: 600,
  },
  discardBtn: {
    padding: "12px 20px", background: "#2a1a1a", color: "#f87171",
    border: "1px solid #3a1a1a", borderRadius: 8, cursor: "pointer", fontSize: 15,
  },
  // Presentation
  pageGrid: {
    display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
    gap: 16, marginBottom: 24,
  },
  pageCard: {
    background: "#1a1a1a", borderRadius: 8, padding: 12, display: "flex",
    flexDirection: "column" as const, gap: 8,
  },
  pageThumbnail: { width: "100%", borderRadius: 6, aspectRatio: "16/9", objectFit: "cover" as const },
  pageNum: { color: "#888", fontSize: 12 },
  // Assets
  // 인덱싱 카드 (2026-06)
  indexCard: {
    background: "linear-gradient(135deg, #1a1a2e 0%, #181828 100%)",
    border: "1px solid #2a2a44",
    borderRadius: 10,
    padding: "14px 18px",
    marginBottom: 18,
    display: "flex" as const,
    flexDirection: "column" as const,
    gap: 10,
  },
  indexCardHeader: {
    display: "flex" as const,
    alignItems: "flex-start" as const,
    justifyContent: "space-between" as const,
    gap: 12,
  },
  indexCardTitle: { color: "#fff", fontSize: 14, fontWeight: 700 },
  indexCardSubtitle: { color: "#888", fontSize: 12, marginTop: 4, maxWidth: 540 },
  indexCardRight: {
    display: "flex" as const,
    alignItems: "center" as const,
    gap: 10,
    flexShrink: 0,
  },
  indexModelSelect: {
    background: "#0f0f1a", color: "#a5b4fc",
    border: "1px solid #2a2a44", borderRadius: 6,
    padding: "5px 8px", fontSize: 12, cursor: "pointer",
  },
  indexCostLabel: { color: "#a5b4fc", fontSize: 13 },
  // estimate API 호출 중 라벨. dash/이전값 깜빡임 대신 일관된 회색 이탤릭 표시.
  indexCostLoading: { color: "#888", fontStyle: "italic" as const, fontSize: 13 },
  indexCardBody: {
    display: "flex" as const,
    alignItems: "center" as const,
    justifyContent: "space-between" as const,
    gap: 12,
  },
  indexCardStats: { color: "#aaa", fontSize: 12 },
  indexBtn: {
    padding: "7px 14px", background: "#4338ca", color: "#fff",
    border: "none", borderRadius: 7, cursor: "pointer", fontSize: 13, fontWeight: 600,
  },
  indexForceBtn: {
    padding: "7px 14px", background: "#252525", color: "#a5b4fc",
    border: "1px solid #2a2a44", borderRadius: 7, cursor: "pointer", fontSize: 12,
  },
  indexProgressBar: {
    position: "relative" as const,
    height: 18, background: "#0f0f1a", borderRadius: 4, overflow: "hidden" as const,
    border: "1px solid #2a2a44",
  },
  indexProgressFill: {
    height: "100%", background: "linear-gradient(90deg, #6366f1, #818cf8)",
    transition: "width 0.2s",
  },
  indexProgressMsg: {
    position: "absolute" as const, inset: 0,
    display: "flex" as const, alignItems: "center" as const, justifyContent: "center" as const,
    color: "#e0e0e0", fontSize: 11,
  },
  assetsHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 },
  importBtn: {
    padding: "7px 14px", background: "#1a2a1a", color: "#4ade80",
    border: "1px solid #2a4a2a", borderRadius: 7, cursor: "pointer", fontSize: 13,
  },
  deleteAllBtn: {
    padding: "7px 14px", background: "#2a1a1a", color: "#f87171",
    border: "1px solid #4a2a2a", borderRadius: 7, cursor: "pointer", fontSize: 13,
  },
  importProgressBar: {
    position: "relative" as const,
    height: 22, background: "#0f1a1a", borderRadius: 4, overflow: "hidden" as const,
    border: "1px solid #2a4a2a", marginBottom: 16,
  },
  importProgressFill: {
    height: "100%", background: "linear-gradient(90deg, #16a34a, #4ade80)",
    transition: "width 0.2s",
  },
  importProgressMsg: {
    position: "absolute" as const, inset: 0,
    display: "flex" as const, alignItems: "center" as const, justifyContent: "center" as const,
    color: "#e0e0e0", fontSize: 12, padding: "0 10px",
    whiteSpace: "nowrap" as const, overflow: "hidden" as const, textOverflow: "ellipsis" as const,
  },
  assetList: { display: "flex", flexDirection: "column" as const, gap: 10 },
  assetCard: {
    display: "flex", alignItems: "center", gap: 14,
    background: "#1a1a1a", borderRadius: 8, padding: "12px 14px",
    border: "1px solid transparent",
  },
  // 선택된 카드 — 테두리만 바꿔 레이아웃이 흔들리지 않게 한다.
  assetCardSelected: {
    display: "flex", alignItems: "center", gap: 14,
    background: "#1f1a1a", borderRadius: 8, padding: "12px 14px",
    border: "1px solid #7f3030",
  },
  assetCheckbox: {
    width: 17, height: 17, flexShrink: 0, cursor: "pointer",
    accentColor: "#f87171",
  },
  selectAllLabel: {
    display: "flex", alignItems: "center", gap: 6,
    color: "#aaa", fontSize: 13, cursor: "pointer", userSelect: "none" as const,
  },
  // 분할 출처 뱃지 — 원본(잘림)은 보라, 조각은 청록으로 구분.
  splitParentBadge: {
    fontSize: 11, padding: "2px 7px", borderRadius: 4,
    background: "#2a1f3a", color: "#c4b5fd", border: "1px solid #4a3a6a",
  },
  splitChildBadge: {
    fontSize: 11, padding: "2px 7px", borderRadius: 4,
    background: "#12282a", color: "#67e8f9", border: "1px solid #1f4a4a",
  },
  assetThumb: {
    width: 192, height: 108, objectFit: "cover" as const,
    borderRadius: 5, flexShrink: 0, background: "#000",
  },
  assetInfo: { flex: 1, minWidth: 0 },
  assetFilename: { color: "#ccc", fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" as const },
  assetMeta: { display: "flex", gap: 6, marginTop: 4 },
  badge: {
    fontSize: 11, padding: "2px 7px", borderRadius: 4,
    background: "#252525", color: "#888", border: "1px solid #333",
  },
  // 2026-06 — 인덱싱된 자산 카드의 AI 분석 요약 블록(2줄 clamp + 전체 텍스트는 title 툴팁).
  assetDescription: {
    marginTop: 6, padding: "6px 10px",
    background: "#1a1f2e", border: "1px solid #2a3a5a", borderRadius: 6,
    fontSize: 12, color: "#a5b4fc", lineHeight: 1.4,
    display: "-webkit-box" as const,
    WebkitLineClamp: 2,
    WebkitBoxOrient: "vertical" as const,
    overflow: "hidden" as const,
    textOverflow: "ellipsis" as const,
  },
  assetDescriptionIcon: { marginRight: 4, fontSize: 11 },
  assetDescriptionText: {},
  assetKeywords: { display: "flex", flexWrap: "wrap" as const, gap: 4, marginTop: 4 },
  assetKeywordBadge: {
    fontSize: 10, padding: "1px 6px", borderRadius: 3,
    background: "#252535", color: "#a5b4fc", border: "1px solid #3a3a5a",
  },
  editRow: { display: "flex", gap: 6, marginTop: 6, alignItems: "center" },
  saveSmBtn: { padding: "5px 12px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12 },
  cancelSmBtn: { padding: "5px 10px", background: "#252525", color: "#888", border: "1px solid #333", borderRadius: 6, cursor: "pointer", fontSize: 12 },
  labelDisplay: { marginTop: 6, cursor: "pointer" },
  labelValue: { color: "#a5b4fc", fontSize: 13 },
  labelEmpty: { color: "#888", fontSize: 13, fontStyle: "italic" as const },
  labelPen: {},
  labelMissingBadge: {
    fontSize: 11, padding: "2px 7px", borderRadius: 4,
    background: "#3a2e0d", color: "#f59e0b", border: "1px solid #5a4a1a",
  },
  sortSelect: {
    padding: "6px 10px", background: "#1a1a1a", color: "#ccc",
    border: "1px solid #333", borderRadius: 7, cursor: "pointer", fontSize: 12,
  },
  labelBanner: {
    display: "flex", alignItems: "center", gap: 10,
    background: "#2a230d", border: "1px solid #5a4a1a", borderRadius: 8,
    padding: "10px 14px", marginBottom: 16, fontSize: 13,
  },
  bannerActionBtn: {
    padding: "5px 10px", background: "#3a2e0d", color: "#f59e0b",
    border: "1px solid #5a4a1a", borderRadius: 6, cursor: "pointer", fontSize: 12, whiteSpace: "nowrap" as const,
  },
  bannerCloseBtn: {
    background: "none", border: "none", color: "#a08a4a", cursor: "pointer",
    fontSize: 18, lineHeight: 1, padding: "0 4px", flexShrink: 0,
  },
  delBtn: {
    background: "none", border: "none", color: "#555", cursor: "pointer",
    fontSize: 22, padding: "0 4px", flexShrink: 0,
  },
  splitSmBtn: {
    padding: "5px 10px", background: "#1a2a1a", color: "#4ade80",
    border: "1px solid #2a4a2a", borderRadius: 6, cursor: "pointer", fontSize: 12,
  },
  cropSmBtn: {
    padding: "5px 10px", background: "#1a1a2a", color: "#a5b4fc",
    border: "1px solid #2a2a4a", borderRadius: 6, cursor: "pointer", fontSize: 12,
  },
  copySmBtn: {
    padding: "5px 10px", background: "#2a2419", color: "#fbbf24",
    border: "1px solid #4a3f1a", borderRadius: 6, cursor: "pointer", fontSize: 12,
  },
  pasteBtn: {
    padding: "7px 14px", background: "#0e2a1a", color: "#34d399",
    border: "1px solid #166534", borderRadius: 7, cursor: "pointer", fontSize: 13,
    maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" as const,
  },
};

// ── 프레젠테이션 탭 (#313) ─────────────────────────────────────────────────
//
// PDF / HTML 을 브라우저에서 직접 캡처하여 이미지 푸티지로 등록한다.
// 백엔드 Playwright(헤드리스 Chromium) 호출을 피해 가장 큰 비용 절감을
// 제공한다. 외부 CORS 리소스가 많은 HTML 등은 자동으로 백엔드 fallback
// (`uploadPresentation`) 으로 우회한다.

type PresentationStatus =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "capturing"; current: number; total: number; mode: "pdf" | "html" }
  | { kind: "uploading"; pages: number; mode: "pdf" | "html" | "fallback" }
  | { kind: "done"; pages: number; mode: "pdf" | "html" | "fallback"; warnings: string[] }
  | { kind: "error"; message: string };

function PresentationTab({
  projectId,
  onAssetsAdded,
}: {
  projectId: string;
  onAssetsAdded: (a: FootageAsset[]) => void;
}) {
  const [status, setStatus] = useState<PresentationStatus>({ kind: "idle" });
  const abortRef = useRef<AbortController | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 컴포넌트 unmount 시 진행 중 작업 취소.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  async function handleFile(file: File) {
    // 새 캡처 시작 — 이전 작업 취소.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setStatus({ kind: "loading" });

    // 동적 import — pdfjs-dist / html2canvas 는 큰 라이브러리이므로
    // 프레젠테이션 탭에 들어와 파일을 고를 때까지 번들 로드를 지연한다.
    try {
      const {
        capturePresentation,
        blobsToPresentationFiles,
        PresentationCaptureUnsupportedError,
      } = await import("../lib/presentationCapture");
      const { uploadPresentation, uploadPresentationImages } = await import(
        "../api/client"
      );

      // 1) 클라이언트 캡처 시도
      //
      // ⚠️ TDZ 주의 (#330):
      //   onProgress 콜백 안에서 `result` 를 참조하면 `await capturePresentation(...)`
      //   이 resolve 되기 전 (즉 `result` 가 초기화되기 전) 에 콜백이 호출될 때
      //   ReferenceError: Cannot access 'result' before initialization 이 발생한다.
      //   pdf.js / html2canvas 모두 동기 진입 직후 첫 onProgress(0, total) 을 보내므로
      //   100% 재현된다. PDF 는 fallback 도 없어 사용자에게 즉시 에러로 노출됐고,
      //   HTML 은 매번 백엔드 fallback 으로 빠져 #313 의 비용 절감 목표가 무효화됐다.
      //
      //   해결: 파일 확장자/MIME 으로 모드를 먼저 추정한 별도 변수 `detectedMode` 를
      //   둬서 콜백은 `result` 가 아닌 그 변수만 읽도록 한다. 캡처가 resolve 된
      //   후에는 실제 결과의 mode 로 갱신 — 이후 단계 (uploading/done) 에서도
      //   동일 변수를 재사용한다.
      let pages: Blob[] = [];
      const initialMode: "pdf" | "html" =
        file.type === "application/pdf" || /\.pdf$/i.test(file.name) ? "pdf" : "html";
      let mode: "pdf" | "html" = initialMode;
      let warnings: string[] = [];
      let totalPages = 0;
      try {
        const result = await capturePresentation(file, file.name, {
          signal: controller.signal,
          onProgress: (current, total) => {
            setStatus({ kind: "capturing", current, total, mode });
          },
        });
        pages = result.blobs;
        mode = result.mode;
        warnings = result.warnings;
        totalPages = result.totalPages;
      } catch (e) {
        // 사용자 취소 — 그냥 idle 로.
        if ((e as Error).name === "AbortError") {
          setStatus({ kind: "idle" });
          return;
        }
        if (e instanceof PresentationCaptureUnsupportedError) {
          // PDF/HTML 이 아닌 형식 — 백엔드 fallback 도 불가능하므로 에러 노출.
          setStatus({
            kind: "error",
            message: e.message,
          });
          return;
        }
        // 그 외 클라이언트 캡처 실패 — 백엔드 fallback 시도 (HTML 만 가능).
        console.warn("[presentation] 클라 캡처 실패, 백엔드 fallback 시도:", e);
        if (/\.html?$/i.test(file.name) || file.type === "text/html") {
          setStatus({ kind: "uploading", pages: 0, mode: "fallback" });
          try {
            const assets = await uploadPresentation(projectId, file);
            onAssetsAdded(assets);
            setStatus({
              kind: "done",
              pages: assets.length,
              mode: "fallback",
              warnings: [
                "클라이언트 캡처에 실패하여 백엔드 Playwright 경로로 처리했습니다 (비용 발생).",
              ],
            });
          } catch (uploadErr) {
            setStatus({
              kind: "error",
              message: `백엔드 fallback 도 실패했습니다: ${(uploadErr as Error).message}`,
            });
          }
          return;
        }
        setStatus({
          kind: "error",
          message: `클라이언트 캡처에 실패했습니다: ${(e as Error).message}`,
        });
        return;
      }

      if (pages.length === 0) {
        setStatus({
          kind: "error",
          message: "추출된 페이지가 없습니다. 파일을 확인하세요.",
        });
        return;
      }

      // 2) 추출된 PNG 들을 백엔드에 일괄 업로드 (이미지 푸티지 등록)
      setStatus({ kind: "uploading", pages: pages.length, mode });
      const stem = file.name.replace(/\.[^.]+$/, "") || "presentation";
      const imageFiles = blobsToPresentationFiles(pages, stem);
      try {
        const { assets, client_capture_used } = await uploadPresentationImages(
          projectId,
          imageFiles,
          { stem, mode, total_pages: totalPages }
        );
        onAssetsAdded(assets);
        const finalWarnings = [...warnings];
        if (client_capture_used === false) {
          // 백엔드가 실수로 fallback 을 탔을 때 — 디버깅 흔적.
          finalWarnings.push(
            "백엔드가 클라이언트 캡처 모드를 인식하지 못했습니다 — 관리자에게 문의하세요."
          );
        }
        setStatus({
          kind: "done",
          pages: assets.length,
          mode,
          warnings: finalWarnings,
        });
      } catch (uploadErr) {
        setStatus({
          kind: "error",
          message: `이미지 업로드에 실패했습니다: ${(uploadErr as Error).message}`,
        });
      }
    } catch (e) {
      setStatus({
        kind: "error",
        message: `처리 중 오류: ${(e as Error).message}`,
      });
    } finally {
      // 파일 input 초기화 — 동일 파일을 다시 선택해도 onChange 가 동작하도록.
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function handleCancel() {
    abortRef.current?.abort();
    setStatus({ kind: "idle" });
  }

  return (
    <div style={pts.root}>
      <div style={pts.intro}>
        <h3 style={pts.title}>프레젠테이션 캡처</h3>
        <p style={pts.desc}>
          PDF 또는 HTML 슬라이드 파일을 업로드하면 페이지별 이미지로 변환해
          footage 에 등록합니다. <strong>변환은 사용자 브라우저에서 수행</strong>
          되어 서버 비용이 발생하지 않습니다.
        </p>
        <ul style={pts.bullets}>
          <li>
            <strong>PDF</strong>: pdf.js 로 페이지 정확히 재현 (해상도 1.5×).
          </li>
          <li>
            <strong>HTML</strong>: <code>&lt;section&gt;</code>, <code>.slide</code>,{" "}
            <code>[data-page]</code>, <code>.step</code> 순으로 슬라이드 감지.
            <span style={pts.note}>
              {" "}
              외부 폰트/이미지(CORS) 는 누락될 수 있으며, 그 경우 백엔드 캡처로 자동 우회합니다.
            </span>
          </li>
        </ul>
      </div>

      {status.kind === "idle" && (
        <div style={pts.dropZone}>
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf,application/pdf,.html,.htm,text/html"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void handleFile(f);
            }}
          />
          <button
            style={pts.uploadBtn}
            onClick={() => fileInputRef.current?.click()}
          >
            PDF / HTML 파일 선택
          </button>
        </div>
      )}

      {status.kind === "loading" && (
        <div style={pts.progress}>
          <p style={pts.progressText}>라이브러리 로드 중…</p>
        </div>
      )}

      {status.kind === "capturing" && (
        <div style={pts.progress}>
          <p style={pts.progressText}>
            {status.mode === "pdf" ? "PDF" : "HTML"} 페이지 캡처 중…{" "}
            <strong>
              {status.current} / {status.total}
            </strong>
          </p>
          <div style={pts.progressBarOuter}>
            <div
              style={{
                ...pts.progressBarInner,
                width: `${Math.round(
                  (status.current / Math.max(1, status.total)) * 100
                )}%`,
              }}
            />
          </div>
          <button style={pts.cancelBtn} onClick={handleCancel}>
            취소
          </button>
        </div>
      )}

      {status.kind === "uploading" && (
        <div style={pts.progress}>
          <p style={pts.progressText}>
            {status.mode === "fallback"
              ? "백엔드 캡처 경로(Playwright) 로 업로드 중… (비용 발생)"
              : `이미지 ${status.pages} 페이지 업로드 중…`}
          </p>
        </div>
      )}

      {status.kind === "done" && (
        <div style={pts.done}>
          <p style={pts.doneText}>
            {status.pages} 페이지를 footage 에 등록했습니다 (
            {status.mode === "pdf"
              ? "PDF 클라이언트 캡처"
              : status.mode === "html"
                ? "HTML 클라이언트 캡처"
                : "백엔드 fallback"}
            ).
          </p>
          {status.warnings.length > 0 && (
            <ul style={pts.warningList}>
              {status.warnings.map((w, i) => (
                <li key={i} style={pts.warning}>
                  ⚠ {w}
                </li>
              ))}
            </ul>
          )}
          <button
            style={pts.uploadBtn}
            onClick={() => setStatus({ kind: "idle" })}
          >
            다른 파일 캡처
          </button>
        </div>
      )}

      {status.kind === "error" && (
        <div style={pts.errorBox}>
          <p>{status.message}</p>
          <button
            style={pts.uploadBtn}
            onClick={() => setStatus({ kind: "idle" })}
          >
            다시 시도
          </button>
        </div>
      )}
    </div>
  );
}

const pts = {
  root: { padding: "8px 4px" } as React.CSSProperties,
  intro: { marginBottom: 24 } as React.CSSProperties,
  title: { color: "#eee", margin: "0 0 8px 0", fontSize: 18 } as React.CSSProperties,
  desc: { color: "#aaa", fontSize: 14, lineHeight: 1.6, margin: "0 0 12px 0" } as React.CSSProperties,
  bullets: {
    color: "#aaa", fontSize: 13, lineHeight: 1.7,
    paddingLeft: 20, margin: 0,
  } as React.CSSProperties,
  note: { color: "#888", fontSize: 12 } as React.CSSProperties,
  dropZone: {
    border: "1px dashed #2a2a4a", background: "#0f0f17",
    borderRadius: 10, padding: 40, textAlign: "center" as const,
  } as React.CSSProperties,
  uploadBtn: {
    padding: "12px 24px", background: "#6366f1", color: "#fff",
    border: "none", borderRadius: 8, cursor: "pointer",
    fontSize: 14, fontWeight: 600,
  } as React.CSSProperties,
  progress: {
    background: "#0f0f17", border: "1px solid #2a2a4a",
    borderRadius: 10, padding: 24, textAlign: "center" as const,
  } as React.CSSProperties,
  progressText: { color: "#ddd", fontSize: 14, margin: "0 0 12px 0" } as React.CSSProperties,
  progressBarOuter: {
    width: "100%", height: 8, background: "#1a1a2e",
    borderRadius: 4, overflow: "hidden" as const, margin: "0 0 16px 0",
  } as React.CSSProperties,
  progressBarInner: {
    height: "100%", background: "#6366f1", transition: "width 0.2s linear",
  } as React.CSSProperties,
  cancelBtn: {
    padding: "8px 16px", background: "#2a1a1a", color: "#f87171",
    border: "1px solid #3a1a1a", borderRadius: 6, cursor: "pointer", fontSize: 13,
  } as React.CSSProperties,
  done: {
    background: "#0f1f0f", border: "1px solid #2a4a2a",
    borderRadius: 10, padding: 24, textAlign: "center" as const,
  } as React.CSSProperties,
  doneText: { color: "#4ade80", fontSize: 14, margin: "0 0 12px 0" } as React.CSSProperties,
  warningList: { listStyle: "none" as const, padding: 0, margin: "8px 0 16px 0" } as React.CSSProperties,
  warning: { color: "#fbbf24", fontSize: 12, marginBottom: 4, textAlign: "left" as const } as React.CSSProperties,
  errorBox: {
    background: "#2a1a1a", border: "1px solid #3a1a1a", color: "#f87171",
    borderRadius: 10, padding: 24, textAlign: "center" as const, fontSize: 13,
  } as React.CSSProperties,
};
