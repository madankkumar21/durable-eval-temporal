# The same run on a plain loop and on Temporal

Workload: MVBench `action_count`, stub model, core cache **off** (`VEU_CACHE=off`) in scenarios 1 to 7, so what you see is the engine, not my cache. Scenario 2c turns the cache on to test the hand-built alternative ([below](#the-hand-built-alternative-a-plain-loop-plus-a-result-cache)). "Calls" are rows in the `http_calls` table for that run id. A call is logged when the request goes out, as a real model bills it, so a call killed mid-flight still counts. Every number below comes from `scripts/temporal_scenarios.sh` (output at the end).

| # | Scenario | Plain loop | Temporal |
|---|---|---|---|
| 1 | A model call raises a rate-limit error | Hand-written `while True` with `sleep(2**attempt)` in `engines/plain.py` | `RetryPolicy(initial 2s, x2, max 60s, 8 attempts, non_retryable=["Fatal"])` on the activity. Item 2 failed on attempt 1 and completed on attempt 2; the workflow never saw the error. 10 calls for 10 items. |
| 2 | Worker killed with `kill -9` mid-run, then restarted (30 items, 1 s each) | Restarts from item 1. **36 calls for 30 items**: 6 before the kill (one of them in flight), then all 30 again. | Resumes from the next item by replaying history. **31 calls for 30 items**, 2 worker PIDs. The one extra is the call in flight at the kill: Temporal retries it after the heartbeat timeout. |
| 3 | A person must approve before results publish | `input()` blocks the process; kill it and the run is gone | `wait_condition` on a decision that arrives as an update, `decide`. Approved while the run was still scoring, it was refused, and the sender saw why (in this run, "still scoring: 1 of 10 items"). Worker killed with `kill -9` while the run waits: the run stays RUNNING with no worker attached, and the `status` query fails until a worker polls again. A new worker answers the query (still waiting for approval), and `approve yes` returns "accepted: yes" and publishes. A second approval after that is refused by the server: "workflow execution already completed". **10 calls for 10 items**: nothing re-paid. The old `approve` signal is kept behind `--signal`: the server stores a signal even with no worker running, while an update waits for a worker. |
| 4 | New workflow code deployed while runs are in flight | Nothing to break, and nothing to protect | **Without `workflow.patched()`:** both in-flight runs hit a nondeterminism error on replay (`answer_item_activity` in history, `progress_activity` in the new code). The runs are not lost: they stay RUNNING. **Rolled forward to the patched code:** both finish on the old path (0 patch markers, no progress step), and a new run takes the new path (1 marker, progress file written). `tests/test_replay.py` catches the unsafe version in pytest, with no server. |
| 5 | A long run: 200 items, continue-as-new every 50 | Memory and patience | A chain of 4 runs, 305 to 322 history events each (the hard limit is 51,200). 200 calls for 200 items. The rows carried to the next run grow by about 12.5 KB per 50 items: 37.7 KB at the last hop. At that rate Temporal's 2 MB payload limit arrives near 8,000 items, so a large batch should write rows to storage and carry a pointer. |
| 6 | Fan-out: 30 items at 1 s each | One at a time | `--concurrency 8` runs items in batches with `asyncio.gather`. **Results in 2.5 s, against 29.4 s** one at a time. Killed mid-batch: **38 calls for 30 items**. The 8 extra are the batch in flight at the kill. A result cache cannot save a call that never returned, so the batch in flight is paid twice either way; smaller batches cap it, or a provider-side idempotency key where the API offers one. |
| 7 | A model call hangs | **Hangs.** After 20 s: 3 calls, still running, until killed. | The activity misses its heartbeat (`--heartbeat-s 5`), Temporal times out the attempt and retries it. Item 3 completed on attempt 2; results in 5.6 s; 11 calls for 10 items. The hung thread stays blocked in the worker until the process exits, so the activity thread pool needs headroom. |

## The hand-built alternative: a plain loop plus a result cache

The obvious way to get crash recovery without a workflow engine is to cache each model result and restart the loop. Scenario 2c (`s2c`, 2026-10-29) tests exactly that: the S2 kill again, with the core cache on (`VEU_CACHE=on`) and the stub's cached rows cleared first, because earlier runs write the cache even when reads are off.

| What a run needs | Plain loop + result cache | What you would still build | Temporal |
|---|---|---|---|
| No re-pay after a crash | **Measured: 31 calls for 30 items.** 6 calls before `kill -9` (one in flight), then 25 on restart, so 5 items came from the cache (derived from the counts). | Nothing more, at concurrency 1 | 31 (S2), and 31 again with the cache on |
| Retry a rate limit | Hand-written backoff (S1). The wait lives in the process's memory and dies with it. | A delayed queue | `RetryPolicy` (S1) |
| Notice the crash and restart | Not measured: the script restarted it by hand | A supervisor to restart the process, and a record of which runs are in progress | The script restarted the worker by hand too: Temporal does not supervise workers. It keeps each run's state on the server, so any worker that polls picks the run up (S2) |
| A call that never returns | **Measured: still hangs.** 3 calls after 20 s, still running. A cache only stores a call that returned. | A watchdog timer on every call | Heartbeat timeout, then a retry: 5.6 s (S7) |
| Wait for a person | Not measured: `input()` in one process; a restart re-scores from the cache and asks again | A state table and an API for the decision | Waits in RUNNING through a `kill -9`; refuses an early approval (S3) |
| Change code mid-run | Not measured: a restart runs the new code from the top, served from the cache where the prompt is unchanged. A prompt change is a new cache key, so every item is paid again. | Nothing for the loop itself: there is no replay to break | Finished items stay in history and are not re-run, but a workflow code change must be patched or versioned, or in-flight runs fail replay (S4). This one is a cost of Temporal, not a gap in the loop. |
| See where a run is | Not measured: nothing records that a run is in progress | A status table | RUNNING, plus a status query |

The cache answers re-pay. The rest is a supervisor, timers, and state for runs and decisions. My read, not a measurement: each piece is easy, and keeping them correct together is the work Temporal removes.

Limits of this comparison:
- The cache matched Temporal at concurrency 1, with an immediate restart by hand. With fan-out, both re-pay the calls in flight at the kill (S6: 38 calls for 30 items).
- With the cache on, Temporal's count did not change (31): the only call it re-paid was the one in flight, which the cache never stored.
- Cost, computed: 36 against 31 calls is 5 extra calls, about $0.02 at Haiku 4.5 prices and $0.04 on Sonnet 5.5. The cache's larger saving is on re-runs after a scoring change, which no scenario here measures.
- Developer velocity is not measured. The table lists what you would build; it does not time building it. For scale only: `engines/plain.py` is 102 lines, and `engines/temporal_app` is 433 lines in six files, including the CLI and the scenario hooks. The supervisor, timer and state-table version was never built, so it cannot be compared.
- The cache is a local SQLite file. It survives a process dying, not the machine: the same limit as the dev server's history here.

## Runs behind the numbers (2026-10-22; scenario 3 on 2026-10-28 and 2026-10-29; scenarios 2c and 5 on 2026-10-29)

The first version of this table (2026-10-03) killed the `uv run` wrapper, not python. uv spawns python as a child, so both the Temporal worker and the plain loop survived as orphans and the "crash" never happened. Those numbers ("30 vs 60") are withdrawn. The script launches `.venv/bin/python` directly, checks the process state after the kill, and reports distinct PIDs from the `http_calls` table. On 2026-10-22 the stub started logging a call when it is sent, not when it returns; that moved scenario 2 from 30 to 31 calls. On 2026-10-28 scenario 3 got its own function, `s3`. Before that, the run reached approval only after the scenario-2 restart, so no worker was ever killed during the wait. On 2026-10-29 scenario 2c was added, and the approval became an update. Scenario 3 now runs 10 items at 1 s each, so the early approval arrives while the run is scoring; the validator's "already decided" branch is covered by `tests/test_approval.py`, not by a live run. A full re-run that day reproduced S1, S2, S4, S6 and S7 within timing noise (S6 at concurrency 8: 2.7 s). S4 first counted 1 nondeterminism error: the worker log showed both runs failing, but the second landed after the fixed 10 s counting window, so the script now polls the log for up to 30 s, and the re-run counts 2. S5's last run has 322 events, one more than before: the update records accepted and completed events where the signal recorded one.

```
S1  attempt 1 failed (simulated 429), attempt 2 ok, published, 10 calls for 10 items
S2  8 calls before kill -9 (confirmed gone), RUNNING with no worker, 31 calls after resume, 2 worker PIDs
S2  plain: 6 calls before kill -9 (confirmed gone), 36 calls after restart, 2 PIDs
S2c plain + cache: 6 calls before kill -9 (confirmed gone), 31 calls after restart, 2 PIDs   (2026-10-29)
    temporal + cache: 8 calls before kill -9, 31 calls after resume, 2 worker PIDs
    plain + cache, item 3 hangs: 3 calls after 20 s, still running
S3  approve while scoring -> "rejected: still scoring: 1 of 10 items"; waiting_for_approval: true, kill -9
    (confirmed gone), RUNNING with no worker (query unavailable), worker restarted, approve -> "accepted: yes",
    published; approve again -> "rejected: workflow execution already completed"; 10 calls for 10 items,
    1 PID made calls   (2026-10-29)
S4  unpatched deploy: 2 nondeterminism errors ("Activity type of scheduled event 'answer_item_activity'
    does not match activity type of activity command 'progress_activity'"), runs RUNNING
    patched deploy: s4-a published, 0 markers, no progress file; s4-b same; s4-new published, 1 marker,
    progress {"done": 5, "n": 5}
S5  4 runs: CONTINUED_AS_NEW 305 events (input 0.2 KB), 305 (12.7 KB), 305 (25.2 KB), COMPLETED 322 (37.7 KB);
    200 calls for 200 items   (2026-10-29)
S6  concurrency 1: 29.4 s; concurrency 8: 2.5 s; killed mid-batch: 16 calls before, 38 after resume
S7  item 3 attempt 1 hung, attempt 2 ok, 5.6 s, 11 calls for 10 items; plain loop: 3 calls after 20 s, still running
```

## What the comparison is and isn't

- Both engines run the same core code on the same items. Only the adapter changes.
- With the core cache on, the plain loop resumes after a crash as cheaply as Temporal: 31 calls for 30 items each (scenario 2c). The cache-off runs show what the engine itself buys you, and the 2c table shows what the cache does not. A fair claim is "the engine journals the result; the cache is belt and braces."
- Stub calls cost nothing. The call counts are what matters; with a real model each count costs about $0.004 on Haiku 4.5 and $0.008 on Sonnet 5.5.
- All runs are on the local dev server (`temporal server start-dev`), one process on SQLite. The README says what that does not show.
