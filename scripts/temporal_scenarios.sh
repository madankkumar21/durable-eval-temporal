#!/usr/bin/env bash
# Replays the Temporal scenarios with the stub model (no key, no cost) and the core cache off
# (except s2c, which turns it on), and prints the numbers behind COMPARISON.md.
#   scripts/temporal_scenarios.sh            # all scenarios
#   scripts/temporal_scenarios.sh s4 s6      # some of them
# Needs: temporal server start-dev --db-filename results/temporal.db  (already running), and `uv sync`.
# Background processes start from .venv/bin/python, not `uv run`: uv spawns python as a child, so
# kill -9 on the uv PID leaves python running and the "crash" never happens.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p results
STAMP=$(date +%H%M%S)
PY=.venv/bin/python
export VEU_CACHE=off
HANG_ITEM=action_count:video_3228   # item 3

calls_for_run() { sqlite3 results/cache.sqlite "SELECT COUNT(*) FROM http_calls WHERE run_id='$1'"; }
pids_for_run() { sqlite3 results/cache.sqlite "SELECT COUNT(DISTINCT pid) FROM http_calls WHERE run_id='$1'"; }
# The cache is written even when VEU_CACHE=off (only reads are switched off), so earlier scenarios
# fill it. Clear the stub's rows before any cache-on run, or old results fake the resume.
clear_stub_cache() { sqlite3 results/cache.sqlite "DELETE FROM calls WHERE model_id='stub'"; }
kill_and_check() {
  kill -9 "$1" 2>/dev/null || true; sleep 1
  # A dead child can linger as a zombie until reaped, and kill -0 still succeeds on it; check the state instead.
  state=$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ' || true)   # ps exits 1 once the PID is gone; don't let set -e abort here
  case "$state" in ""|Z*) ;; *) echo "FATAL: PID $1 survived kill -9 (state $state)" >&2; exit 1;; esac
  echo "killed PID $1 (confirmed gone)"
}
# start_worker [VAR=value ...]: extra env for this worker only (stub delay, faults, workflow version).
start_worker() {
  env "$@" nohup "$PY" -m engines.temporal_app.worker >> results/worker.log 2>&1 &
  echo $! > results/worker.pid; sleep 5
}
stop_worker() { kill_and_check "$(cat results/worker.pid)"; }
run() { "$PY" -m engines.temporal_app.run "$@"; }
inspect() { "$PY" -m scripts.inspect_run "$@"; }
attempts_for_run() { grep -c "^\[$1\] item" results/worker.log || true; }

s1() {
  echo "== S1: item 2 raises a simulated 429 once; Temporal retries =="
  rm -f results/.fault-* 2>/dev/null || true
  start_worker VEU_STUB_DELAY_S=1 VEU_FAIL_ITEM=action_count:video_7958
  run start --run-id "s1-$STAMP" --n 10 >/dev/null
  inspect ready "s1-$STAMP" >/dev/null
  run approve --run-id "s1-$STAMP" --decision yes --wait | grep -E '"published"|"correct"'
  grep "^\[s1-$STAMP\]" results/worker.log | grep "attempt" | head -3
  echo "calls: $(calls_for_run "s1-$STAMP") for 10 items"
  stop_worker
}

s2() {
  echo; echo "== S2: kill -9 the worker mid-run, restart, resume (S3: the run then waits for approval) =="
  start_worker VEU_STUB_DELAY_S=1
  run start --run-id "s2-$STAMP" --n 30 --heartbeat-s 5 >/dev/null
  sleep 8
  echo "calls before kill: $(calls_for_run "s2-$STAMP")"
  stop_worker; sleep 2
  run status --run-id "s2-$STAMP" | head -1
  start_worker VEU_STUB_DELAY_S=1; echo "worker restarted"
  inspect ready "s2-$STAMP" >/dev/null
  run status --run-id "s2-$STAMP" | grep -E '"waiting_for_approval"'
  run approve --run-id "s2-$STAMP" --decision yes --wait | grep -E '"published"'
  echo "calls after resume: $(calls_for_run "s2-$STAMP") for 30 items; distinct worker PIDs: $(pids_for_run "s2-$STAMP")"
  stop_worker

  echo; echo "== S2 baseline: plain loop, killed at the same point =="
  ( VEU_STUB_DELAY_S=1 "$PY" -m engines.plain --model stub --n 30 --run-id "plain-$STAMP" --yes >/dev/null 2>&1 & echo $! > results/plain.pid )
  sleep 7; kill_and_check "$(cat results/plain.pid)"
  echo "calls before kill: $(calls_for_run "plain-$STAMP")"
  VEU_STUB_DELAY_S=1 "$PY" -m engines.plain --model stub --n 30 --run-id "plain-$STAMP" --yes >/dev/null 2>&1
  echo "calls after restart: $(calls_for_run "plain-$STAMP") for 30 items; distinct PIDs: $(pids_for_run "plain-$STAMP")"
}

s2c() {
  echo; echo "== S2c: the S2 kill again, with the result cache on (the hand-built alternative) =="
  clear_stub_cache
  ( VEU_CACHE=on VEU_STUB_DELAY_S=1 "$PY" -m engines.plain --model stub --n 30 --run-id "plainc-$STAMP" --yes >/dev/null 2>&1 & echo $! > results/plain.pid )
  sleep 7; kill_and_check "$(cat results/plain.pid)"
  echo "plain + cache: calls before kill: $(calls_for_run "plainc-$STAMP")"
  VEU_CACHE=on VEU_STUB_DELAY_S=1 "$PY" -m engines.plain --model stub --n 30 --run-id "plainc-$STAMP" --yes >/dev/null 2>&1
  echo "plain + cache: calls after restart: $(calls_for_run "plainc-$STAMP") for 30 items; distinct PIDs: $(pids_for_run "plainc-$STAMP")"

  echo "-- Temporal, same kill, cache on --"
  clear_stub_cache
  start_worker VEU_CACHE=on VEU_STUB_DELAY_S=1
  run start --run-id "s2c-$STAMP" --n 30 --heartbeat-s 5 >/dev/null
  sleep 8
  echo "temporal + cache: calls before kill: $(calls_for_run "s2c-$STAMP")"
  stop_worker; sleep 2
  start_worker VEU_CACHE=on VEU_STUB_DELAY_S=1; echo "worker restarted"
  inspect ready "s2c-$STAMP" >/dev/null
  run approve --run-id "s2c-$STAMP" --decision yes --wait | grep -E '"published"'
  echo "temporal + cache: calls after resume: $(calls_for_run "s2c-$STAMP") for 30 items; distinct worker PIDs: $(pids_for_run "s2c-$STAMP")"
  stop_worker

  echo "-- plain loop + cache, item 3 hangs (as in S7) --"
  clear_stub_cache; rm -f results/.hang-* 2>/dev/null || true
  ( VEU_CACHE=on VEU_HANG_ITEM=$HANG_ITEM "$PY" -m engines.plain --model stub --n 10 --run-id "plainc7-$STAMP" --yes >/dev/null 2>&1 & echo $! > results/plain.pid )
  sleep 20
  echo "plain + cache after 20 s: $(calls_for_run "plainc7-$STAMP") calls, still running: $(ps -p "$(cat results/plain.pid)" >/dev/null && echo yes || echo no)"
  kill_and_check "$(cat results/plain.pid)"
}

s3() {
  echo; echo "== S3: approve too early (refused), kill -9 the worker while the run waits, restart, approve =="
  start_worker VEU_STUB_DELAY_S=1
  run start --run-id "s3-$STAMP" --n 10 >/dev/null
  echo "-- approve while the run is still scoring:"
  # The update's validator must refuse (exit 1); an accepted early approval is a failure.
  if run approve --run-id "s3-$STAMP" --decision yes; then echo "FATAL: early approval was accepted" >&2; exit 1; fi
  inspect ready "s3-$STAMP" >/dev/null
  echo "calls before kill: $(calls_for_run "s3-$STAMP") for 10 items"
  stop_worker; sleep 10
  echo "-- no worker attached:"
  run status --run-id "s3-$STAMP" || true   # the run stays RUNNING; the query needs a worker, so it fails
  start_worker; echo "worker restarted"
  run status --run-id "s3-$STAMP" | grep -E '"waiting_for_approval"'
  run approve --run-id "s3-$STAMP" --decision yes --wait | grep -E '^accepted|"published"'
  echo "-- approve again after publishing:"
  run approve --run-id "s3-$STAMP" --decision yes || true
  echo "calls after approve: $(calls_for_run "s3-$STAMP") for 10 items; distinct worker PIDs: $(pids_for_run "s3-$STAMP")"
  stop_worker
}

s4() {
  echo; echo "== S4: deploy new workflow code while runs are in flight =="
  start_worker VEU_STUB_DELAY_S=1 VEU_WORKFLOW_VERSION=v1
  run start --run-id "s4-a-$STAMP" --n 20 --heartbeat-s 3 >/dev/null
  run start --run-id "s4-b-$STAMP" --n 20 --heartbeat-s 3 >/dev/null
  sleep 6; stop_worker; echo "v1 worker stopped; two runs in flight"

  echo "-- deploy the change WITHOUT patched() --"
  before=$(grep -c 'Nondeterminism' results/worker.log || true)
  start_worker VEU_STUB_DELAY_S=1 VEU_WORKFLOW_VERSION=v2-unpatched
  # The killed calls time out (3 s heartbeat), the runs replay on the new code, and fail. Poll for
  # both: a fixed 10 s wait sometimes counted only the first.
  for _ in $(seq 1 30); do
    new=$(( $(grep -c 'Nondeterminism' results/worker.log || true) - before ))
    [ "$new" -ge 2 ] && break; sleep 1
  done
  echo "new nondeterminism errors in the worker log: $new"
  run status --run-id "s4-a-$STAMP" | head -1
  stop_worker

  echo "-- roll forward to the change WITH patched() --"
  start_worker VEU_STUB_DELAY_S=1 VEU_WORKFLOW_VERSION=v2
  run start --run-id "s4-new-$STAMP" --n 5 >/dev/null
  for w in a b new; do inspect ready "s4-$w-$STAMP" >/dev/null; done
  for w in a b new; do
    out=$(run approve --run-id "s4-$w-$STAMP" --decision yes --wait | grep -c '"published": true' || true)
    echo "s4-$w: published=$out  patch markers: $(inspect markers "s4-$w-$STAMP")  progress file: $(cat "results/s4-$w-$STAMP.progress.json" 2>/dev/null || echo none)"
  done
  stop_worker
}

s5() {
  echo; echo "== S5: 200 items, continue-as-new every 50 =="
  start_worker
  run start --run-id "s5-$STAMP" --n 200 --chunk 50 >/dev/null
  inspect ready "s5-$STAMP" --timeout 300 >/dev/null
  run approve --run-id "s5-$STAMP" --decision yes --wait | grep -E '"published"'
  inspect chain "s5-$STAMP"
  echo "calls: $(calls_for_run "s5-$STAMP") for 200 items"
  stop_worker
}

s6() {
  echo; echo "== S6: fan-out, 30 items at 1 s each =="
  start_worker VEU_STUB_DELAY_S=1
  for c in 1 8; do
    run start --run-id "s6-c$c-$STAMP" --n 30 --concurrency "$c" >/dev/null
    echo "concurrency $c: results in $(inspect ready "s6-c$c-$STAMP") s"
    run approve --run-id "s6-c$c-$STAMP" --decision no >/dev/null
  done
  echo "-- kill mid-batch at concurrency 8 --"
  run start --run-id "s6-kill-$STAMP" --n 30 --concurrency 8 --heartbeat-s 5 >/dev/null
  sleep 1.5; stop_worker
  echo "calls before kill: $(calls_for_run "s6-kill-$STAMP")"
  start_worker VEU_STUB_DELAY_S=1
  inspect ready "s6-kill-$STAMP" >/dev/null
  run approve --run-id "s6-kill-$STAMP" --decision yes --wait | grep -E '"published"'
  echo "calls after resume: $(calls_for_run "s6-kill-$STAMP") for 30 items"
  stop_worker
}

s7() {
  echo; echo "== S7: item 3's model call hangs once =="
  rm -f results/.hang-* 2>/dev/null || true
  start_worker VEU_HANG_ITEM=$HANG_ITEM
  run start --run-id "s7-$STAMP" --n 10 --heartbeat-s 5 >/dev/null
  echo "results in $(inspect ready "s7-$STAMP" --timeout 60) s"
  run approve --run-id "s7-$STAMP" --decision yes --wait | grep -E '"published"'
  grep "^\[s7-$STAMP\]" results/worker.log | grep "${HANG_ITEM#*:}"
  echo "calls: $(calls_for_run "s7-$STAMP") for 10 items"
  stop_worker

  echo "-- plain loop, same hang --"
  rm -f results/.hang-* 2>/dev/null || true
  ( VEU_HANG_ITEM=$HANG_ITEM "$PY" -m engines.plain --model stub --n 10 --run-id "plain7-$STAMP" --yes >/dev/null 2>&1 & echo $! > results/plain.pid )
  sleep 20
  echo "plain loop after 20 s: $(calls_for_run "plain7-$STAMP") calls, still running: $(ps -p "$(cat results/plain.pid)" >/dev/null && echo yes || echo no)"
  kill_and_check "$(cat results/plain.pid)"
}

# shellcheck disable=SC2068
for s in ${@:-s1 s2 s2c s3 s4 s5 s6 s7}; do $s; done
