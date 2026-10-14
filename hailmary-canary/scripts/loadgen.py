#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""Send concurrent chat requests through the gateway and report where they landed"""

import argparse
import asyncio
import math
import sys
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8080/v1/chat/completions"
DEFAULT_REQUESTS = 200
DEFAULT_CONCURRENCY = 20
DEFAULT_MODEL = "test-model"
DEFAULT_MAX_TOKENS = 32
REQUEST_TIMEOUT_S = 120.0
SERVED_BY_HEADER = "x-served-by"
INFERENCE_POD_HEADER = "x-inference-pod"
POD_HASH_SEGMENTS = 2
UNKNOWN_BACKEND = "unknown"
TRANSPORT_ERROR_STATUS = 0


@dataclass(frozen=True)
class Sample:
    """Outcome of one request"""

    status: int
    served_by: str
    total_s: float
    ttft_s: float | None


def parse_headers(raw: list[str]) -> dict[str, str]:
    """Parse 'Name: value' strings into a header dict

    Args:
        raw: header strings in 'Name: value' form

    Raises:
        ValueError: if an entry has no colon or an empty name
    """
    headers: dict[str, str] = {}
    for item in raw:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"invalid header {item!r}, expected 'Name: value'")
        headers[name.strip()] = value.strip()
    return headers


def percentile(values: list[float], pct: float) -> float:
    """Return the nearest-rank percentile of a non-empty list

    Args:
        values: samples in any order
        pct: percentile between 0 and 100

    Raises:
        ValueError: if values is empty
    """
    if not values:
        raise ValueError("percentile of an empty list")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def backend_name(headers: Mapping[str, str]) -> str:
    """Name the backend that served a response

    The gateway route sets x-served-by where it can. The simulator also reports its pod name, and stripping the
    ReplicaSet and pod hashes gives the Deployment name, which keeps attribution working on routes that cannot
    set response headers.

    Args:
        headers: response headers, case-insensitive
    """
    served_by = headers.get(SERVED_BY_HEADER)
    if served_by:
        return served_by
    pod = headers.get(INFERENCE_POD_HEADER)
    if pod:
        parts = pod.split("-")
        if len(parts) > POD_HASH_SEGMENTS + 1:
            return "-".join(parts[:-POD_HASH_SEGMENTS])
        return pod
    return UNKNOWN_BACKEND


def is_success(sample: Sample) -> bool:
    """Report whether a sample is a 2xx response"""
    return 200 <= sample.status < 300


def group_by_backend(samples: list[Sample]) -> dict[str, list[Sample]]:
    """Group samples by the backend that served them

    Args:
        samples: one entry per request
    """
    grouped: dict[str, list[Sample]] = {}
    for sample in samples:
        grouped.setdefault(sample.served_by, []).append(sample)
    return grouped


def summarise(samples: list[Sample], stream: bool) -> list[str]:
    """Build the report lines for a finished run

    Args:
        samples: one entry per request
        stream: whether time-to-first-token columns are meaningful
    """
    lines = []
    statuses = Counter(s.status for s in samples)
    lines.append("status: " + ", ".join(f"{code}={count}" for code, count in sorted(statuses.items())))

    header = f"{'served-by':<24}{'count':>7}{'share':>8}{'p50 s':>9}{'p95 s':>9}"
    if stream:
        header += f"{'ttft p50':>10}{'ttft p95':>10}"
    lines.append(header)

    for name, group in sorted(group_by_backend(samples).items()):
        totals = [s.total_s for s in group]
        row = (
            f"{name:<24}{len(group):>7}{len(group) / len(samples):>8.1%}"
            f"{percentile(totals, 50):>9.2f}{percentile(totals, 95):>9.2f}"
        )
        ttfts = [s.ttft_s for s in group if s.ttft_s is not None]
        if stream and ttfts:
            row += f"{percentile(ttfts, 50):>10.2f}{percentile(ttfts, 95):>10.2f}"
        lines.append(row)
    return lines


def build_payload(model: str, max_tokens: int, stream: bool) -> dict[str, Any]:
    """Build the chat completion request body

    Args:
        model: model name, which the AI gateway also routes on
        max_tokens: completion length
        stream: whether to request server-sent events
    """
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello"}],
        "max_tokens": max_tokens,
        "stream": stream,
    }


async def _send(client: httpx.AsyncClient, url: str, payload: dict[str, Any], headers: dict[str, str]) -> Sample:
    start = time.perf_counter()
    ttft: float | None = None
    try:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            async for chunk in resp.aiter_bytes():
                if ttft is None and chunk:
                    ttft = time.perf_counter() - start
            status = resp.status_code
            served_by = backend_name(resp.headers)
    except httpx.HTTPError as exc:
        return Sample(TRANSPORT_ERROR_STATUS, f"error:{type(exc).__name__}", time.perf_counter() - start, None)
    return Sample(status, served_by, time.perf_counter() - start, ttft)


async def run_load(
    url: str, payload: dict[str, Any], headers: dict[str, str], requests: int, concurrency: int
) -> list[Sample]:
    """Send requests with bounded concurrency and collect one sample each

    Args:
        url: chat completions endpoint
        payload: request body
        headers: extra request headers
        requests: total requests
        concurrency: maximum in-flight requests
    """
    gate = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, limits=limits) as client:

        async def bounded() -> Sample:
            async with gate:
                return await _send(client, url, payload, headers)

        return await asyncio.gather(*(bounded() for _ in range(requests)))


def add_load_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the target and load argument groups shared with the ramp script

    Args:
        parser: parser to extend
    """
    target = parser.add_argument_group("target")
    target.add_argument("--url", default=DEFAULT_URL, help="chat completions endpoint")
    target.add_argument("--model", default=DEFAULT_MODEL, help="model name in the request body")
    target.add_argument(
        "-H", "--header", action="append", default=[], metavar="'Name: value'", help="extra header, repeatable"
    )

    load = parser.add_argument_group("load")
    load.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="in-flight requests")
    load.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="completion length")
    load.add_argument("--stream", action="store_true", help="request SSE streaming and report time to first token")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=True,
    )
    add_load_arguments(parser)
    parser.add_argument("-n", "--requests", type=int, default=DEFAULT_REQUESTS, help="total requests")
    return parser


def main() -> None:
    """Run the load test and print the report"""
    args = _build_parser().parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.max_tokens < 1:
        print("error: --requests, --concurrency and --max-tokens must be at least 1", file=sys.stderr)
        sys.exit(1)
    try:
        headers = parse_headers(args.header)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    payload = build_payload(args.model, args.max_tokens, args.stream)
    samples = asyncio.run(run_load(args.url, payload, headers, args.requests, args.concurrency))
    print("\n".join(summarise(samples, args.stream)))


if __name__ == "__main__":
    main()
