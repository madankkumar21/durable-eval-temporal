"""Client CLI: start a run, check status, approve or reject, wait for the result."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from temporalio.client import Client, WorkflowUpdateFailedError, WorkflowUpdateRPCTimeoutOrCancelledError
from temporalio.service import RPCError, RPCStatusCode

from . import TASK_QUEUE, connect
from .shared import EvalParams
from .workflows import EvalWorkflow


async def _client() -> Client:
    return await connect()


async def start(a: argparse.Namespace) -> None:
    client = await _client()
    params = EvalParams(run_id=a.run_id, model_id=a.model, task=a.task, n=a.n, n_frames=a.frames,
                        shuffle_seed=a.shuffle_seed, label_style=a.labels, chunk=a.chunk,
                        concurrency=a.concurrency, heartbeat_s=a.heartbeat_s,
                        approval_timeout_s=a.approval_timeout_s)
    handle = await client.start_workflow(EvalWorkflow.run, params, id=a.run_id, task_queue=TASK_QUEUE)
    print(f"started workflow id={handle.id} run_id={handle.result_run_id}")
    if a.wait:
        print(json.dumps(await handle.result(), indent=2))


async def status(a: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle_for(EvalWorkflow.run, a.run_id)
    desc = await handle.describe()
    print(f"status: {desc.status.name}")
    if desc.status.name == "RUNNING" and not a.no_query:
        try:
            print(json.dumps(await handle.query(EvalWorkflow.status), indent=2))
        except Exception as e:  # a query needs a live worker; with none polling, the run is still RUNNING
            print(f"(query unavailable: {str(e).splitlines()[0]})")


async def approve(a: argparse.Namespace) -> int:
    client = await _client()
    handle = client.get_workflow_handle_for(EvalWorkflow.run, a.run_id)
    if a.signal:
        # Stored by the server even with no worker running, but never checked or answered.
        try:
            await handle.signal(EvalWorkflow.approve, a.decision)
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                raise
            print(f"rejected: {e.message}")
            return 1
        print(f"signalled approve={a.decision}")
    else:
        # An update waits for a worker to run the validator, then returns its answer. With no
        # worker polling, the client would retry forever, so the wait is capped.
        try:
            async with asyncio.timeout(a.timeout):
                print(await handle.execute_update(EvalWorkflow.decide, a.decision))
        except (TimeoutError, WorkflowUpdateRPCTimeoutOrCancelledError):
            print(f"no worker answered within {a.timeout:g} s; start a worker, or use --signal")
            return 1
        except WorkflowUpdateFailedError as e:
            print(f"rejected: {getattr(e.cause, 'message', e.cause)}")
            return 1
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                raise
            print(f"rejected: {e.message}")
            return 1
    if a.wait:
        print(json.dumps(await handle.result(), indent=2))
    return 0


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
    s.add_argument("--approval-timeout-s", type=int, default=None,
                   help="the approval wait expires after this many seconds; default: wait forever (scenario 3b)")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(fn=start)

    st = sub.add_parser("status")
    st.add_argument("--run-id", required=True)
    st.add_argument("--no-query", action="store_true",
                    help="print the server's status only; with no worker polling, the query can block")
    st.set_defaults(fn=status)

    ap_ = sub.add_parser("approve")
    ap_.add_argument("--run-id", required=True)
    ap_.add_argument("--decision", default="yes", choices=["yes", "no"])
    ap_.add_argument("--signal", action="store_true",
                     help="send the unchecked signal instead of the update (works with no worker running)")
    ap_.add_argument("--timeout", type=float, default=30, help="seconds to wait for a worker to answer the update")
    ap_.add_argument("--wait", action="store_true")
    ap_.set_defaults(fn=approve)

    r = sub.add_parser("result")
    r.add_argument("--run-id", required=True)
    r.set_defaults(fn=result)

    a = ap.parse_args()
    sys.exit(asyncio.run(a.fn(a)) or 0)


if __name__ == "__main__":
    main()
