# The same run on a plain loop and on Temporal

Workload: MVBench `action_count`, stub model, core cache **off** (`VEU_CACHE=off`), so what you see is the engine, not my cache. "Calls" are rows in the `http_calls` table for that run id. A call is logged when the request goes out, as a real model bills it, so a call killed mid-flight still counts. Every number below comes from `scripts/temporal_scenarios.sh` (output at the end).

| # | Scenario | Plain loop | Temporal |
|---|---|---|---|
| 1 | A model call raises a rate-limit error | Hand-written `while True` with `sleep(2**attempt)` in `engines/plain.py` | `RetryPolicy(initial 2s, x2, max 60s, 8 attempts, non_retryable=["Fatal"])` on the activity. Item 2 failed on attempt 1 and completed on attempt 2; the workflow never saw the error. 10 calls for 10 items. |
| 2 | Worker killed with `kill -9` mid-run, then restarted (30 items, 1 s each) | Restarts from item 1. **36 calls for 30 items**: 6 before the kill (one of them in flight), then all 30 again. | Resumes from the next item by replaying history. **31 calls for 30 items**, 2 worker PIDs. The one extra is the call in flight at the kill: Temporal retries it after the heartbeat timeout. |
| 3 | A person must approve before results publish | `input()` blocks the process; kill it and the run is gone | `wait_condition` on an `approve` signal. Worker killed with `kill -9` while the run waits: the run stays RUNNING with no worker attached, and the `status` query fails until a worker polls again. A new worker answers the query (still waiting for approval), and `approve yes` publishes. **3 calls for 3 items**: nothing re-paid. |
| 4 | New workflow code deployed while runs are in flight | Nothing to break, and nothing to protect | **Without `workflow.patched()`:** both in-flight runs hit a nondeterminism error on replay (`answer_item_activity` in history, `progress_activity` in the new code). The runs are not lost: they stay RUNNING. **Rolled forward to the patched code:** both finish on the old path (0 patch markers, no progress step), and a new run takes the new path (1 marker, progress file written). `tests/test_replay.py` catches the unsafe version in pytest, with no server. |
| 5 | A long run: 200 items, continue-as-new every 50 | Memory and patience | A chain of 4 runs, 305 to 321 history events each (the hard limit is 51,200). 200 calls for 200 items. The rows carried to the next run grow by about 12.5 KB per 50 items: 37.7 KB at the last hop. At that rate Temporal's 2 MB payload limit arrives near 8,000 items, so a large batch should write rows to storage and carry a pointer. |
| 6 | Fan-out: 30 items at 1 s each | One at a time | `--concurrency 8` runs items in batches with `asyncio.gather`. **Results in 2.5 s, against 29.4 s** one at a time. Killed mid-batch: **38 calls for 30 items**. The 8 extra are the batch in flight at the kill, so a parallel run needs idempotency keys or a result cache on the model call. |
| 7 | A model call hangs | **Hangs.** After 20 s: 3 calls, still running, until killed. | The activity misses its heartbeat (`--heartbeat-s 5`), Temporal times out the attempt and retries it. Item 3 completed on attempt 2; results in 5.6 s; 11 calls for 10 items. The hung thread stays blocked in the worker until the process exits, so the activity thread pool needs headroom. |

## Runs behind the numbers (2026-10-22; scenario 3 on 2026-10-28)

The first version of this table (2026-10-03) killed the `uv run` wrapper, not python. uv spawns python as a child, so both the Temporal worker and the plain loop survived as orphans and the "crash" never happened. Those numbers ("30 vs 60") are withdrawn. The script launches `.venv/bin/python` directly, checks the process state after the kill, and reports distinct PIDs from the `http_calls` table. On 2026-10-22 the stub started logging a call when it is sent, not when it returns; that moved scenario 2 from 30 to 31 calls. On 2026-10-28 scenario 3 got its own function, `s3`. Before that, the run reached approval only after the scenario-2 restart, so no worker was ever killed during the wait.

```
S1  attempt 1 failed (simulated 429), attempt 2 ok, published, 10 calls for 10 items
S2  8 calls before kill -9 (confirmed gone), RUNNING with no worker, 31 calls after resume, 2 worker PIDs
S2  plain: 6 calls before kill -9 (confirmed gone), 36 calls after restart, 2 PIDs
S3  waiting_for_approval: true, kill -9 (confirmed gone), RUNNING with no worker (query unavailable),
    worker restarted, approve yes -> published, 3 calls for 3 items, 1 PID made calls   (2026-10-28)
S4  unpatched deploy: 2 nondeterminism errors ("Activity type of scheduled event 'answer_item_activity'
    does not match activity type of activity command 'progress_activity'"), runs RUNNING
    patched deploy: s4-a published, 0 markers, no progress file; s4-b same; s4-new published, 1 marker,
    progress {"done": 5, "n": 5}
S5  4 runs: CONTINUED_AS_NEW 305 events (input 0.2 KB), 305 (12.7 KB), 305 (25.2 KB), COMPLETED 321 (37.7 KB);
    200 calls for 200 items
S6  concurrency 1: 29.4 s; concurrency 8: 2.5 s; killed mid-batch: 16 calls before, 38 after resume
S7  item 3 attempt 1 hung, attempt 2 ok, 5.6 s, 11 calls for 10 items; plain loop: 3 calls after 20 s, still running
```

## What the comparison is and isn't

- Both engines run the same core code on the same items. Only the adapter changes.
- With the core cache on, every engine resumes without re-paying, including the plain loop. The cache-off runs show what the engine itself buys you. A fair claim is "the engine journals the result; the cache is belt and braces."
- Stub calls cost nothing. The call counts are what matters; with a real model each count costs about $0.004 on Haiku 4.5 and $0.008 on Sonnet 5.5.
- All runs are on the local dev server (`temporal server start-dev`), one process on SQLite. The README says what that does not show.
