# durable-eval-temporal

A model-evaluation harness on [Temporal](https://temporal.io), measured against a plain Python loop on the same code. Each model call is one Temporal activity. I wrote it to see what durable execution buys for long, expensive, failure-prone AI runs, and to test it the way a customer would before adopting it.

**Why this exists.** Building contextual ad packages on TV content at an AI company, I kept finding results that looked right and weren't. When a prospect asked how I knew the tags were right, I had a process to show and no number. So I built a benchmark harness on public data (MVBench) and public models. Each run makes hundreds of paid model calls. They hit rate limits, they hang, workers die mid-run, and a person should approve results before they publish. That is the shape of work Temporal is built for, so I ran the same harness on a plain loop and on Temporal and measured the difference. (The benchmark findings themselves are a separate write-up, available on request.)

## What I tested

Every scenario runs with a free stub model, so anyone can reproduce it. Full table, with the raw output: [COMPARISON.md](COMPARISON.md).

| # | What a customer needs | What happened on Temporal | Plain loop |
|---|---|---|---|
| 1 | Survive rate limits | A simulated 429 retried under a `RetryPolicy`; the workflow never saw it | Hand-written backoff |
| 2 | Survive a crash without re-paying | `kill -9` mid-run: **31 calls for 30 items** (one was in flight) | **36 calls** |
| 3 | Wait for a person | Waits in RUNNING with no worker attached, including through a `kill -9` of the worker during the wait. The approval is an update: sent while the run was still scoring, it was refused with "still scoring: 1 of 10 items" (10 calls for 10 items) | `input()`; lost on a crash |
| 4 | Deploy new code while runs are in flight | Unpatched change: nondeterminism error, runs held, not lost. `workflow.patched()`: old runs finish on the old path, new runs take the new one. Caught in pytest by a replay test | n/a |
| 5 | Run long | 200 items as 4 runs via continue-as-new, about 305 events each; carried state 37.7 KB at the last hop | n/a |
| 6 | Throughput | 8 calls in flight: **2.5 s against 29.4 s**; a crash mid-batch re-pays the batch in flight (38 calls for 30) | n/a |
| 7 | A call that never returns | Heartbeat timeout fires, Temporal retries, run completes in 5.6 s | **Hangs** |
| 7b | A late result must not count | A call hung 12 s and then returned, after its retry had finished. The worker cancelled the old attempt as it came back, and history kept only the retry's result. Both calls were paid: **11 calls for 10 items** | Waited out the 12 s: 10 calls, done in 14 s |

**What about a result cache instead of an engine?** Scenario 2c reruns the crash with the plain loop's result cache on. It matched Temporal on crash cost (31 calls for 30 items each), but the hung call still hung. By design, nothing in it records which runs are in progress, and its approval lives in one process. [COMPARISON.md](COMPARISON.md#the-hand-built-alternative-a-plain-loop-plus-a-result-cache) lists what you would still build.

What I took from it, as advice I would give a customer:
- Durable execution removes the re-pay on a crash, but not the call in flight. With fan-out, that is a whole batch, and a result cache cannot save a call that never returned. Keep batches small, or use a provider-side idempotency key where the API offers one.
- Treat workflow code changes as a deploy concern from day one. Prefer Worker Versioning where your deploys allow it; otherwise `workflow.patched()` plus a replay test in CI against saved histories, as here.
- Do not carry growing results through continue-as-new. Store them and carry a pointer, well before the 2 MB payload limit.
- Make approvals answer back. An update with a validator refuses an approval of results that do not exist yet, and the sender sees why. Keep a signal for approvals while no worker runs: an update waits for a worker.
- Set heartbeat timeouts on model calls. Without one, a hung call blocks until the start-to-close timeout. This harness heartbeats once, at the start, so the timeout bounds a silent attempt; for calls longer than the timeout, heartbeat from a background thread.

## How it fits together

![Architecture: CLI, Temporal dev server, worker, plain-Python core, model, results](docs/architecture.png)

The numbered walkthrough, the failure table, the design decisions and the production path are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Layout

```
core/        plain Python, never imports an engine; every function is idempotent
  tasks.py   load MVBench items; fetch only the needed videos from Hugging Face by range request
  model.py   one model call; SQLite result cache and call log; price table; retryable vs fatal errors; stub model
  score.py   letter-by-position grading
  stats.py   Wilson interval, McNemar, paired bootstrap
  publish.py results/<run_id>.jsonl + summary
engines/
  plain.py       the baseline loop
  temporal_app/  workflow, activities, worker, client CLI
scripts/
  temporal_scenarios.sh  runs scenarios 1 to 7, 2c and 7b, and prints the numbers
  inspect_run.py         read-only checks: time to results, the continue-as-new chain, patch markers, retried attempts
docs/
  ARCHITECTURE.md        the diagram, a step-by-step walkthrough, design decisions, production path
  architecture.svg/.png  the diagram
tests/
  test_replay.py         scenario 4: replays a recorded v1 history against v1, v2 and unpatched v2 code
  histories/v1_run.json  that history
```

## Run it

```bash
uv sync --extra temporal --group dev
uv run pytest                                   # includes the replay test; no server needed

brew install temporal
temporal server start-dev --db-filename results/temporal.db   # keep the db file or history dies with the server
scripts/temporal_scenarios.sh                   # all scenarios, stub model, no key, no cost
scripts/temporal_scenarios.sh s4 s6             # or a few
```

By hand:

```bash
uv run python -m engines.temporal_app.worker
uv run python -m engines.temporal_app.run start   --run-id demo --model stub --n 10 --concurrency 4
uv run python -m engines.temporal_app.run status  --run-id demo
uv run python -m engines.temporal_app.run approve --run-id demo --decision yes --wait
```

Real models: copy `.env.example` to `.env` and add an Anthropic key. A 100-question run on Haiku 4.5 costs about 40 cents; 10 items cost $0.04 on 2026-10-29. No request sends `temperature`. API version `2026-10-21`, which the `anthropic` 2.x SDK sends, rejects it with a 400, so Haiku 4.5 samples at its default.

### Knobs

| Setting | Effect |
|---|---|
| `--concurrency N` | model calls in flight at once (scenario 6) |
| `--heartbeat-s S` | a call silent this long is timed out and retried (scenario 7) |
| `--chunk N` | continue-as-new after N items (scenario 5) |
| `VEU_WORKFLOW_VERSION` | `v1`, `v2` (patched) or `v2-unpatched`: the workflow code a worker runs (scenario 4) |
| `VEU_CACHE=off` | bypass the result cache, so the engine's own resume is what you measure |
| `VEU_STUB_DELAY_S`, `VEU_FAIL_ITEM`, `VEU_HANG_ITEM`, `VEU_HANG_S` | slow the stub, make one item fail once, make one item hang once, or end that hang after S seconds (scenario 7b) |
| `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY` | point the worker and client at Temporal Cloud; TLS turns on when a key is set |

## What the local dev server does not show

All of the above ran on `temporal server start-dev`: one process on SQLite. None of these is a feature that failed. They are the parts of production that a customer either runs or buys.
- **Persistence.** Without `--db-filename`, the history died with the server. Self-hosted production means four services (frontend, history, matching, worker) on a database, with a shard count fixed when the cluster is created.
- **Availability and region failover.** Temporal Cloud offers high availability and failover per namespace. Self-hosted, that is multi-cluster replication you set up and run yourself.
- **Security and compliance.** SAML SSO and audit logging come with Temporal Cloud plans. Self-hosted, you supply them.

The worker and client already read `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` and `TEMPORAL_API_KEY`, so the same scenarios can run against a Cloud namespace with no code change. I have not run them on Cloud yet.

Sources: Temporal's [self-hosted vs Cloud feature comparison](https://docs.temporal.io/evaluate/development-production-features/cloud-vs-self-hosted-features) and [pricing](https://temporal.io/pricing).
