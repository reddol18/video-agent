#!/usr/bin/env bash
# 이팀장 cron 스크립트 — 10분마다 실행
# .state/noh_done 파일이 있을 때만 리뷰를 실행한다

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$PROJECT_ROOT/.state"
LOG_FILE="$PROJECT_ROOT/agents/lee.log"
SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
SLACK_USER_ID="${SLACK_USER_ID:-}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [이팀장] $*" | tee -a "$LOG_FILE"; }

cd "$PROJECT_ROOT"

# .env 로드
if [ -f "$PROJECT_ROOT/.env" ]; then
  export $(grep -v '^#' "$PROJECT_ROOT/.env" | xargs)
fi

# 노팀장 완료 신호 확인
if [ ! -f "$STATE_DIR/noh_done" ]; then
  log "노팀장 완료 신호 없음. 건너뜀."
  exit 0
fi

BRANCH=$(cat "$STATE_DIR/noh_done")
log "노팀장 완료 감지. 브랜치: $BRANCH. 리뷰 시작."

# 브랜치 최신화
git fetch origin
git checkout "$BRANCH"
git pull origin "$BRANCH"

# main 대비 diff 추출
DIFF=$(git diff origin/main...HEAD -- src/ 2>/dev/null | head -c 6000)

# needs-review 이슈 목록
ISSUES=$(gh issue list \
  --repo reddol18/video-agent \
  --label "needs-review" \
  --state open \
  --json number,title,body \
  --jq 'sort_by(.number)')

log "리뷰 대상 이슈: $(echo "$ISSUES" | jq -r '[.[].number] | map("#\(.)") | join(", ")')"

# 이팀장 Claude 실행
REVIEW_OUTPUT=$(claude --dangerously-skip-permissions -p "
당신은 이팀장입니다. CLAUDE.md의 이팀장 역할 규약을 따르세요.

현재 브랜치: $BRANCH
GitHub 리포지토리: reddol18/video-agent

## 리뷰 대상 이슈
$ISSUES

## 변경된 코드 (main 대비 diff)
$DIFF

## 지시사항
1. docs/technical-design.md를 먼저 읽고 설계 의도를 파악하세요
2. 위 diff가 설계 의도와 일치하는지 꼼꼼히 검토하세요
3. docs/review-notes.md에 리뷰 결과를 기록하세요

**이상 없음인 경우:**
- docs/review-notes.md에 '✅ 이상 없음' 을 포함하여 기록
- 각 이슈에 gh issue comment로 리뷰 완료 코멘트 작성
- src/ 파일은 절대 수정하지 마세요

**문제 발견인 경우:**
- docs/review-notes.md에 문제 내용 상세 기록
- 각 문제마다 gh issue create로 새 이슈 생성
  (레이블: needs-implementation, priority-high 또는 priority-mid)
- 기존 이슈에는 리뷰 결과 코멘트 작성
- src/ 파일은 절대 수정하지 마세요
" 2>&1)

echo "$REVIEW_OUTPUT" | tee -a "$LOG_FILE"

# 리뷰 노트 커밋
git config user.name "이팀장 (Claude)"
git config user.email "noreply@anthropic.com"
git add docs/review-notes.md 2>/dev/null || true
if ! git diff --staged --quiet; then
  git commit -m "docs: 이팀장 리뷰 결과 기록 $(date +%Y%m%d)

Co-Authored-By: Claude (이팀장) <noreply@anthropic.com>"
  git push origin "$BRANCH"
fi

# 결과 판단
if grep -q "✅ 이상 없음" docs/review-notes.md; then
  RESULT="ok"
else
  RESULT="issues_found"
fi

# .state/noh_done 삭제 (어느 경우든)
rm -f "$STATE_DIR/noh_done"
log "결과: $RESULT. .state/noh_done 삭제."

# 메일 발송
ISSUE_REFS=$(echo "$ISSUES" | jq -r '[.[].number] | map("#\(.)") | join(", ")')

if [ "$RESULT" = "ok" ]; then
  SUBJECT="[video-agent] ✅ 이팀장 리뷰 완료 — 박테스터 테스트 지시 필요"
  BODY="이팀장 리뷰가 완료됐습니다. 이상 없음.\n\n처리 이슈: $ISSUE_REFS\n브랜치: $BRANCH\n\n박테스터에게 테스트를 지시하려면:\n  touch $STATE_DIR/audit_requested"
  log "✅ 이상 없음. 오너에게 슬랙 발송."
else
  SUBJECT="[video-agent] ⚠️ 이팀장 리뷰 — 문제 발견, 다음 사이클 재처리 예정"
  BODY="이팀장 리뷰 중 문제가 발견됐습니다.\n\n처리 이슈: $ISSUE_REFS\n\n새 이슈가 등록됐으며 다음 사이클에서 자동 처리됩니다.\ndocs/review-notes.md에서 상세 내용을 확인하세요."
  log "⚠️ 문제 발견. 오너에게 슬랙 발송."
fi

# 슬랙 DM 발송
if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_USER_ID:-}" ]; then
  curl -s -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H "Content-type: application/json" \
    -d "{\"channel\": \"$SLACK_USER_ID\", \"text\": \"$SUBJECT\n\n$(echo -e "$BODY")\"}" \
    && log "슬랙 DM 발송 완료."
else
  log "SENDGRID_API_KEY 또는 OWNER_EMAIL 미설정. 메일 건너뜀."
  log "수동 확인: $SUBJECT"
fi
