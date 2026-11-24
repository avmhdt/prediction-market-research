#!/usr/bin/env bash
# One warehouse build step per run, from cron once an hour (owner decision 2026-11-24, plan A).
#   20 * * * * flock -n $HOME/deploy/pmr-warehouse.lock $HOME/deploy/prediction-market-research/scripts/pmr-warehouse-step.sh # pmr-warehouse
# A run starts only when MemAvailable >= 45 GB and the paper heartbeat is under 90 s old. It
# commits one step (0.5-2 GB written) with DuckDB at 8 GB and a 1 GB spill cap, at nice 10 and
# idle I/O, with thread pools capped at 8. The --guard watcher rolls the step back on low
# memory or stalled paper polling (exit 75, retried next hour) and on a disk error or the 4 GB
# write cap (exit 74, writes warehouse.STOP). Any other failure also writes warehouse.STOP, so
# nothing repeats until a person looks. When the build completes it runs the inventory and
# writes warehouse.DONE, after which the cron line does nothing.
set -u
DEPLOY="$HOME/deploy/prediction-market-research"
export PMR_DATA_DIR="$HOME/data/prediction-market-research"  # as the paper supervisor
DATA="$PMR_DATA_DIR"
LOG="$DATA/logs/warehouse_steps.log"
STOP="$DATA/warehouse.STOP"
DONE="$DATA/warehouse.DONE"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export OMP_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8 MKL_NUM_THREADS=8 NUMEXPR_NUM_THREADS=8
export NUMBA_NUM_THREADS=8 POLARS_MAX_THREADS=8
export PMR_DUCKDB_MEMORY=8GB PMR_DUCKDB_TEMP_MAX=1GB
ts() { date -u +%FT%TZ; }
mkdir -p "$DATA/logs"
[ -e "$DONE" ] && exit 0
if [ -e "$STOP" ]; then
  echo "$(ts) skipped: $STOP exists ($(head -1 "$STOP"))" >> "$LOG"; exit 0
fi
avail=$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo)
if [ "$avail" -lt 45 ]; then
  echo "$(ts) skipped: MemAvailable ${avail} GB < 45 GB" >> "$LOG"; exit 0
fi
hb="$DATA/paper/heartbeat.json"
if [ -e "$hb" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$hb") ))
  if [ "$age" -gt 90 ]; then
    echo "$(ts) skipped: paper heartbeat ${age} s old" >> "$LOG"; exit 0
  fi
fi
cd "$DEPLOY" || exit 1
echo "$(ts) start (MemAvailable ${avail} GB, $(git log --oneline -1 | cut -c1-12))" >> "$LOG"
nice -n 10 ionice -c3 .venv/bin/pmr warehouse build --max-steps 1 --guard >> "$LOG" 2>&1
rc=$?
echo "$(ts) exit $rc" >> "$LOG"
case $rc in
  0)
    if tail -5 "$LOG" | grep -q "pmr warehouse build: complete"; then
      nice -n 10 ionice -c3 .venv/bin/pmr inventory >> "$LOG" 2>&1
      echo "$(ts) inventory exit $?" >> "$LOG"
      date -u +%FT%TZ > "$DONE"
    fi
    ;;
  75) ;;
  *) [ -e "$STOP" ] || echo "exit $rc at $(ts); see $LOG" > "$STOP" ;;
esac
