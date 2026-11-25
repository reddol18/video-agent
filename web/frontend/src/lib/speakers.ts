/**
 * 다중 화자 — 씬 태그에서 화자를 읽어 목소리를 정한다.
 *
 * 백엔드 `web/backend/speakers.py` 와 같은 규칙을 구현한다. UI 가 보여주는 목소리와
 * 실제 렌더 결과가 어긋나면 안 되므로, 규칙을 바꿀 때는 두 파일을 함께 고친다.
 *
 * 태그 문법: `speaker:홍길동`
 * 우선순위: segment.voice_name > 씬 speaker 태그 > 프로젝트 기본 tts_voice
 */
import type { NarrationSegment, Project, SceneCard, Speaker } from "../types";

export const SPEAKER_TAG_PREFIX = "speaker:";

/** 비교용 정규화 — 앞뒤 공백/대소문자 차이는 같은 이름으로 본다. */
export function normalizeSpeakerName(name: string): string {
  return (name || "").trim().toLocaleLowerCase();
}

export function speakerTag(name: string): string {
  return `${SPEAKER_TAG_PREFIX}${(name || "").trim()}`;
}

export function isSpeakerTag(tag: string): boolean {
  const t = (tag || "").trim();
  return t.toLocaleLowerCase().startsWith(SPEAKER_TAG_PREFIX) && t.length > SPEAKER_TAG_PREFIX.length;
}

/** 씬 태그에서 화자명 추출. 씬당 1명이며, 여러 개면 첫 번째를 쓴다. */
export function extractSpeakerName(tags: string[] | undefined | null): string | null {
  for (const raw of tags || []) {
    if (typeof raw !== "string") continue;
    const t = raw.trim();
    if (!isSpeakerTag(t)) continue;
    const name = t.slice(SPEAKER_TAG_PREFIX.length).trim();
    if (name) return name;
  }
  return null;
}

export function findSpeaker(speakers: Speaker[] | undefined | null, name: string | null): Speaker | null {
  if (!name) return null;
  const key = normalizeSpeakerName(name);
  return (speakers || []).find((s) => normalizeSpeakerName(s.name) === key) ?? null;
}

/** 씬에 적용될 목소리. 태그가 미등록 이름이면 기본 목소리로 떨어진다. */
export function resolveSceneVoice(
  scene: SceneCard,
  project: Pick<Project, "tts_voice" | "speakers">,
): { voice: string; speakerName: string | null; known: boolean } {
  const base = project.tts_voice || "";
  const name = extractSpeakerName(scene.tags);
  if (!name) return { voice: base, speakerName: null, known: true };
  const sp = findSpeaker(project.speakers, name);
  if (!sp) return { voice: base, speakerName: name, known: false };
  return { voice: sp.voice_name || base, speakerName: sp.name, known: true };
}

/** segment 에 실제로 쓰일 목소리. */
export function resolveSegmentVoice(
  segment: Pick<NarrationSegment, "voice_name">,
  scene: SceneCard,
  project: Pick<Project, "tts_voice" | "speakers">,
): string {
  if (segment.voice_name) return segment.voice_name;
  return resolveSceneVoice(scene, project).voice;
}

/** 씬 태그에서 화자 태그만 교체한다(없으면 추가, name=null 이면 제거). */
export function setSpeakerTag(tags: string[] | undefined | null, name: string | null): string[] {
  const rest = (tags || []).filter((t) => !isSpeakerTag(t));
  return name ? [speakerTag(name), ...rest] : rest;
}
