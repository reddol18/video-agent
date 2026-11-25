#!/usr/bin/env bash
# 노팀장 cron 스크립트 — 1시간마다 실행
# needs-implementation 이슈가 있을 때만 claude를 실행한다

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$PROJECT_ROOT/.state"
LOG_FILE="$PROJECT_ROOT/agents/noh.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [노팀장] $*" | tee -a "$LOG_FILE"; }

cd "$PROJECT_ROOT"

# 이미 작업 중이면 건너뜀
if [ -f "$STATE_DIR/noh_done" ]; then
  log "이팀장 리뷰 대기 중. 건너뜀."
  exit 0
fi

# needs-implementation 이슈 확인
ISSUE_COUNT=$(gh issue list \
  --repo reddol18/video-agent \
  --label "needs-implementation" \
  --state open \
  --json number \
  --jq 'length')

if [ "$ISSUE_COUNT" -eq 0 ]; then
  log "처리할 이슈 없음. 건너뜀."
  exit 0
fi

log "needs-implementation 이슈 ${ISSUE_COUNT}개 발견. 노팀장 실행."

# feature 브랜치 준비
BRANCH="feature/issue-batch-$(date +%Y%m%d)"
git fetch origin
git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" origin/main 2>/dev/null || git checkout -b "$BRANCH"

# 이슈 목록 수집
ISSUES=$(gh issue list \
  --repo reddol18/video-agent \
  --label "needs-implementation" \
  --state open \
  --json number,title,body,labels \
  --jq 'sort_by(
    if (.labels[] | select(.name == "priority-high")) then 0
    elif (.labels[] | select(.name == "priority-mid")) then 1
    else 2 end,
    .number
  )')

log "이슈 목록: $(echo "$ISSUES" | jq -r '[.[].number] | map("#\(.)") | join(", ")')"

# 노팀장 Claude 실행
claude --dangerously-skip-permissions -p "
당신은 노팀장입니다. CLAUDE.md의 노팀장 역할 규약을 따르세요.

현재 브랜치: $BRANCH
GitHub 리포지토리: reddol18/video-agent

## 처리할 이슈 목록 (우선순위 순)
$ISSUES

## 지시사항
1. docs/technical-design.md와 docs/development-plan.md를 먼저 읽고 설계 의도를 파악하세요
2. 위 이슈를 우선순위 순서대로 모두 구현하세요
3. 구현 완료 후 docs/review-notes.md에 변경 요약을 추가하세요
   형식: ## $(date +%Y%m%d) 배치 처리\n- #이슈번호: 변경 내용 한 줄
4. 각 이슈에 gh issue comment로 구현 완료 코멘트를 작성하세요
5. 각 이슈 레이블을 needs-review로 변경하세요 (gh issue edit --remove-label needs-implementation --add-label needs-review)
6. src/ 외 파일(docs/technical-design.md, CLAUDE.md 등)은 절대 수정하지 마세요
" 2>&1 | tee -a "$LOG_FILE"

# 변경사항 커밋 & 푸시
git config user.name "노팀장 (Claude)"
git config user.email "noreply@anthropic.com"
git add src/ docs/review-notes.md pyproject.toml .env.example 2>/dev/null || true
if ! git diff --staged --quiet; then
  ISSUE_REFS=$(echo "$ISSUES" | jq -r '[.[].number] | map("#\(.)") | join(", ")')
  git commit -m "feat: 이슈 배치 처리 $(date +%Y%m%d)

처리 이슈: $ISSUE_REFS

Co-Authored-By: Claude (노팀장) <noreply@anthropic.com>"
  git push origin "$BRANCH"
  log "커밋 & 푸시 완료: $BRANCH"
fi

# 완료 신호
mkdir -p "$STATE_DIR"
echo "$BRANCH" > "$STATE_DIR/noh_done"
log "완료. .state/noh_done 생성됨."
