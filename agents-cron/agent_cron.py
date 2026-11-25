"""agent_cron.py — 노팀장·이팀장·박테스터 cron 진입점 (OS 무관).

bash 3종(noh.sh 94 · lee.sh 127 · park.sh 156)을 대체한다.
세 에이전트가 같은 뼈대를 쓴다: 상태파일 확인 → gh 조회 → claude 실행 → 커밋/푸시.
다른 건 트리거 조건과 프롬프트뿐이라 여기 한 곳에 모았다.

사용:
  python agents-cron/agent_cron.py noh     # 1시간마다 (needs-implementation 감지)
  python agents-cron/agent_cron.py lee     # 10분마다 (.state/noh_done 감지)
  python agents-cron/agent_cron.py park    # 10분마다 (.state/audit_requested 감지)

크론 등록 (Windows 작업 스케줄러 / WSL cron / launchd 공통):
  python <이 파일> <역할>
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
AGENTS = ROOT / "agents"
REPO = os.environ.get("VIDEO_AGENT_REPO", "reddol18/video_agent")


def log(role: str, msg: str) -> None:
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [{role}] {msg}"
    print(line, flush=True)
    try:
        AGENTS.mkdir(parents=True, exist_ok=True)
        with (AGENTS / f"{ROLE_KEY}.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sh(args: list[str], *, check: bool = False, timeout: int = 600) -> tuple[int, str]:
    """외부 명령 실행. shell=False 로 Windows 따옴표 문제를 피한다."""
    try:
        p = subprocess.run(args, cwd=str(ROOT), capture_output=True,
                           text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        if check and p.returncode != 0:
            raise RuntimeError(out[:300])
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, f"[타임아웃 {timeout}s] {' '.join(args[:3])}"
    except FileNotFoundError:
        return 127, f"[명령 없음] {args[0]}"


def load_env() -> None:
    """.env 를 환경변수로 (bash 판의 export $(grep ...) 대체)."""
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def gh_issues(label: str) -> list[dict]:
    rc, out = sh(["gh", "issue", "list", "--repo", REPO, "--label", label,
                  "--state", "open", "--json", "number,title,body,labels"])
    if rc != 0:
        log(ROLE_KEY, f"gh 조회 실패: {out[:150]}")
        return []
    try:
        issues = json.loads(out or "[]")
    except Exception:
        return []

    def rank(i: dict) -> tuple[int, int]:
        names = {l.get("name") for l in i.get("labels", [])}
        pri = 0 if "priority-high" in names else (1 if "priority-mid" in names else 2)
        return pri, i.get("number", 0)

    return sorted(issues, key=rank)


def run_claude(prompt: str) -> str:
    """claude CLI 를 비대화형으로 실행."""
    rc, out = sh(["claude", "--dangerously-skip-permissions", "-p", prompt],
                 timeout=3600)
    if rc == 127:
        log(ROLE_KEY, "claude CLI 를 찾을 수 없음 — PATH 확인 필요")
    return out


def git_commit_push(branch: str, paths: list[str], message: str) -> bool:
    sh(["git", "config", "user.name", f"{ROLE_NAME} (Claude)"])
    sh(["git", "config", "user.email", "noreply@anthropic.com"])
    for p in paths:
        sh(["git", "add", p])
    rc, _ = sh(["git", "diff", "--staged", "--quiet"])
    if rc == 0:
        return False                      # 변경 없음
    sh(["git", "commit", "-m", message])
    sh(["git", "push", "origin", branch])
    return True


# ──────────────────────────────────────────────────────────────────────
def role_noh() -> int:
    """노팀장 — needs-implementation 이슈를 우선순위 순으로 일괄 구현."""
    if (STATE / "noh_done").exists():
        log("노팀장", "이팀장 리뷰 대기 중. 건너뜀.")
        return 0
    issues = gh_issues("needs-implementation")
    if not issues:
        log("노팀장", "처리할 이슈 없음. 건너뜀.")
        return 0
    log("노팀장", f"needs-implementation 이슈 {len(issues)}개 발견. 실행.")

    today = datetime.date.today().strftime("%Y%m%d")
    branch = f"feature/issue-batch-{today}"
    sh(["git", "fetch", "origin"])
    if sh(["git", "checkout", branch])[0] != 0:
        if sh(["git", "checkout", "-b", branch, "origin/main"])[0] != 0:
            sh(["git", "checkout", "-b", branch])

    refs = ", ".join(f"#{i['number']}" for i in issues)
    log("노팀장", f"이슈 목록: {refs}")

    out = run_claude(f"""당신은 노팀장입니다. CLAUDE.md의 노팀장 역할 규약을 따르세요.

현재 브랜치: {branch}
GitHub 리포지토리: {REPO}

## 처리할 이슈 목록 (우선순위 순)
{json.dumps(issues, ensure_ascii=False, indent=1)}

## 지시사항
1. docs/technical-design.md와 docs/development-plan.md를 먼저 읽고 설계 의도를 파악하세요
2. 위 이슈를 우선순위 순서대로 모두 구현하세요
3. 구현 완료 후 docs/review-notes.md에 변경 요약을 추가하세요
   형식: ## {today} 배치 처리 / - #이슈번호: 변경 내용 한 줄
4. 각 이슈에 gh issue comment로 구현 완료 코멘트를 작성하세요
5. 각 이슈 레이블을 needs-review로 변경하세요
   (gh issue edit --remove-label needs-implementation --add-label needs-review)
6. src/ 외 파일(docs/technical-design.md, CLAUDE.md 등)은 절대 수정하지 마세요
""")
    log("노팀장", out[-800:] if out else "(출력 없음)")

    pushed = git_commit_push(
        branch,
        ["src/", "docs/review-notes.md", "pyproject.toml", ".env.example"],
        f"feat: 이슈 배치 처리 {today}\n\n처리 이슈: {refs}\n\n"
        f"Co-Authored-By: Claude (노팀장) <noreply@anthropic.com>")
    if pushed:
        log("노팀장", f"커밋 & 푸시 완료: {branch}")

    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "noh_done").write_text(branch, encoding="utf-8")
    log("노팀장", "완료. .state/noh_done 생성됨.")
    return 0


def role_lee() -> int:
    """이팀장 — 노팀장 구현을 설계 의도와 대조해 리뷰."""
    load_env()
    stamp = STATE / "noh_done"
    if not stamp.exists():
        log("이팀장", "노팀장 완료 신호 없음. 건너뜀.")
        return 0
    branch = stamp.read_text(encoding="utf-8").strip()
    log("이팀장", f"노팀장 완료 감지. 브랜치: {branch}. 리뷰 시작.")

    sh(["git", "fetch", "origin"])
    sh(["git", "checkout", branch])
    sh(["git", "pull", "origin", branch])
    _, diff = sh(["git", "diff", "origin/main...HEAD", "--", "src/"])
    diff = diff[:6000]

    issues = gh_issues("needs-review")
    out = run_claude(f"""당신은 이팀장입니다. CLAUDE.md의 이팀장 역할 규약을 따르세요.

현재 브랜치: {branch}
GitHub 리포지토리: {REPO}

## 리뷰 대상 이슈
{json.dumps(issues, ensure_ascii=False, indent=1)}

## 변경 diff (src/ 한정, 앞부분)
{diff}

## 지시사항
비판적 리뷰 원칙을 따르세요. 노팀장의 설명을 그대로 믿지 말고 코드를 직접 읽어 확인하세요.
신호 판정 도메인 필수 점검(분할 금액 정합성·보유 평단·신호 경계·추세 지표 엣지·look-ahead)을
반드시 코드로 확인하세요.

- 이상 없음: docs/review-notes.md에 근거(파일명:줄번호)와 함께 기록, 각 이슈에 코멘트
- 문제 발견: 각 문제마다 [리뷰지적] 제목으로 이슈 생성(needs-implementation + 우선순위)
둘 다: docs/review-notes.md 커밋·푸시 후 결과를 보고하세요.
슬랙·메일 등 외부 발송은 금지입니다.
""")
    log("이팀장", out[-800:] if out else "(출력 없음)")

    git_commit_push(branch, ["docs/review-notes.md"],
                    "docs: 이팀장 리뷰 결과\n\n"
                    "Co-Authored-By: Claude (이팀장) <noreply@anthropic.com>")
    try:
        stamp.unlink()
        log("이팀장", ".state/noh_done 삭제됨.")
    except Exception:
        pass
    return 0


def role_park() -> int:
    """박테스터 — 산출물 정합성 감사."""
    load_env()
    stamp = STATE / "audit_requested"
    if not stamp.exists():
        log("박테스터", "감사 지시 없음. 건너뜀.")
        return 0
    log("박테스터", "감사 지시 감지. 시작.")
    sh(["git", "fetch", "origin"])

    out = run_claude(f"""당신은 박테스터입니다. CLAUDE.md의 박테스터 역할 규약을 따르세요.

GitHub 리포지토리: {REPO}

## 지시사항
cache/·output/ 산출물이 설계 의도대로 올바른 값을 담고 있는지 Phase 순서(1·2→3→4→5)로
감사하세요. 코드가 "실행됐느냐"가 아니라 "산출물이 옳으냐"를 봅니다.

- 통과: docs/test-report.md에 "✅ 전체 통과" 기록, 관련 이슈에 staging-ready 레이블,
        PR 생성(feature/* → test, 이후 test → main)
- 실패: docs/test-report.md에 상세 기록, 각 실패마다 [감사실패] 이슈 생성,
        기존 이슈에 audit-failed 레이블
닫힌 이슈를 근거로 쓰지 마세요. 슬랙·메일 등 외부 발송은 금지입니다.
""")
    log("박테스터", out[-800:] if out else "(출력 없음)")

    git_commit_push("HEAD", ["docs/test-report.md"],
                    "docs: 박테스터 감사 결과\n\n"
                    "Co-Authored-By: Claude (박테스터) <noreply@anthropic.com>")
    try:
        stamp.unlink()
        log("박테스터", ".state/audit_requested 삭제됨.")
    except Exception:
        pass
    return 0


ROLES = {
    "noh":  ("노팀장",   role_noh),
    "lee":  ("이팀장",   role_lee),
    "park": ("박테스터", role_park),
}

ROLE_KEY = "noh"
ROLE_NAME = "노팀장"


def main() -> int:
    global ROLE_KEY, ROLE_NAME
    if len(sys.argv) < 2 or sys.argv[1] not in ROLES:
        print(f"사용법: agent_cron.py {{{'|'.join(ROLES)}}}")
        return 1
    ROLE_KEY = sys.argv[1]
    ROLE_NAME, fn = ROLES[ROLE_KEY]
    try:
        return fn()
    except Exception as e:                                   # noqa: BLE001
        log(ROLE_NAME, f"예외: {e!r}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
