#!/usr/bin/env bash
# 박테스터 cron 스크립트 — 10분마다 실행
# .state/audit_requested 파일이 있을 때만 감사를 실행한다
# 프로덕트 오너가 수동으로 touch .state/audit_requested 를 실행해야 시작됨

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$PROJECT_ROOT/.state"
LOG_FILE="$PROJECT_ROOT/agents/park.log"
SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
SLACK_USER_ID="${SLACK_USER_ID:-}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [박테스터] $*" | tee -a "$LOG_FILE"; }

cd "$PROJECT_ROOT"

# .env 로드
if [ -f "$PROJECT_ROOT/.env" ]; then
  export $(grep -v '^#' "$PROJECT_ROOT/.env" | xargs)
fi

# 프로덕트 오너 지시 확인
if [ ! -f "$STATE_DIR/audit_requested" ]; then
  log "감사 요청 없음. 건너뜀."
  exit 0
fi

log "감사 요청 감지. 테스트 시작."

# needs-audit 이슈 목록
ISSUES=$(gh issue list \
  --repo reddol18/video_agent \
  --label "needs-audit" \
  --state open \
  --json number,title \
  --jq 'sort_by(.number)')

log "감사 대상 이슈: $(echo "$ISSUES" | jq -r '[.[].number] | map("#\(.)") | join(", ")')"

# 최신 feature 브랜치 확인
BRANCH=$(git branch -r | grep 'origin/feature/issue-batch' | sort | tail -1 | xargs | sed 's|origin/||')
log "감사 대상 브랜치: $BRANCH"

git fetch origin
git checkout "$BRANCH"
git pull origin "$BRANCH"

# 박테스터 Claude 실행
AUDIT_OUTPUT=$(claude --dangerously-skip-permissions -p "
당신은 박테스터입니다. CLAUDE.md의 박테스터 역할 규약을 따르세요.

현재 브랜치: $BRANCH
GitHub 리포지토리: reddol18/video_agent

## 감사 대상 이슈
$ISSUES

## 지시사항
1. CLAUDE.md의 감사 항목 표를 기준으로 cache/, output/ 산출물을 꼼꼼히 검사하세요
2. docs/test-report.md에 감사 결과를 기록하세요
   형식: ## $(date +%Y%m%d) 감사\n| 항목 | 결과 | 비고 |\n...
3. 각 이슈에 gh issue comment로 감사 결과 코멘트를 작성하세요

**통과인 경우:**
- docs/test-report.md에 '✅ 전체 통과' 포함하여 기록
- 각 이슈에 staging-ready 레이블 추가
  (gh issue edit --add-label staging-ready)
- src/, cache/, output/ 파일은 절대 수정하지 마세요

**실패인 경우:**
- docs/test-report.md에 실패 항목 상세 기록
- 각 실패 항목마다 gh issue create로 새 이슈 생성
  (레이블: needs-implementation + priority-high)
- 기존 이슈에 audit-failed 레이블 추가
  (gh issue edit --add-label audit-failed)
- src/, cache/, output/ 파일은 절대 수정하지 마세요
" 2>&1)

echo "$AUDIT_OUTPUT" | tee -a "$LOG_FILE"

# 감사 결과 판단
if grep -q "✅ 전체 통과" docs/test-report.md; then
  RESULT="pass"
else
  RESULT="fail"
fi

ISSUE_REFS=$(echo "$ISSUES" | jq -r '[.[].number] | map("#\(.)") | join(", ")')

if [ "$RESULT" = "pass" ]; then
  log "✅ 감사 통과. PR 생성 중..."

  # feature → test PR 생성
  TOKEN=$(gh auth token --hostname github.com)
  git remote set-url origin "https://reddol18:${TOKEN}@github.com/reddol18/video_agent.git"

  PR1_URL=$(gh pr create \
    --repo reddol18/video_agent \
    --title "[batch] $(date +%Y%m%d) 구현 완료 — $ISSUE_REFS" \
    --body "## 처리된 이슈
$(echo "$ISSUES" | jq -r '.[].number' | awk '{print "Closes #"$1}')

## 감사 결과
박테스터 감사 통과 ✅

$(grep -A 30 "## $(date +%Y%m%d) 감사" docs/test-report.md | head -30)

---
🤖 박테스터(Claude)가 자동 생성한 PR" \
    --base test \
    --head "$BRANCH" 2>/dev/null || echo "이미 PR 존재")

  log "1차 PR (feature→test): $PR1_URL"

  # test → main PR 생성
  PR2_URL=$(gh pr create \
    --repo reddol18/video_agent \
    --title "[release] $(date +%Y%m%d) 스테이징 검증 완료" \
    --body "## 감사 통과 내역
$ISSUE_REFS

## 테스트 결과 요약
$(grep -A 20 "## $(date +%Y%m%d) 감사" docs/test-report.md | head -20)

---
🤖 박테스터(Claude)가 자동 생성한 PR" \
    --base main \
    --head test 2>/dev/null || echo "이미 PR 존재")

  log "2차 PR (test→main): $PR2_URL"

  SUBJECT="[video-agent] ✅ 박테스터 감사 통과 — PR 머지 요청"
  BODY="박테스터 감사가 통과됐습니다.\n\n감사 이슈: $ISSUE_REFS\n\n1차 PR (feature→test): $PR1_URL\n2차 PR (test→main): $PR2_URL\n\n검토 후 머지해주세요."

else
  log "❌ 감사 실패. 새 이슈 등록 완료."
  SUBJECT="[video-agent] ❌ 박테스터 감사 실패 — 재작업 예정"
  BODY="박테스터 감사에서 실패 항목이 발견됐습니다.\n\n감사 이슈: $ISSUE_REFS\n\n새 이슈가 등록됐으며 다음 사이클에서 노팀장이 재처리합니다.\ndocs/test-report.md에서 상세 내용을 확인하세요."
fi

# 완료 신호 제거
rm -f "$STATE_DIR/audit_requested"
log ".state/audit_requested 삭제."

# 슬랙 DM 발송
if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_USER_ID:-}" ]; then
  curl -s -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H "Content-type: application/json" \
    -d "{\"channel\": \"$SLACK_USER_ID\", \"text\": \"$SUBJECT\n\n$(echo -e "$BODY")\"}" \
    && log "슬랙 DM 발송 완료."
else
  log "SLACK_BOT_TOKEN 또는 SLACK_USER_ID 미설정. 메시지 건너뜀."
  log "수동 확인: $SUBJECT"
fi
