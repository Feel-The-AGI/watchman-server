"""Gentle load against a free Render instance.

Twenty requests in flight for 60 seconds, first on /health and then on the
public pricing endpoint. Stops early if errors pile up.
"""

import asyncio
import json
import os
import time

import pytest


async def _sustain(base_url: str, path: str, seconds: float = 60, concurrency: int = 20):
    import httpx

    deadline = time.perf_counter() + seconds
    latencies = []
    statuses = []
    errors = []
    stop = False

    async def one(client):
        nonlocal stop
        started = time.perf_counter()
        try:
            response = await client.get(path)
        except Exception as exc:
            errors.append(type(exc).__name__)
            return
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        statuses.append(response.status_code)
        if response.status_code >= 500:
            errors.append(str(response.status_code))
        sample = len(statuses) + len(errors)
        if sample >= 30 and len(errors) / sample > 0.25:
            stop = True

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        inflight = set()
        while time.perf_counter() < deadline and not stop:
            while len(inflight) < concurrency and time.perf_counter() < deadline and not stop:
                task = asyncio.create_task(one(client))
                inflight.add(task)
            if not inflight:
                break
            done, inflight = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)

    ordered = sorted(latencies)

    def pct(p):
        if not ordered:
            return None
        index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
        return round(ordered[index] * 1000, 1)

    return {
        "path": path,
        "requests": len(statuses) + len(errors),
        "ok": sum(1 for status in statuses if status == 200),
        "statuses": {str(code): statuses.count(code) for code in sorted(set(statuses))},
        "errors": len(errors),
        "error_samples": errors[:5],
        "stopped_early": stop,
        "elapsed_s": seconds,
        "latency_ms": {
            "min": round(ordered[0] * 1000, 1) if ordered else None,
            "p50": pct(50),
            "p95": pct(95),
            "p99": pct(99),
            "max": round(ordered[-1] * 1000, 1) if ordered else None,
        },
    }


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.asyncio
async def test_gentle_load(live_base_url):
    health = await _sustain(live_base_url, "/health")
    pricing = await _sustain(live_base_url, "/api/payments/pricing")
    report = {"health": health, "pricing": pricing}
    path = os.environ.get("LIVE_REPORT_PATH", "/tmp/watchman-live-load.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps(report))
    for name, result in report.items():
        assert result["requests"] > 0, name
        assert not result["stopped_early"], result
        assert result["errors"] / result["requests"] < 0.1, result
        assert result["ok"] / result["requests"] >= 0.9, result
