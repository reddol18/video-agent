"""파일 해시 / 수정일 기반 캐시 유틸"""

import hashlib
import json
import os
from pathlib import Path


def file_fingerprint(path: str) -> dict:
    """파일의 수정일시와 크기를 반환. 캐시 유효성 판단에 사용."""
    s = os.stat(path)
    return {"mtime": s.st_mtime, "size": s.st_size}


def text_hash(text: str) -> str:
    """텍스트의 SHA-256 해시 반환. 시나리오 변경 감지에 사용."""
    return hashlib.sha256(text.encode()).hexdigest()


def load_json(path: str) -> dict | list | None:
    """JSON 파일 읽기. 파일이 없으면 None 반환."""
    p = Path(path)
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict | list) -> None:
    """JSON 파일 저장. 상위 디렉토리가 없으면 자동 생성."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
