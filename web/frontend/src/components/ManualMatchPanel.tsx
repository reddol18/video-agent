import { useEffect, useState } from "react";
import { useDraggable, useDroppable, DndContext } from "@dnd-kit/core";
import type { DragEndEvent } from "@dnd-kit/core";
import { updateScene as dbUpdateScene, setScenes as dbSetScenes } from "../lib/project";
import { getProjectDirectory } from "../lib/fileStore";
import { makeFootageFromFilename } from "../lib/footage";
import type { SceneCard } from "../types";
import { useStore } from "../store";
import { ui } from "../store/ui";
import type { FootageAsset, Project } from "../types";
import LocalThumbnail from "./LocalThumbnail";
import ServerFootageThumbnail from "./ServerFootageThumbnail";

interface Props {
  project: Project;
  footageDir?: string;
  onAllMatched: () => void;
  onMatchCountChange?: (matched: number) => void;
}

export default function ManualMatchPanel({ project, onAllMatched, onMatchCountChange }: Props) {
  const { updateScene, setProject } = useStore();
  // 레이블 텍스트 기준 numeric 정렬 — 대본 입력 화면(Setup.tsx)의 footage 레이블 순서와 동일.
  // 자동 배치와 좌측 그리드가 같은 순서를 쓰도록 여기서 한 번 정렬해 공유한다.
  const assets = [...project.studio_assets].sort((a, b) =>
    (a.label || a.filename).localeCompare(b.label || b.filename, undefined, { numeric: true })
  );
  const [matchedScenes, setMatchedScenes] = useState<Set<number>>(new Set());
  const [autoAssigning, setAutoAssigning] = useState(false);
  // #272: 로컬 파일명 → File 매핑. 서버 footage_dir에 없는 로컬 파일도 썸네일 표시되도록.
  const [localFileMap, setLocalFileMap] = useState<Map<string, File>>(new Map());

  useEffect(() => {
    const alreadyMatched = new Set(
      project.scenes
        .filter((s) => s.footage && ("file" in s.footage ? s.footage.file : "image" in s.footage))
        .map((s) => s.scene_id)
    );
    setMatchedScenes(alreadyMatched);
    onMatchCountChange?.(alreadyMatched.size);
  }, [project.project_id]);

  // #272: 디렉토리 핸들이 있으면 모든 파일을 미리 File로 변환해 두어 썸네일 컴포넌트에 전달.
  useEffect(() => {
    let cancelled = false;
    getProjectDirectory(project.project_id).then(async (dir) => {
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
      if (!cancelled) setLocalFileMap(map);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [project.project_id]);

  async function handleDragEnd(e: DragEndEvent) {
    const { active, over } = e;
    if (!over) return;
    const assetId = active.id as string;
    const sceneId = Number(over.id);
    const asset = assets.find((a) => a.asset_id === assetId);
    if (!asset) return;

    const footage = makeFootageFromFilename(asset.filename);
    updateScene(sceneId, { footage });
    await dbUpdateScene(project.project_id, sceneId, { footage });

    setMatchedScenes((prev) => {
      const next = new Set(prev).add(sceneId);
      onMatchCountChange?.(next.size);
      if (next.size >= project.scenes.length) onAllMatched();
      return next;
    });
  }

  // 씬 배열 순서 ↔ 레이블 정렬된 footage 순서로 1:1 전체 재배치(덮어쓰기).
  // 씬이 많을 때(예: 81개) 드래그&드롭을 대체한다. footage 가 씬보다 적으면 남는 씬은 비워둔다.
  //
  // 성능: 씬별로 dbUpdateScene 을 N번 부르면 매번 프로젝트 전체 read+write+서버 PUT 이 일어나
  //   81개에서 멈춘 것처럼 느려진다. 대신 메모리에서 배정을 다 계산한 뒤 setScenes/setProject 로
  //   한 번에 저장한다(IndexedDB write 1회 + 서버 PUT 1회).
  async function handleAutoAssign() {
    if (autoAssigning) return;
    const n = Math.min(project.scenes.length, assets.length);
    if (n === 0) return;
    setAutoAssigning(true);
    try {
      const next = new Set<number>();
      const newScenes = project.scenes.map((scene, i) => {
        if (i < n) {
          next.add(scene.scene_id);
          return { ...scene, footage: makeFootageFromFilename(assets[i].filename) };
        }
        return scene; // footage 가 부족해 남는 씬은 그대로 둔다.
      });

      // IndexedDB + 서버에 한 번에 저장.
      await dbSetScenes(project.project_id, newScenes);
      // store(=화면 표시)도 한 번에 교체.
      setProject({ ...project, scenes: newScenes });

      setMatchedScenes(next);
      onMatchCountChange?.(next.size);
      if (next.size >= project.scenes.length) onAllMatched();
    } catch (e) {
      console.error("[ManualMatchPanel] 자동 배치 실패:", e);
      ui.error("자동 배치에 실패했습니다. 콘솔을 확인하세요.");
    } finally {
      setAutoAssigning(false);
    }
  }

  const matchedCount = matchedScenes.size;
  const total = project.scenes.length;

  return (
    <DndContext onDragEnd={handleDragEnd}>
      <div style={styles.container}>
        <div style={styles.left}>
          <div style={styles.leftHeader}>
            <span style={styles.sectionTitle}>footage 파일</span>
            <button
              type="button"
              style={{ ...styles.autoBtn, ...(autoAssigning || assets.length === 0 ? styles.autoBtnDisabled : {}) }}
              onClick={handleAutoAssign}
              disabled={autoAssigning || assets.length === 0}
              title="씬 순서대로 footage 를 레이블 순으로 1:1 자동 배치합니다(기존 배치 덮어쓰기)."
            >
              {autoAssigning ? "배치 중…" : "⚡ 순서대로 자동 배치"}
            </button>
          </div>
          {assets.length > 0 && assets.length !== total && (
            <div style={styles.countHint}>
              footage {assets.length}개 · 씬 {total}개 —{" "}
              {assets.length < total
                ? `앞 ${assets.length}개 씬만 배치되고 나머지는 비워집니다.`
                : `앞 ${total}개 footage 만 사용됩니다.`}
            </div>
          )}
          {assets.length === 0 && (
            <div style={{ color: "#555", fontSize: 12 }}>footage 스튜디오에서 footage를 추가하세요</div>
          )}
          <div style={styles.grid}>
            {assets.map((a) => {
              // #272: 로컬 폴더에 동일 이름 파일이 있으면 origin="local"로 표시 → 브라우저 썸네일 사용.
              const localFile = localFileMap.get(a.filename);
              const assetWithLocal: FootageAsset = localFile
                ? { ...a, origin: "local", localFile }
                : a;
              return (
                <DraggableThumb key={a.asset_id} asset={assetWithLocal} projectId={project.project_id} />
              );
            })}
          </div>
        </div>

        <div style={styles.right}>
          <div style={styles.sectionTitle}>
            씬 목록 ({matchedCount}/{total} 완료)
            {matchedCount >= total && total > 0 && (
              <span style={styles.allDoneBadge}> ✓ 모두 완료</span>
            )}
          </div>
          <div style={styles.scenes}>
            {project.scenes.map((scene) => {
              const f = scene.footage;
              const fname = f && ("file" in f ? f.file : "image" in f ? f.image : null);
              const asset = fname ? project.studio_assets?.find((a) => a.filename === fname) : null;
              return (
                <SceneDropZone
                  key={scene.scene_id}
                  scene={scene}
                  isMatched={matchedScenes.has(scene.scene_id)}
                  matchedLabel={asset?.label || null}
                />
              );
            })}
          </div>
        </div>
      </div>
    </DndContext>
  );
}

function DraggableThumb({ asset, projectId }: { asset: FootageAsset; projectId: string }) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: asset.asset_id });
  // #272: 로컬 파일이면 브라우저에서 직접 썸네일 생성 (서버 404 회피).
  // localFile 미로드 시 placeholder 표시.
  const isLocalAsset = asset.origin === "local";
  const hasLocalFile = !!asset.localFile;
  return (
    <div ref={setNodeRef} {...listeners} {...attributes}
      style={{ ...styles.thumb, opacity: isDragging ? 0.4 : 1 }}
      title={asset.label || asset.filename}
    >
      {isLocalAsset ? (
        hasLocalFile ? (
          <LocalThumbnail file={asset.localFile!} alt={asset.filename} style={styles.thumbImg} />
        ) : (
          <div style={{ ...styles.thumbImg, background: "#1a1a1a" }} aria-label={asset.filename} />
        )
      ) : (
        // #307: 서버 푸티지도 브라우저에서 추출 + IndexedDB 캐시 (백엔드 ffmpeg 절감).
        <ServerFootageThumbnail
          projectId={projectId}
          filename={asset.filename}
          assetType={asset.asset_type}
          alt={asset.filename}
          style={styles.thumbImg}
        />
      )}
      <div style={styles.thumbLabel}>{asset.label || asset.filename}</div>
    </div>
  );
}

function SceneDropZone({ scene, isMatched, matchedLabel }: { scene: SceneCard; isMatched: boolean; matchedLabel: string | null }) {
  const { isOver, setNodeRef } = useDroppable({ id: scene.scene_id });
  const matchedFile = scene.footage
    ? ("file" in scene.footage ? scene.footage.file : "image" in scene.footage ? scene.footage.image : null)
    : null;
  // 사용자가 footage에 부여한 라벨을 우선 표시(없으면 파일명 폴백).
  const matchedDisplay = matchedLabel || matchedFile || "완료";

  return (
    <div
      ref={setNodeRef}
      style={{
        ...styles.dropZone,
        ...(isOver ? styles.dropZoneOver : {}),
        ...(isMatched ? styles.dropZoneMatched : {}),
      }}
    >
      <span style={styles.sceneNum}>{scene.scene_id}</span>
      <span style={styles.sceneText}>{scene.narration_text?.slice(0, 50) || scene.description?.slice(0, 50)}</span>
      {isMatched && <span style={styles.matchedBadge} title={matchedFile ?? ""}>✓ {matchedDisplay}</span>}
      {!isMatched && <span style={styles.dropHint}>여기에 드롭</span>}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: { display: "flex", gap: 16, marginTop: 16, maxHeight: 400 },
  left: { flex: 1, overflow: "auto" },
  right: { flex: 1, overflow: "auto" },
  sectionTitle: { color: "#aaa", fontSize: 12, fontWeight: 600 },
  leftHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: 8 },
  autoBtn: { background: "#4f46e5", color: "#fff", border: "none", borderRadius: 6, padding: "4px 10px", fontSize: 11, fontWeight: 600, cursor: "pointer", flexShrink: 0 },
  autoBtnDisabled: { background: "#333", color: "#777", cursor: "not-allowed" },
  countHint: { color: "#c9a227", fontSize: 11, marginBottom: 8 },
  grid: { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 },
  thumb: { cursor: "grab", background: "#222", borderRadius: 6, overflow: "hidden", textAlign: "center" },
  thumbImg: { width: "100%", aspectRatio: "16/9", objectFit: "cover", display: "block" },
  thumbLabel: { color: "#888", fontSize: 10, padding: "4px 2px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  scenes: { display: "flex", flexDirection: "column", gap: 6 },
  dropZone: { display: "flex", alignItems: "center", gap: 8, padding: "10px 12px", border: "2px dashed #444", borderRadius: 8, background: "#111", transition: "all 0.15s" },
  dropZoneOver: { borderColor: "#6366f1", background: "#1e1b4b" },
  dropZoneMatched: { borderColor: "#22c55e", borderStyle: "solid" },
  sceneNum: { background: "#333", color: "#aaa", borderRadius: 4, padding: "2px 6px", fontSize: 11, flexShrink: 0 },
  sceneText: { color: "#ccc", fontSize: 12, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  matchedBadge: { color: "#22c55e", fontSize: 11, flexShrink: 0 },
  allDoneBadge: { color: "#22c55e", fontWeight: 700 },
  dropHint: { color: "#555", fontSize: 11, flexShrink: 0 },
};
