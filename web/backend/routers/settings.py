from __future__ import annotations
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

_ROOT = Path(__file__).parent.parent.parent.parent
_ENV_FILE = _ROOT / ".env"
_GCP_KEY_FILE = _ROOT / "gcp_key.json"

router = APIRouter(prefix="/api/settings", tags=["settings"])

_MANAGED_KEYS = {
    "GOOGLE_API_KEY",
    "GEMINI_REQUEST_INTERVAL_SEC",
    "GEMINI_MAX_RETRY",
    "TTS_VOICE_NAME",
    "TTS_SPEAKING_RATE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "TYPECAST_API_KEY",
}


def _read_env() -> dict[str, str]:
    if not _ENV_FILE.exists():
        return {}
    result: dict[str, str] = {}
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line)
        if m:
            result[m.group(1)] = m.group(2)
    return result


def _write_env(updates: dict[str, str]) -> None:
    if not _ENV_FILE.exists():
        lines = []
    else:
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()

    written: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        m = re.match(r'^([A-Z_][A-Z0-9_]*)=', stripped)
        if m and m.group(1) in updates:
            key = m.group(1)
            new_lines.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            new_lines.append(line)

    for key, val in updates.items():
        if key not in written:
            new_lines.append(f"{key}={val}")

    _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


class AppSettings(BaseModel):
    google_api_key: str = ""
    gemini_request_interval_sec: str = "4"
    gemini_max_retry: str = "3"
    tts_voice_name: str = "ko-KR-Chirp3-HD-Orus"
    tts_speaking_rate: str = "0.9"
    gcp_key_configured: bool = False
    # Typecast — 키 자체는 노출하지 않고 설정 여부만 bool 로 알린다.
    typecast_api_key_configured: bool = False


class UpdateSettingsRequest(BaseModel):
    google_api_key: str | None = None
    gemini_request_interval_sec: str | None = None
    gemini_max_retry: str | None = None
    tts_voice_name: str | None = None
    tts_speaking_rate: str | None = None
    typecast_api_key: str | None = None


@router.get("", response_model=AppSettings)
def get_settings():
    env = _read_env()
    return AppSettings(
        google_api_key=env.get("GOOGLE_API_KEY", ""),
        gemini_request_interval_sec=env.get("GEMINI_REQUEST_INTERVAL_SEC", "4"),
        gemini_max_retry=env.get("GEMINI_MAX_RETRY", "3"),
        tts_voice_name=env.get("TTS_VOICE_NAME", "ko-KR-Chirp3-HD-Orus"),
        tts_speaking_rate=env.get("TTS_SPEAKING_RATE", "0.9"),
        gcp_key_configured=_GCP_KEY_FILE.exists() and _GCP_KEY_FILE.stat().st_size > 0,
        typecast_api_key_configured=bool(env.get("TYPECAST_API_KEY", "").strip()),
    )


@router.put("")
def update_settings(req: UpdateSettingsRequest):
    updates: dict[str, str] = {}
    if req.google_api_key is not None:
        updates["GOOGLE_API_KEY"] = req.google_api_key
    if req.gemini_request_interval_sec is not None:
        updates["GEMINI_REQUEST_INTERVAL_SEC"] = req.gemini_request_interval_sec
    if req.gemini_max_retry is not None:
        updates["GEMINI_MAX_RETRY"] = req.gemini_max_retry
    if req.tts_voice_name is not None:
        updates["TTS_VOICE_NAME"] = req.tts_voice_name
    if req.tts_speaking_rate is not None:
        updates["TTS_SPEAKING_RATE"] = req.tts_speaking_rate
    # 빈 문자열은 무시 — 마스킹된 값을 다시 저장해 키를 지우는 사고 방지.
    if req.typecast_api_key is not None and req.typecast_api_key.strip():
        updates["TYPECAST_API_KEY"] = req.typecast_api_key.strip()

    if updates:
        _write_env(updates)

    return {"ok": True}


@router.post("/gcp-key")
async def upload_gcp_key(file: UploadFile = File(...)):
    content = await file.read()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(400, "유효한 JSON 파일이 아닙니다")

    if parsed.get("type") != "service_account":
        raise HTTPException(400, 'service_account 타입의 키 파일이 아닙니다 (type 필드 확인)')

    _GCP_KEY_FILE.write_bytes(content)
    _write_env({"GOOGLE_APPLICATION_CREDENTIALS": "./gcp_key.json"})

    return {"ok": True, "project_id": parsed.get("project_id", "")}
