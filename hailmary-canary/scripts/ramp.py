#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""Shift traffic to the new backend in steps and roll back if a gate fails"""

import argparse
import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass

import loadgen
from loadgen import Sample

DEFAULT_STEPS = "5,25,50,100"
DEFAULT_REQUESTS = 100
DEFAULT_WARMUP = 20
DEFAULT_MAX_ERROR_RATE = 0.02
DEFAULT_MAX_P95_RATIO = 1.25
DEFAULT_SETTLE_S = 3.0
MIN_GATE_SAMPLES = 5
MAX_PERCENT = 100


@dataclass(frozen=True)
class GateResult:
    """Verdict for one ramp step"""

    passed: bool
    reason: str


def parse_steps(raw: str) -> list[int]:
    """Parse a comma-separated list of traffic percentages

    Args:
        raw: for example '5,25,50,100'

    Raises:
        ValueError: if a step is not an integer in 1-100 or the steps do not strictly increase
    """
    try:
        steps = [int(part) for part in raw.split(",")]
    except ValueError as exc:
        raise ValueError(f"steps must be integers, got {raw!r}") from exc
    if any(step < 1 or step > MAX_PERCENT for step in steps):
        raise ValueError(f"each step must be between 1 and {MAX_PERCENT}")
    if any(later <= earlier for earlier, later in zip(steps, steps[1:])):
        raise ValueError("steps must strictly increase")
    return steps


def error_rate(samples: list[Sample]) -> float:
    """Return the share of samples that were not 2xx responses

    Args:
        samples: one entry per request
    """
    if not samples:
        return 0.0
    return sum(1 for s in samples if not loadgen.is_success(s)) / len(samples)


def backend_p95(samples: list[Sample], name: str) -> float | None:
    """Return the p95 total latency for one backend, or None when it has too few successful samples

    Args:
        samples: one entry per request
        name: backend name as reported by the load generator
    """
    totals = [s.total_s for s in samples if s.served_by == name and loadgen.is_success(s)]
    if len(totals) < MIN_GATE_SAMPLES:
        return None
    return loadgen.percentile(totals, 95)


def evaluate_gate(
    samples: list[Sample], baseline_p95: float, new_name: str, max_error_rate: float, max_p95_ratio: float
) -> GateResult:
    """Decide whether a ramp step may proceed

    Args:
        samples: results of the step
        baseline_p95: stable backend p95 measured before any traffic moved
        new_name: backend name of the new cluster
        max_error_rate: highest tolerated share of non-2xx responses
        max_p95_ratio: highest tolerated new p95 as a multiple of the baseline
    """
    errors = error_rate(samples)
    if errors > max_error_rate:
        return GateResult(False, f"error rate {errors:.1%} is above {max_error_rate:.1%}")

    new_p95 = backend_p95(samples, new_name)
    if new_p95 is None:
        return GateResult(True, f"fewer than {MIN_GATE_SAMPLES} samples from {new_name}, latency gate skipped")
    limit = baseline_p95 * max_p95_ratio
    if new_p95 > limit:
        return GateResult(False, f"{new_name} p95 {new_p95:.2f}s is above {limit:.2f}s ({max_p95_ratio}x baseline)")
    return GateResult(True, f"{new_name} p95 {new_p95:.2f}s is within {limit:.2f}s")


def set_weight(make_target: str, percent: int) -> None:
    """Move traffic by calling the Makefile weight target

    Args:
        make_target: 'weight' for the plain gateway or 'ai-weight' for the AI gateway
        percent: share of default traffic for the new backend

    Raises:
        subprocess.CalledProcessError: if make fails
    """
    subprocess.run(["make", "--no-print-directory", make_target, f"NEW={percent}"], check=True, capture_output=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=True,
    )
    loadgen.add_load_arguments(parser)

    ramp = parser.add_argument_group("ramp")
    ramp.add_argument("--steps", default=DEFAULT_STEPS, help="comma-separated percentages for the new backend")
    ramp.add_argument("-n", "--requests", type=int, default=DEFAULT_REQUESTS, help="requests per step")
    ramp.add_argument("--warmup", type=int, default=DEFAULT_WARMUP, help="discarded requests before the baseline")
    ramp.add_argument(
        "--make-target", default="weight", choices=["weight", "ai-weight"], help="Makefile weight target"
    )
    ramp.add_argument("--stable", default="llm-stable", help="stable backend name")
    ramp.add_argument("--new", default="llm-new", help="new backend name")
    ramp.add_argument("--settle", type=float, default=DEFAULT_SETTLE_S, help="seconds to wait after a weight change")

    gate = parser.add_argument_group("gate")
    gate.add_argument("--max-error-rate", type=float, default=DEFAULT_MAX_ERROR_RATE, help="highest non-2xx share")
    gate.add_argument("--max-p95-ratio", type=float, default=DEFAULT_MAX_P95_RATIO, help="highest new p95 / baseline")
    return parser


def _run_step(args: argparse.Namespace, headers: dict[str, str], percent: int, requests: int) -> list[Sample]:
    set_weight(args.make_target, percent)
    time.sleep(args.settle)
    payload = loadgen.build_payload(args.model, args.max_tokens, args.stream)
    return asyncio.run(loadgen.run_load(args.url, payload, headers, requests, args.concurrency))


def main() -> None:
    """Run the ramp and exit 1 if a gate fails"""
    args = _build_parser().parse_args()
    try:
        steps = parse_steps(args.steps)
        headers = loadgen.parse_headers(args.header)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.requests < 1 or args.concurrency < 1 or args.max_tokens < 1 or args.warmup < 0:
        print(
            "error: --requests, --concurrency and --max-tokens must be at least 1, --warmup at least 0",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        # Cold connections and first-request effects would inflate the baseline and let a slow backend pass the gate
        if args.warmup > 0:
            _run_step(args, headers, 0, args.warmup)
        print("== baseline at 0%")
        baseline_samples = _run_step(args, headers, 0, args.requests)
        print("\n".join(loadgen.summarise(baseline_samples, args.stream)))
        baseline_p95 = backend_p95(baseline_samples, args.stable)
        if baseline_p95 is None:
            print(f"error: too few successful samples from {args.stable} to set a baseline", file=sys.stderr)
            sys.exit(1)

        for percent in steps:
            print(f"\n== step {percent}%")
            samples = _run_step(args, headers, percent, args.requests)
            print("\n".join(loadgen.summarise(samples, args.stream)))
            gate = evaluate_gate(samples, baseline_p95, args.new, args.max_error_rate, args.max_p95_ratio)
            print(f"gate: {'PASS' if gate.passed else 'FAIL'}, {gate.reason}")
            if not gate.passed:
                set_weight(args.make_target, 0)
                print("rolled back to 0%", file=sys.stderr)
                sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"error: make failed: {exc.stderr.decode('utf-8', errors='replace').strip()}", file=sys.stderr)
        sys.exit(1)
    print("\nramp complete")


if __name__ == "__main__":
    main()
