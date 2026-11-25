"""인메모리 프로젝트 저장소. 수정 시마다 web/data/{id}/state.json에 덤프."""
from __future__ import annotations
import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from .models import NarrationSegment, Project, SceneCard

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_projects: dict[str, Project] = {}
_lock = asyncio.Lock()

# #405 — project_id 별 직렬화 잠금. 같은 project 의 _dump 동시 호출을 막아
# 마지막 write 가 다른 쪽 변경을 덮어쓰거나 부분 쓰기로 손상되는 회귀를 차단한다.
# uvicorn 단일 프로세스 가정. 멀티 프로세스 환경에서는 별도 IPC 락 필요.
_dump_locks: dict[str, threading.Lock] = {}
_dump_locks_guard = threading.Lock()


def _get_dump_lock(project_id: str) -> threading.Lock:
    """project_id 별 Lock 을 안전하게 lazy-create."""
    with _dump_locks_guard:
        lock = _dump_locks.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _dump_locks[project_id] = lock
        return lock


def _project_dir(project_id: str) -> Path:
    d = DATA_DIR / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dump(project: Project) -> None:
    """state.json 을 원자적으로 기록한다.

    #405 — 다음 두 가지를 보장:
      1) tmp 파일에 전체 내용을 쓴 뒤 os.replace 로 원자적 rename (POSIX 보장).
         → 다른 프로세스가 도중에 읽어도 부분/손상 JSON 을 보지 않는다.
      2) project_id 별 threading.Lock 으로 같은 프로젝트의 동시 _dump 직렬화.
         → 마지막 write 가 다른 쪽 변경을 덮어쓰는 race 차단.

    #407 — 추가로 다음을 수행 (데이터 손실 사고 재발 방지):
      3) 변경 전후 diff 를 audit log(jsonl) 에 append.
      4) 위험 변경(씬 50% 이상 감소 등) 감지 시 풀 백업(timestamped) 자동 생성.
    """
    path = _project_dir(project.project_id) / "state.json"
    payload = project.model_dump_json(indent=2)
    lock = _get_dump_lock(project.project_id)
    with lock:
        # 변경 전 상태 로드 (diff 계산용). 실패해도 dump 자체는 막지 않는다.
        prev_state: dict | None = None
        if path.exists():
            try:
                prev_state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                prev_state = None

        # 같은 디렉토리 내에서 tmp 생성 → os.replace 는 같은 파일시스템에서만 원자성 보장.
        tmp_path = path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, path)
        except Exception:
            # 실패 시 tmp 잔재 제거.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            raise

        # 여기까지 도달했으면 새 state.json 저장 성공. 감사 로그 + 백업 (#407).
        try:
            _audit_log_change(project, prev_state, path)
        except Exception as _audit_err:
            # 감사 로그 실패는 dump 자체를 깨지 않는다. stdout 에만 알림.
            print(f"[audit#407] log 실패 (무시): {_audit_err}", flush=True)


# ── #407 — 변경 감사 로그 + 위험 변경 자동 백업 ─────────────────────────────
# 사용자 데이터 손실 사고(같은 register 호출이 27씬→1씬 만든 사례 2건) 재발 방지.
# 모든 _dump 호출을 추적하고, 의심스러운 변경 시 별도 백업을 즉시 만든다.

# 위험 변경 임계치 — 씬 수가 이 비율 이하로 줄어들면 자동 풀백업.
_AUDIT_DANGER_SCENE_LOSS_RATIO = 0.5  # 50% 이상 감소
_AUDIT_DANGER_MIN_PREV_COUNT = 5      # 이전 씬이 5개 미만이면 percent 기반 판정 skip

# 감사 로그 최대 누적 크기 (10MB). 초과 시 로테이션.
_AUDIT_LOG_MAX_BYTES = 10 * 1024 * 1024

# 2026-09-16 — 씬 전멸 사고(74씬 → 0씬) 재발 방지용 "쓰기 거부" 플래그 이름.
# 클라이언트가 의도적으로 대량 삭제를 하려면 body 에 이 키를 True 로 넣어야 한다.
SCENE_BULK_DELETE_FLAG = "allow_bulk_scene_delete"


def is_dangerous_scene_loss(prev_count: int, new_count: int) -> str | None:
    """씬 수 감소가 "사고로 의심되는 대량 소실" 인지 판정한다.

    감사 로그(`_audit_log_change`)의 danger 판정과 **같은 상수**를 쓴다.
    같은 개념을 두 벌로 유지하지 않기 위해 여기서만 정의하고, 라우터는 이 함수를 쓴다.

    Returns:
        위험하면 사람이 읽을 수 있는 사유 문자열, 아니면 None.
    """
    if prev_count < _AUDIT_DANGER_MIN_PREV_COUNT:
        # 신규/소규모 프로젝트는 비율 판정이 의미 없다 (1씬→0씬 = 100% 손실).
        return None
    loss = prev_count - new_count
    if loss <= 0:
        return None
    if loss / prev_count < _AUDIT_DANGER_SCENE_LOSS_RATIO:
        return None
    if new_count == 0:
        return f"클라이언트가 빈 씬 목록을 보냈습니다 (서버 {prev_count}씬 전멸)"
    return f"씬 수가 {prev_count}→{new_count} 로 {loss}개({loss / prev_count:.0%}) 감소"


def _audit_log_change(project: Project, prev_state: dict | None, dump_path) -> None:
    """state.json 변경을 audit log(jsonl) 에 append + 위험 변경 시 백업.

    Args:
        project: 새 상태 (이미 dump 된 후).
        prev_state: 이전 state.json 의 dict (없으면 신규 프로젝트).
        dump_path: state.json 경로 (백업 위치 결정용).

    감사 로그 형식 (한 줄당 JSON):
        {
          "ts": "2026-06-25T01:23:45+00:00",
          "project_id": "...",
          "scenes_before": 27,
          "scenes_after": 1,
          "scene_ids_added": [],
          "scene_ids_removed": [1,2,4,...,28],
          "scenes_changed_text": [16, 24],          # narration_text 또는 seg_*.text 변경
          "scenes_changed_segments": [16, 21],      # segments 개수 또는 id 변경
          "danger": true,                            # 위험 변경 플래그
          "backup_path": "state.json.bak.20260625-012345"  # danger=true 일 때만
        }
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    new_scenes = list(project.scenes or [])
    new_ids = {s.scene_id for s in new_scenes}

    prev_scenes = (prev_state or {}).get("scenes") if prev_state else None
    if not isinstance(prev_scenes, list):
        prev_scenes = []
    prev_ids = {s.get("scene_id") for s in prev_scenes if isinstance(s, dict)}
    prev_by_id = {s.get("scene_id"): s for s in prev_scenes if isinstance(s, dict)}

    ids_added = sorted(new_ids - prev_ids)
    ids_removed = sorted(prev_ids - new_ids)

    # 텍스트 변경 감지 (narration_text 또는 segments[*].text)
    changed_text: list[int] = []
    changed_segments: list[int] = []
    for s in new_scenes:
        sid = s.scene_id
        if sid not in prev_by_id:
            continue
        prev_s = prev_by_id[sid]
        if (s.narration_text or "") != (prev_s.get("narration_text") or ""):
            changed_text.append(sid)
        # segments 개수/id 변경
        new_segs = s.narration_segments or []
        prev_segs = prev_s.get("narration_segments") or []
        new_seg_ids = [seg.id for seg in new_segs]
        prev_seg_ids = [seg.get("id") for seg in prev_segs if isinstance(seg, dict)]
        if new_seg_ids != prev_seg_ids:
            changed_segments.append(sid)
        else:
            # 같은 id 라도 text 가 바뀌면 changed_text 에 추가
            prev_seg_by_id = {seg.get("id"): seg for seg in prev_segs if isinstance(seg, dict)}
            for seg in new_segs:
                ps = prev_seg_by_id.get(seg.id)
                if ps is not None and (seg.text or "") != (ps.get("text") or ""):
                    if sid not in changed_text:
                        changed_text.append(sid)
                    break

    # 위험 변경 판정
    danger = False
    danger_reason: list[str] = []
    if len(prev_ids) >= _AUDIT_DANGER_MIN_PREV_COUNT:
        loss = len(prev_ids) - len(new_ids)
        if loss > 0 and loss / len(prev_ids) >= _AUDIT_DANGER_SCENE_LOSS_RATIO:
            danger = True
            danger_reason.append(
                f"scene_count {len(prev_ids)}→{len(new_ids)} (loss {loss})"
            )
    # 텍스트가 채워진 segment 가 비워지는 경우도 위험
    for s in new_scenes:
        sid = s.scene_id
        if sid not in prev_by_id:
            continue
        prev_s = prev_by_id[sid]
        prev_segs = prev_s.get("narration_segments") or []
        prev_seg_by_id = {seg.get("id"): seg for seg in prev_segs if isinstance(seg, dict)}
        for seg in (s.narration_segments or []):
            ps = prev_seg_by_id.get(seg.id)
            if ps is None:
                continue
            prev_text = (ps.get("text") or "").strip()
            new_text = (seg.text or "").strip()
            if prev_text and not new_text:
                danger = True
                danger_reason.append(f"scene={sid} seg={seg.id} text wiped")

    # 2026-07-16 — footage 매칭이 다수 비워지는 회귀 감지.
    #   여름나기 1부 프로젝트에서 클라이언트 stale store push 로 인해
    #   매칭 21개가 조용히 유실된 사건 후 도입.
    #   씬 개수는 그대로인데 footage 필드만 wipe 되는 경우 audit 이 놓쳤었다.
    footage_wiped = []
    for s in new_scenes:
        sid = s.scene_id
        if sid not in prev_by_id:
            continue
        prev_s = prev_by_id[sid]
        prev_footage = prev_s.get("footage")
        new_footage = getattr(s, "footage", None)
        if prev_footage and not new_footage:
            footage_wiped.append(sid)
    if footage_wiped:
        # 단일 씬 wipe 는 사용자가 직접 해제한 정상 편집일 수 있으니 3개 이상만 danger.
        if len(footage_wiped) >= 3:
            danger = True
            danger_reason.append(f"footage wiped on {len(footage_wiped)} scenes: {footage_wiped[:10]}")

    backup_path: str | None = None
    if danger:
        # 위험 변경 — 변경 *전* 상태(prev_state) 를 백업으로 보존.
        # prev_state 가 None 이면 백업 불가 (신규 프로젝트라 의미 없음).
        if prev_state is not None:
            ts_for_name = now.replace(":", "-").replace("+", "_").replace(".", "_")
            backup_file = dump_path.with_suffix(f".json.bak.{ts_for_name}")
            try:
                backup_file.write_text(
                    json.dumps(prev_state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                backup_path = str(backup_file)
                print(
                    f"[audit#407] ⚠️ DANGER {project.project_id}: "
                    f"{', '.join(danger_reason)} → 백업 생성: {backup_file.name}",
                    flush=True,
                )
            except Exception as _bak_err:
                print(f"[audit#407] 백업 실패: {_bak_err}", flush=True)

    # 감사 로그 jsonl append.
    entry = {
        "ts": now,
        "project_id": project.project_id,
        "scenes_before": len(prev_ids),
        "scenes_after": len(new_ids),
        "scene_ids_added": ids_added,
        "scene_ids_removed": ids_removed,
        "scenes_changed_text": changed_text,
        "scenes_changed_segments": changed_segments,
        "footage_wiped": footage_wiped,
        "danger": danger,
    }
    if danger_reason:
        entry["danger_reason"] = danger_reason
    if backup_path:
        entry["backup_path"] = backup_path

    log_path = _project_dir(project.project_id) / "audit_log.jsonl"
    # 로테이션: 너무 커지면 .1 로 옮기고 새로 시작.
    try:
        if log_path.exists() and log_path.stat().st_size > _AUDIT_LOG_MAX_BYTES:
            rotated = log_path.with_suffix(".jsonl.1")
            os.replace(log_path, rotated)
    except Exception:
        pass

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 위험 변경이 아니어도 stdout 에 요약 (조용히 무시하기 위함 아님).
    if (ids_added or ids_removed or changed_text or changed_segments):
        summary_parts = []
        if ids_removed:
            summary_parts.append(f"-{len(ids_removed)}씬")
        if ids_added:
            summary_parts.append(f"+{len(ids_added)}씬")
        if changed_text:
            summary_parts.append(f"text:{len(changed_text)}")
        if changed_segments:
            summary_parts.append(f"segs:{len(changed_segments)}")
        prefix = "⚠️ DANGER" if danger else "[audit#407]"
        print(
            f"{prefix} {project.project_id[:8]} {' '.join(summary_parts)}",
            flush=True,
        )


def _migrate_match_trim_to_clip(project: Project) -> bool:
    """#389 — 기존 프로젝트의 match.trim_start/trim_end 를 footage.clip_*_sec 에 1회 자동 적용.

    매칭은 정상적으로 끝났지만 footage.clip_start_sec/clip_end_sec 가 null 인 채로 남아
    인스펙터/TimelineBar/렌더링이 footage 원본 전체 길이를 사용해 어긋나는 회귀를 보정한다.

    정책:
        1. footage 가 clip 타입이고 clip_start_sec/clip_end_sec 가 모두 null 인 씬만 대상.
        2. match.trim_end > match.trim_start 일 때 trim 값으로 채움.
        3. match.source_file basename 과 footage.file basename 이 일치하지 않으면 적용하지 않음
           (오래된/잘못된 match 데이터로 인한 회귀 방지 — matchTrustworthy 가드).

    Returns:
        True 면 1개 이상 씬 변경 → 호출자가 _dump 필요.
    """
    from pathlib import Path as _P
    changed = False
    for scene in project.scenes:
        footage = scene.footage
        match = scene.match
        if footage is None or match is None:
            continue
        if getattr(footage, "type", None) != "clip":
            continue
        # 이미 사용자가 설정한 값이 있으면 보존.
        if footage.clip_start_sec is not None or footage.clip_end_sec is not None:
            continue
        trim_start = float(match.trim_start or 0.0)
        trim_end = float(match.trim_end or 0.0)
        if not (trim_end > trim_start):
            continue
        # basename 일치 가드.
        match_basename = _P(str(match.source_file or "")).name
        footage_basename = _P(str(footage.file or "")).name
        if match_basename and footage_basename and match_basename != footage_basename:
            continue
        footage.clip_start_sec = trim_start
        footage.clip_end_sec = trim_end
        changed = True
        print(
            f"[migrate:389] scene={scene.scene_id} clip_*_sec 자동 채움: "
            f"{trim_start:.3f}~{trim_end:.3f}",
            flush=True,
        )
    return changed


def _migrate_narration_segments(project: Project) -> bool:
    """#382 — narration_segments 가 비어있는 씬을 seg_0 단일 segment 로 자동 변환.

    기존 데이터(narration_text + tts)를 그대로 seg_0 으로 옮긴다. seg_0 만 있는
    프로젝트는 N=1 케이스이므로 렌더/SBV 결과가 마이그레이션 전과 동일해야 한다.

    또한 기존 narration WAV 파일 경로 ``narration_scene_NNN.wav`` 가 디스크에 있으면
    신규 경로 규약 ``scene_NNN_seg_seg_0.wav`` 로 복사한다(원본은 유지하여 회귀 안전).

    Returns:
        True 면 1개 이상 씬이 변경됨 → 호출자가 _dump 필요.
    """
    changed = False
    narr_dir = _project_dir(project.project_id) / "output" / "narration"
    for scene in project.scenes:
        if scene.narration_segments:
            continue
        # 빈 segments 이면서 narration_text 가 비어 있으면 seg_0 도 만들지 않는다.
        # → 빈 씬은 그대로 빈 채로 두고 사용자가 채우게 한다.
        if not (scene.narration_text and scene.narration_text.strip()):
            continue

        tts_meta = scene.tts
        seg = NarrationSegment(
            id="seg_0",
            text=scene.narration_text,
            offset_sec=0.0,
            audio_url=tts_meta.audio_url if tts_meta else "",
            duration_sec=tts_meta.duration_sec if tts_meta else 0.0,
            status=tts_meta.status if tts_meta else "idle",
            synced=tts_meta.synced if tts_meta else False,
            sbv_text=scene.subtitle_text,
        )
        scene.narration_segments = [seg]
        changed = True

        # 기존 WAV 파일을 신규 경로 규약으로 복사 (원본 유지).
        try:
            legacy = narr_dir / f"narration_scene_{scene.scene_id:03d}.wav"
            new_name = narr_dir / f"scene_{scene.scene_id:03d}_seg_seg_0.wav"
            if legacy.exists() and not new_name.exists():
                import shutil as _sh
                _sh.copy2(str(legacy), str(new_name))
        except Exception as e:
            # 마이그레이션 실패는 무시 — 다음 TTS 생성 시 새 경로로 재생성된다.
            print(f"[migrate] WAV 복사 실패 scene={scene.scene_id}: {e}", flush=True)
    return changed


def load_all() -> None:
    """기동 시 web/data/ 하위 state.json 전부 로드.

    `_templates/` 폴더는 템플릿 저장소(#406)이며 일반 프로젝트가 아니므로 제외한다.
    폴더 이름이 UUID 가 아니라 자연스럽게 걸리지 않지만, 명시적으로 차단해
    실수로 `_templates/{id}/state.json` 이 생겨도 프로젝트 목록을 오염시키지 않도록 한다.
    """
    for state_file in DATA_DIR.glob("*/state.json"):
        # 부모 폴더가 `_` 로 시작하면(현재 `_templates`) 시스템 폴더로 간주 → 스킵.
        if state_file.parent.name.startswith("_"):
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            p = Project(**data)
            # #382 — 로드 직후 자동 마이그레이션. 변경 발생 시 디스크에 즉시 반영.
            #         #389 마이그레이션과 통합 — 둘 중 하나만 변경되어도 _dump 호출.
            migrated_segments = _migrate_narration_segments(p)
            migrated_trim = _migrate_match_trim_to_clip(p)
            if migrated_segments or migrated_trim:
                _dump(p)
            _projects[p.project_id] = p
        except Exception as e:
            print(f"[state] 프로젝트 로드 실패 {state_file}: {e}", flush=True)


def create_project(name: str, footage_dir: str = "", scenario_text: str = "",
                   auto_footage_dir: bool = False) -> Project:
    now = datetime.now(timezone.utc).isoformat()
    project_id = str(uuid.uuid4())
    # auto_footage_dir이 True이거나 footage_dir이 비어있으면 자동 경로 사용
    if auto_footage_dir or not footage_dir:
        fd = DATA_DIR / project_id / "footage"
        fd.mkdir(parents=True, exist_ok=True)
        footage_dir = str(fd)
    p = Project(
        project_id=project_id,
        name=name,
        footage_dir=footage_dir,
        scenario_text=scenario_text,
        created_at=now,
        updated_at=now,
    )
    _projects[p.project_id] = p
    _dump(p)
    return p


def duplicate_project(source_id: str) -> Project | None:
    """프로젝트 복제. footage_dir은 원본 경로를 그대로 공유한다(파일 복사 없음).

    state.json은 새 project_id로 별도 저장하고, scenes/studio_assets 등은 deep copy.
    """
    src = _projects.get(source_id)
    if not src:
        return None
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    # Pydantic 모델 deep copy 후 식별자만 교체
    dup = src.model_copy(deep=True)
    dup.project_id = new_id
    dup.name = f"[복사] {src.name}" if src.name else "[복사]"
    dup.created_at = now
    dup.updated_at = now
    # footage_dir은 원본 경로를 그대로 공유 (파일 미복사)
    _projects[new_id] = dup
    _dump(dup)
    return dup


def get_project(project_id: str) -> Project | None:
    return _projects.get(project_id)


def list_projects() -> list[Project]:
    return list(_projects.values())


def update_project(project_id: str, **kwargs) -> Project | None:
    """프로젝트 필드를 갱신한다.

    ⚠ setattr 만 하면 JSON body 로 들어온 dict 가 Pydantic 모델로 변환되지 않고
    raw dict 그대로 메모리에 남는다(Project 에 validate_assignment 가 없음).
    그러면 state.json 직렬화는 되지만, 이후 `scene.scene_id` 처럼 속성 접근을
    하는 코드가 'dict' object has no attribute ... 로 500 을 낸다.
    (재현: PATCH /projects/{id} 로 scenes 배열 저장 → PATCH /projects/{id}/scenes/{sid})

    그래서 선언된 필드 타입으로 한 번 검증/변환한 뒤 대입한다. 이미 올바른 모델이면
    그대로 통과하므로 기존 호출부는 영향받지 않는다.
    """
    p = _projects.get(project_id)
    if not p:
        return None
    for k, v in kwargs.items():
        if not hasattr(p, k):
            continue
        field = Project.model_fields.get(k)
        if field is not None and v is not None:
            try:
                v = TypeAdapter(field.annotation).validate_python(v)
            except ValidationError as e:
                raise ValueError(f"{k} 검증 실패: {e}") from e
        setattr(p, k, v)
    p.updated_at = datetime.now(timezone.utc).isoformat()
    _dump(p)
    return p


def delete_project(project_id: str) -> bool:
    if project_id not in _projects:
        return False
    del _projects[project_id]
    import shutil
    shutil.rmtree(_project_dir(project_id), ignore_errors=True)
    return True


def update_scene(project_id: str, scene_id: int, **kwargs) -> SceneCard | None:
    p = _projects.get(project_id)
    if not p:
        return None
    for scene in p.scenes:
        if scene.scene_id == scene_id:
            for k, v in kwargs.items():
                if hasattr(scene, k):
                    setattr(scene, k, v)
            p.updated_at = datetime.now(timezone.utc).isoformat()
            _dump(p)
            return scene
    return None


def get_cache_dir(project_id: str) -> Path:
    d = _project_dir(project_id) / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_output_dir(project_id: str) -> Path:
    d = _project_dir(project_id) / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d
