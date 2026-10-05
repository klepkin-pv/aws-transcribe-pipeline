"""Async load test for the jobs API.

    API_BASE_URL=https://... API_TOKEN=<cognito-jwt> python scripts/load_test.py \
        --concurrency 10 --duration 30

Each iteration creates a job and lists the user's jobs, collects latencies
and prints p50/p95/p99 plus throughput. Percentiles land in the README after
the first deployment to a real AWS account (see docs/runbook.md).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import string
import time

import httpx


def percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * share), len(ordered) - 1)
    return ordered[index] * 1000


async def one_iteration(client: httpx.AsyncClient, base_url: str, token: str) -> float:
    name = "load-" + "".join(random.choices(string.ascii_lowercase, k=8)) + ".mp3"
    headers = {"Authorization": f"Bearer {token}"}

    start = time.perf_counter()
    created = await client.post(
        f"{base_url}/jobs",
        headers=headers,
        json={"filename": name, "content_type": "audio/mpeg"},
    )
    if created.status_code != 201:
        raise RuntimeError(f"POST /jobs -> {created.status_code}: {created.text[:120]}")
    post_latency = time.perf_counter() - start

    start = time.perf_counter()
    listed = await client.get(f"{base_url}/jobs", headers=headers, params={"limit": 20})
    if listed.status_code != 200:
        raise RuntimeError(f"GET /jobs -> {listed.status_code}: {listed.text[:120]}")
    get_latency = time.perf_counter() - start

    return (post_latency + get_latency) / 2


async def run_worker(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    deadline: float,
    latencies: list[float],
) -> None:
    while time.monotonic() < deadline:
        try:
            latencies.append(await one_iteration(client, base_url, token))
        except RuntimeError as exc:
            print(f"error: {exc}")
            return


async def main() -> None:
    parser = argparse.ArgumentParser(description="Load test for the jobs API.")
    parser.add_argument("--base-url", default=os.environ.get("API_BASE_URL", ""))
    parser.add_argument("--token", default=os.environ.get("API_TOKEN", ""))
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duration", type=int, default=30, help="seconds")
    args = parser.parse_args()

    if not args.base_url or not args.token:
        parser.error("pass --base-url/API_BASE_URL and --token/API_TOKEN (a Cognito JWT)")

    deadline = time.monotonic() + args.duration
    latencies: list[float] = []
    limits = httpx.Limits(max_connections=args.concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:
        await asyncio.gather(
            *(
                run_worker(client, args.base_url, args.token, deadline, latencies)
                for _ in range(args.concurrency)
            )
        )

    total = len(latencies)
    print(f"\niterations: {total} ({total / args.duration:.1f} it/s)")
    print(f"p50 = {percentile(latencies, 0.50):.0f} ms")
    print(f"p95 = {percentile(latencies, 0.95):.0f} ms")
    print(f"p99 = {percentile(latencies, 0.99):.0f} ms")


if __name__ == "__main__":
    asyncio.run(main())
