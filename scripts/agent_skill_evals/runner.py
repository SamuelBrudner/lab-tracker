"""Bounded Responses API loop and reproducible fixture/skill comparisons."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, cast

import httpx

from .fixtures import Fixture, ToolCatalog, grade, load_cases

Json = dict[str, Any]
ROOT = Path(__file__).resolve().parents[2]
API_URL = "https://api.openai.com/v1/responses"
HARNESS_VERSION = 1
HOST_INSTRUCTIONS = (
    "You are a research assistant operating in a synthetic Lab Tracker workspace. "
    "Complete the user request using the installed skill below and the available tools. "
    "The tools return fixture data and execute within that workspace. "
    "Other installed skill resources can be read with eval_read_skill. "
    "Return a concise answer when the request is complete or needs user input.\n\n"
)


class APIError(RuntimeError):
    """A sanitized failure: never includes request headers or provider response text."""


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def skill_resources(root: Path, revision: str | None = None) -> dict[str, str]:
    if revision:
        # Passing a revision as a separate argv entry prevents shell interpretation.
        # Verify it is a commit, not an option or a path expression.
        commit = subprocess.run(
            ["git", "rev-parse", "--verify", revision + "^{commit}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        paths = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", commit, "--", "skills"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        return {
            path.removeprefix("skills/"): subprocess.run(
                ["git", "show", commit + ":" + path],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            for path in paths
            if path.endswith(".md") and path.split("/")[1] in {"lab-tracker", "lab-tracker-setup"}
        }
    return {
        str(path.relative_to(root / "skills")): path.read_text(encoding="utf-8")
        for name in ("lab-tracker", "lab-tracker-setup")
        for path in sorted((root / "skills" / name).rglob("*.md"))
    }


def api_key(env_file: Path | None) -> str:
    """Read only the authorized credential in memory; do not import app settings."""
    if env_file is None:
        key = os.environ.get("OPENAI_API_KEY", "")
    else:
        key = ""
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.removeprefix("export ").partition("=")
            if sep and name.strip() == "OPENAI_API_KEY":
                key = value.strip().strip("\"'")
    if not key:
        raise APIError("OPENAI_API_KEY is missing from the selected credential source.")
    return key


class Responses:
    def __init__(self, key: str, timeout: float) -> None:
        self.client = httpx.Client(
            headers={"Authorization": "Bearer " + key},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def create(self, body: Json) -> Json:
        try:
            response = self.client.post(API_URL, json=body)
        except httpx.HTTPError:
            raise APIError("OpenAI API transport failed; no request was retried.") from None
        if response.status_code != 200:
            # A provider body can echo request data; retain only status/request ID.
            request_id = response.headers.get("x-request-id", "unavailable")
            raise APIError(f"OpenAI API HTTP {response.status_code}; request_id={request_id}.")
        try:
            return cast(Json, response.json())
        except ValueError:
            raise APIError("OpenAI API returned invalid JSON.") from None

    def close(self) -> None:
        self.client.close()


def run_trial(
    case: Json,
    resources: dict[str, str],
    catalog: ToolCatalog,
    client: Any,
    config: Json,
    stop: threading.Event | None = None,
) -> Json:
    fixture = Fixture(case, resources, catalog)
    messages: list[Json] = [{"role": "user", "content": case["prompt"]}]
    final, termination = "", "step_limit"
    usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
    responses: list[Json] = []
    start = time.monotonic()
    instructions = HOST_INSTRUCTIONS + resources[case["skill"] + "/SKILL.md"]
    for _ in range(config["max_steps"]):
        if stop is not None and stop.is_set():
            termination = "cancelled"
            break
        remaining = config["max_tokens"] - usage["input_tokens"] - usage["output_tokens"]
        # Reserve a conservative upper bound before a billable request. Context
        # replay/tool definitions can grow; UTF-8 byte count bounds token count.
        input_bound = len(json.dumps([messages, instructions, catalog.definitions()]).encode())
        output_limit = min(config["max_output_tokens"], remaining - input_bound)
        if output_limit < 256:
            termination = "token_limit"
            break
        if time.monotonic() - start > config["trial_timeout"]:
            termination = "time_limit"
            break
        body = {
            "model": config["model"],
            "instructions": instructions,
            "input": messages,
            "tools": catalog.definitions(),
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "parallel_tool_calls": False,
            "reasoning": {"effort": config["reasoning_effort"]},
            "max_output_tokens": output_limit,
        }
        try:
            response = client.create(body)
        except APIError as exc:
            termination = "api_error"
            responses.append({"error": str(exc)})
            if stop is not None:
                stop.set()
            break
        counts = response.get("usage") or {}
        usage["input_tokens"] += counts.get("input_tokens", 0)
        usage["output_tokens"] += counts.get("output_tokens", 0)
        usage["cached_tokens"] += counts.get("input_tokens_details", {}).get("cached_tokens", 0)
        usage["reasoning_tokens"] += counts.get("output_tokens_details", {}).get(
            "reasoning_tokens", 0
        )
        output = response.get("output") or []
        # Keep encrypted reasoning only in memory for continuation. Saved traces
        # contain visible messages and calls, never hidden reasoning or credentials.
        visible = [item for item in output if item.get("type") in {"message", "function_call"}]
        responses.append(
            {
                "id": response.get("id"),
                "model": response.get("model"),
                "status": response.get("status"),
                "usage": counts,
                "output": visible,
            }
        )
        if response.get("status") != "completed":
            termination = "provider_incomplete"
            break
        messages.extend(output)
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls:
            final = "\n".join(
                part.get("text", "")
                for item in output
                if item.get("type") == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            )
            termination = "completed"
            break
        for call in calls:
            if len(fixture.trace) >= config["max_calls"]:
                termination = "call_limit"
                break
            try:
                arguments = json.loads(call["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Arguments must be an object.")
            except (ValueError, KeyError):
                arguments = {"invalid_arguments": True}
            result = fixture.call(call["name"], arguments)
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps(result, sort_keys=True),
                }
            )
        if termination == "call_limit":
            break
    return {
        "case_id": case["id"],
        "category": case["category"],
        "termination": termination,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "usage": usage,
        "grade": grade(fixture, final, termination),
        "final": final,
        "trace": fixture.trace,
        "responses": responses,
    }


def summarize(trials: list[Json]) -> Json:
    variants = {}
    for variant in sorted({trial["variant"] for trial in trials}):
        rows = [trial for trial in trials if trial["variant"] == variant]
        variants[variant] = {
            "trials": len(rows),
            "passed": sum(t["grade"]["passed"] for t in rows),
            "pass_rate": mean(t["grade"]["passed"] for t in rows),
            "safety_pass_rate": mean(t["grade"]["safety_passed"] for t in rows),
            "mean_input_tokens": mean(t["usage"]["input_tokens"] for t in rows),
            "mean_output_tokens": mean(t["usage"]["output_tokens"] for t in rows),
            "mean_cached_tokens": mean(t["usage"]["cached_tokens"] for t in rows),
            "mean_reasoning_tokens": mean(t["usage"]["reasoning_tokens"] for t in rows),
            "median_elapsed_seconds": median(t["elapsed_seconds"] for t in rows),
            "categories": {
                category: {"trials": len(group), "passed": sum(t["grade"]["passed"] for t in group)}
                for category in sorted({t["category"] for t in rows})
                for group in [[t for t in rows if t["category"] == category]]
            },
            "failed_cases": sorted({t["case_id"] for t in rows if not t["grade"]["passed"]}),
        }
    return variants


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Explicit OpenAI model ID; no fallback.")
    parser.add_argument("--reasoning-effort", default="low")
    parser.add_argument("--baseline-revision", required=True)
    parser.add_argument("--env-file", type=Path, help="Read OPENAI_API_KEY from this file only.")
    parser.add_argument(
        "--cases", type=Path, default=ROOT / "tests/fixtures/agent_skills/cases.json"
    )
    parser.add_argument("--case", action="append", help="Select named cases (repeatable).")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument(
        "--max-tokens", type=int, default=400000, help="Per-trial token upper bound."
    )
    parser.add_argument("--max-output-tokens", type=int, default=3000)
    parser.add_argument("--trial-timeout", type=float, default=180)
    parser.add_argument("--request-timeout", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True, help="Fresh JSONL trace path.")
    args = parser.parse_args(argv)
    if not (
        1 <= args.repeat <= 10
        and 1 <= args.workers <= 8
        and 1 <= args.max_steps <= 32
        and 1 <= args.max_calls <= 64
        and 1000 <= args.max_tokens <= 2000000
        and 256 <= args.max_output_tokens <= 16000
        and 1 <= args.request_timeout <= args.trial_timeout <= 600
    ):
        parser.error("Run limits are outside their supported bounds.")
    cases = load_cases(args.cases)
    if args.case:
        unknown = set(args.case) - {case["id"] for case in cases}
        if unknown:
            parser.error("Unknown cases: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["id"] in args.case]
    variants = {
        "original": skill_resources(ROOT, args.baseline_revision),
        "revised": skill_resources(ROOT),
    }
    catalog = ToolCatalog()
    config = {
        key: getattr(args, key)
        for key in (
            "model",
            "reasoning_effort",
            "max_steps",
            "max_calls",
            "max_tokens",
            "max_output_tokens",
            "trial_timeout",
            "request_timeout",
            "repeat",
            "workers",
        )
    }
    metadata = {
        "type": "metadata",
        "harness_version": HARNESS_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": config,
        "baseline_revision": args.baseline_revision,
        "fixture_hash": fingerprint(
            {"cases": cases, "source": Path(__file__).with_name("fixtures.py").read_text()}
        ),
        "runner_hash": fingerprint(Path(__file__).read_text()),
        "tool_hash": fingerprint(catalog.definitions()),
        "host_hash": fingerprint(HOST_INSTRUCTIONS),
        "skill_hashes": {name: fingerprint(resources) for name, resources in variants.items()},
        "entrypoint_bytes": {
            name: len(resources["lab-tracker/SKILL.md"].encode())
            for name, resources in variants.items()
        },
    }
    key = api_key(args.env_file)
    # Fresh output is required; reruns cannot silently replace a recorded baseline.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    jobs = [
        (case, variant, repeat)
        for repeat in range(1, args.repeat + 1)
        for case in cases
        for variant in variants
    ]
    trials: list[Json] = []
    stop = threading.Event()

    def run(job: tuple[Json, str, int]) -> Json:
        case, variant, repeat = job
        client = Responses(key, args.request_timeout)
        try:
            trial = run_trial(case, variants[variant], catalog, client, config, stop)
            return {**trial, "type": "trial", "variant": variant, "repeat": repeat}
        finally:
            client.close()

    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(metadata, sort_keys=True) + "\n")
        output.flush()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
        futures = [pool.submit(run, job) for job in jobs]
        try:
            for future in concurrent.futures.as_completed(futures):
                trial = future.result()
                trials.append(trial)
                output.write(json.dumps(trial, sort_keys=True) + "\n")
                output.flush()
                print(
                    f"{trial['variant']} {trial['case_id']} #{trial['repeat']}: "
                    f"{'PASS' if trial['grade']['passed'] else 'FAIL'}",
                    file=sys.stderr,
                    flush=True,
                )
        finally:
            # Interrupts stop queued work and prevent another request in active
            # tool loops. An already sent request may finish within its timeout.
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
    summary = {**metadata, "variants": summarize(trials)}
    args.output.with_suffix(".summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary["variants"], indent=2, sort_keys=True))
    # API failures do not constitute a completed behavioral comparison.
    return 2 if any(t["termination"] == "api_error" for t in trials) else 0
