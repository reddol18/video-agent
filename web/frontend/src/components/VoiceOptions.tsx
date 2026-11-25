import { Fragment } from "react";
import type { TtsVoice } from "../lib/ttsVoices";

/**
 * TTS 목소리 <select> 안에 넣을 <option> 묶음.
 * Google / Typecast 를 optgroup 으로 나눠 렌더한다(Typecast 는 596개라 개수 라벨 표시).
 *
 * 사용처: Editor 프로젝트 tts_voice select, TtsStartModal, (필요 시) segment inspector.
 * merge 된 voices 를 그대로 넘기면 되며, 임시 "로드 중…" option(현재값 유지용)은
 * provider 미분류 그룹으로 흡수된다.
 */
export default function VoiceOptions({ voices }: { voices: TtsVoice[] }) {
  const google = voices.filter((v) => (v.provider ?? "google") === "google");
  const typecast = voices.filter((v) => v.provider === "typecast");
  return (
    <Fragment>
      <optgroup label={`Google (${google.length})`}>
        {google.map((v) => (
          <option key={v.value} value={v.value}>
            {v.label}
          </option>
        ))}
      </optgroup>
      {typecast.length > 0 && (
        <optgroup label={`Typecast (${typecast.length})`}>
          {typecast.map((v) => (
            <option key={v.value} value={v.value}>
              {v.label}
            </option>
          ))}
        </optgroup>
      )}
    </Fragment>
  );
}
