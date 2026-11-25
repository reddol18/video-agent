/**
 * SceneSpeakerPicker — 씬의 TTS 화자 선택 (다중 화자 기능)
 *
 * 씬당 화자는 1명이다. 고르면 씬 태그에 `speaker:{name}` 이 붙고, 그 씬의 TTS 는
 * 화자에게 지정된 목소리로 생성된다. 기존 효과 태그(horror 등)는 건드리지 않는다.
 *
 * 목소리가 실제로 바뀌는 건 TTS 를 다시 만들 때다 — 이미 생성된 음성은 그대로이므로,
 * 화자를 바꾸면 재생성이 필요하다는 것을 UI 에서 알려 준다.
 */
import type { SceneCard, Speaker } from "../types";
import { extractSpeakerName, findSpeaker, setSpeakerTag } from "../lib/speakers";

interface Props {
  scene: SceneCard;
  speakers: Speaker[];
  /** 프로젝트 기본 목소리 — 화자 미지정 시 쓰인다. */
  defaultVoice: string;
  /** 태그 배열 전체를 갱신한다. */
  onChangeTags: (tags: string[]) => void;
  /** 화자 사전 편집 열기. */
  onManage: () => void;
}

export default function SceneSpeakerPicker({
  scene,
  speakers,
  defaultVoice,
  onChangeTags,
  onManage,
}: Props) {
  const name = extractSpeakerName(scene.tags);
  const matched = findSpeaker(speakers, name);
  // 태그에는 있는데 사전에 없는 이름 — 오타이거나 화자를 지운 경우.
  const unknown = !!name && !matched;

  return (
    <div style={styles.row} onClick={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
      <span style={styles.label} title="이 씬의 TTS 목소리를 정할 화자">
        🎙 화자
      </span>

      <select
        value={matched ? matched.name : ""}
        onChange={(e) => onChangeTags(setSpeakerTag(scene.tags, e.target.value || null))}
        style={{ ...styles.select, ...(unknown ? styles.selectWarn : null) }}
        title={
          matched
            ? `${matched.name} — ${matched.voice_name}`
            : "화자를 지정하지 않으면 프로젝트 기본 목소리로 생성됩니다"
        }
      >
        <option value="">기본 목소리</option>
        {speakers.map((s) => (
          <option key={s.name} value={s.name}>
            {s.name}
          </option>
        ))}
      </select>

      {/* 실제 적용될 목소리를 항상 보여 준다 — 렌더 결과와 어긋나지 않도록. */}
      <span style={styles.voice} title="이 씬에 적용될 TTS 목소리">
        {matched ? matched.voice_name : defaultVoice || "(기본)"}
      </span>

      {unknown && (
        <span style={styles.warn} title={`태그의 '${name}' 가 화자 사전에 없습니다 — 기본 목소리로 생성됩니다`}>
          ⚠ '{name}' 미등록
        </span>
      )}

      <button style={styles.manage} onClick={onManage} title="화자 사전 편집">
        관리
      </button>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  row: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    flexWrap: "wrap",
    padding: "4px 0",
  },
  label: { color: "#a78bfa", fontSize: 11, fontWeight: 700, flexShrink: 0 },
  select: {
    background: "#12121a",
    color: "#e2e8f0",
    border: "1px solid #2a2a3a",
    borderRadius: 4,
    fontSize: 11,
    padding: "2px 4px",
    maxWidth: 130,
  },
  selectWarn: { borderColor: "#b45309" },
  voice: {
    color: "#64748b",
    fontSize: 10,
    fontFamily: "monospace",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    maxWidth: 190,
  },
  warn: { color: "#fbbf24", fontSize: 10 },
  manage: {
    background: "transparent",
    border: "1px solid #2a2a3a",
    color: "#94a3b8",
    borderRadius: 4,
    fontSize: 10,
    padding: "1px 6px",
    cursor: "pointer",
    marginLeft: "auto",
  },
};
