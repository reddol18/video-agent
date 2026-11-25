/**
 * File System Access API 기반 파일 저장소
 * footage 파일을 사용자 로컬 디스크에 저장
 */
import { v4 as uuidv4 } from "uuid";
import { dbSaveDirectoryHandle, dbGetDirectoryHandle } from "./db";
import type { FootageAsset } from "../types";

// ── 디렉토리 핸들 관리 ─────────────────────────────────────────────────────

export async function requestProjectDirectory(
  projectId: string
): Promise<FileSystemDirectoryHandle> {
  const handle = await window.showDirectoryPicker({
    id: "video-agent-footage",
    mode: "readwrite",
    startIn: "videos",
  });
  await dbSaveDirectoryHandle(projectId, handle);
  return handle;
}

export async function getProjectDirectory(
  projectId: string
): Promise<FileSystemDirectoryHandle | null> {
  const handle = await dbGetDirectoryHandle(projectId);
  if (!handle) return null;
  // 권한 재확인
  const perm = await handle.queryPermission({ mode: "readwrite" });
  if (perm === "granted") return handle;
  const req = await handle.requestPermission({ mode: "readwrite" });
  return req === "granted" ? handle : null;
}

/**
 * 권한 prompt 없이 현재 상태만 조회한다.
 * user gesture가 없는 자동 호출(예: 컴포넌트 마운트)에서 사용.
 * - permission이 "prompt"/"denied"면 UI에서 사용자 클릭으로 권한 부여를 안내해야 함.
 */
export async function getProjectDirectoryIfGranted(
  projectId: string
): Promise<{ handle: FileSystemDirectoryHandle | null; permission: "granted" | "prompt" | "denied" | "missing" }> {
  const handle = await dbGetDirectoryHandle(projectId);
  if (!handle) return { handle: null, permission: "missing" };
  const perm = await handle.queryPermission({ mode: "readwrite" });
  return { handle, permission: perm };
}

// ── 파일 저장 ──────────────────────────────────────────────────────────────

export async function saveFile(
  projectId: string,
  blob: Blob,
  filename: string
): Promise<string> {
  const dir = await getProjectDirectory(projectId);
  if (!dir) throw new Error("프로젝트 폴더가 설정되지 않았습니다.");
  const fileHandle = await dir.getFileHandle(filename, { create: true });
  const writable = await fileHandle.createWritable();
  await writable.write(blob);
  await writable.close();
  return filename;
}

export async function readFile(
  projectId: string,
  filename: string
): Promise<File> {
  const dir = await getProjectDirectory(projectId);
  if (!dir) throw new Error("프로젝트 폴더가 설정되지 않았습니다.");
  const fileHandle = await dir.getFileHandle(filename);
  return fileHandle.getFile();
}

export async function getFileUrl(
  projectId: string,
  filename: string
): Promise<string> {
  const file = await readFile(projectId, filename);
  return URL.createObjectURL(file);
}

export async function deleteFile(
  projectId: string,
  filename: string
): Promise<void> {
  const dir = await getProjectDirectory(projectId);
  if (!dir) return;
  await dir.removeEntry(filename).catch(() => {});
}

export async function listFiles(
  projectId: string
): Promise<string[]> {
  const dir = await getProjectDirectory(projectId);
  if (!dir) return [];
  const names: string[] = [];
  for await (const [name] of dir.entries()) {
    names.push(name);
  }
  return names;
}

// ── Asset 저장 헬퍼 ────────────────────────────────────────────────────────

const VIDEO_EXTS = new Set([".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"]);
const IMAGE_EXTS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp"]);
// #289/#290 — 오디오 자산 확장자 (BGM/SFX 공용)
const AUDIO_EXTS = new Set([".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"]);

export async function saveAsset(
  projectId: string,
  blob: Blob,
  originalName: string,
  label = "",
  source: FootageAsset["source"] = "recorded"
): Promise<FootageAsset> {
  const ext = originalName.match(/\.[^.]+$/)?.[0].toLowerCase() ?? ".bin";
  const uniqueName = `${uuidv4().slice(0, 8)}_${originalName}`;
  await saveFile(projectId, blob, uniqueName);

  // 서버 footage_dir에도 같은 파일명으로 저장 — 미리보기/렌더가 항상 동작하도록.
  // 서버가 "진실의 원천"이므로 서버 저장 실패는 치명적으로 처리한다. 예전에는
  // 실패를 console.warn 으로 삼켜, 파일은 로컬에만 있고 서버에는 없는데 메타데이터
  // (studio_assets)는 등록되어 다른 세션/브라우저에서 footage 404 가 나는 유령 자산이
  // 생겼다. 이제 실패 시 throw 하여 호출부(녹화/분할/크롭 저장)의 try/catch 가
  // 에러 토스트를 띄우고 자산 등록 자체를 중단하도록 한다.
  const { syncStudioAssetFile } = await import("../api/client");
  await syncStudioAssetFile(projectId, blob, uniqueName);

  const asset_type: FootageAsset["asset_type"] =
    VIDEO_EXTS.has(ext) ? "video"
    : IMAGE_EXTS.has(ext) ? "image"
    : AUDIO_EXTS.has(ext) ? "audio"
    : "video";

  return {
    asset_id: uuidv4(),
    filename: uniqueName,
    label,
    asset_type,
    source,
    created_at: new Date().toISOString(),
    crop: null,
  };
}

// ── ObjectURL 캐시 (렌더링 중 중복 생성 방지) ──────────────────────────────

const _urlCache = new Map<string, string>();

export async function getCachedFileUrl(
  projectId: string,
  filename: string
): Promise<string> {
  const key = `${projectId}:${filename}`;
  if (_urlCache.has(key)) return _urlCache.get(key)!;
  // 1순위: 로컬 디렉토리 핸들에서 ObjectURL 생성 (오프라인·캐시 무관 동작)
  // 2순위: 핸들이 없거나 권한 만료(복제 프로젝트 등) 시 서버 스트림으로 폴백
  try {
    const url = await getFileUrl(projectId, filename);
    _urlCache.set(key, url);
    return url;
  } catch {
    const url = `/api/projects/${encodeURIComponent(projectId)}/footage/${encodeURIComponent(filename)}/stream`;
    _urlCache.set(key, url);
    return url;
  }
}

export function revokeCachedUrls(): void {
  // ObjectURL(blob:)만 revoke 대상 — 서버 폴백 URL(/api/...)은 revoke 불필요
  _urlCache.forEach((url) => {
    if (url.startsWith("blob:")) URL.revokeObjectURL(url);
  });
  _urlCache.clear();
}
