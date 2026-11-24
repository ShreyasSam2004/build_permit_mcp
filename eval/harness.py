"""Run the eval: the same model, prompt and tool-call cap against each arm's MCP server.

    uv run --group eval python -m eval.harness --arms permits,baseline [--pilot] [--questions ...]
    uv run --group eval python -m eval.harness --dry-run      # start each server and list tools; no API calls

Arms
  permits       this server (python -m permits_mcp), stdio
  baseline      opengov-mcp-server@0.1.1 (one generic `get_data` tool), stdio; the system prompt also
                gets each city's portal hostname, so the run measures schema understanding, not discovery
  baseline_ids  baseline plus each city's dataset id ("strong baseline")

Each trial gets a fresh server process (no warm cache, no shared state), a manual tool loop with the
Anthropic SDK and a cap of 20 tool calls. Outputs go to eval/results/runs/<run id>/:
  results.jsonl   one row per trial that produced a scorable outcome (answered, no answer, refusal, ...)
  errors.jsonl    trials that never produced one (API failure after retries, server crash, timeout);
                  these are not scored and are retried by --resume
  transcripts/    every message, tool call and tool result per trial, plus the server's stderr
Grading is separate and deterministic: eval/grade.py.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from .common import (
    CITIES,
    EVAL_DIR,
    REPO_DIR,
    RUNS_DIR,
    Question,
    latest_ground_truth,
    load_questions,
    read_json,
    snapshot_path,
    write_json,
)

MODEL = "claude-opus-5-5"
# USD per million tokens, from the Anthropic price list (checked 2026-11-23). cache_write is the 5-minute TTL rate.
PRICES_PER_MTOK = {MODEL: {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20}}
MAX_TOOL_CALLS = 20
MAX_API_CALLS = 40  # backstop; the tool-call cap ends a trial first in practice
MAX_TOKENS = 16_000  # per response; tool-loop turns are short, so hitting it is recorded as `truncated`
MAX_TOOL_RESULT_CHARS = 100_000  # same cap for every arm; truncation is marked inside the result
MAX_API_RETRIES = 6
TRIAL_TIMEOUT_S = 900.0
PILOT = ("chi-08", "sea-01", "aus-03")  # address spelling trap, date-field semantics, status semantics

BASELINE_PACKAGE = "opengov-mcp-server"
BASELINE_VERSION = "0.1.1"
BASELINE_DIR = EVAL_DIR / "baseline"
BASELINE_ENTRY = BASELINE_DIR / "node_modules" / BASELINE_PACKAGE / "dist" / "index.js"
ARMS = ("permits", "baseline", "baseline_ids")
FINAL_ANSWER = re.compile(r"^\s*\**\s*FINAL ANSWER\s*:?\s*\**\s*:?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

SYSTEM_PROMPT = """\
You answer questions about building permits in US cities using only the tools provided. Look the data up \
with the tools; never answer from memory or estimate. Today's date is {today}.

When you are done, end your reply with one line in exactly this form:
FINAL ANSWER: <answer>

Write the answer as:
- a count: an integer without commas, e.g. 1234
- a dollar amount: a plain number without $ or commas, e.g. 1234567.89
- a list of permit numbers: the numbers separated by commas
- a month: YYYY-MM
- a city: its name
If the data needed to answer is not published, write: FINAL ANSWER: UNAVAILABLE"""

BASELINE_PORTALS = """

The tools query Socrata open-data portals. Each city's portal: Chicago data.cityofchicago.org, Seattle \
data.seattle.gov, Austin data.austintexas.gov. Pass the hostname as `domain` to query a portal other than the \
default one."""

BASELINE_DATASETS = """
Building permit datasets: Chicago ydr8-5enu (Building Permits), Seattle 76t5-zqzr (Building Permits), \
Austin 3syk-w9eu (Issued Construction Permits)."""


class TrialAbort(Exception):
    """The trial can't produce a scorable outcome; it goes to errors.jsonl, not results.jsonl."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class Trial:
    question: Question
    arm: str
    rep: int

    @property
    def key(self) -> str:
        return f"{self.question.id}__{self.arm}__r{self.rep}"


@dataclass
class Config:
    run_dir: Path
    arms: tuple[str, ...]
    effort: str
    concurrency: int
    today: str


@dataclass
class Stats:
    usage: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(("input", "output", "cache_write", "cache_read"), 0)
    )
    api_calls: int = 0
    api_retries: int = 0
    model_latency_s: float = 0.0
    tools: list[dict[str, Any]] = field(default_factory=list)

    def add_response(self, response: Any, latency: float, retries: int) -> dict[str, int]:
        u = response.usage
        call = {
            "input": u.input_tokens,
            "output": u.output_tokens,
            "cache_write": u.cache_creation_input_tokens or 0,
            "cache_read": u.cache_read_input_tokens or 0,
        }
        for k, v in call.items():
            self.usage[k] += v
        self.api_calls += 1
        self.api_retries += retries
        self.model_latency_s += latency
        return call


def cost_usd(usage: dict[str, int], model: str = MODEL) -> float:
    price = PRICES_PER_MTOK[model]
    return round(sum(usage[k] * price[k] for k in price) / 1_000_000, 6)


def system_prompt(arm: str, today: str) -> str:
    prompt = SYSTEM_PROMPT.format(today=today)
    if arm in ("baseline", "baseline_ids"):
        prompt += BASELINE_PORTALS
    if arm == "baseline_ids":
        prompt += BASELINE_DATASETS
    return prompt


def minimal_env(**extra: str) -> dict[str, str]:
    """Servers get only what they need to run, never this machine's secrets (the baseline also loads a
    .env from its working directory, which is why it runs from eval/baseline)."""
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
    return {**{k: os.environ[k] for k in keep if k in os.environ}, **extra}


def server_params(arm: str, question: Question) -> StdioServerParameters:
    if arm == "permits":
        # No Socrata app token for either arm: the baseline can't send one, so neither gets the higher quota.
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "permits_mcp"],
            env=minimal_env(PERMITS_LOG_LEVEL="WARNING"),
            cwd=str(REPO_DIR),
        )
    node = shutil.which("node")
    if node is None:
        raise SystemExit("node is not on PATH; the baseline arms need Node.js 18+.")
    portal = CITIES[question.cities[0]].domain  # the user would configure the portal of the city they ask about
    return StdioServerParameters(
        command=node,
        args=[str(BASELINE_ENTRY)],
        env=minimal_env(DATA_PORTAL_URL=f"https://{portal}"),
        cwd=str(BASELINE_DIR),
    )


def check_baseline_installed() -> None:
    manifest = BASELINE_DIR / "node_modules" / BASELINE_PACKAGE / "package.json"
    if not manifest.exists():
        raise SystemExit(f"The baseline isn't installed. Run once: cd {BASELINE_DIR} && npm ci --ignore-scripts")
    version = read_json(manifest)["version"]
    if version != BASELINE_VERSION:
        raise SystemExit(
            f"Installed baseline is {BASELINE_PACKAGE}@{version}; expected {BASELINE_VERSION}. Run npm ci."
        )


def anthropic_tool(tool: Any) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}


def parse_final_answer(text: str) -> str | None:
    matches = FINAL_ANSWER.findall(text)
    return matches[-1].strip().strip("`*").strip() if matches else None


def _retry_delay(exc: anthropic.APIStatusError | None, attempt: int) -> float:
    if exc is not None:
        header = exc.response.headers.get("retry-after")
        if header and header.replace(".", "", 1).isdigit():
            return min(float(header), 120.0)
    return min(60.0, 2.0 * 2**attempt) * random.uniform(0.5, 1.0)  # exponential backoff, full-ish jitter


async def call_model(client: anthropic.AsyncAnthropic, request: dict[str, Any]) -> tuple[Any, float, int]:
    """One model call with our own retries, so latency covers only the successful attempt."""
    retries = 0
    while True:
        start = time.perf_counter()
        try:
            response = await client.messages.create(**request)
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code in (408, 409, 429) or exc.status_code >= 500
            if not retryable or retries >= MAX_API_RETRIES:
                raise TrialAbort("api_error", f"HTTP {exc.status_code}: {exc.message}") from exc
            delay = _retry_delay(exc, retries)
        except anthropic.APIConnectionError as exc:  # includes timeouts
            if retries >= MAX_API_RETRIES:
                raise TrialAbort("api_error", repr(exc)) from exc
            delay = _retry_delay(None, retries)
        else:
            return response, time.perf_counter() - start, retries
        retries += 1
        await asyncio.sleep(delay)


async def call_tool(mcp: Client, use: Any, stats: Stats) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        result = await mcp.call_tool(use.name, use.input)
    except Exception as exc:  # a protocol failure or dead server, not a tool error the model can read
        raise TrialAbort("mcp_error", f"{use.name}: {exc!r}") from exc
    latency = time.perf_counter() - start
    parts = [c.text if c.type == "text" else json.dumps(c.model_dump(mode="json")) for c in result.content]
    text = "\n".join(parts) or "(empty result)"
    size = len(text)
    if size > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + (
            f"\n[eval harness: result truncated to {MAX_TOOL_RESULT_CHARS:,} of {size:,} characters]"
        )
    stats.tools.append(
        {
            "name": use.name,
            "latency_s": round(latency, 3),
            "is_error": bool(result.is_error),
            "chars": size,
            "truncated": size > MAX_TOOL_RESULT_CHARS,
        }
    )
    return {"type": "tool_result", "tool_use_id": use.id, "content": text, "is_error": bool(result.is_error)}


async def agent_loop(
    trial: Trial, mcp: Client, client: anthropic.AsyncAnthropic, cfg: Config, transcript: dict[str, Any], stats: Stats
) -> dict[str, Any]:
    listed = (await mcp.list_tools()).tools
    tools = [anthropic_tool(t) for t in listed]
    tools_json = json.dumps(tools, sort_keys=True)
    transcript["tools"] = {
        "names": [t["name"] for t in tools],
        "sha256": hashlib.sha256(tools_json.encode()).hexdigest(),
    }
    tools_file = cfg.run_dir / f"tools-{trial.arm}-{trial.question.cities[0]}.json"
    if not tools_file.exists():
        write_json(tools_file, tools)
    system = system_prompt(trial.arm, cfg.today)
    transcript["system"] = system
    messages: list[Any] = [{"role": "user", "content": trial.question.question}]
    turns: list[dict[str, Any]] = [{"role": "user", "content": trial.question.question}]
    transcript["turns"] = turns
    request: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "tools": tools,
        "messages": messages,
        "thinking": {"type": "adaptive", "display": "summarized"},  # summaries make failures debuggable
        "output_config": {"effort": cfg.effort},
        "cache_control": {"type": "ephemeral"},  # the tools + system prefix repeats on every turn
        # No `fallbacks`: every answer must come from the model under test; a refusal is recorded as such.
    }
    tool_calls, budget_exhausted = 0, False
    for _ in range(MAX_API_CALLS):
        response, latency, retries = await call_model(client, request)
        if response.model != MODEL:
            raise TrialAbort("served_model_mismatch", f"asked for {MODEL}, served by {response.model}")
        call_usage = stats.add_response(response, latency, retries)
        turns.append(
            {
                "role": "assistant",
                "stop_reason": response.stop_reason,
                "latency_s": round(latency, 3),
                "retries": retries,
                "usage": call_usage,
                "content": [b.model_dump(mode="json", exclude_none=True) for b in response.content],
            }
        )
        messages.append({"role": "assistant", "content": response.content})  # unchanged, thinking blocks included

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            return {"status": "refusal", "refusal_category": getattr(details, "category", None)}
        if response.stop_reason == "max_tokens":
            return {"status": "truncated"}
        if response.stop_reason == "pause_turn":
            continue
        uses = [b for b in response.content if b.type == "tool_use"]
        if not uses:
            text = "\n".join(b.text for b in response.content if b.type == "text")
            answer = parse_final_answer(text)
            return {
                "status": "answered" if answer is not None else "no_answer",
                "final_answer": answer,
                "budget_exhausted": budget_exhausted,
            }
        if budget_exhausted:
            return {"status": "turn_cap"}
        if tool_calls + len(uses) > MAX_TOOL_CALLS:
            budget_exhausted = True
            note = f"Tool budget exhausted ({MAX_TOOL_CALLS} calls). Reply now with your best FINAL ANSWER."
            results = [{"type": "tool_result", "tool_use_id": u.id, "content": note, "is_error": True} for u in uses]
        else:
            tool_calls += len(uses)
            results = list(await asyncio.gather(*(call_tool(mcp, u, stats) for u in uses)))
        messages.append({"role": "user", "content": results})
        turns.append(
            {"role": "user", "tool_calls": [{"name": u.name, "input": u.input} for u in uses], "content": results}
        )
    return {"status": "turn_cap"}


async def run_trial(trial: Trial, client: anthropic.AsyncAnthropic, cfg: Config) -> tuple[str, dict[str, Any]]:
    tdir = cfg.run_dir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    transcript: dict[str, Any] = {
        "key": trial.key,
        "question_id": trial.question.id,
        "arm": trial.arm,
        "rep": trial.rep,
        "question": trial.question.question,
    }
    stats = Stats()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    start = time.perf_counter()
    kind, outcome = "result", {}
    try:
        async with asyncio.timeout(TRIAL_TIMEOUT_S):
            with (tdir / f"{trial.key}.server.log").open("w", encoding="utf-8") as errlog:
                async with Client(stdio_client(server_params(trial.arm, trial.question), errlog=errlog)) as mcp:
                    outcome = await agent_loop(trial, mcp, client, cfg, transcript, stats)
    except TrialAbort as exc:
        kind, outcome = "error", {"error_class": exc.kind, "detail": exc.detail}
    except TimeoutError:
        kind, outcome = "error", {"error_class": "timeout", "detail": f"trial exceeded {TRIAL_TIMEOUT_S:.0f}s"}
    except Exception as exc:  # anything else is harness plumbing, never a model failure
        kind, outcome = "error", {"error_class": "harness_error", "detail": repr(exc)}
    row = {
        "key": trial.key,
        "question_id": trial.question.id,
        "arm": trial.arm,
        "rep": trial.rep,
        "started_at": started_at,
        **outcome,
        "tool_calls": len(stats.tools),
        "tool_errors": sum(t["is_error"] for t in stats.tools),
        "tool_results_truncated": sum(t["truncated"] for t in stats.tools),
        "api_calls": stats.api_calls,
        "api_retries": stats.api_retries,
        "usage": stats.usage,
        "cost_usd": cost_usd(stats.usage),
        "model_latency_s": round(stats.model_latency_s, 2),
        "tool_latency_s": round(sum(t["latency_s"] for t in stats.tools), 2),
        "wall_s": round(time.perf_counter() - start, 2),
        "transcript": f"transcripts/{trial.key}.json",
    }
    transcript["outcome"] = row
    transcript["tool_stats"] = stats.tools
    write_json(tdir / f"{trial.key}.json", transcript)
    return kind, row


def _git_state() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True, text=True, check=False).stdout.strip()

    # Only what changes a run's behaviour: eval/results is written by the run itself (ground truth first).
    code = ("src", "eval", "pyproject.toml", "uv.lock", ":(exclude)eval/results")
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--", *code))}


def _done_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)["key"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


async def run(trials: list[Trial], cfg: Config) -> None:
    client = anthropic.AsyncAnthropic(max_retries=0)  # retries are ours, so latency excludes backoff
    try:
        await client.models.retrieve(MODEL)  # free; fails fast on missing credentials or model access
    except anthropic.APIError as exc:
        raise SystemExit(f"Can't reach {MODEL}: {exc}. Set ANTHROPIC_API_KEY (see .env.example).") from exc
    results_path, errors_path = cfg.run_dir / "results.jsonl", cfg.run_dir / "errors.jsonl"
    done = _done_keys(results_path)
    todo = [t for t in trials if t.key not in done]
    print(f"{len(trials)} trials, {len(done)} already done, running {len(todo)} (concurrency {cfg.concurrency})")
    lock, semaphore = asyncio.Lock(), asyncio.Semaphore(cfg.concurrency)
    total_cost = 0.0

    async def one(trial: Trial) -> None:
        nonlocal total_cost
        async with semaphore:
            kind, row = await run_trial(trial, client, cfg)
        async with lock:
            path = results_path if kind == "result" else errors_path
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
            total_cost += row["cost_usd"]
            label = row.get("status") or f"ERROR {row['error_class']}"
            print(
                f"  {trial.key:<28} {label:<12} tools={row['tool_calls']:<3} ${row['cost_usd']:.3f}  "
                f"{row['wall_s']:.0f}s  (run total ${total_cost:.2f})"
            )

    await asyncio.gather(*(one(t) for t in todo))
    await client.close()


async def dry_run(arms: tuple[str, ...], question: Question) -> None:
    for arm in arms:
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with Client(stdio_client(server_params(arm, question), errlog=errlog)) as mcp:
                tools = [anthropic_tool(t) for t in (await mcp.list_tools()).tools]
        size = len(json.dumps(tools))
        print(f"{arm}: {len(tools)} tool(s) {[t['name'] for t in tools]}, {size:,} chars of definitions")
        print(f"  system prompt: {len(system_prompt(arm, 'YYYY-MM-DD')):,} chars")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arms", default="permits,baseline", help=f"comma-separated, from {', '.join(ARMS)}")
    parser.add_argument("--questions", help="comma-separated question ids (default: all)")
    parser.add_argument("--pilot", action="store_true", help=f"only the pilot questions {', '.join(PILOT)}")
    parser.add_argument("--reps", type=int, default=1, help="trials per question per arm (default 1)")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--run-id", help="resume or name a run (default: a UTC timestamp)")
    parser.add_argument("--ground-truth", type=Path, help="snapshot to grade against (default: latest)")
    parser.add_argument("--dry-run", action="store_true", help="start each server and list its tools; no API calls")
    args = parser.parse_args(argv)

    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    if unknown := set(arms) - set(ARMS):
        parser.error(f"unknown arms {sorted(unknown)}; choose from {ARMS}")
    questions = load_questions()
    if args.pilot:
        questions = [q for q in questions if q.id in PILOT]
    elif args.questions:
        wanted = set(args.questions.split(","))
        if unknown := wanted - {q.id for q in questions}:
            parser.error(f"unknown question ids {sorted(unknown)}")
        questions = [q for q in questions if q.id in wanted]
    if any(a != "permits" for a in arms):
        check_baseline_installed()
    if args.dry_run:
        asyncio.run(dry_run(arms, questions[0]))
        return 0

    gt_path = (args.ground_truth or latest_ground_truth()).resolve()
    snapshot = read_json(gt_path)
    gt_path = snapshot_path(snapshot["computed_at"])  # record the immutable file, never the moving latest.json
    if not gt_path.exists():
        raise SystemExit(f"Snapshot file {gt_path.name} is missing; rerun eval.ground_truth.")
    missing = [q.id for q in questions if "expected" not in snapshot["questions"].get(q.id, {})]
    if missing:
        raise SystemExit(f"Ground truth {gt_path.name} has no answer for {missing}; rerun eval.ground_truth.")
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS_DIR / run_id
    today = datetime.now(ZoneInfo(CITIES["chicago"].timezone)).date().isoformat()
    created = datetime.now(UTC).isoformat(timespec="seconds")
    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        meta = read_json(meta_path)
        if meta["model"] != MODEL or meta["effort"] != args.effort:
            raise SystemExit(f"Run {run_id} used {meta['model']} at effort {meta['effort']}; can't resume differently.")
        today, created = meta["today"], meta["created"]  # a resumed run keeps an identical system prompt
    write_json(
        meta_path,
        {
            "run_id": run_id,
            "created": created,
            "model": MODEL,
            "effort": args.effort,
            "today": today,
            "arms": list(arms),
            "reps": args.reps,
            "question_ids": [q.id for q in questions],
            "pilot": args.pilot,
            "ground_truth": {"file": gt_path.name, "computed_at": snapshot["computed_at"]},
            "limits": {
                "max_tool_calls": MAX_TOOL_CALLS,
                "max_tokens": MAX_TOKENS,
                "max_tool_result_chars": MAX_TOOL_RESULT_CHARS,
                "trial_timeout_s": TRIAL_TIMEOUT_S,
            },
            "baseline": f"{BASELINE_PACKAGE}@{BASELINE_VERSION}",
            "prices_per_mtok": PRICES_PER_MTOK,
            "git": _git_state(),
            "system_prompts": {arm: system_prompt(arm, today) for arm in arms},
        },
    )
    cfg = Config(run_dir=run_dir, arms=arms, effort=args.effort, concurrency=args.concurrency, today=today)
    trials = [Trial(q, arm, rep) for rep in range(1, args.reps + 1) for q in questions for arm in arms]
    asyncio.run(run(trials, cfg))
    print(f"\nRun directory: {run_dir}\nGrade it: uv run --group eval python -m eval.grade {run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
