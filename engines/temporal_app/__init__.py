"""Temporal adapter: one workflow per run, one activity per model call.

  temporal server start-dev --db-filename results/temporal.db
  uv run python -m engines.temporal_app.worker
  uv run python -m engines.temporal_app.run start --model stub --n 10 --run-id demo
  uv run python -m engines.temporal_app.run status --run-id demo
  uv run python -m engines.temporal_app.run approve --run-id demo --decision yes
"""

import os

TASK_QUEUE = "video-eval"


async def connect():
    """Local dev server by default. Set TEMPORAL_API_KEY (plus address and namespace) for Temporal Cloud."""
    from temporalio.client import Client

    key = os.environ.get("TEMPORAL_API_KEY") or None
    return await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        api_key=key,
        tls=bool(key),
    )
