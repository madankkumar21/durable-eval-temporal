"""Start a worker. Kill it mid-run (scenario 2) and start it again; the workflow resumes."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.worker import Worker

from . import TASK_QUEUE, connect
from .activities import answer_item_activity, progress_activity, publish_activity, summarize_activity
from .shared import WORKFLOW_VERSION
from .workflows import EvalWorkflow


async def main() -> None:
    client = await connect()
    # Room for scenario 6 (8 calls in flight) plus a thread a hung call (scenario 7) never gives back.
    with ThreadPoolExecutor(max_workers=16) as executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[EvalWorkflow],
            activities=[answer_item_activity, summarize_activity, publish_activity, progress_activity],
            activity_executor=executor,
            max_concurrent_activities=12,
        )
        print(f"worker up on '{TASK_QUEUE}' (pid {os.getpid()}, workflow code {WORKFLOW_VERSION})", flush=True)
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
