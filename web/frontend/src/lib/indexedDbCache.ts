/**
 * indexedDbCache (#307)
 * ─────────────────────────────────────────────────────────────────────────
 * 푸티지 썸네일용 IndexedDB 캐시 어댑터.
 *
 * `db.ts`의 기존 `localThumbnailCache` objectStore를 재사용해 store 스키마
 * 변경 없이(version bump 없이) 서버 푸티지용 캐시도 같이 보관한다.
 * 키 네임스페이스로 충돌을 막는다:
 *   - 로컬 파일(#272): `${filename}:${lastModified}`
 *   - 서버 푸티지(#307): `server:${projectId}:${filename}`
 *
 * IndexedDB가 사용 불가한 환경(시크릿 모드 등)에서는 in-memory 폴백을 사용한다.
 */

import { dbGetLocalThumbnail, dbSaveLocalThumbnail } from "./db";

export interface ThumbnailCache {
  get(key: string): Promise<string | null>;
  set(key: string, dataUrl: string): Promise<void>;
}

/** 시크릿 모드 등 IndexedDB 미사용 환경 폴백. 프로세스 메모리에만 보관. */
class MemoryThumbnailCache implements ThumbnailCache {
  private map = new Map<string, string>();
  async get(key: string): Promise<string | null> {
    return this.map.get(key) ?? null;
  }
  async set(key: string, dataUrl: string): Promise<void> {
    this.map.set(key, dataUrl);
  }
}

class IndexedDbThumbnailCache implements ThumbnailCache {
  private memoryFallback = new MemoryThumbnailCache();
  private idbWorks: boolean | null = null;

  async get(key: string): Promise<string | null> {
    if (this.idbWorks === false) return this.memoryFallback.get(key);
    try {
      const v = await dbGetLocalThumbnail(key);
      this.idbWorks = true;
      if (v) return v;
      // IDB miss → memory에 임시 저장된 값이 있을 수 있다(저장 실패 케이스).
      return this.memoryFallback.get(key);
    } catch {
      this.idbWorks = false;
      return this.memoryFallback.get(key);
    }
  }

  async set(key: string, dataUrl: string): Promise<void> {
    // 메모리 캐시는 항상 갱신 (IDB 성공 여부와 무관하게 같은 세션 내 재추출 방지).
    await this.memoryFallback.set(key, dataUrl);
    if (this.idbWorks === false) return;
    try {
      await dbSaveLocalThumbnail(key, dataUrl);
      this.idbWorks = true;
    } catch {
      this.idbWorks = false;
    }
  }
}

let _instance: ThumbnailCache | null = null;

export function getThumbnailCache(): ThumbnailCache {
  if (!_instance) _instance = new IndexedDbThumbnailCache();
  return _instance;
}

/**
 * 서버 푸티지 자산 캐시 키 — `server:{projectId}:{filename}`.
 * 백엔드가 mtime 기반으로 자체 캐시 무효화를 하지만, 클라 캐시는
 * (1) 파일이 거의 갱신되지 않고 (2) 사용자가 명시적으로 페이지를
 * 새로고침하는 경우가 드물기 때문에 단순 키를 사용한다.
 * 무효화가 필요한 경우 사용자가 브라우저 캐시 삭제로 처리 가능.
 */
export function serverFootageCacheKey(projectId: string, filename: string): string {
  return `server:${projectId}:${filename}`;
}

/** 테스트용 — 주입된 캐시 구현으로 교체. */
export function _setThumbnailCacheForTesting(cache: ThumbnailCache | null): void {
  _instance = cache;
}
