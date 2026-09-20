#!/usr/bin/env python3
"""Capture parsing, metrics, baselines and host command construction for
`dev.py perf`.

Deliberately free of subprocess calls and of every filesystem write, so each
metric can be unit tested without a build, a ROM, or a 20 second render. The
only I/O here is reading one capture file.
"""

from __future__ import annotations

import csv
import pathlib
import statistics
from typing import NamedTuple


class Block(NamedTuple):
    sample: int
    frames: int
    late_seconds: float
    render_seconds: float


def read_blocks(csv_path: pathlib.Path) -> list[Block]:
    """Parse latency_host's per-callback CSV.

    The sibling JSON capture carries run metadata only; the per-block timings
    live here. render_ms is the plug-in's own render duration. late_ms is
    scheduler arrival and is kept separate throughout: OS wake-up jitter is
    not a plug-in cost.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(f"latency_host produced no {csv_path}")

    with csv_path.open(newline="", encoding="utf-8") as handle:
        return [
            Block(
                int(row["sample"]),
                int(row["count"]),
                float(row["late_ms"]) / 1000.0,
                float(row["render_ms"]) / 1000.0,
            )
            for row in csv.DictReader(handle)
            if row.get("render_ms")
        ]


def summarize(blocks: list[Block], rate: int, block_frames: int,
              warmup_seconds: float) -> dict:
    """Split a capture at the warm-up boundary and reduce each part.

    The opening seconds carry firmware boot and DSP JIT. Folding them into the
    totals made the overrun count useless -- three repeats of one identical
    build configuration produced 55, 86 and 108 -- so they are summarized
    separately and never merged into the headline figures.
    """
    budget = block_frames / rate
    skip = warmup_seconds * rate
    steady = [b for b in blocks if b.sample >= skip]
    startup = [b for b in blocks if b.sample < skip]

    result: dict = {
        "warmupSeconds": warmup_seconds,
        "callbacks": len(steady),
        "startupCallbacks": len(startup),
    }

    if steady:
        loads = sorted(b.render_seconds / budget for b in steady)
        audio = sum(b.frames for b in steady) / rate
        render = sum(b.render_seconds for b in steady)
        result.update({
            "audioSeconds": audio,
            "renderSeconds": render,
            # Wall clock includes process start, plug-in instantiation and
            # firmware boot -- about 2% of a 20s run on an M3 Max. This ratio
            # counts only time spent inside the render callback, so it is the
            # figure that moves when emulation itself gets faster.
            "xRenderOnly": audio / render if render > 0 else 0.0,
            "loadP50": statistics.median(loads),
            "loadP99": loads[min(len(loads) - 1, int(len(loads) * 0.99))],
            "loadMax": loads[-1],
            "overruns": sum(1 for load in loads if load > 1.0),
        })

    if startup:
        startup_loads = [b.render_seconds / budget for b in startup]
        result.update({
            "startupOverruns": sum(1 for load in startup_loads if load > 1.0),
            "startupLoadMax": max(startup_loads),
        })

    return result


# Every axis that makes two runs incomparable. A delta across different rates,
# block sizes or scenarios is not a regression signal, it is a category error.
CONFIG_KEYS = ("product", "mode", "scenario", "rate", "block", "seconds",
               "warmupSeconds")


def baseline_name(product: str, mode: str, scenario: str, rate: int,
                  block_frames: int) -> str:
    return f"{product}-{mode}-{scenario}-{rate}-{block_frames}.json"


def config_mismatch(report: dict, baseline: dict) -> list[str]:
    """Names of configuration fields that differ between two reports.

    A field absent from either side counts as differing, so baselines written
    before an axis existed are rejected instead of silently matching.
    """
    return [key for key in CONFIG_KEYS
            if report.get(key) != baseline.get(key)]


# The figure each mode is compared on. Throughput rises when things improve;
# callback load falls.
HEADLINE_KEY = {"throughput": "xRenderOnly", "paced": "loadP50"}


def headline_values(samples: list[dict], mode: str) -> list[float]:
    key = HEADLINE_KEY[mode]
    return [s[key] for s in samples if key in s]


def spread(values: list[float]) -> float:
    """Range relative to the median, as a fraction.

    A comparison cannot resolve a tolerance smaller than the run-to-run
    spread, so the caller refuses instead of reporting a confident delta on
    noise.
    """
    if not values:
        return 0.0
    median = statistics.median(values)
    if median == 0:
        return 0.0
    return (max(values) - min(values)) / median


# Report-level keys, one per mode, and which direction counts as worse.
HEADLINE_MEDIAN_KEY = {"throughput": "xRenderOnlyMedian", "paced": "loadP50Median"}
LOWER_IS_BETTER = {"throughput": False, "paced": True}


def compare(report: dict, baseline: dict, tolerance: float) -> tuple[bool, str]:
    """Compare one report against a baseline on that mode's headline figure.

    Only moves in the worse direction fail: getting faster is never a
    regression. Returns (passed, human readable message).
    """
    mode = report["mode"]
    key = HEADLINE_MEDIAN_KEY[mode]
    new, old = report.get(key), baseline.get(key)
    if new is None or old is None or old == 0:
        return False, f"cannot compare: {key} missing or zero in report or baseline"

    delta = (new - old) / old
    worse = delta > tolerance if LOWER_IS_BETTER[mode] else delta < -tolerance
    return (not worse), f"{key} {old:.4f} -> {new:.4f} ({delta:+.1%})"
