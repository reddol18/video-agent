import { useEffect, useRef, useState } from "react";
import Setup from "./pages/Setup";
import Editor from "./pages/Editor";
import Settings from "./pages/Settings";
import Studio from "./pages/Studio";
import Migrate from "./pages/Migrate";
import BgmEditor from "./pages/BgmEditor";
import { listProjects, deleteProject, getProject, createProject, duplicateProject, updateProject } from "./lib/project";
import { useStore } from "./store";
import { ui } from "./store/ui";
import UiHost from "./components/UiHost";
import { globalThemeCss } from "./styles/tokens";
import type { Project } from "./types";
import { groupProjectsBySeries } from "./lib/groupProjectsBySeries";
import { resolveDragMoveTargets } from "./lib/resolveDragMoveTargets";
import { useDragAutoScroll } from "./lib/useDragAutoScroll";
import { isSeriesCollapsed, setSeriesCollapsed } from "./lib/seriesCollapse";
// #407 — 템플릿 선택 모달 (홈 화면 '템플릿으로 만들기' 진입점).
import TemplatePickerModal from "./components/TemplatePickerModal";
import SfxLibraryModal from "./components/SfxLibraryModal";
import {
  createProjectFromTemplate,
  listSeries,
  createSeries,
  deleteSeries,
  type SeriesItem,
} from "./api/client";

// #289 — bgm 화면 추가 (Editor에서 렌더 완료 후 진입).
type Screen = "home" | "setup" | "editor" | "settings" | "studio" | "migrate" | "bgm";
const VALID_SCREENS: Screen[] = ["home", "setup", "editor", "settings", "studio", "migrate", "bgm"];

// URL 쿼리스트링과 화면 state를 양방향 동기화한다.
// 새로고침 시에도 같은 화면 복원, 브라우저 뒤로가기/앞으로가기도 동작.
function readRouteFromUrl(): { screen: Screen; projectId: string | null; assetId?: string } {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get("screen");
  const screen: Screen = (VALID_SCREENS as string[]).includes(raw ?? "") ? (raw as Screen) : "home";
  const pid = params.get("pid");
  const aid = params.get("aid") ?? undefined;
  return { screen, projectId: pid && pid.length > 0 ? pid : null, assetId: aid };
}

function writeRouteToUrl(screen: Screen, projectId: string | null, assetId?: string, replace = false) {
  const params = new URLSearchParams();
  if (screen !== "home") params.set("screen", screen);
  if (projectId) params.set("pid", projectId);
  if (assetId) params.set("aid", assetId);
  const qs = params.toString();
  const url = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
  if (replace) window.history.replaceState(null, "", url);
  else window.history.pushState(null, "", url);
}

export default function App() {
  // UiHost(공용 Toast/Modal)를 항상 렌더 — AppInner 의 여러 early-return 와 무관하게 최상위에 1회 마운트.
  return (
    <>
      {/* #300 — 전역 다크 테마 일관성: focus-visible outline / disabled / 링크 hover */}
      <style>{globalThemeCss}</style>
      <UiHost />
      <AppInner />
    </>
  );
}

function AppInner() {
  const initial = (() => { try { return readRouteFromUrl(); } catch { return { screen: "home" as Screen, projectId: null, assetId: undefined }; } })();
  const [screen, setScreenState] = useState<Screen>(initial.screen);
  const [projectId, setProjectId] = useState<string | null>(initial.projectId);
  const [studioProjectName, setStudioProjectName] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [editorKey, setEditorKey] = useState(0);
  const [studioInitialAssetId, setStudioInitialAssetId] = useState<string | undefined>(initial.assetId);
  // #407 — 템플릿 선택 모달 표시 여부.
  const [templatePickerOpen, setTemplatePickerOpen] = useState(false);
  const [sfxLibraryOpen, setSfxLibraryOpen] = useState(false);
  // #293 — URL 직접 진입 시 프로젝트 로드 상태. 새 브라우저/시크릿(IndexedDB 없음)에서
  // 서버 fallback이 끝날 때까지 스피너를, 실패 시 명확한 안내를 보여주기 위함.
  const [projectLoadState, setProjectLoadState] = useState<"idle" | "loading" | "error">("idle");
  // 시리즈 목록(빈 시리즈 포함). 홈 그룹핑 합집합 + DnD 드롭 대상용.
  const [seriesList, setSeriesList] = useState<SeriesItem[]>([]);
  // 멀티셀렉트로 선택된 project_id 집합. 카드 체크박스 + DnD 일괄 이동 대상.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const { setProject, setSetupStep } = useStore();

  // 카드 드래그 중 화면 상/하단 가장자리에서 window 자동 스크롤(긴 리스트에서 뒤쪽 그룹 접근용).
  // 카드 드래그(DND_PROJECT_MIME)일 때만 발동 — 일반 스크롤은 방해하지 않음.
  useDragAutoScroll((types) => types.includes(DND_PROJECT_MIME));

  // setScreen 래퍼 — state 변경과 URL 동기화를 함께 수행.
  // 동일 화면으로의 호출은 무시(중복 history push 방지).
  function setScreen(next: Screen) {
    setScreenState((prev) => {
      if (prev !== next) writeRouteToUrl(next, projectId, studioInitialAssetId);
      return next;
    });
  }

  // projectId / assetId 변경 시 URL replace(같은 화면의 파라미터만 갱신, 히스토리 누적 방지).
  useEffect(() => {
    writeRouteToUrl(screen, projectId, studioInitialAssetId, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, studioInitialAssetId]);

  // popstate — 브라우저 뒤로/앞으로가기 시 URL을 읽어 state 복원.
  useEffect(() => {
    function onPop() {
      const r = readRouteFromUrl();
      setScreenState(r.screen);
      setProjectId(r.projectId);
      setStudioInitialAssetId(r.assetId);
    }
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // 새로고침/URL 직접 진입 직후 — URL에 projectId가 있는데 store에 project가 없으면 로드.
  // getProject는 IndexedDB → 서버 fallback 순서로 동작(#293)하므로, 새 브라우저/시크릿
  // 모드처럼 IndexedDB가 비어 있어도 서버에서 받아와 화면을 정상 렌더한다.
  // 로딩 중에는 스피너를, 서버에도 없으면 명확한 안내를 보여준다.
  useEffect(() => {
    if (!projectId) {
      setProjectLoadState("idle");
      return;
    }
    const current = useStore.getState().project;
    if (current?.project_id === projectId) {
      setProjectLoadState("idle");
      return;
    }
    let cancelled = false;
    setProjectLoadState("loading");
    getProject(projectId, { checkConflict: true })
      .then((p) => {
        if (cancelled) return;
        setProject(p);
        setStudioProjectName(p.name);
        setProjectLoadState("idle");
      })
      .catch(() => {
        if (cancelled) return;
        setProjectLoadState("error");
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    if (screen === "home") {
      listProjects().then(setProjects).catch(() => {});
      // 빈 시리즈까지 포함한 시리즈 목록. 실패해도 프로젝트 series 값 기준 그룹핑은 동작.
      listSeries().then(setSeriesList).catch(() => {});
    }
  }, [screen]);

  async function handleResume(id: string) {
    try {
      const p = await getProject(id, { checkConflict: true });
      setProject(p);
      setProjectId(id);
      setScreen("editor");
    } catch {
      ui.error("프로젝트를 불러오지 못했습니다.");
    }
  }

  async function handleDelete(id: string) {
    const ok = await ui.confirm({
      title: "프로젝트 삭제",
      message: "이 프로젝트를 삭제하시겠습니까?",
      variant: "danger",
      confirmLabel: "삭제",
    });
    if (!ok) return;
    await deleteProject(id);
    setProjects((prev) => prev.filter((p) => p.project_id !== id));
  }

  async function handleDuplicate(id: string) {
    try {
      const newProject = await duplicateProject(id);
      setProjects((prev) => [newProject, ...prev]);
    } catch {
      ui.error("프로젝트 복제에 실패했습니다.");
    }
  }

  async function handleRename(id: string, newName: string) {
    const trimmed = newName.trim();
    if (!trimmed) return;
    try {
      await updateProject(id, { name: trimmed });
      setProjects((prev) =>
        prev.map((p) => (p.project_id === id ? { ...p, name: trimmed } : p))
      );
    } catch {
      ui.error("프로젝트 이름 변경에 실패했습니다.");
    }
  }

  async function handleSetSeries(id: string, series: string) {
    const trimmed = series.trim();
    try {
      await updateProject(id, { series: trimmed });
      setProjects((prev) =>
        prev.map((p) => (p.project_id === id ? { ...p, series: trimmed } : p))
      );
    } catch {
      ui.error("시리즈 지정에 실패했습니다.");
    }
  }

  // 새 시리즈 생성 — 프로젝트 0개인 빈 그룹으로 즉시 표시된다.
  async function handleCreateSeries() {
    const name = await ui.prompt({
      title: "새 시리즈",
      message: "새 시리즈 이름을 입력하세요. (먼저 만들고 나중에 프로젝트를 넣을 수 있습니다)",
      defaultValue: "",
      confirmLabel: "만들기",
    });
    if (!name || !name.trim()) return;
    try {
      const created = await createSeries(name.trim());
      setSeriesList((prev) =>
        prev.some((s) => s.id === created.id) ? prev : [...prev, created]
      );
    } catch {
      ui.error("시리즈 생성에 실패했습니다.");
    }
  }

  // 시리즈 삭제 — 서버가 소속 프로젝트 series 를 미편성으로 되돌린다. 로컬도 반영.
  async function handleDeleteSeries(seriesId: string, seriesName: string) {
    const ok = await ui.confirm({
      title: "시리즈 삭제",
      message: `"${seriesName}" 시리즈를 삭제할까요? 이 시리즈의 프로젝트는 삭제되지 않고 '시리즈 미편성'으로 이동합니다.`,
      variant: "danger",
      confirmLabel: "삭제",
    });
    if (!ok) return;
    try {
      await deleteSeries(seriesId);
      setSeriesList((prev) => prev.filter((s) => s.id !== seriesId));
      setProjects((prev) =>
        prev.map((p) => ((p.series || "").trim() === seriesName ? { ...p, series: "" } : p))
      );
    } catch {
      ui.error("시리즈 삭제에 실패했습니다.");
    }
  }

  function toggleSelect(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function clearSelection() {
    setSelectedIds(new Set());
  }

  // DnD 드롭 처리 — 드래그한 카드가 선택 집합에 포함되면 선택 전체를, 아니면 그 카드만
  // 대상 시리즈로 일괄 이동한다. targetSeries="" 는 미편성으로의 이동.
  async function handleMoveToSeries(draggedId: string, targetSeries: string) {
    const moveIds = resolveDragMoveTargets(draggedId, selectedIds);
    const target = targetSeries.trim();
    // 이미 대상 시리즈면 제외(불필요한 서버 호출 방지).
    const toMove = moveIds.filter((id) => {
      const p = projects.find((pp) => pp.project_id === id);
      return p && (p.series || "").trim() !== target;
    });
    if (toMove.length === 0) {
      clearSelection();
      return;
    }
    // 낙관적 반영.
    setProjects((prev) =>
      prev.map((p) => (toMove.includes(p.project_id) ? { ...p, series: target } : p))
    );
    clearSelection();
    try {
      await Promise.all(toMove.map((id) => updateProject(id, { series: target })));
    } catch {
      ui.error("시리즈 이동에 실패했습니다. 새로고침 후 확인해 주세요.");
      // 실패 시 서버 기준으로 재동기화.
      listProjects().then(setProjects).catch(() => {});
    }
  }

  async function handleNewStudio() {
    // 그룹(시리즈)은 선택 입력 — 취소 시 반영되지 않도록 확정 시점에만 값이 채워진다.
    let pickedSeries = "";
    const name = await ui.prompt({
      title: "새 스튜디오 프로젝트",
      message: "스튜디오 프로젝트 이름을 입력하세요",
      defaultValue: "새 스튜디오 프로젝트",
      confirmLabel: "생성",
      extraField: {
        label: "프로젝트 그룹 (선택 — 비워두면 미편성)",
        placeholder: "예: AI펀드매니저",
        options: knownSeries,
        onChange: (v) => { pickedSeries = v; },
      },
    });
    if (!name) return;
    try {
      const p = await createProject(name, "", "", pickedSeries);
      // 새 그룹명이면 시리즈 저장소에도 등록(멱등) — 홈 화면 그룹 목록에 즉시 나타나게 한다.
      if (pickedSeries) {
        try {
          await createSeries(pickedSeries);
          setSeriesList(await listSeries());
        } catch { /* 목록 등록 실패해도 프로젝트의 series 값은 이미 반영됨 */ }
      }
      setProject(p);
      setProjectId(p.project_id);
      setStudioProjectName(p.name);
      setScreen("studio");
    } catch {
      ui.error("프로젝트 생성에 실패했습니다.");
    }
  }

  // #407 — 템플릿 선택 모달에서 사용자가 템플릿을 고르면 호출.
  // 백엔드 createProjectFromTemplate 로 새 project_id 발급 후 Editor 로 진입.
  async function handlePickTemplate(templateId: string, defaultTitle: string) {
    const title = await ui.prompt({
      title: "새 프로젝트 이름",
      message: `템플릿 "${defaultTitle}" 으로 만들 새 프로젝트의 이름을 입력하세요.`,
      defaultValue: `[템플릿] ${defaultTitle}`,
      confirmLabel: "만들기",
    });
    if (!title || !title.trim()) return;
    try {
      const newProject = await createProjectFromTemplate({
        template_id: templateId,
        title: title.trim(),
      });
      setTemplatePickerOpen(false);
      // 새 프로젝트를 즉시 진입. 서버에 이미 저장된 상태라 IndexedDB 미러링은
      // getProject(_, { checkConflict }) 흐름에서 자동으로 일어난다.
      setProject(newProject);
      setProjectId(newProject.project_id);
      setStudioProjectName(newProject.name);
      // 편집기 진입 — 사용자는 씬별로 영상만 업로드하면 된다.
      setScreen("editor");
      ui.success(`템플릿에서 새 프로젝트가 생성되었습니다 (씬 ${newProject.scenes.length}개)`);
    } catch (e) {
      ui.error(`템플릿에서 프로젝트 생성 실패: ${(e as Error).message}`);
    }
  }

  async function handleOpenStudio(id: string, name: string, assetId?: string) {
    try {
      const p = await getProject(id, { checkConflict: true });
      setProject(p);
      setProjectId(id);
      setStudioProjectName(name);
      setStudioInitialAssetId(assetId);
      setScreen("studio");
    } catch {
      ui.error("프로젝트를 불러오지 못했습니다.");
    }
  }

  // #293 — URL로 project 종속 화면에 직접 진입했고 아직 store에 프로젝트가 없을 때의 처리.
  // 서버 fallback(getProject) 진행 중이면 스피너, 서버에도 없으면 안내 + 홈 이동.
  // home/settings/migrate는 프로젝트가 필요 없으므로 이 오버레이를 적용하지 않는다.
  const needsProject = screen !== "home" && screen !== "settings" && screen !== "migrate";
  const storeProject = useStore.getState().project;
  const projectReady = !!projectId && storeProject?.project_id === projectId;

  function goHome() {
    setProjectId(null);
    setScreen("home");
  }

  if (needsProject && projectId && !projectReady) {
    if (projectLoadState === "error") {
      return (
        <div style={styles.centerScreen}>
          <div style={styles.centerCard}>
            <div style={{ fontSize: 18, color: "#fff", marginBottom: 8 }}>프로젝트를 찾을 수 없습니다</div>
            <div style={{ fontSize: 13, color: "#888", marginBottom: 20 }}>
              요청한 프로젝트가 서버에 존재하지 않거나 불러오지 못했습니다.
            </div>
            <button style={styles.resumeBtn} onClick={goHome}>홈으로</button>
          </div>
        </div>
      );
    }
    // loading / idle(로드 시작 직전) — 스피너 표시.
    return (
      <div style={styles.centerScreen}>
        <style>{"@keyframes va-spin { to { transform: rotate(360deg); } }"}</style>
        <div style={styles.centerCard}>
          <div style={styles.spinner} />
          <div style={{ fontSize: 14, color: "#aaa", marginTop: 16 }}>프로젝트 불러오는 중...</div>
        </div>
      </div>
    );
  }

  if (screen === "migrate") {
    return <Migrate onDone={() => { setScreen("home"); }} />;
  }

  if (screen === "settings") {
    return <Settings onBack={() => setScreen("home")} onMigrate={() => setScreen("migrate")} />;
  }

  if (screen === "setup") {
    return (
      <Setup
        onComplete={(id) => { setProjectId(id); setScreen("editor"); }}
        onBack={() => setScreen(projectId ? "studio" : "home")}
        initialProjectId={projectId ?? undefined}
        initialProjectName={studioProjectName || undefined}
      />
    );
  }

  if (screen === "editor" && projectId) {
    const editorProject = projects.find((p) => p.project_id === projectId);
    return (
      <Editor
        key={`editor-${projectId}-${editorKey}`}
        projectId={projectId}
        onBack={() => setScreen("home")}
        onOpenStudio={(assetId) => handleOpenStudio(projectId, editorProject?.name ?? "", assetId)}
        onOpenBgm={() => setScreen("bgm")}
      />
    );
  }

  // #289 — BGM 후처리 에디터 (렌더 완료 후 진입).
  if (screen === "bgm" && projectId) {
    return (
      <BgmEditorScreen
        projectId={projectId}
        onBack={() => setScreen("editor")}
      />
    );
  }

  if (screen === "studio" && projectId) {
    return (
      <Studio
        projectId={projectId}
        projectName={studioProjectName}
        onGoToScenario={async () => {
          const p = await getProject(projectId).catch(() => null);
          if (p) setProject(p);
          setEditorKey((k) => k + 1);
          setScreen("setup");
        }}
        onBack={() => setScreen("home")}
        initialAssetId={studioInitialAssetId}
      />
    );
  }

  // Home screen
  const matchedProjects = projects.filter((p) =>
    p.scenes.length > 0 && p.scenes.every((s) => s.footage !== null)
  );
  const inProgressProjects = projects.filter((p) =>
    !matchedProjects.includes(p)
  );

  // 기존 시리즈명 목록(자동완성 datalist 제안용 + 빈 시리즈 병합용).
  // = 프로젝트들의 series 값 ∪ 서버 시리즈 목록(빈 시리즈 포함). 미편성 제외.
  const seriesNames = seriesList.map((s) => s.name);
  const knownSeries = Array.from(
    new Set([
      ...projects.map((p) => (p.series || "").trim()).filter(Boolean),
      ...seriesNames.map((n) => n.trim()).filter(Boolean),
    ])
  ).sort();
  // 시리즈명 → id 역인덱스(빈 그룹 삭제 버튼용).
  const seriesIdByName = new Map(seriesList.map((s) => [s.name.trim(), s.id]));

  // status 2섹션 구조를 유지하되 각 섹션 내부를 시리즈로 서브그룹핑한다.
  // seriesNames 를 넘겨 프로젝트 0개의 빈 시리즈도 그룹(드롭 대상)으로 렌더한다.
  const renderSection = (list: Project[], inProgress: boolean) =>
    groupProjectsBySeries(list, seriesNames).map((group) => {
      // 드롭 대상 시리즈명. 미편성 그룹은 "" (series 해제).
      const dropSeries = group.ungrouped ? "" : group.series;
      const deletableSeriesId = group.ungrouped ? undefined : seriesIdByName.get(group.series);
      return (
        <SeriesGroupView
          key={group.series}
          group={group}
          onDropProject={(draggedId) => handleMoveToSeries(draggedId, dropSeries)}
          onDeleteSeries={
            deletableSeriesId
              ? () => handleDeleteSeries(deletableSeriesId, group.series)
              : undefined
          }
        >
          {group.projects.map((p) => (
            <ProjectCard
              key={p.project_id}
              project={p}
              knownSeries={knownSeries}
              selected={selectedIds.has(p.project_id)}
              onToggleSelect={() => toggleSelect(p.project_id)}
              onDragStartCard={() => {
                // 드래그 시작한 카드가 선택 집합에 없으면 그 카드만 대상이 되도록
                // 선택을 유지한다(resolveDragMoveTargets 가 판정). 별도 처리 불필요.
              }}
              onResume={() => handleResume(p.project_id)}
              onDelete={() => handleDelete(p.project_id)}
              onOpenStudio={() => handleOpenStudio(p.project_id, p.name)}
              onDuplicate={() => handleDuplicate(p.project_id)}
              onRename={(newName) => handleRename(p.project_id, newName)}
              onSetSeries={(s) => handleSetSeries(p.project_id, s)}
              inProgress={inProgress}
            />
          ))}
        </SeriesGroupView>
      );
    });

  return (
    <div style={styles.container}>
      <div style={styles.inner}>
        <h1 style={styles.title}>🎬 video-agent 편집기</h1>

        <div style={styles.topRow}>
          {/* #291 — 상단 4개 버튼에 hover tooltip 추가 */}
          <button style={styles.newBtn} onClick={() => { setProjectId(null); setStudioProjectName(""); setSetupStep(1); setScreen("setup"); }} title="새 영상 프로젝트 만들기">
            + 새 프로젝트
          </button>
          <button style={styles.studioBtn} onClick={handleNewStudio} title="재료 영상/이미지를 관리">
            footage 스튜디오
          </button>
          {/* #407 — 저장된 템플릿을 출발점으로 새 프로젝트를 만든다. */}
          <button
            style={styles.studioBtn}
            onClick={() => setTemplatePickerOpen(true)}
            title="저장된 영상 구조 템플릿으로 새 프로젝트 만들기"
          >
            📐 템플릿으로 만들기
          </button>
          {/* 빈 시리즈도 먼저 만들 수 있게 하는 진입점. */}
          <button
            style={styles.studioBtn}
            onClick={handleCreateSeries}
            title="빈 시리즈를 먼저 만들고 나중에 프로젝트를 넣기"
          >
            + 새 시리즈
          </button>
          {/* 모든 프로젝트가 공유하는 효과음을 올리고 관리. */}
          <button
            style={styles.studioBtn}
            onClick={() => setSfxLibraryOpen(true)}
            title="모든 프로젝트에서 쓸 효과음 올리기·관리"
          >
            🔊 효과음 라이브러리
          </button>
          {/* #298 — 자주 쓰지 않는 일회성 "가져오기"는 ⚙ 설정 메뉴 안으로 이동. */}
          <button style={styles.settingsBtn} onClick={() => setScreen("settings")} title="설정">
            ⚙
          </button>
        </div>

        {/* 멀티셀렉트 툴바 — 1개 이상 선택 시 노출. 시리즈 헤더로 드래그해 일괄 이동. */}
        {selectedIds.size > 0 && (
          <div style={styles.selectBar}>
            <span style={styles.selectBarText}>
              {selectedIds.size}개 선택됨 · 카드를 시리즈 그룹으로 드래그해 이동하세요
            </span>
            <button style={styles.selectClearBtn} onClick={clearSelection}>
              선택 해제
            </button>
          </div>
        )}

        {/* #291 — 빈 상태: 한 줄 메시지 대신 온보딩 가이드 카드 */}
        {projects.length === 0 && (
          <div style={styles.emptyCard}>
            <div style={styles.emptyIcon}>🎬</div>
            <div style={styles.emptyHeadline}>AI로 영상을 만들어보세요</div>
            <div style={styles.emptySub}>
              시나리오 텍스트와 재료 영상만 있으면, 나레이션과 자막이 붙은 완성본을 자동으로 만들어 드립니다.
            </div>
            <div style={styles.emptySteps}>
              <span style={styles.emptyStepChip}>1) footage 등록</span>
              <span style={styles.emptyStepArrow}>→</span>
              <span style={styles.emptyStepChip}>2) 시나리오 작성</span>
              <span style={styles.emptyStepArrow}>→</span>
              <span style={styles.emptyStepChip}>3) 매칭/편집</span>
              <span style={styles.emptyStepArrow}>→</span>
              <span style={styles.emptyStepChip}>4) 렌더링</span>
            </div>
            <button
              style={styles.emptyCta}
              onClick={() => { setProjectId(null); setStudioProjectName(""); setSetupStep(1); setScreen("setup"); }}
            >
              시작하기 →
            </button>
          </div>
        )}

        {matchedProjects.length > 0 && (
          <section>
            <div style={styles.sectionTitle}>편집 가능한 프로젝트</div>
            {renderSection(matchedProjects, false)}
          </section>
        )}

        {inProgressProjects.length > 0 && (
          <section style={{ marginTop: 24 }}>
            <div style={styles.sectionTitle}>매칭 진행 중</div>
            {renderSection(inProgressProjects, true)}
          </section>
        )}
      </div>
      {/* #407 — 템플릿 선택 모달. 홈 화면에서만 노출. */}
      <TemplatePickerModal
        open={templatePickerOpen}
        onClose={() => setTemplatePickerOpen(false)}
        onPick={handlePickTemplate}
      />
      <SfxLibraryModal open={sfxLibraryOpen} onClose={() => setSfxLibraryOpen(false)} />
    </div>
  );
}

// DnD dataTransfer 에 담는 project_id 의 MIME 키. drop 시 이 값으로 드래그 대상을 식별.
const DND_PROJECT_MIME = "application/x-va-project-id";

// 시리즈 그룹 뷰 — 헤더가 드롭존이 되어 카드 드래그를 받는다. 빈 그룹도 드롭 대상.
function SeriesGroupView({
  group, children, onDropProject, onDeleteSeries,
}: {
  group: import("./lib/groupProjectsBySeries").SeriesGroup;
  children: React.ReactNode;
  onDropProject: (draggedProjectId: string) => void;
  onDeleteSeries?: () => void;
}) {
  const [dragOver, setDragOver] = useState(false);
  // 접기/펼치기 상태. localStorage 에 시리즈명 기준 저장(기본 펼침).
  // 미편성(ungrouped) 그룹은 항상 펼침 유지(접기 대상 아님).
  const [collapsed, setCollapsedState] = useState(() =>
    group.ungrouped ? false : isSeriesCollapsed(group.series)
  );
  function toggleCollapsed() {
    const next = !collapsed;
    setCollapsedState(next);
    setSeriesCollapsed(group.series, next);
  }

  function handleDragOver(e: React.DragEvent) {
    // project 카드 드래그일 때만 드롭 허용(파일 드롭 등과 구분).
    if (e.dataTransfer.types.includes(DND_PROJECT_MIME)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (!dragOver) setDragOver(true);
    }
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const id = e.dataTransfer.getData(DND_PROJECT_MIME);
    if (id) onDropProject(id);
  }

  const empty = group.projects.length === 0;
  return (
    <div>
      <div
        style={{
          ...styles.seriesHeader,
          ...(group.ungrouped ? styles.seriesHeaderUngrouped : {}),
          ...(dragOver ? styles.seriesHeaderDropOver : {}),
        }}
        onDragOver={handleDragOver}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        title="여기로 프로젝트 카드를 드래그해 이 시리즈로 이동"
      >
        {/* 접기/펼치기 토글 — 미편성 그룹에는 노출하지 않음. 접어도 헤더는 드롭존 유지. */}
        {!group.ungrouped && (
          <button
            style={styles.seriesToggleBtn}
            onClick={toggleCollapsed}
            title={collapsed ? "펼치기" : "접기"}
            // 토글 클릭이 카드 드래그로 오인되지 않도록.
            draggable={false}
          >
            {collapsed ? "▸" : "▾"}
          </button>
        )}
        <span>{group.ungrouped ? "📂 " : "📁 "}{group.series}</span>
        <span style={styles.seriesCount}>({group.projects.length})</span>
        {onDeleteSeries && (
          <button
            style={styles.seriesDeleteBtn}
            onClick={onDeleteSeries}
            title="이 시리즈 삭제(프로젝트는 미편성으로 이동)"
          >
            ✕
          </button>
        )}
      </div>
      {/* 접힌 그룹은 헤더만 남긴다(헤더 자체가 드롭존이라 접어도 이동 가능).
          빈 시리즈 안내와 카드 목록은 펼침 상태에서만 렌더. */}
      {!collapsed && empty && !group.ungrouped && (
        <div
          style={{ ...styles.emptySeriesDrop, ...(dragOver ? styles.emptySeriesDropOver : {}) }}
          onDragOver={handleDragOver}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
        >
          비어 있는 시리즈 — 프로젝트 카드를 여기로 드래그하세요
        </div>
      )}
      {!collapsed && children}
    </div>
  );
}

function ProjectCard({
  project, onResume, onDelete, onOpenStudio, onDuplicate, onRename, onSetSeries, knownSeries, inProgress,
  selected, onToggleSelect, onDragStartCard,
}: {
  project: Project;
  onResume: () => void;
  onDelete: () => void;
  onOpenStudio: () => void;
  onDuplicate: () => void;
  onRename: (newName: string) => void;
  onSetSeries: (series: string) => void;
  knownSeries: string[];
  inProgress?: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  onDragStartCard: () => void;
}) {
  const matched = project.scenes.filter((s) => s.footage !== null).length;
  const total = project.scenes.length;
  const ttsReady = project.scenes.filter((s) => s.tts?.status === "ready").length;
  const date = new Date(project.updated_at).toLocaleString("ko-KR");

  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(project.name);
  const cancelledRef = useRef(false);

  // 시리즈 인라인 편집 상태
  const [seriesEditing, setSeriesEditing] = useState(false);
  const [seriesValue, setSeriesValue] = useState(project.series || "");
  const seriesCancelledRef = useRef(false);
  const seriesListId = `series-options-${project.project_id}`;

  function startSeriesEdit() {
    seriesCancelledRef.current = false;
    setSeriesValue(project.series || "");
    setSeriesEditing(true);
  }

  function commitSeriesEdit() {
    if (seriesCancelledRef.current) {
      seriesCancelledRef.current = false;
      return;
    }
    const trimmed = seriesValue.trim();
    // 값이 실제로 바뀌었을 때만 저장(빈 문자열 = 시리즈 해제 허용).
    if (trimmed !== (project.series || "").trim()) {
      onSetSeries(trimmed);
    }
    setSeriesEditing(false);
  }

  function handleSeriesKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      commitSeriesEdit();
    } else if (e.key === "Escape") {
      seriesCancelledRef.current = true;
      setSeriesEditing(false);
    }
  }

  function startEdit() {
    cancelledRef.current = false;
    setEditValue(project.name);
    setEditing(true);
  }

  function commitEdit() {
    if (cancelledRef.current) {
      cancelledRef.current = false;
      return;
    }
    const trimmed = editValue.trim();
    if (trimmed) {
      onRename(trimmed);
    }
    setEditing(false);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      commitEdit();
    } else if (e.key === "Escape") {
      cancelledRef.current = true;
      setEditing(false);
    }
  }

  function handleCardDragStart(e: React.DragEvent) {
    onDragStartCard();
    e.dataTransfer.setData(DND_PROJECT_MIME, project.project_id);
    e.dataTransfer.effectAllowed = "move";
  }

  return (
    <div
      style={{ ...styles.card, ...(selected ? styles.cardSelected : {}) }}
      draggable
      onDragStart={handleCardDragStart}
    >
      {/* 멀티셀렉트 체크박스 — DnD 없이도 선택은 항상 동작(접근성 폴백). */}
      <input
        type="checkbox"
        checked={selected}
        onChange={onToggleSelect}
        style={styles.cardCheckbox}
        title="여러 프로젝트를 선택해 시리즈로 일괄 이동"
        // 체크박스 클릭이 카드 드래그로 오인되지 않도록.
        draggable={false}
        onClick={(e) => e.stopPropagation()}
      />
      <div style={styles.cardLeft}>
        {editing ? (
          <input
            style={styles.cardNameInput}
            value={editValue}
            autoFocus
            onChange={(e) => setEditValue(e.target.value)}
            onBlur={commitEdit}
            onKeyDown={handleKeyDown}
          />
        ) : (
          <div
            style={{ ...styles.cardName, ...(project.name ? {} : { color: "#555", fontStyle: "italic" }) }}
            onClick={startEdit}
            title="클릭하여 이름 수정"
          >
            {project.name || "이름 없음 (클릭하여 수정)"}
          </div>
        )}
        <div style={styles.cardMeta}>
          씬 {total}개 · 매칭 {matched}/{total}
          {ttsReady > 0 && ` · TTS ${ttsReady}개`}
        </div>
        <div style={styles.cardDate}>{date}</div>
        <div style={styles.cardSeries}>
          {seriesEditing ? (
            <>
              <input
                style={styles.cardSeriesInput}
                value={seriesValue}
                autoFocus
                list={seriesListId}
                placeholder="시리즈명 (비우면 해제)"
                onChange={(e) => setSeriesValue(e.target.value)}
                onBlur={commitSeriesEdit}
                onKeyDown={handleSeriesKeyDown}
              />
              <datalist id={seriesListId}>
                {knownSeries.map((s) => (
                  <option key={s} value={s} />
                ))}
              </datalist>
            </>
          ) : project.series ? (
            <span style={styles.cardSeriesChip} onClick={startSeriesEdit} title="클릭하여 시리즈 변경">
              📁 {project.series}
            </span>
          ) : (
            <span style={styles.cardSeriesEmpty} onClick={startSeriesEdit} title="클릭하여 시리즈 지정">
              + 시리즈 지정
            </span>
          )}
        </div>
        {inProgress && (
          <div style={styles.progressBar}>
            <div style={{ ...styles.progressFill, width: `${total ? (matched / total) * 100 : 0}%` }} />
          </div>
        )}
      </div>
      <div style={styles.cardActions}>
        <button style={styles.studioCardBtn} onClick={onOpenStudio} title="footage 스튜디오">
          스튜디오
        </button>
        <button style={styles.duplicateBtn} onClick={onDuplicate} title="footage 폴더를 공유하는 복제 프로젝트 생성">
          복제
        </button>
        <button style={styles.resumeBtn} onClick={onResume}>
          {inProgress ? "이어서 매칭" : "편집 열기"}
        </button>
        <button style={styles.deleteBtn} onClick={onDelete}>삭제</button>
      </div>
    </div>
  );
}

// ── #289 — BGM 후처리 에디터 진입용 래퍼 ───────────────────────────────────
// IndexedDB에서 프로젝트를 로드하고 final_output.mp4 길이를 ffprobe 없이
// HTMLVideoElement.duration으로 측정한 뒤 BgmEditor에 넘긴다.
function BgmEditorScreen({ projectId, onBack }: { projectId: string; onBack: () => void }) {
  const [project, setProjectState] = useState<Project | null>(null);
  const [duration, setDuration] = useState<number>(0);
  const [error, setError] = useState<string | null>(null);
  // #297 — 로딩 단계 메시지 + 3초 이상 지연 시 보조 액션 노출.
  const [loadStage, setLoadStage] = useState<string>("렌더링 결과 확인 중...");
  const [slow, setSlow] = useState(false);

  useEffect(() => {
    const slowTimer = setTimeout(() => setSlow(true), 3000);
    return () => clearTimeout(slowTimer);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoadStage("BGM 트랙 불러오는 중...");
    // BgmEditor 진입 시: IndexedDB + 서버 state를 모두 가져와 머지한다.
    // 이유: audio-upload는 서버 state.json에 직접 audio asset을 추가하므로 IndexedDB에는 없는 자산이 서버에 존재할 수 있다.
    // 머지 결과(서버 audio 자산까지 포함된 studio_assets)를 IndexedDB에도 저장해
    // 이후 register/syncScenesToServer가 호출돼도 옛 studio_assets로 서버를 덮어쓰지 않게 한다.
    (async () => {
      try {
        const [local, srvRes] = await Promise.all([
          getProject(projectId),
          fetch(`/api/projects/${projectId}`).then((r) => r.ok ? r.json() : null).catch(() => null),
        ]);
        if (cancelled) return;
        const merged: Project = { ...local };
        const srvAssets: Array<{ asset_id: string }> = (srvRes && Array.isArray(srvRes.studio_assets)) ? srvRes.studio_assets : [];
        const localAssets = local.studio_assets || [];
        const localIds = new Set(localAssets.map((a) => a.asset_id));
        const onlyOnServer = srvAssets.filter((a) => !localIds.has(a.asset_id));
        if (onlyOnServer.length > 0) {
          merged.studio_assets = [...localAssets, ...onlyOnServer] as typeof local.studio_assets;
          // IndexedDB에도 머지 결과 저장.
          try {
            const { updateProject: dbUpdate } = await import("./lib/project");
            await dbUpdate(projectId, { studio_assets: merged.studio_assets });
          } catch { /* ignore */ }
        }
        // bgm_tracks도 서버가 더 최신일 수 있음(다른 세션에서 추가).
        if (srvRes && Array.isArray(srvRes.bgm_tracks) && (!local.bgm_tracks || local.bgm_tracks.length === 0)) {
          merged.bgm_tracks = srvRes.bgm_tracks;
        }
        setProjectState(merged);
      } catch {
        if (!cancelled) setError("프로젝트를 불러오지 못했습니다.");
      }
    })();
    // final_output.mp4 길이는 video metadata로 측정 (별도 API 없이 동작).
    setLoadStage("영상 길이 분석 중...");
    const v = document.createElement("video");
    v.preload = "metadata";
    v.src = `/api/projects/${projectId}/render/output`;
    v.onloadedmetadata = () => {
      if (cancelled) return;
      if (isFinite(v.duration) && v.duration > 0) setDuration(v.duration);
    };
    v.onerror = () => {
      if (cancelled) return;
      setError("렌더링된 영상(final_output.mp4)을 불러올 수 없습니다. 편집기에서 BGM 편집 버튼을 다시 눌러 영상을 업로드하거나 먼저 렌더링하세요.");
    };
    return () => { cancelled = true; };
  }, [projectId]);

  if (error) {
    // #297 — final_output.mp4 없음 등 진입 실패 시 명확한 안내 + 보조 액션.
    return (
      <div style={styles.centerScreen}>
        <div style={{ ...styles.centerCard, maxWidth: 460 }}>
          <div style={{ fontSize: 40, marginBottom: 10 }}>🎵</div>
          <div style={{ fontSize: 18, color: "#fff", marginBottom: 10 }}>BGM 편집을 시작할 수 없습니다</div>
          <div style={{ fontSize: 13, color: "#999", lineHeight: 1.6, marginBottom: 20 }}>{error}</div>
          <button style={styles.resumeBtn} onClick={onBack}>← 편집기로 돌아가기</button>
        </div>
      </div>
    );
  }
  if (!project || duration <= 0) {
    // #297 — 단계별 로딩 메시지 + 3초 이상 지연 시 보조 액션.
    return (
      <div style={styles.centerScreen}>
        <style>{"@keyframes va-spin { to { transform: rotate(360deg); } }"}</style>
        <div style={{ ...styles.centerCard, maxWidth: 420 }}>
          <div style={styles.spinner} />
          <div style={{ fontSize: 15, color: "#ddd", marginTop: 18, fontWeight: 600 }}>BGM 에디터 준비 중</div>
          <div style={{ fontSize: 13, color: "#888", marginTop: 6 }}>{loadStage}</div>
          {slow && (
            <div style={{ marginTop: 22, padding: "12px 14px", background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 8, fontSize: 12, color: "#aaa", lineHeight: 1.6 }}>
              오래 걸리고 있나요?
              <div style={{ display: "flex", gap: 8, marginTop: 10, justifyContent: "center" }}>
                <button style={{ ...styles.migrateBtn, padding: "8px 14px", fontSize: 12 }} onClick={() => window.location.reload()}>새로고침</button>
                <button style={{ ...styles.settingsBtn, padding: "8px 14px", fontSize: 12 }} onClick={onBack}>← 편집기에서 영상 업로드</button>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }
  return (
    <BgmEditor
      projectId={projectId}
      project={project}
      videoDurationSec={duration}
      onProjectChange={setProjectState}
      onBack={onBack}
    />
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: { minHeight: "100vh", background: "#0f0f0f", display: "flex", alignItems: "flex-start", justifyContent: "center", padding: "60px 24px" },
  inner: { width: "100%", maxWidth: 640 },
  title: { color: "#fff", fontSize: 28, marginBottom: 32 },
  topRow: { display: "flex", gap: 10, marginBottom: 32 },
  newBtn: { flex: 1, padding: "14px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 10, fontSize: 16, cursor: "pointer" },
  studioBtn: { flex: 1, padding: "14px", background: "#1a2a1a", color: "#4ade80", border: "1px solid #2a4a2a", borderRadius: 10, fontSize: 16, cursor: "pointer" },
  migrateBtn: { padding: "14px 16px", background: "#1a1a2a", color: "#a5b4fc", border: "1px solid #2a2a4a", borderRadius: 10, fontSize: 14, cursor: "pointer" },
  settingsBtn: { padding: "14px 18px", background: "#252525", color: "#aaa", border: "1px solid #333", borderRadius: 10, fontSize: 18, cursor: "pointer" },
  studioCardBtn: { padding: "8px 12px", background: "#1a2a1a", color: "#4ade80", border: "1px solid #2a4a2a", borderRadius: 7, cursor: "pointer", fontSize: 12 },
  empty: { color: "#555", textAlign: "center", marginTop: 40 },
  // #291 — 빈 상태 온보딩 카드
  emptyCard: { background: "#1a1a1a", border: "1px solid #262626", borderRadius: 14, padding: "40px 28px", marginTop: 24, display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center", gap: 14 },
  emptyIcon: { fontSize: 44, lineHeight: 1 },
  emptyHeadline: { color: "#fff", fontSize: 20, fontWeight: 700 },
  emptySub: { color: "#999", fontSize: 14, lineHeight: 1.6, maxWidth: 440 },
  emptySteps: { display: "flex", alignItems: "center", flexWrap: "wrap" as const, justifyContent: "center", gap: 8, marginTop: 4 },
  emptyStepChip: { background: "#1e1b4b", color: "#a5b4fc", border: "1px solid #312e81", borderRadius: 999, padding: "5px 12px", fontSize: 12, fontWeight: 600 },
  emptyStepArrow: { color: "#555", fontSize: 13 },
  emptyCta: { marginTop: 10, padding: "13px 32px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 10, fontSize: 16, fontWeight: 600, cursor: "pointer" },
  sectionTitle: { color: "#666", fontSize: 11, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: 1, marginBottom: 10 },
  // 시리즈 서브그룹 헤더 (시리즈명 + 개수)
  seriesHeader: { display: "flex", alignItems: "center", gap: 8, color: "#a5b4fc", fontSize: 13, fontWeight: 700, margin: "14px 0 8px 4px", padding: "4px 8px", borderRadius: 8, border: "1px dashed transparent" },
  seriesToggleBtn: { background: "transparent", color: "#a5b4fc", border: "none", cursor: "pointer", fontSize: 12, padding: 0, marginRight: 2, lineHeight: 1, width: 14, textAlign: "center" as const },
  seriesHeaderUngrouped: { color: "#666" },
  seriesHeaderDropOver: { border: "1px dashed #6366f1", background: "#1e1b4b" },
  seriesCount: { color: "#666", fontSize: 12, fontWeight: 500 },
  seriesDeleteBtn: { marginLeft: 4, background: "transparent", color: "#666", border: "none", cursor: "pointer", fontSize: 12, padding: "0 4px" },
  emptySeriesDrop: { color: "#555", fontSize: 12, fontStyle: "italic" as const, border: "1px dashed #333", borderRadius: 8, padding: "12px", textAlign: "center" as const, marginBottom: 10 },
  emptySeriesDropOver: { border: "1px dashed #6366f1", background: "#1e1b4b", color: "#a5b4fc" },
  // 멀티셀렉트 툴바
  selectBar: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, background: "#1e1b4b", border: "1px solid #312e81", borderRadius: 10, padding: "10px 16px", marginBottom: 16 },
  selectBarText: { color: "#a5b4fc", fontSize: 13, fontWeight: 600 },
  selectClearBtn: { background: "#252525", color: "#aaa", border: "1px solid #333", borderRadius: 7, padding: "6px 12px", cursor: "pointer", fontSize: 12 },
  cardCheckbox: { width: 18, height: 18, cursor: "pointer", flexShrink: 0, accentColor: "#6366f1" },
  cardSelected: { border: "1px solid #6366f1", background: "#181826" },
  // 카드 내 시리즈 표시/편집 행
  cardSeries: { display: "inline-flex", alignItems: "center", gap: 6, marginTop: 4, fontSize: 12 },
  cardSeriesChip: { color: "#a5b4fc", background: "#1a1a2a", border: "1px solid #2a2a4a", borderRadius: 6, padding: "1px 8px", cursor: "pointer" },
  cardSeriesEmpty: { color: "#555", cursor: "pointer", fontStyle: "italic" as const },
  cardSeriesInput: { color: "#fff", fontSize: 12, background: "#2a2a2a", border: "1px solid #555", borderRadius: 4, padding: "2px 6px", outline: "none", width: 160 },
  card: { background: "#1a1a1a", borderRadius: 10, padding: "16px 20px", marginBottom: 10, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16 },
  cardLeft: { flex: 1, minWidth: 0 },
  cardName: { color: "#fff", fontWeight: 700, fontSize: 16, marginBottom: 4, cursor: "text" },
  cardNameInput: { color: "#fff", fontWeight: 700, fontSize: 16, marginBottom: 4, background: "#2a2a2a", border: "1px solid #555", borderRadius: 4, padding: "2px 6px", outline: "none", width: "100%" },
  cardMeta: { color: "#888", fontSize: 13, marginBottom: 2 },
  cardDate: { color: "#555", fontSize: 12 },
  progressBar: { height: 4, background: "#333", borderRadius: 2, marginTop: 8, overflow: "hidden" },
  progressFill: { height: "100%", background: "#6366f1", borderRadius: 2 },
  cardActions: { display: "flex", gap: 8, flexShrink: 0 },
  duplicateBtn: { padding: "8px 12px", background: "#1a1a2a", color: "#a5b4fc", border: "1px solid #2a2a4a", borderRadius: 7, cursor: "pointer", fontSize: 12 },
  resumeBtn: { padding: "8px 16px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 7, cursor: "pointer", fontSize: 13 },
  deleteBtn: { padding: "8px 12px", background: "#2a1a1a", color: "#f87171", border: "1px solid #3a1a1a", borderRadius: 7, cursor: "pointer", fontSize: 13 },
  // #293 — URL 직접 진입 시 프로젝트 로딩/에러 오버레이.
  centerScreen: { minHeight: "100vh", background: "#0f0f0f", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 },
  centerCard: { display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center", maxWidth: 360 },
  spinner: { width: 36, height: 36, border: "3px solid #2a2a2a", borderTopColor: "#6366f1", borderRadius: "50%", animation: "va-spin 0.8s linear infinite" },
};
