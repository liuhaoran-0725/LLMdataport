"""Page timings and an adaptive, explicitly approximate remaining-time range."""
from __future__ import annotations

import math
from statistics import median
import time
from typing import Any


def record_progress(job: dict[str, Any], event: dict[str, Any], *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    timing = dict(job.get("runtime_timing") or {})
    phase = event.get("phase", timing.get("phase", "preparing"))
    timing["phase"] = phase
    if event.get("total_pages") is not None:
        timing["total_pages"] = int(event["total_pages"])
    if event.get("event") == "phase_started":
        timing["current_page"] = None
        timing["page_started_at"] = None
    elif event.get("event") == "page_started":
        timing["current_page"] = event.get("logical_page")
        timing["page_started_at"] = now
    elif event.get("event") == "page_finished":
        pages_key = f"{phase}_pages"
        pages = list(timing.get(pages_key) or [])
        page = event.get("logical_page")
        if page not in pages:
            pages.append(page)
            timing[pages_key] = pages
            duration = float(event.get("duration_seconds") or 0)
            if not event.get("cached") and event.get("success", True) and math.isfinite(duration) and duration > 0:
                samples_key = f"{phase}_samples"
                timing[samples_key] = [*(timing.get(samples_key) or []), duration]
        timing["current_page"] = None
        timing["page_started_at"] = None
    job["runtime_timing"] = timing


def _duration_range(samples: list[float]) -> tuple[float, float, float]:
    recent = samples[-5:]
    adaptive = recent[0]
    for duration in recent[1:]:
        adaptive = 0.4 * duration + 0.6 * adaptive
    central = 0.6 * adaptive + 0.4 * median(samples)
    ordered = sorted(recent)
    # These are descriptive bounds with a margin, not statistical confidence intervals.
    margin = 0.4 if len(samples) < 5 else 0.25
    low = min(central * (1 - margin), ordered[0])
    high = max(central * (1 + margin), ordered[-1])
    return max(0.1, low), central, high


def estimate_remaining(
    timing: dict[str, Any], *, total_pages: int, enable_check: bool,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    result: dict[str, Any] = {
        "eta_seconds": None, "eta_min_seconds": None, "eta_max_seconds": None,
        "eta_state": "collecting", "eta_scope": "total",
    }
    phase = timing.get("phase")
    if phase not in {"extraction", "checking"}:
        result["eta_state"] = "finalizing" if phase == "finalizing" else "preparing"
        return result
    completed = len(timing.get(f"{phase}_pages") or [])
    remaining = max(0, total_pages - completed)
    if remaining == 0:
        result["eta_state"] = "collecting" if phase == "extraction" and enable_check else "finalizing"
        return result
    samples = [float(v) for v in timing.get(f"{phase}_samples", []) if math.isfinite(float(v)) and float(v) > 0]
    if len(samples) < 2:
        return result
    low, central, high = _duration_range(samples)
    active = timing.get("current_page") is not None and timing.get("page_started_at") is not None
    age = max(0.0, now - float(timing["page_started_at"])) if active else 0.0
    # Never run a countdown to zero while the external API is still taking longer than observed pages.
    if active and age >= high:
        result["eta_state"] = "delayed"
        return result
    future_pages = remaining - (1 if active else 0)
    current_low = max(0.0, low - age) if active else 0
    current_high = max(0.0, high - age) if active else 0
    current_central = max(0.0, central - age) if active else 0
    result.update({
        "eta_seconds": max(1, math.ceil(future_pages * central + current_central)),
        "eta_min_seconds": max(1, math.ceil(future_pages * low + current_low)),
        "eta_max_seconds": max(1, math.ceil(future_pages * high + current_high)),
        "eta_state": "estimated",
        "eta_scope": "extraction" if phase == "extraction" and enable_check else "total",
    })
    return result
