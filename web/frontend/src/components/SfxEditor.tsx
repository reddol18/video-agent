/**
 * SfxEditor — 오버레이 SFX 선택·볼륨·미리듣기 (#290, #sfx-presets 확장)
 *
 * TextOverlay / ShapeOverlay / GraphicOverlay / ImageOverlay 편집 패널에 공통 삽입.
 *
 * SFX 소스가 두 가지로 늘었다.
 *  - 스튜디오 자산 탭: Project.studio_assets 중 asset_type === "audio".
 *    선택 시 sfx_asset_id = asset.asset_id (uuid 그대로).
 *  - 프리셋 탭: 앱 내장 기본 효과음(assets/sfx_presets, #418) + 사용자 효과음 라이브러리.
 *    선택 시 sfx_asset_id = `"preset:{preset_key}"`. 백엔드 `_resolve_sfx` 가
 *    prefix 분기하여 절대 경로로 해석한다.
 *
 * 회귀 안전
 * ─────────
 *  - 기존 studio_assets uuid 형식 sfx_asset_id 는 prefix 가 없으므로 옛 로직대로 동작.
 *  - 프리셋 카탈로그 로드 실패(내장 manifest 손상 등)는 빈 배열로 처리되고,
 *    프리셋 탭은 "프리셋 없음" 안내만 표시한다.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { FootageAsset } from "../types";
import {
  listSfxPresets,
  resolveSfxStreamUrl,
  type SfxPreset,
} from "../api/client";
import { addStudioAssets } from "../lib/project";
import { useStore } from "../store";
import { ui } from "../store/ui";

const AUDIO_ACCEPT = "audio/*,.mp3,.wav,.m4a,.ogg,.aac,.flac";

interface Props {
  projectId: string;
  audioAssets: FootageAsset[];
  sfxAssetId: string | null | undefined;
  sfxVolume: number;
  onChange: (patch: { sfx_asset_id?: string | null; sfx_volume?: number }) => void;
  /** 적용 색상 (텍스트=인디고, 도형=초록, 그래픽=보라, 이미지=시안). */
  accent?: string;
}

/** sfx_asset_id 가 프리셋인지 studio_asset 인지 구분. */
function isPresetId(id: string | null | undefined): boolean {
  return typeof id === "string" && id.startsWith("preset:");
}

/** 현재 sfx_asset_id 가 어느 탭에 속하는지 판정해 초기 탭을 선택. */
function inferInitialTab(id: string | null | undefined): "studio" | "preset" {
  return isPresetId(id) ? "preset" : "studio";
}

// 모듈 단위 캐시 — listSfxPresets() 가 정적이라서 컴포넌트 마운트마다 새로 부르지 않는다.
let _presetCache: SfxPreset[] | null = null;
let _presetPromise: Promise<SfxPreset[]> | null = null;
function fetchPresetsCached(): Promise<SfxPreset[]> {
  if (_presetCache) return Promise.resolve(_presetCache);
  if (!_presetPromise) {
    _presetPromise = listSfxPresets()
      .then((ps) => {
        _presetCache = ps;
        return ps;
      })
      .catch(() => {
        _presetCache = [];
        return [];
      });
  }
  return _presetPromise;
}

/** 공용 효과음 라이브러리가 바뀌면 호출 — 다음 마운트 때 목록을 새로 받는다. */
export function invalidateSfxPresetCache() {
  _presetCache = null;
  _presetPromise = null;
}

export default function SfxEditor({
  projectId,
  audioAssets,
  sfxAssetId,
  sfxVolume,
  onChange,
  accent = "#6366f1",
}: Props) {
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const [tab, setTab] = useState<"studio" | "preset">(() => inferInitialTab(sfxAssetId));
  // 부모가 sfx_asset_id 를 외부에서 바꿔도 탭을 옳은 쪽으로 따라가게 한다.
  useEffect(() => {
    setTab(inferInitialTab(sfxAssetId));
  }, [sfxAssetId]);

  const [presets, setPresets] = useState<SfxPreset[] | null>(_presetCache);
  const [presetSearch, setPresetSearch] = useState("");

  useEffect(() => {
    if (presets !== null) return;
    let cancelled = false;
    fetchPresetsCached().then((ps) => {
      if (!cancelled) setPresets(ps);
    });
    return () => { cancelled = true; };
  }, [presets]);

  // preset_id 별로 그룹핑 — 75개 탐색에 그룹 헤더가 유리.
  const presetGroups = useMemo(() => {
    if (!presets) return [] as Array<{ preset_id: string; entries: SfxPreset[] }>;
    const q = presetSearch.trim().toLowerCase();
    const filtered = q
      ? presets.filter(
          (p) =>
            p.preset_id.toLowerCase().includes(q) ||
            (p.prompt || "").toLowerCase().includes(q),
        )
      : presets;
    const map = new Map<string, SfxPreset[]>();
    for (const p of filtered) {
      const list = map.get(p.preset_id);
      if (list) list.push(p);
      else map.set(p.preset_id, [p]);
    }
    return Array.from(map.entries()).map(([preset_id, entries]) => ({ preset_id, entries }));
  }, [presets, presetSearch]);

  // 현재 선택된 sfx 의 라벨 — 헤더에 한 줄 표시.
  const selectedLabel = useMemo(() => {
    if (!sfxAssetId) return "";
    if (isPresetId(sfxAssetId)) {
      const key = sfxAssetId.slice("preset:".length);
      const p = presets?.find((x) => x.preset_key === key);
      if (p) return p.is_user ? `⭐ ${p.preset_id}` : `🎵 ${p.preset_id} #${p.seq}`;
      return `🎵 ${key}`;
    }
    const asset = audioAssets.find((a) => a.asset_id === sfxAssetId);
    if (asset) return asset.label || asset.filename;
    return sfxAssetId;
  }, [sfxAssetId, presets, audioAssets]);

  // ── 효과음 파일 직접 업로드 → 스튜디오 오디오 자산으로 등록 후 바로 지정 ──────
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);

  async function handleUpload(file: File) {
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`/api/projects/${projectId}/studio/audio-upload`, {
        method: "POST",
        body: form,
      });
      if (!res.ok) {
        const t = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status} ${t.slice(0, 200)}`);
      }
      const data = await res.json();
      const asset = data?.asset as FootageAsset | undefined;
      if (!asset?.asset_id) throw new Error("응답에 자산 정보가 없습니다");
      // 서버는 이미 등록했다. IndexedDB 에도 넣어(중복은 dedupe) 이후 저장이
      // 옛 studio_assets 로 서버를 덮어 이 자산을 지우지 않게 한다.
      const p = await addStudioAssets(projectId, [asset]);
      const cur = useStore.getState().project;
      if (cur?.project_id === projectId) {
        useStore.getState().setProject({ ...cur, studio_assets: p.studio_assets });
      }
      setTab("studio");
      onChange({ sfx_asset_id: asset.asset_id });
      ui.success(`효과음 "${asset.label || asset.filename}" 업로드 후 지정했습니다.`);
    } catch (e) {
      ui.error(`효과음 업로드 실패: ${(e as Error).message || e}`);
    } finally {
      setUploading(false);
    }
  }

  function pickStudio(assetId: string) {
    if (assetId === "") onChange({ sfx_asset_id: null });
    else onChange({ sfx_asset_id: assetId });
  }

  function pickPreset(presetKey: string) {
    onChange({ sfx_asset_id: `preset:${presetKey}` });
  }

  function clearSelection() {
    onChange({ sfx_asset_id: null });
  }

  function preview() {
    if (!audioRef.current || !sfxAssetId) return;
    audioRef.current.src = resolveSfxStreamUrl(projectId, sfxAssetId);
    audioRef.current.volume = Math.max(0, Math.min(1, sfxVolume));
    audioRef.current.currentTime = 0;
    audioRef.current.play().catch(() => {});
  }

  function previewPresetByKey(presetKey: string) {
    if (!audioRef.current) return;
    audioRef.current.src = resolveSfxStreamUrl(projectId, `preset:${presetKey}`);
    // 미리듣기는 볼륨 미적용 — 본 볼륨 슬라이더는 트리거 시점에만 의미가 있다.
    audioRef.current.volume = 0.9;
    audioRef.current.currentTime = 0;
    audioRef.current.play().catch(() => {});
  }

  // ── 스타일 토큰 ─────────────────────────────────────────────────────────
  const containerStyle: React.CSSProperties = {
    marginTop: 8,
    padding: 10,
    background: "#0f0f0f",
    border: "1px solid #2a2a2a",
    borderRadius: 6,
    display: "flex",
    flexDirection: "column",
    gap: 8,
  };
  const tabBtn = (active: boolean): React.CSSProperties => ({
    flex: 1,
    padding: "4px 8px",
    background: active ? "#1a1a1a" : "transparent",
    color: active ? accent : "#888",
    border: `1px solid ${active ? accent : "#2a2a2a"}`,
    borderRadius: 4,
    cursor: "pointer",
    fontSize: 11,
    fontWeight: active ? 700 : 500,
  });

  return (
    <div style={containerStyle}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ color: accent, fontSize: 11, fontWeight: 700, letterSpacing: 0.4 }}>
          🔊 SFX (효과음)
        </div>
        {sfxAssetId && (
          <button
            onClick={clearSelection}
            style={{
              background: "transparent",
              color: "#888",
              border: "1px solid #333",
              borderRadius: 4,
              fontSize: 10,
              cursor: "pointer",
              padding: "2px 6px",
            }}
            title="SFX 제거"
          >
            없음
          </button>
        )}
      </div>

      {sfxAssetId && (
        <div style={{ color: "#bbb", fontSize: 11, lineHeight: 1.3 }}>
          현재: <span style={{ color: accent }}>{selectedLabel}</span>
        </div>
      )}

      {/* 탭 헤더 */}
      <div style={{ display: "flex", gap: 4 }}>
        <button style={tabBtn(tab === "studio")} onClick={() => setTab("studio")}>
          스튜디오 자산
        </button>
        <button style={tabBtn(tab === "preset")} onClick={() => setTab("preset")}>
          프리셋 ({presets?.length ?? 0})
        </button>
      </div>

      {/* 탭 본문 */}
      {tab === "studio" ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <input
          ref={fileInputRef}
          type="file"
          accept={AUDIO_ACCEPT}
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) handleUpload(f);
          }}
        />
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={uploading}
          style={{
            padding: "4px 8px",
            background: "#1a1a1a",
            color: accent,
            border: `1px dashed ${accent}`,
            borderRadius: 4,
            cursor: uploading ? "wait" : "pointer",
            fontSize: 11,
          }}
          title="mp3/wav/m4a/ogg/aac/flac 파일을 올려 이 오버레이의 효과음으로 바로 지정"
        >
          {uploading ? "업로드 중…" : "＋ 효과음 파일 업로드"}
        </button>
        {audioAssets.length === 0 ? (
          <div style={{ color: "#666", fontSize: 11 }}>
            등록된 오디오 자산이 없습니다. 위 버튼으로 올려 주세요.
          </div>
        ) : (
          <select
            value={!isPresetId(sfxAssetId) && sfxAssetId ? sfxAssetId : ""}
            onChange={(e) => pickStudio(e.target.value)}
            style={{
              background: "#1a1a1a",
              color: "#ccc",
              border: "1px solid #333",
              borderRadius: 4,
              padding: "4px 6px",
              fontSize: 12,
              width: "100%",
            }}
          >
            <option value="">없음</option>
            {audioAssets.map((a) => (
              <option key={a.asset_id} value={a.asset_id}>
                {a.label || a.filename}
              </option>
            ))}
          </select>
        )}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {presets === null ? (
            <div style={{ color: "#666", fontSize: 11 }}>프리셋 로드 중…</div>
          ) : presets.length === 0 ? (
            <div style={{ color: "#666", fontSize: 11, lineHeight: 1.4 }}>
              SFX 프리셋이 없습니다. <br />
              <span style={{ color: "#555" }}>
                기본 효과음 목록(assets/sfx_presets/manifest.json)을 읽지 못했습니다.
              </span>
            </div>
          ) : (
            <>
              <input
                type="text"
                placeholder="검색 (preset_id / 설명)"
                value={presetSearch}
                onChange={(e) => setPresetSearch(e.target.value)}
                style={{
                  background: "#1a1a1a",
                  color: "#ddd",
                  border: "1px solid #2a2a2a",
                  borderRadius: 4,
                  padding: "4px 6px",
                  fontSize: 11,
                }}
              />
              <div
                style={{
                  maxHeight: 240,
                  overflowY: "auto",
                  border: "1px solid #1f1f1f",
                  borderRadius: 4,
                  padding: 4,
                  background: "#0a0a0a",
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                }}
              >
                {presetGroups.length === 0 ? (
                  <div style={{ color: "#666", fontSize: 11, padding: 8 }}>검색 결과 없음</div>
                ) : (
                  presetGroups.map((g) => (
                    <PresetGroup
                      key={g.preset_id}
                      group={g}
                      selectedKey={
                        isPresetId(sfxAssetId)
                          ? (sfxAssetId as string).slice("preset:".length)
                          : ""
                      }
                      accent={accent}
                      onPick={pickPreset}
                      onPreview={previewPresetByKey}
                    />
                  ))
                )}
              </div>
            </>
          )}
        </div>
      )}

      {sfxAssetId && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#888", fontSize: 11, minWidth: 60 }}>볼륨</label>
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={sfxVolume}
              onChange={(e) => onChange({ sfx_volume: Number(e.target.value) })}
              style={{ flex: 1, accentColor: accent }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Math.round(sfxVolume * 100)}%
            </span>
          </div>
          <button
            style={{
              padding: "3px 10px",
              background: "#1a1a1a",
              color: accent,
              border: `1px solid ${accent}`,
              borderRadius: 5,
              cursor: "pointer",
              fontSize: 11,
              width: "fit-content",
            }}
            onClick={preview}
          >
            ▶ 미리듣기
          </button>
          <div style={{ color: "#666", fontSize: 10, lineHeight: 1.4 }}>
            재생: 오버레이 enter(start_sec) 시점. 영상 렌더 시 TTS 활성 구간에서는 자동 더킹(-10dB).
          </div>
        </>
      )}
      <audio ref={audioRef} style={{ display: "none" }} />
    </div>
  );
}

// ── 프리셋 그룹 한 묶음 ────────────────────────────────────────────────────
function PresetGroup({
  group,
  selectedKey,
  accent,
  onPick,
  onPreview,
}: {
  group: { preset_id: string; entries: SfxPreset[] };
  selectedKey: string;
  accent: string;
  onPick: (presetKey: string) => void;
  onPreview: (presetKey: string) => void;
}) {
  const firstPrompt = group.entries[0]?.prompt || "";
  return (
    <div style={{ background: "#0f0f0f", border: "1px solid #1a1a1a", borderRadius: 4, padding: 6 }}>
      <div style={{ color: "#ccc", fontSize: 11, fontWeight: 700, marginBottom: 2 }}>
        {group.entries[0]?.is_user ? `⭐ ${group.preset_id}` : group.preset_id}
      </div>
      {firstPrompt && (
        <div
          style={{
            color: "#555",
            fontSize: 10,
            marginBottom: 4,
            lineHeight: 1.3,
            overflow: "hidden",
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
          }}
          title={firstPrompt}
        >
          {firstPrompt}
        </div>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
        {group.entries.map((e) => {
          const active = e.preset_key === selectedKey;
          return (
            <div key={e.preset_key} style={{ display: "flex", alignItems: "center", gap: 2 }}>
              <button
                onClick={() => onPreview(e.preset_key)}
                style={{
                  padding: "2px 5px",
                  background: "#1a1a1a",
                  color: "#888",
                  border: "1px solid #2a2a2a",
                  borderRadius: 3,
                  cursor: "pointer",
                  fontSize: 10,
                }}
                title="미리듣기"
              >
                ▶
              </button>
              <button
                onClick={() => onPick(e.preset_key)}
                style={{
                  padding: "2px 8px",
                  background: active ? "#1f2937" : "#1a1a1a",
                  color: active ? accent : "#bbb",
                  border: `1px solid ${active ? accent : "#2a2a2a"}`,
                  borderRadius: 3,
                  cursor: "pointer",
                  fontSize: 10,
                  fontWeight: active ? 700 : 500,
                }}
                title={`선택 — ${e.duration.toFixed(1)}s`}
              >
                #{e.seq} · {e.duration.toFixed(1)}s
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
