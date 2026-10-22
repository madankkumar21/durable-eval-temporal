"""Read-only checks for the scenario script: time to results, the continue-as-new chain, markers."""

from __future__ import annotations

import argparse
import asyncio
import time

from engines.temporal_app import connect
from engines.temporal_app.workflows import EvalWorkflow


async def ready(wid: str, timeout: float) -> None:
    """Seconds from now until the run has results and waits for approval."""
    client = await connect()
    handle = client.get_workflow_handle_for(EvalWorkflow.run, wid)
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if (await handle.query(EvalWorkflow.status))["waiting_for_approval"]:
                print(f"{time.time() - t0:.1f}")
                return
        except Exception:
            pass
        await asyncio.sleep(0.2)
    print("timeout")


async def chain(wid: str) -> None:
    """Every run under one workflow id (continue-as-new makes a chain), oldest first."""
    client = await connect()
    runs = [r async for r in client.list_workflows(f"WorkflowId='{wid}'")]
    runs.sort(key=lambda r: r.start_time)
    for r in runs:
        hist = await client.get_workflow_handle(wid, run_id=r.run_id).fetch_history()
        started = hist.events[0].workflow_execution_started_event_attributes
        carried = sum(len(p.data) for p in started.input.payloads)
        print(f"run {r.run_id[:8]}  status {r.status.name:<16} events {r.history_length:>4}  input {carried / 1024:.1f} KB")


async def markers(wid: str) -> None:
    """How many patch markers the run recorded (scenario 4)."""
    client = await connect()
    hist = await client.get_workflow_handle(wid).fetch_history()
    print(sum(1 for e in hist.events if e.HasField("marker_recorded_event_attributes")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ready", "chain", "markers"])
    ap.add_argument("wid")
    ap.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()
    if a.cmd == "ready":
        asyncio.run(ready(a.wid, a.timeout))
    elif a.cmd == "chain":
        asyncio.run(chain(a.wid))
    else:
        asyncio.run(markers(a.wid))


if __name__ == "__main__":
    main()
