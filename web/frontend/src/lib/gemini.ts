/**
 * Gemini API 브라우저 직접 호출
 */
import { GoogleGenerativeAI } from "@google/generative-ai";
import { getGoogleApiKey } from "./apiKey";

async function getClient(): Promise<GoogleGenerativeAI> {
  // 서버 우선으로 키 조회(웹서비스 전환). 다른 origin/기기에서도 동작.
  const apiKey = await getGoogleApiKey();
  return new GoogleGenerativeAI(apiKey);
}

/** 씬 목록 기반 footage 매칭 추천 */
export async function matchFootageByLabel(
  scenes: Array<{ scene_id: number; title: string; description: string; keywords: string[] }>,
  assets: Array<{ asset_id: string; filename: string; label: string }>
): Promise<Record<number, string>> {
  const genai = await getClient();
  const model = genai.getGenerativeModel({ model: "gemini-2.5-flash" });

  const prompt = `
다음 씬 목록과 footage 목록을 보고 각 씬에 가장 적합한 footage를 매칭해주세요.

씬 목록:
${scenes.map((s) => `- scene_id=${s.scene_id}: ${s.title} / ${s.description} / 키워드: ${s.keywords.join(", ")}`).join("\n")}

footage 목록:
${assets.map((a) => `- asset_id=${a.asset_id}: 파일명=${a.filename}, 레이블=${a.label}`).join("\n")}

응답 형식 (JSON만, 설명 없이):
{"scene_id": "asset_id", ...}

매칭이 불확실하면 해당 scene_id는 포함하지 마세요.
`.trim();

  const result = await model.generateContent(prompt);
  const text = result.response.text().replace(/```json|```/g, "").trim();
  try {
    const raw = JSON.parse(text);
    const out: Record<number, string> = {};
    for (const [k, v] of Object.entries(raw)) {
      out[parseInt(k)] = v as string;
    }
    return out;
  } catch {
    return {};
  }
}

/** 시나리오 텍스트 → 씬 목록 파싱 */
export async function parseScenario(scenarioText: string): Promise<Array<{
  title: string;
  description: string;
  mood: string;
  keywords: string[];
  duration_hint_sec: number;
  narration_text: string;
}>> {
  const genai = await getClient();
  const model = genai.getGenerativeModel({ model: "gemini-2.5-flash" });

  const prompt = `
다음 시나리오 텍스트를 씬 단위로 파싱해서 JSON 배열로 반환하세요.

시나리오:
${scenarioText}

응답 형식 (JSON 배열만, 설명 없이):
[
  {
    "title": "씬 제목",
    "description": "씬 설명 (영상 내용)",
    "mood": "분위기",
    "keywords": ["키워드1", "키워드2"],
    "duration_hint_sec": 5,
    "narration_text": "나레이션 텍스트"
  }
]
`.trim();

  const result = await model.generateContent(prompt);
  const text = result.response.text().replace(/```json|```/g, "").trim();
  try {
    return JSON.parse(text);
  } catch {
    return [];
  }
}
