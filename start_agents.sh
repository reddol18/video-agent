#!/bin/bash

# 1. 세션 이름 및 경로 설정
SESSION="video_agents"
PROJECT_DIR="/Users/jkon/Projects/video-agent"

# 2. 기존 세션이 있는지 확인 (있으면 바로 접속)
tmux has-session -t $SESSION 2>/dev/null
if [ $? -eq 0 ]; then
    tmux attach-session -t $SESSION
    exit 0
fi

# 3. 새로운 세션 생성 및 각 탭별 CLI 실행
# [노팀장 - Claude CLI]
tmux new-session -d -s $SESSION -n '에이전트팀1' -c "$PROJECT_DIR"
tmux send-keys -t $SESSION:0 "claude" C-m

# [이팀장 - Claude CLI]
tmux new-window -t $SESSION -n '에이전트팀2' -c "$PROJECT_DIR"
tmux send-keys -t $SESSION:1 "claude" C-m

# [김감독 - Gemini CLI]
tmux new-window -t $SESSION -n '김감독' -c "$PROJECT_DIR"
tmux send-keys -t $SESSION:2 "gemini" C-m

# 4. '김감독' 탭을 활성화 (2번 인덱스)
tmux select-window -t $SESSION:2

# 5. 세션 접속
tmux attach-session -t $SESSION
