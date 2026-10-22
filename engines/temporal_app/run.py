"""Client CLI: start a run, check status, approve or reject, wait for the result."""

from __future__ import annotations

import argparse
import asyncio
import json
from temporalio.client import Client

from . import TASK_QUEUE, connect
from .shared import EvalParams
from .workflows import EvalWorkflow


async def _client() -> Client:
    return await connect()


async def start(a: argparse.Namespace) -> None:
    client = await _client()
    params = EvalParams(run_id=a.run_id, model_id=a.model, task=a.task, n=a.n, n_frames=a.frames,
                        shuffle_seed=a.shuffle_seed, label_style=a.labels, chunk=a.chunk,
                        concurrency=a.concurrency, heartbeat_s=a.heartbeat_s)
    handle = await client.start_workflow(EvalWorkflow.run, params, id=a.run_id, task_queue=TASK_QUEUE)
    print(f"started workflow id={handle.id} run_id={handle.result_run_id}")
    if a.wait:
        print(json.dumps(await handle.result(), indent=2))


async def status(a: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle_for(EvalWorkflow.run, a.run_id)
    desc = await handle.describe()
    print(f"status: {desc.status.name}")
    if desc.status.name == "RUNNING":
        try:
            print(json.dumps(await handle.query(EvalWorkflow.status), indent=2))
        except Exception as e:  # a query needs a live worker; with none polling, the run is still RUNNING
            print(f"(query unavailable: {str(e).splitlines()[0]})")


async def approve(a: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle_for(EvalWorkflow.run, a.run_id)
    await handle.signal(EvalWorkflow.approve, a.decision)
    print(f"signalled approve={a.decision}")
    if a.wait:
        print(json.dumps(await handle.result(), indent=2))


async def result(a: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle_for(EvalWorkflow.run, a.run_id)
    print(json.dumps(await handle.result(), indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--run-id", required=True)
    s.add_argument("--model", default="stub")
    s.add_argument("--task", default="action_count")
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--frames", type=int, default=8)
    s.add_argument("--shuffle-seed", type=int, default=None)
    s.add_argument("--labels", default="letter", choices=["letter", "number"])
    s.add_argument("--chunk", type=int, default=200)
    s.add_argument("--concurrency", type=int, default=1, help="items in flight at once (scenario 6)")
    s.add_argument("--heartbeat-s", type=int, default=90, help="heartbeat timeout per model call (scenario 7)")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(fn=start)

    st = sub.add_parser("status")
    st.add_argument("--run-id", required=True)
    st.set_defaults(fn=status)

    ap_ = sub.add_parser("approve")
    ap_.add_argument("--run-id", required=True)
    ap_.add_argument("--decision", default="yes", choices=["yes", "no"])
    ap_.add_argument("--wait", action="store_true")
    ap_.set_defaults(fn=approve)

    r = sub.add_parser("result")
    r.add_argument("--run-id", required=True)
    r.set_defaults(fn=result)

    a = ap.parse_args()
    asyncio.run(a.fn(a))


if __name__ == "__main__":
    main()
