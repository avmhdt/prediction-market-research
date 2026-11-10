#!/usr/bin/env bash
# Keeps the paper service (`pmr paper run`: book recorder + paper strategies) running.
#
# Runs from the pinned deploy clone in WSL ext4, never from /mnt/d, so edits in the working
# repo never change the running code. Cron starts it at boot and every 2 minutes; `flock -n`
# makes every start after the first a no-op while the service is alive. Missed time is logged
# as gaps by the service itself (DQ-14), never filled.
#
# Crontab lines (tag: pmr-paper):
#   @reboot     $HOME/deploy/prediction-market-research/scripts/pmr-paper-supervisor.sh # pmr-paper
#   */2 * * * * $HOME/deploy/prediction-market-research/scripts/pmr-paper-supervisor.sh # pmr-paper
set -u
DEPLOY="$HOME/deploy/prediction-market-research"
export PMR_DATA_DIR="$HOME/data/prediction-market-research"
export PMR_LOG_LEVEL=INFO
LOG_DIR="$PMR_DATA_DIR/logs"
STOP_FILE="$PMR_DATA_DIR/paper/STOP"
mkdir -p "$LOG_DIR"
# A STOP file pauses the service (used for redeploys and after the paper period ends).
[ -e "$STOP_FILE" ] && exit 0
cd "$DEPLOY" || exit 1
exec flock -n "$HOME/deploy/pmr-paper.lock" \
  nice -n 10 "$DEPLOY/.venv/bin/pmr" paper run >> "$LOG_DIR/paper.log" 2>&1
