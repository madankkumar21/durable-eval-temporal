"""One model call per item, idempotent.

- Results are cached in results/cache.sqlite keyed by (model_id, item_id, prompt_hash).
  A hit returns cached=True and cost_usd=0. Set VEU_CACHE=off to bypass the cache
  and measure an engine's native resume instead.
- Every real HTTP call is logged in the http_calls table, so "API calls after
  resume" can be counted across process restarts.
- Errors are normalised to RetryableModelError / FatalModelError so engines can
  classify without importing the Anthropic SDK.
- model_id "stub" and "stub-b" answer deterministically without a key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import REPO_ROOT, RESULTS_DIR
from .frames import FrameCfg, sample_frames
from .tasks import Item, ensure_videos, labels_for, video_path

load_dotenv(REPO_ROOT / ".env")

# USD per million tokens (input, output). Pin dated ids in results.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "stub": (0.0, 0.0),
    "stub-b": (0.0, 0.0),
}

# API version 2026-10-21, which the anthropic 2.x SDK sends, rejects temperature, top_p and top_k on
# every model, so no request sends them. Haiku 4.5 rows cached before that were sampled at temperature 0;
# its new rows get their own cache key, so those old rows are not reused.
WAS_TEMPERATURE_0 = {"claude-haiku-4-5-20251001", "claude-haiku-4-5"}

SYSTEM_PROMPT = (
    "You answer multiple-choice questions about a short video from frames sampled "
    "uniformly in time. Pick the single best option. Do not explain."
)
# Thinking counts against max_tokens. Opus 5.5 cannot turn thinking off, so it needs room to think before answering.
MAX_TOKENS: dict[str, int] = {"claude-opus-5-5": 4096}
DEFAULT_MAX_TOKENS = 64

PROMPT_VERSION = "v2-structured"  # bump when the prompt or output format changes; part of the cache key

LETTER_RE = re.compile(r"\b([A-Z])\b")


class RetryableModelError(Exception):
    """Rate limit, timeout, connection, 5xx. Engines should retry with backoff."""


class FatalModelError(Exception):
    """Bad request, auth, permission. Engines must not retry."""


@dataclass(frozen=True)
class ModelResult:
    item_id: str
    model_id: str
    prompt_hash: str
    pred_letter: str | None
    raw_text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    cached: bool
    run_id: str


# ---------------------------------------------------------------- cache

def _db(results_dir: Path = RESULTS_DIR) -> sqlite3.Connection:
    results_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(results_dir / "cache.sqlite", timeout=30)
    con.execute(
        """CREATE TABLE IF NOT EXISTS calls (
            model_id TEXT, item_id TEXT, prompt_hash TEXT,
            pred_letter TEXT, raw_text TEXT, input_tokens INT, output_tokens INT,
            cost_usd REAL, latency_s REAL, ts REAL, run_id TEXT,
            PRIMARY KEY (model_id, item_id, prompt_hash))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS http_calls (
            ts REAL, model_id TEXT, item_id TEXT, run_id TEXT, pid INT)"""
    )
    return con


def cache_enabled() -> bool:
    return os.environ.get("VEU_CACHE", "on").lower() != "off"


def cache_get(model_id: str, item_id: str, prompt_hash: str, run_id: str) -> ModelResult | None:
    if not cache_enabled():
        return None
    with _db() as con:
        row = con.execute(
            "SELECT pred_letter, raw_text, input_tokens, output_tokens, latency_s FROM calls "
            "WHERE model_id=? AND item_id=? AND prompt_hash=?",
            (model_id, item_id, prompt_hash),
        ).fetchone()
    if row is None:
        return None
    return ModelResult(item_id, model_id, prompt_hash, row[0], row[1], row[2], row[3], 0.0, row[4], True, run_id)


def cache_put(r: ModelResult) -> None:
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r.model_id, r.item_id, r.prompt_hash, r.pred_letter, r.raw_text, r.input_tokens,
             r.output_tokens, r.cost_usd, r.latency_s, time.time(), r.run_id),
        )


def log_http_call(model_id: str, item_id: str, run_id: str) -> None:
    with _db() as con:
        con.execute("INSERT INTO http_calls VALUES (?,?,?,?,?)", (time.time(), model_id, item_id, run_id, os.getpid()))


def http_calls_since(ts: float, model_id: str | None = None) -> int:
    with _db() as con:
        if model_id:
            return con.execute("SELECT COUNT(*) FROM http_calls WHERE ts>=? AND model_id=?", (ts, model_id)).fetchone()[0]
        return con.execute("SELECT COUNT(*) FROM http_calls WHERE ts>=?", (ts,)).fetchone()[0]


# ---------------------------------------------------------------- prompt

def build_prompt(item: Item, frames: list[bytes], label_style: str = "letter") -> tuple[list[dict], str]:
    """Return the user content blocks and a hash that covers everything the model sees."""
    content: list[dict] = []
    for i, jpg in enumerate(frames, 1):
        content.append({"type": "text", "text": f"Frame {i}/{len(frames)}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(jpg).decode()}})
    question = f"Question: {item.question}\nOptions:\n{item.options_text(label_style)}"
    content.append({"type": "text", "text": question})

    h = hashlib.sha256()
    h.update(PROMPT_VERSION.encode())
    h.update(label_style.encode())
    h.update(SYSTEM_PROMPT.encode())
    h.update(question.encode())
    for jpg in frames:
        h.update(hashlib.sha256(jpg).digest())
    return content, h.hexdigest()[:16]


def parse_letter(text: str, n_options: int, label_style: str = "letter") -> str | None:
    """Structured output gives {"answer": "<label>"}; map the label back to a letter by position.
    Falls back to the first bare capital letter in free text."""
    labels = labels_for(label_style, n_options)
    try:
        label = str(json.loads(text).get("answer", "")).strip().upper()
    except (json.JSONDecodeError, AttributeError):
        m = LETTER_RE.search(text.strip().upper())
        label = m.group(1) if m else ""
        labels = labels_for("letter", n_options)
    if label in labels:
        return "ABCDEFGHIJ"[labels.index(label)]
    return None


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = PRICES.get(model_id, (0.0, 0.0))
    return input_tokens * 1e-6 * p_in + output_tokens * 1e-6 * p_out


# ---------------------------------------------------------------- stub

def _stub_fault_once(item: Item) -> None:
    """If VEU_FAIL_ITEM names this item, raise a retryable error exactly once (across processes)."""
    if os.environ.get("VEU_FAIL_ITEM") != item.item_id:
        return
    marker = RESULTS_DIR / f".fault-{item.item_id.replace(':', '_')}"
    if not marker.exists():
        marker.write_text("raised")
        raise RetryableModelError(f"simulated 429 on {item.item_id}")


def _stub_hang_once(item: Item) -> None:
    """If VEU_HANG_ITEM names this item, hang once (across processes), like a model call that never returns."""
    if os.environ.get("VEU_HANG_ITEM") != item.item_id:
        return
    marker = RESULTS_DIR / f".hang-{item.item_id.replace(':', '_')}"
    if not marker.exists():
        marker.write_text("hung")
        time.sleep(3600)


def _stub_answer(item: Item, model_id: str) -> str:
    """Deterministic: model 'stub' is right ~80% of the time, 'stub-b' ~70%."""
    target = 0.80 if model_id == "stub" else 0.70
    h = int(hashlib.sha256(f"{model_id}:{item.item_id}".encode()).hexdigest(), 16)
    if (h % 1000) / 1000 < target:
        return item.answer_letter
    wrong = [c for c in "ABCDEFG"[: len(item.candidates)] if c != item.answer_letter]
    return wrong[h % len(wrong)]


# ---------------------------------------------------------------- call

def _client():
    import anthropic

    # VEU_ANTHROPIC_API_KEY is for cloud sessions, where a plain ANTHROPIC_API_KEY in the
    # environment could also be picked up by Claude Code itself. Local .env can use either.
    key = os.environ.get("VEU_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise FatalModelError("VEU_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) is not set; see .env.example")
    # max_retries=0: the engine owns retries, so retry counts in demos are honest.
    return anthropic.Anthropic(api_key=key, max_retries=0, timeout=90.0)


def _answer_schema(n_options: int, label_style: str) -> dict:
    return {"type": "json_schema", "schema": {
        "type": "object",
        "properties": {"answer": {"type": "string", "enum": labels_for(label_style, n_options)}},
        "required": ["answer"], "additionalProperties": False}}


def _request_kwargs(model_id: str) -> dict:
    """Per-model request settings so every model answers from perception alone, no extended thinking.

    No model gets sampling params: API version 2026-10-21 returns a 400 for any of them (WAS_TEMPERATURE_0).
    - Haiku 4.5: nothing extra; thinking is off unless asked for. Default sampling (temperature 1.0).
    - Sonnet 5.5: thinking can't be disabled; `between_tools` is the lowest setting (no extended
      thinking before the answer).
    - Opus 5.5: thinking can't be disabled or set to `between_tools`; effort is the only control.
      Effort is set to medium explicitly.
    An "output_config" entry here is merged into the request's output_config by _call_anthropic.
    No server-side fallbacks: in an evaluation a silent model substitution would corrupt the result.
    """
    if model_id.startswith("claude-sonnet-5-5"):
        return {"thinking": {"type": "between_tools"}}
    if model_id.startswith("claude-opus-5-5"):
        return {"output_config": {"effort": "medium"}}
    return {}


def _call_anthropic(model_id: str, content: list[dict], n_options: int, label_style: str) -> tuple[str, int, int]:
    import anthropic

    kwargs = _request_kwargs(model_id)
    output_config = {"format": _answer_schema(n_options, label_style), **kwargs.pop("output_config", {})}
    try:
        msg = _client().messages.create(
            model=model_id,
            max_tokens=MAX_TOKENS.get(model_id, DEFAULT_MAX_TOKENS),
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config=output_config,
            **kwargs,
        )
    except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError,
            anthropic.InternalServerError) as e:
        raise RetryableModelError(str(e)) from e
    except (anthropic.BadRequestError, anthropic.AuthenticationError, anthropic.PermissionDeniedError,
            anthropic.NotFoundError) as e:
        raise FatalModelError(str(e)) from e
    if msg.stop_reason == "refusal":
        cat = getattr(getattr(msg, "stop_details", None), "category", None)
        return f"refusal:{cat}", msg.usage.input_tokens, msg.usage.output_tokens
    if msg.stop_reason == "max_tokens":
        # Truncated before the answer finished; a partial reply must not be graded. Counts as a parse failure.
        return "max_tokens", msg.usage.input_tokens, msg.usage.output_tokens
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return text, msg.usage.input_tokens, msg.usage.output_tokens


def answer_item(item: Item, model_id: str, cfg: FrameCfg = FrameCfg(), run_id: str = "adhoc") -> ModelResult:
    """The one step that fails and costs money. Idempotent via the cache."""
    is_stub = model_id.startswith("stub")
    if is_stub:
        frames: list[bytes] = []
        content, prompt_hash = build_prompt(item, frames, cfg.label_style)
        prompt_hash = f"{prompt_hash}-{cfg.key()}"
    elif cfg.blind:
        # No video is fetched or decoded: the model sees the question and options only.
        content, prompt_hash = build_prompt(item, [], cfg.label_style)
    else:
        ensure_videos([item])
        frames = sample_frames(video_path(item), cfg)
        content, prompt_hash = build_prompt(item, frames, cfg.label_style)
    if cfg.blind:
        prompt_hash = f"{prompt_hash}-blind"
    if model_id in WAS_TEMPERATURE_0:
        # Default sampling now, so a row cached at temperature 0 is a different call and is not reused.
        prompt_hash = f"{prompt_hash}-tdefault"

    hit = cache_get(model_id, item.item_id, prompt_hash, run_id)
    if hit is not None:
        return hit

    t0 = time.time()
    if is_stub:
        _stub_fault_once(item)  # a 429 is refused before it is billed, so it is not logged
        # Logged when the request goes out, as for a real model: a call killed mid-flight still costs.
        log_http_call(model_id, item.item_id, run_id)
        _stub_hang_once(item)
        time.sleep(float(os.environ.get("VEU_STUB_DELAY_S", "0")))  # slow the stub down for kill/resume demos
        text, tin, tout = _stub_answer(item, model_id), 0, 0
    else:
        log_http_call(model_id, item.item_id, run_id)
        text, tin, tout = _call_anthropic(model_id, content, len(item.candidates), cfg.label_style)
    latency = time.time() - t0

    result = ModelResult(
        item_id=item.item_id, model_id=model_id, prompt_hash=prompt_hash,
        pred_letter=parse_letter(text, len(item.candidates), cfg.label_style), raw_text=text,
        input_tokens=tin, output_tokens=tout, cost_usd=cost_usd(model_id, tin, tout),
        latency_s=latency, cached=False, run_id=run_id,
    )
    cache_put(result)
    return result


def result_to_json(r: ModelResult) -> str:
    return json.dumps(asdict(r))
