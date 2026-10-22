"""Write a run's rows and summary. Reading them back is what the MCP tool does."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import RESULTS_DIR
from .score import Scored
from .stats import wilson


def run_summary(rows: list[Scored]) -> dict:
    n = len(rows)
    k = sum(r.correct for r in rows)
    acc = wilson(k, n)
    return {
        "model_id": rows[0].model_id if rows else None,
        "n": n,
        "correct": k,
        "accuracy": acc.acc,
        "wilson_lo": acc.lo,
        "wilson_hi": acc.hi,
        "parse_failures": sum(r.parse_failed for r in rows),
        "cached_items": sum(r.cached for r in rows),
        "input_tokens": sum(r.input_tokens for r in rows),
        "output_tokens": sum(r.output_tokens for r in rows),
        "cost_usd": round(sum(r.cost_usd for r in rows), 4),
        "latency_p50_s": round(sorted(r.latency_s for r in rows)[n // 2], 2) if n else None,
    }


def publish(run_id: str, rows: list[Scored], results_dir: Path = RESULTS_DIR, extra: dict | None = None) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    out = results_dir / f"{run_id}.jsonl"
    with open(out, "w") as f:
        for r in rows:
            f.write(json.dumps(asdict(r)) + "\n")
    s = run_summary(rows)
    s.update(extra or {})
    s["run_id"] = run_id
    s["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (results_dir / f"{run_id}.summary.json").write_text(json.dumps(s, indent=2))
    (results_dir / f"{run_id}.summary.md").write_text(
        f"# {run_id}\n\n"
        f"- model: `{s['model_id']}`\n"
        f"- accuracy: **{s['accuracy']:.1%}** (Wilson 95% [{s['wilson_lo']:.1%}, {s['wilson_hi']:.1%}], n={s['n']})\n"
        f"- parse failures: {s['parse_failures']}; cached items: {s['cached_items']}\n"
        f"- tokens: {s['input_tokens']} in / {s['output_tokens']} out; cost: ${s['cost_usd']:.4f}\n"
        f"- latency p50: {s['latency_p50_s']} s\n"
        f"- published: {s['published_at']}\n"
    )
    return out


def load_run(run_id_or_path: str | Path, results_dir: Path = RESULTS_DIR) -> list[dict]:
    p = Path(run_id_or_path)
    if not p.exists():
        p = results_dir / f"{run_id_or_path}.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def latest_run_for_model(model_id: str, results_dir: Path = RESULTS_DIR) -> Path | None:
    cands = []
    for p in results_dir.glob("*.summary.json"):
        s = json.loads(p.read_text())
        if s.get("model_id") == model_id:
            cands.append((s.get("published_at", ""), results_dir / f"{s['run_id']}.jsonl"))
    return max(cands)[1] if cands else None
