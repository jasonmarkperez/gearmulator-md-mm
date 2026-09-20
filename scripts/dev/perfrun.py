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
