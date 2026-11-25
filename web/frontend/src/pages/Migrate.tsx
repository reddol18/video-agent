/**
 * 백엔드(state.json) → IndexedDB + FileStore 마이그레이션 페이지
 *
 * 마이그레이션 항목:
 * 1. 프로젝트 메타 (state.json) → IndexedDB projects
 * 2. studio_assets 파일 → File System Access API 디렉토리
 * 3. footage_dir 영상 클립 (씬에 매칭된 파일) → 같은 디렉토리
 * 4. TTS 오디오 → IndexedDB ttsAudio
 */

import { useEffect, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { dbSaveProject, dbSaveTtsAudio, dbGetProject, dbListProjects } from "../lib/db";
import { dbSaveDirectoryHandle } from "../lib/db";
import { ui } from "../store/ui";
import type { Project, FootageAsset, CropRect } from "../types";

const BASE = window.location.origin;

interface MigrateLog {
  level: "info" | "ok" | "warn" | "error";
  msg: string;
}

/** 진입 시 서버 프로젝트 목록 조회 결과(#298 DoD 1·2). */
type Preview =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; count: number };

interface Props {
  onDone: () => void;
}

export default function Migrate({ onDone }: Props) {
  const [logs, setLogs] = useState<MigrateLog[]>([]);
  const [running, setRunning] = useState(false);
  const [done, setDone] = useState(false);

  // #298 DoD 1·2 — 진입 시 마이그레이션 대상 프로젝트 수 미리 조회.
  const [preview, setPreview] = useState<Preview>({ kind: "loading" });
  // #298 DoD 4 — 진행률(완료 프로젝트 수 / 전체) + 현재 처리 중 프로젝트명.
  const [progress, setProgress] = useState<{ current: number; total: number; name: string } | null>(null);
  // #298 DoD 3 — 이미 IndexedDB 에 존재하는 project_id 의 처리 정책.
  //   "ask"(기본): 프로젝트마다 덮어쓰기/건너뛰기 확인. "overwrite"/"skip": 일괄 적용.
  const [duplicatePolicy, setDuplicatePolicy] =
    useState<"ask" | "overwrite" | "skip">("ask");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${BASE}/api/projects`);
        if (!res.ok) throw new Error(`백엔드 응답 오류: ${res.status}`);
        const projects: Project[] = await res.json();
        if (!cancelled) setPreview({ kind: "ready", count: projects.length });
      } catch (e) {
        if (!cancelled) setPreview({ kind: "error", message: (e as Error).message });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  function log(level: MigrateLog["level"], msg: string) {
    setLogs((prev) => [...prev, { level, msg }]);
  }

  async function downloadAndSave(
    dir: FileSystemDirectoryHandle,
    url: string,
    filename: string,
    label: string
  ): Promise<boolean> {
    try {
      const res = await fetch(url);
      if (!res.ok) {
        log("warn", `  다운로드 실패 (${res.status}): ${label}`);
        return false;
      }
      const blob = await res.blob();
      const fileHandle = await dir.getFileHandle(filename, { create: true });
      const writable = await fileHandle.createWritable();
      await writable.write(blob);
      await writable.close();
      log("ok", `  저장: ${label}`);
      return true;
    } catch (e) {
      log("warn", `  저장 실패 (${label}): ${(e as Error).message}`);
      return false;
    }
  }

  async function runMigration() {
    setRunning(true);
    setDone(false);
    setLogs([]);
    setProgress(null);

    // #298 DoD 3 — 일괄 적용 정책 결정용으로, 시작 시 IndexedDB 의 기존 project_id 집합을 확보.
    let existingIds = new Set<string>();
    try {
      existingIds = new Set((await dbListProjects()).map((p) => p.project_id));
    } catch {
      // 조회 실패 시 빈 집합 — 개별 프로젝트 단계에서 dbGetProject 로 재확인한다.
    }
    const policy = duplicatePolicy;

    try {
      log("info", "백엔드에서 프로젝트 목록 조회 중...");
      const res = await fetch(`${BASE}/api/projects`);
      if (!res.ok) throw new Error(`백엔드 응답 오류: ${res.status}`);
      const projects: Project[] = await res.json();
      log("ok", `프로젝트 ${projects.length}개 발견`);

      let index = 0;
      for (const project of projects) {
        index += 1;
        // #298 DoD 4 — 진행률 + 현재 처리 중 프로젝트명.
        setProgress({ current: index, total: projects.length, name: project.name });
        log("info", `━━ [${project.name}] 마이그레이션 시작 (${index}/${projects.length})`);

        // #298 DoD 3 — IndexedDB 에 같은 project_id 가 이미 있으면 덮어쓰기/건너뛰기 결정.
        const alreadyExists =
          existingIds.has(project.project_id) ||
          (await dbGetProject(project.project_id)) !== undefined;
        if (alreadyExists) {
          if (policy === "skip") {
            log("warn", `  이미 존재 — 건너뜀`);
            continue;
          }
          if (policy === "ask") {
            const overwrite = await ui.confirm({
              variant: "warning",
              title: "이미 존재하는 프로젝트",
              message: `"${project.name}" 은(는) 이미 브라우저에 저장되어 있습니다. 덮어쓸까요?\n(취소하면 이 프로젝트는 건너뜁니다)`,
              confirmLabel: "덮어쓰기",
              cancelLabel: "건너뛰기",
            });
            if (!overwrite) {
              log("warn", `  사용자 선택: 건너뜀`);
              continue;
            }
            log("info", `  사용자 선택: 덮어쓰기`);
          }
          // policy === "overwrite" 면 그대로 진행.
        }

        const detailRes = await fetch(`${BASE}/api/projects/${project.project_id}`);
        const detail: Project = await detailRes.json();

        // ── 씬에서 footage 파일명 수집 ────────────────────────────
        const footageFilenames = new Set<string>();
        function collectFootageFiles(f: Record<string, unknown>) {
          if (typeof f.file === "string" && f.file) footageFilenames.add(f.file);
          if (typeof f.image === "string" && f.image) footageFilenames.add(f.image);
          if (Array.isArray(f.files)) {
            for (const fi of f.files) {
              if (typeof fi === "string" && fi) footageFilenames.add(fi);
            }
          }
          // sync 타입의 inner 중첩 처리
          if (f.inner && typeof f.inner === "object") {
            collectFootageFiles(f.inner as Record<string, unknown>);
          }
        }
        for (const scene of detail.scenes) {
          // match.source_file: 절대경로일 수 있으므로 basename만 추출
          const m = scene.match as Record<string, unknown> | null | undefined;
          if (m && typeof m.source_file === "string" && m.source_file) {
            const basename = m.source_file.split("/").pop()!;
            if (basename) footageFilenames.add(basename);
          }
          if (!scene.footage) continue;
          collectFootageFiles(scene.footage as unknown as Record<string, unknown>);
        }

        const hasAssets = (detail.studio_assets?.length ?? 0) > 0;
        const hasFootage = footageFilenames.size > 0;
        const needsDir = hasAssets || hasFootage;
        const canPickDir = typeof window.showDirectoryPicker === "function";

        let dir: FileSystemDirectoryHandle | null = null;

        if (needsDir) {
          if (!canPickDir) {
            log("warn", `  폴더 선택 API 미지원 (localhost로 접속해야 사용 가능) — 파일 마이그레이션 건너뜀`);
          } else {
            const totalFiles = (detail.studio_assets?.length ?? 0) + footageFilenames.size;
            log("info", `  파일 ${totalFiles}개 저장할 폴더를 선택하세요 (팝업)`);
            try {
              dir = await window.showDirectoryPicker({ mode: "readwrite", startIn: "videos" });
              await dbSaveDirectoryHandle(detail.project_id, dir);
              log("ok", `  폴더 선택 완료`);
            } catch (e) {
              if ((e as Error).name === "AbortError") {
                log("warn", `  폴더 선택 취소 — 파일 마이그레이션 건너뜀`);
              } else {
                log("warn", `  폴더 선택 실패: ${(e as Error).message}`);
              }
            }
          }
        }

        // studio_assets에 없는 footage 파일을 imported 에셋으로 추가
        const existingFilenames = new Set(detail.studio_assets.map((a) => a.filename));
        const importedAssets: FootageAsset[] = [];
        for (const filename of footageFilenames) {
          if (!existingFilenames.has(filename)) {
            const ext = filename.match(/\.[^.]+$/)?.[0].toLowerCase() ?? "";
            const VIDEO_EXTS = new Set([".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"]);
            const IMAGE_EXTS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp"]);
            const asset_type = VIDEO_EXTS.has(ext) ? "video" : IMAGE_EXTS.has(ext) ? "image" : "video";
            importedAssets.push({
              asset_id: uuidv4(),
              filename,
              label: filename.replace(/\.[^.]+$/, ""),
              asset_type,
              source: "imported",
              created_at: new Date().toISOString(),
              crop: null,
            });
          }
        }

        if (dir) {
          // ── studio_assets 파일 다운로드 ────────────────────────
          if (hasAssets) {
            log("info", `  studio_assets ${detail.studio_assets.length}개 다운로드 중...`);
            for (const asset of detail.studio_assets) {
              const url = `${BASE}/api/projects/${detail.project_id}/studio/assets/${asset.asset_id}/stream`;
              await downloadAndSave(dir, url, asset.filename, asset.filename);
            }
          }

          // ── footage_dir 영상 클립 다운로드 ─────────────────────
          if (hasFootage) {
            log("info", `  footage 클립 ${footageFilenames.size}개 다운로드 중...`);
            for (const filename of footageFilenames) {
              const url = `${BASE}/api/projects/${detail.project_id}/footage/${encodeURIComponent(filename)}/stream`;
              await downloadAndSave(dir, url, filename, filename);
            }
          }
        }

        // ── TTS 오디오 마이그레이션 ────────────────────────────────
        const scenesWithTts = detail.scenes.filter((s) => s.tts?.status === "ready");
        if (scenesWithTts.length > 0) {
          log("info", `  TTS ${scenesWithTts.length}개 마이그레이션 중...`);
          for (const scene of scenesWithTts) {
            try {
              const audioUrl = `${BASE}/api/projects/${detail.project_id}/tts/${scene.scene_id}/audio`;
              const audioRes = await fetch(audioUrl);
              if (!audioRes.ok) {
                log("warn", `  TTS 다운로드 실패: 씬 ${scene.scene_id}`);
                scene.tts = null;
                continue;
              }
              const audioBlob = await audioRes.blob();
              await dbSaveTtsAudio(detail.project_id, scene.scene_id, audioBlob);
              scene.tts = {
                ...scene.tts!,
                audio_url: `idb:${detail.project_id}:${scene.scene_id}`,
              };
              log("ok", `  TTS 저장: 씬 ${scene.scene_id}`);
            } catch (e) {
              log("warn", `  TTS 저장 실패 (씬 ${scene.scene_id}): ${(e as Error).message}`);
              scene.tts = null;
            }
          }
        }

        // ── 정규화 후 IndexedDB 저장 ──────────────────────────────
        const normalizedProject: Project = {
          ...detail,
          tts_voice: detail.tts_voice ?? "ko-KR-Chirp3-HD-Orus",
          studio_assets: [
            ...(detail.studio_assets ?? []).map((a: FootageAsset) => ({
              ...a,
              crop: (a as FootageAsset & { crop?: CropRect | null }).crop ?? null,
            })),
            ...importedAssets,
          ],
          default_crop: detail.default_crop ?? null,
          created_at: detail.created_at ?? new Date().toISOString(),
          updated_at: detail.updated_at ?? new Date().toISOString(),
        };
        if (importedAssets.length > 0) {
          log("info", `  footage 클립 ${importedAssets.length}개를 studio_assets에 추가`);
        }

        await dbSaveProject(normalizedProject);
        log("ok", `  [${project.name}] IndexedDB 저장 완료`);
      }

      log("ok", "━━ 마이그레이션 완료!");
      setDone(true);
    } catch (e) {
      log("error", `마이그레이션 중 오류: ${(e as Error).message}`);
    } finally {
      setRunning(false);
      setProgress(null);
    }
  }

  return (
    <div style={s.container}>
      <div style={s.card}>
        <h1 style={s.title}>데이터 마이그레이션</h1>
        <p style={s.desc}>
          기존 백엔드(state.json)에 저장된 프로젝트를 브라우저 IndexedDB로 옮깁니다.
          백엔드 서버(<code>{window.location.host}</code>)가 실행 중이어야 합니다.
        </p>
        <p style={s.desc}>
          파일이 있는 프로젝트는 <strong style={{ color: "#ccc" }}>저장할 로컬 폴더 선택 팝업</strong>이 프로젝트마다 뜹니다.
          studio_assets, footage 클립 모두 해당 폴더에 저장됩니다.
        </p>

        {/* #298 DoD 1·2 — 진입 시 마이그레이션 대상 수 표시 / 0개·오류 안내. */}
        {!running && !done && (
          <div style={s.previewBox}>
            {preview.kind === "loading" && (
              <span style={{ color: "#888" }}>마이그레이션 대상 조회 중...</span>
            )}
            {preview.kind === "error" && (
              <span style={{ color: "#f59e0b" }}>
                백엔드에 연결할 수 없습니다 ({preview.message}). 백엔드 서버가 실행 중인지 확인하세요.
              </span>
            )}
            {preview.kind === "ready" && preview.count > 0 && (
              <span style={{ color: "#ccc" }}>
                마이그레이션 대상: <strong style={{ color: "#fff" }}>{preview.count}개 프로젝트</strong>
              </span>
            )}
            {preview.kind === "ready" && preview.count === 0 && (
              <span style={{ color: "#888" }}>
                가져올 프로젝트가 없습니다 — 이 기능은 v1.x 백엔드에서 v2.x로 데이터를 옮길 때 사용합니다.
              </span>
            )}
          </div>
        )}

        {/* #298 DoD 3 — 이미 존재하는 프로젝트 처리 정책 선택. */}
        {!running && !done && preview.kind === "ready" && preview.count > 0 && (
          <div style={s.policyRow}>
            <span style={{ color: "#888", fontSize: 13 }}>이미 가져온 프로젝트가 있으면:</span>
            {([
              ["ask", "물어보기"],
              ["overwrite", "덮어쓰기"],
              ["skip", "건너뛰기"],
            ] as const).map(([value, label]) => (
              <label key={value} style={s.policyOption}>
                <input
                  type="radio"
                  name="dup-policy"
                  checked={duplicatePolicy === value}
                  onChange={() => setDuplicatePolicy(value)}
                  style={{ accentColor: "#6366f1" }}
                />
                {label}
              </label>
            ))}
          </div>
        )}

        {!running && !done && (
          <button
            style={{
              ...s.btn,
              ...(preview.kind === "ready" && preview.count === 0
                ? { opacity: 0.4, cursor: "not-allowed" }
                : {}),
            }}
            onClick={runMigration}
            disabled={preview.kind === "ready" && preview.count === 0}
          >
            마이그레이션 시작
          </button>
        )}

        {running && (
          <div style={{ marginBottom: 16 }}>
            <div style={s.runningBadge}>
              {progress
                ? `마이그레이션 진행 중... (${progress.current}/${progress.total}) ${progress.name}`
                : "마이그레이션 준비 중..."}
            </div>
            {progress && (
              <div style={s.progressTrack}>
                <div
                  style={{
                    ...s.progressBar,
                    width: `${Math.round((progress.current / Math.max(1, progress.total)) * 100)}%`,
                  }}
                />
              </div>
            )}
          </div>
        )}

        {logs.length > 0 && (
          <div style={s.logBox}>
            {logs.map((l, i) => (
              <div key={i} style={{ ...s.logLine, color: LOG_COLOR[l.level] }}>
                {LOG_PREFIX[l.level]} {l.msg}
              </div>
            ))}
          </div>
        )}

        {done && (
          <div style={{ marginTop: 24, display: "flex", gap: 12 }}>
            <button style={s.btn} onClick={onDone}>홈으로 이동 →</button>
            <button style={{ ...s.btn, background: "#333" }} onClick={runMigration}>다시 실행</button>
          </div>
        )}
      </div>
    </div>
  );
}

const LOG_COLOR: Record<MigrateLog["level"], string> = {
  info: "#888", ok: "#4ade80", warn: "#f59e0b", error: "#f87171",
};
const LOG_PREFIX: Record<MigrateLog["level"], string> = {
  info: "·", ok: "✓", warn: "⚠", error: "✕",
};

const s: Record<string, React.CSSProperties> = {
  container: { minHeight: "100vh", background: "#0f0f0f", display: "flex", alignItems: "flex-start", justifyContent: "center", padding: "60px 24px" },
  card: { width: "100%", maxWidth: 640, background: "#1a1a1a", borderRadius: 16, padding: 40 },
  title: { color: "#fff", fontSize: 24, marginBottom: 16 },
  desc: { color: "#888", fontSize: 14, marginBottom: 12, lineHeight: 1.6 },
  btn: { padding: "12px 28px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 8, fontSize: 15, cursor: "pointer", fontWeight: 600 },
  runningBadge: { color: "#6366f1", fontWeight: 600, marginBottom: 10 },
  previewBox: { background: "#111", border: "1px solid #2a2a2a", borderRadius: 8, padding: "12px 16px", marginBottom: 16, fontSize: 14, lineHeight: 1.5 },
  policyRow: { display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" as const, marginBottom: 16 },
  policyOption: { display: "flex", alignItems: "center", gap: 6, color: "#ccc", fontSize: 13, cursor: "pointer" },
  progressTrack: { height: 6, background: "#222", borderRadius: 3, overflow: "hidden" },
  progressBar: { height: "100%", background: "#6366f1", borderRadius: 3, transition: "width 0.25s ease" },
  logBox: { marginTop: 20, background: "#111", borderRadius: 8, padding: "14px 16px", fontFamily: "monospace", fontSize: 13, maxHeight: 400, overflowY: "auto", display: "flex", flexDirection: "column", gap: 4 },
  logLine: { lineHeight: 1.5 },
};
