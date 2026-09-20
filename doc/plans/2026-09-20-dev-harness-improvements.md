# Dev Harness Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `scripts/dev/dev.py perf` produce numbers that can be trusted and compared, widen it beyond its single hardcoded workload, and give the standalone driver an actual scenario library instead of manual one-off MCP calls.

**Architecture:** Extract the perf subsystem's pure logic (capture parsing, metrics, baseline naming, comparison, host command construction) out of `dev.py` into `scripts/dev/perfrun.py` so it can be unit tested without a build, a ROM, or a 20-second render. Then fix three measurement defects on top of that testable base, expose the workload axes `latency_host` already supports, and add a small scenario module driven over MCP. Python 3 stdlib and `unittest` throughout, matching `scripts/macos/test_check_mdmm_core_capacity.py`.

**Tech Stack:** Python 3 (stdlib only), `unittest`, CMake/Ninja Multi-Config, the existing `latency_host` VST3 render host, the embedded MCP server.

**Spec:** No separate spec document. This plan derives from defects measured in the current harness during the session that produced commits `a2a26a2b`, `04bd48e2`, `a0f87f3d` and `2a713f83`. The behaviour being changed is documented in `doc/dev_workflow.md`; every claimed number below was measured on this machine and is reproduced in the task that depends on it.

## Global Constraints

- Python: 3.x **stdlib only**. No third-party packages. 4-space indent (Python files), matching `scripts/macos/*.py`.
- Tests: stdlib `unittest`, file named `test_<module>.py` beside the module, runnable directly as `python3 scripts/dev/test_perfrun.py`. This is the existing convention (`scripts/macos/test_check_mdmm_core_capacity.py`, invoked that way at `.github/workflows/elektron-macos.yml:66`).
- `perf` remains **Release-only**. `base.cmake` applies `-Ofast -funroll-loops` only to `Release`, and `GEARMULATOR_MDMM_APPLE_THINLTO` / `_OPTIMIZE_DSP` are Release-only. Timings from any other configuration are meaningless.
- `latency_host` rejects `seconds < 20` (`source/pluginTester/latency/latency_host.cpp:76`), requires an explicit `.vst3` (line 81), requires `GEARMULATOR_DATA_ROOT` to be set (line 66), requires `0 <= phase < block`, and refuses to overwrite an existing output prefix.
- Do **not** modify release tooling: `scripts/macos/build_mdmm.sh`, `check_mdmm_core_capacity.py`, `write_mdmm_receipt.py`, `verify_mdmm_package.sh` are out of scope.
- Do **not** commit ROMs. `/roms/` is gitignored; keep it that way.
- Performance baselines are machine-specific. `--check` stays a local gate, never a CI gate.
- Commit after every task. No `Co-authored-by` trailers.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/dev/perfrun.py` | **Create.** Pure perf logic: capture parsing, metric reduction, baseline naming and compatibility, repeat-spread, comparison, host command construction. No subprocess, no writes; the only I/O is reading one capture file. |
| `scripts/dev/test_perfrun.py` | **Create.** `unittest` suite for the above, using synthetic CSV fixtures. |
| `scripts/dev/scenarios.py` | **Create.** Named standalone scenarios driven over MCP. Each raises `AssertionError` on failure. |
| `scripts/dev/dev.py` | **Modify.** Delegate perf logic to `perfrun`; add `--scenario`, configuration-keyed baselines, spread guard, `ui` subcommand. |
| `scripts/dev/baselines/*.json` | **Replace.** Re-recorded under the new metric and naming scheme. |
| `doc/dev_workflow.md` | **Modify.** Document the new metric, the pacing caveat, the scenario library. |
| `.github/workflows/mdmm-core.yml` | **Modify.** Run the Python unit tests. |

Why extract: the metric code currently lives inside `cmd_perf`, interleaved with `subprocess.run` and a 20-second render, so none of it can be tested. Extraction is what makes every subsequent task verifiable in milliseconds instead of minutes.

---

### Task 1: Extract the perf subsystem into a testable module

No behaviour change. This task must leave `dev.py perf` producing the same numbers it produces today.

**Files:**
- Create: `scripts/dev/perfrun.py`
- Create: `scripts/dev/test_perfrun.py`
- Modify: `scripts/dev/dev.py` (remove `_render_seconds` and the inline metric block in `cmd_perf`; leave `_compare` alone, Task 4 replaces it)

**Interfaces:**
- Produces: `perfrun.Block`, `perfrun.read_blocks(csv_path) -> list[Block]`, `perfrun.summarize(blocks, rate, block_frames, warmup_seconds) -> dict`. Later tasks add `baseline_name`, `config_mismatch`, `spread`, `compare`, `host_command` to this module.

- [ ] **Step 1: Write the failing test**

Create `scripts/dev/test_perfrun.py`:

```python
#!/usr/bin/env python3

from __future__ import annotations

import csv
import pathlib
import tempfile
import unittest

import perfrun


class BlocksFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)

    def write_blocks(self, render_ms: list[float], frames: int = 100) -> pathlib.Path:
        """Write a capture in latency_host's blocks CSV format."""
        path = self.root / "capture.blocks.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                ["sample", "count", "late_ms", "render_ms", "reported_latency_samples"]
            )
            for index, duration in enumerate(render_ms):
                writer.writerow([index * frames, frames, 0.25, duration, 32])
        return path


class ReadBlocksTest(BlocksFixture):
    def test_rows_are_parsed_with_milliseconds_converted_to_seconds(self) -> None:
        blocks = perfrun.read_blocks(self.write_blocks([1.0, 2.0]))
        self.assertEqual([b.sample for b in blocks], [0, 100])
        self.assertEqual([b.frames for b in blocks], [100, 100])
        self.assertAlmostEqual(blocks[0].render_seconds, 0.001)
        self.assertAlmostEqual(blocks[1].render_seconds, 0.002)

    def test_scheduler_lateness_is_kept_separate_from_render_duration(self) -> None:
        blocks = perfrun.read_blocks(self.write_blocks([1.0]))
        self.assertAlmostEqual(blocks[0].late_seconds, 0.00025)
        self.assertAlmostEqual(blocks[0].render_seconds, 0.001)

    def test_missing_capture_is_reported_by_path(self) -> None:
        with self.assertRaises(FileNotFoundError):
            perfrun.read_blocks(self.root / "absent.blocks.csv")


class SummarizeTest(BlocksFixture):
    # rate 1000 / 100 frames gives a 0.1s budget per callback, so loads are
    # readable by inspection: a 50ms render is exactly half the budget.
    def summary(self, render_ms: list[float], warmup: float):
        blocks = perfrun.read_blocks(self.write_blocks(render_ms))
        return perfrun.summarize(blocks, rate=1000, block_frames=100,
                                 warmup_seconds=warmup)

    def test_warmup_window_is_excluded_from_the_steady_figures(self) -> None:
        # Five expensive callbacks then five cheap ones; warm-up covers the
        # first five (samples 0..400).
        summary = self.summary([200.0] * 5 + [50.0] * 5, warmup=0.5)
        self.assertEqual(summary["callbacks"], 5)
        self.assertEqual(summary["startupCallbacks"], 5)
        self.assertAlmostEqual(summary["loadP50"], 0.5)
        self.assertAlmostEqual(summary["loadMax"], 0.5)
        self.assertEqual(summary["overruns"], 0)

    def test_warmup_window_is_reported_separately_never_merged(self) -> None:
        summary = self.summary([200.0] * 5 + [50.0] * 5, warmup=0.5)
        self.assertEqual(summary["startupOverruns"], 5)
        self.assertAlmostEqual(summary["startupLoadMax"], 2.0)

    def test_a_callback_exactly_on_budget_is_not_an_overrun(self) -> None:
        summary = self.summary([100.0, 100.0], warmup=0.0)
        self.assertEqual(summary["overruns"], 0)
        summary = self.summary([100.1, 100.1], warmup=0.0)
        self.assertEqual(summary["overruns"], 2)

    def test_steady_audio_and_render_totals_cover_only_the_steady_window(self) -> None:
        summary = self.summary([200.0] * 5 + [50.0] * 5, warmup=0.5)
        self.assertAlmostEqual(summary["audioSeconds"], 0.5)
        self.assertAlmostEqual(summary["renderSeconds"], 0.25)

    def test_an_all_warmup_capture_yields_no_steady_figures(self) -> None:
        summary = self.summary([50.0, 50.0], warmup=99.0)
        self.assertEqual(summary["callbacks"], 0)
        self.assertNotIn("loadP50", summary)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'perfrun'`

- [ ] **Step 3: Write the module**

Create `scripts/dev/perfrun.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Delete the superseded code from dev.py**

In `scripts/dev/dev.py`, delete the whole `_render_seconds` function, and replace the per-repeat metric block inside `cmd_perf` (currently the `try: renders = _render_seconds(prefix)` block through the `startupLoadMax` assignment) with:

```python
        try:
            blocks = perfrun.read_blocks(
                prefix.with_name(prefix.name + ".blocks.csv"))
        except FileNotFoundError as e:
            print(f"warning: {e}", file=sys.stderr)
            blocks = []

        record.update(perfrun.summarize(blocks, args.rate, args.block, args.warmup))
```

Add the import next to the existing `mcpclient` import near the top of `dev.py`:

```python
from mcpclient import McpClient, McpError, read_instances  # noqa: E402
import perfrun  # noqa: E402
```

Also delete the two now-dead locals just above that block, `budget = args.block / args.rate` and `skip_samples = int(args.warmup * args.rate)`; `summarize` computes both internally. Delete the now-unused `import csv` line if nothing else in `dev.py` uses it (check with `grep -n 'csv\.' scripts/dev/dev.py`).

- [ ] **Step 6: Verify dev.py still runs and reports the same shape**

Run: `python3 scripts/dev/dev.py perf md --mode paced --repeats 1`
Expected: completes, prints a `repeat 0:` record containing `loadP50`, `loadP99`, `loadMax`, `overruns`, `startupOverruns`, `startupLoadMax`, and a `MD paced:` headline. `loadMax` should be roughly 1.7-1.8 (the lock-wait defect was fixed in `2a713f83`); anything near 57 means a regression, not a harness problem.

- [ ] **Step 7: Commit**

```bash
git add scripts/dev/perfrun.py scripts/dev/test_perfrun.py scripts/dev/dev.py
git commit -m "Extract perf metrics into a unit-testable module"
```

---

### Task 2: Report render-only throughput

`xRealtime` divides audio seconds by **wall clock**, which includes process launch, plug-in instantiation and firmware boot. Measured on this machine from captures already on disk: wall-clock `xRealtime` reads 1.80x while the same runs spend only 10.87s of 20s inside the render callback, i.e. **1.86x** over all callbacks. The fixed startup offset is roughly 2-3% — small, but it sits inside the 5% comparison tolerance and dilutes every delta.

`xRenderOnly` is computed over the **steady window only**, consistently with every other metric here, and that is not the same number. On MD the excluded warm-up window is *cheaper* per callback than the steady window — 1.95x against 1.80x — because the scenario's notes fire at 10.0s, 13.1s and 16.3s, all after the 8s warm-up boundary, so the warm-up window has no voices playing. Steady-only render-only therefore lands near **1.80x**, which can sit marginally *below* wall clock on MD. That is a property of the workload, not a wiring error: wall clock blends the cheap boot phase in, and the steady figure deliberately does not.

A second finding this exposes, which must be documented rather than hidden: the identical build renders at **1.84x unpaced but 1.61x render-only when paced**. Idling between callbacks costs cache warmth and clock boost. Throughput mode is therefore a *relative* A/B instrument; it overstates real-time capability, and paced p50 remains the number that describes headroom.

**Files:**
- Modify: `scripts/dev/perfrun.py`
- Modify: `scripts/dev/test_perfrun.py`
- Modify: `scripts/dev/dev.py` (report aggregation and headline)

**Interfaces:**
- Consumes: `perfrun.summarize` from Task 1.
- Produces: `summarize` gains an `xRenderOnly` key; reports gain `xRenderOnlyMedian`.

- [ ] **Step 1: Write the failing test**

Append to `SummarizeTest` in `scripts/dev/test_perfrun.py`:

```python
    def test_render_only_throughput_ignores_everything_outside_the_callback(self) -> None:
        # Ten callbacks, 50ms render each against a 0.1s budget: the renderer
        # is exactly twice real time regardless of how long the process lived.
        summary = self.summary([50.0] * 10, warmup=0.0)
        self.assertAlmostEqual(summary["xRenderOnly"], 2.0)

    def test_render_only_throughput_covers_the_steady_window_only(self) -> None:
        # Warm-up callbacks are four times as expensive; excluding them must
        # raise the ratio from 0.8x to 2.0x.
        summary = self.summary([200.0] * 5 + [50.0] * 5, warmup=0.5)
        self.assertAlmostEqual(summary["xRenderOnly"], 2.0)

    def test_render_only_throughput_is_absent_without_steady_callbacks(self) -> None:
        summary = self.summary([50.0], warmup=99.0)
        self.assertNotIn("xRenderOnly", summary)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: FAIL with `KeyError: 'xRenderOnly'`

- [ ] **Step 3: Add the metric**

In `scripts/dev/perfrun.py`, inside `summarize`'s `if steady:` block, add to the `result.update({...})` call, immediately after `"renderSeconds": render,`:

```python
            # Wall clock includes process start, plug-in instantiation and
            # firmware boot -- about 2% of a 20s run on an M3 Max. This ratio
            # counts only time spent inside the render callback, so it is the
            # figure that moves when emulation itself gets faster.
            "xRenderOnly": audio / render if render > 0 else 0.0,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Make it the throughput headline in dev.py**

In `cmd_perf`, replace the `if args.mode == "throughput":` aggregation branch with:

```python
    if args.mode == "throughput":
        renders = [s["xRenderOnly"] for s in samples if "xRenderOnly" in s]
        walls = [s["xRealtime"] for s in samples]
        report["xRenderOnlyMedian"] = statistics.median(renders) if renders else None
        report["xRealtimeMedian"] = statistics.median(walls)
        headline = (f"render-only {report['xRenderOnlyMedian']:.3f}x "
                    f"(wall {report['xRealtimeMedian']:.3f}x, includes startup)"
                    if renders else "no callback timings in capture")
```

Leave `xRealtime` in each sample record. It is a useful cross-check: if wall and render-only ever diverge by much more than a few percent, startup cost has changed and that is itself worth knowing.

- [ ] **Step 6: Verify against the machine**

Run: `python3 scripts/dev/dev.py perf md --mode throughput --repeats 3`
Expected: `render-only` and `wall` both between roughly **1.75x and 1.86x**. Do **not** require one to exceed the other: `xRenderOnly` covers the steady window while wall clock blends in the cheaper warm-up phase, so on MD render-only legitimately reads slightly below wall. The real check is that both figures print, that render-only is stable across repeats, and that it tracks the steady window — cross-check by confirming `xRenderOnly` for a single repeat is close to that repeat's `audioSeconds / renderSeconds`.

- [ ] **Step 7: Commit**

```bash
git add scripts/dev/perfrun.py scripts/dev/test_perfrun.py scripts/dev/dev.py
git commit -m "Report render-only throughput instead of wall clock"
```

---

### Task 3: Key baselines by configuration and refuse mismatched comparisons

`dev.py` currently writes `baseline_file = BASELINES / f"{key}-{args.mode}.json"`. Rate, block size, scenario and warm-up are not in the name and are not checked, so `perf md --mode paced --block 512 --check` silently compares against a 128-sample baseline and prints a confident, meaningless delta.

**Files:**
- Modify: `scripts/dev/perfrun.py`
- Modify: `scripts/dev/test_perfrun.py`
- Modify: `scripts/dev/dev.py`
- Delete: `scripts/dev/baselines/md-throughput.json`, `md-paced.json`, `mm-throughput.json`, `mm-paced.json`

The four existing baselines are deleted rather than renamed: Task 2 changed the headline metric, so their contents are stale regardless of filename. Re-recording costs about two minutes per product.

**Interfaces:**
- Produces: `perfrun.baseline_name(product, mode, scenario, rate, block_frames) -> str`, `perfrun.config_mismatch(report, baseline) -> list[str]`, `perfrun.CONFIG_KEYS`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/dev/test_perfrun.py`, before the `if __name__` block:

```python
class BaselineIdentityTest(unittest.TestCase):
    def test_baseline_name_distinguishes_every_workload_axis(self) -> None:
        self.assertEqual(
            perfrun.baseline_name("md", "paced", "notes", 48000, 128),
            "md-paced-notes-48000-128.json")
        self.assertNotEqual(
            perfrun.baseline_name("md", "paced", "notes", 48000, 128),
            perfrun.baseline_name("md", "paced", "notes", 48000, 512))
        self.assertNotEqual(
            perfrun.baseline_name("md", "paced", "notes", 48000, 128),
            perfrun.baseline_name("md", "paced", "chords", 48000, 128))

    def config(self, **overrides) -> dict:
        base = {"product": "MD", "mode": "paced", "scenario": "notes",
                "rate": 48000, "block": 128, "warmupSeconds": 8.0}
        base.update(overrides)
        return base

    def test_identical_configurations_compare_cleanly(self) -> None:
        self.assertEqual(
            perfrun.config_mismatch(self.config(), self.config()), [])

    def test_a_different_block_size_is_refused(self) -> None:
        self.assertEqual(
            perfrun.config_mismatch(self.config(), self.config(block=512)),
            ["block"])

    def test_every_differing_axis_is_named(self) -> None:
        mismatch = perfrun.config_mismatch(
            self.config(), self.config(rate=96000, scenario="chords"))
        self.assertEqual(sorted(mismatch), ["rate", "scenario"])

    def test_a_baseline_missing_an_axis_counts_as_a_mismatch(self) -> None:
        # Pre-Task-3 baselines have no scenario field; they must be rejected
        # rather than silently treated as matching.
        legacy = self.config()
        del legacy["scenario"]
        self.assertEqual(
            perfrun.config_mismatch(self.config(), legacy), ["scenario"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: FAIL with `AttributeError: module 'perfrun' has no attribute 'baseline_name'`

- [ ] **Step 3: Implement**

Append to `scripts/dev/perfrun.py`:

```python
# Every axis that makes two runs incomparable. A delta across different rates,
# block sizes or scenarios is not a regression signal, it is a category error.
CONFIG_KEYS = ("product", "mode", "scenario", "rate", "block", "warmupSeconds")


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: PASS, 16 tests

- [ ] **Step 5: Wire into dev.py**

In `cmd_perf`, add `"product"` and `"scenario"` to the `report` dict (`product` already present as `cfg["model"]`; add `"scenario": args.scenario` — Task 5 adds the argument, so for now hardcode `"notes"` and Task 5 replaces it).

Replace the baseline resolution and check:

```python
    baseline_file = BASELINES / perfrun.baseline_name(
        key, args.mode, report["scenario"], args.rate, args.block)
    if args.save_baseline:
        BASELINES.mkdir(parents=True, exist_ok=True)
        baseline_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"saved baseline {baseline_file}")
        return 0

    if args.check:
        if not baseline_file.is_file():
            fail(f"no baseline at {baseline_file}; record one with --save-baseline")
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
        mismatch = perfrun.config_mismatch(report, baseline)
        if mismatch:
            fail(f"baseline {baseline_file.name} was recorded with a different "
                 f"{', '.join(mismatch)}; re-record it with --save-baseline")
        return _compare(report, baseline, args.tolerance, args.mode)
```

- [ ] **Step 6: Delete the stale baselines and re-record**

```bash
git rm scripts/dev/baselines/md-throughput.json scripts/dev/baselines/md-paced.json \
       scripts/dev/baselines/mm-throughput.json scripts/dev/baselines/mm-paced.json
python3 scripts/dev/dev.py perf md --mode throughput --repeats 3 --save-baseline
python3 scripts/dev/dev.py perf md --mode paced --repeats 3 --save-baseline
python3 scripts/dev/dev.py perf mm --mode throughput --repeats 3 --save-baseline
python3 scripts/dev/dev.py perf mm --mode paced --repeats 3 --save-baseline
```

Expected new files: `md-throughput-notes-48000-128.json` and three siblings.

- [ ] **Step 7: Verify the guard actually refuses**

Run: `python3 scripts/dev/dev.py perf md --mode paced --block 512 --repeats 1 --check`
Expected: exits non-zero with `no baseline at .../md-paced-notes-48000-512.json`, **not** a comparison against the 128 baseline.

Run: `python3 scripts/dev/dev.py perf md --mode paced --repeats 2 --check`
Expected: a real comparison, `within tolerance`.

- [ ] **Step 8: Commit**

```bash
git add -A scripts/dev
git commit -m "Key perf baselines by workload configuration"
```

---

### Task 4: Move the comparison into the tested module and guard it with a noise floor

`--check` currently compares medians without asking whether the run was stable enough for the comparison to mean anything. If three repeats span 8% and the tolerance is 5%, the verdict is noise. Measured spreads on an idle M3 Max are about 1%, so a 3% default leaves ample headroom while catching a run taken on a busy or thermally throttled machine.

This task also moves the comparison itself out of `dev._compare` and into `perfrun`, because the one thing it encodes — which direction counts as worse, per mode — is exactly where a sign error would silently pass every regression, and it is currently untested.

**Files:**
- Modify: `scripts/dev/perfrun.py`
- Modify: `scripts/dev/test_perfrun.py`
- Modify: `scripts/dev/dev.py`

**Interfaces:**
- Produces: `perfrun.spread(values) -> float`, `perfrun.HEADLINE_KEY`, `perfrun.headline_values(samples, mode) -> list[float]`, `perfrun.HEADLINE_MEDIAN_KEY`, `perfrun.LOWER_IS_BETTER`, `perfrun.compare(report, baseline, tolerance) -> tuple[bool, str]`. Removes `dev._compare`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/dev/test_perfrun.py`:

```python
class SpreadTest(unittest.TestCase):
    def test_spread_is_the_range_relative_to_the_median(self) -> None:
        self.assertAlmostEqual(perfrun.spread([1.0, 1.0, 1.0]), 0.0)
        self.assertAlmostEqual(perfrun.spread([0.9, 1.0, 1.1]), 0.2)

    def test_spread_of_a_single_repeat_is_zero(self) -> None:
        self.assertAlmostEqual(perfrun.spread([1.7]), 0.0)

    def test_spread_is_defined_for_an_empty_or_zero_series(self) -> None:
        self.assertAlmostEqual(perfrun.spread([]), 0.0)
        self.assertAlmostEqual(perfrun.spread([0.0, 0.0]), 0.0)


class HeadlineTest(unittest.TestCase):
    def test_throughput_headline_reads_render_only_from_each_repeat(self) -> None:
        samples = [{"xRenderOnly": 1.8}, {"xRenderOnly": 1.9}]
        self.assertEqual(
            perfrun.headline_values(samples, "throughput"), [1.8, 1.9])

    def test_paced_headline_reads_the_median_load(self) -> None:
        samples = [{"loadP50": 0.6}, {"loadP50": 0.62}]
        self.assertEqual(perfrun.headline_values(samples, "paced"), [0.6, 0.62])

    def test_repeats_without_timings_are_skipped_not_counted_as_zero(self) -> None:
        samples = [{"loadP50": 0.6}, {}]
        self.assertEqual(perfrun.headline_values(samples, "paced"), [0.6])


class CompareTest(unittest.TestCase):
    def report(self, mode: str, value: float) -> dict:
        key = perfrun.HEADLINE_MEDIAN_KEY[mode]
        return {"mode": mode, key: value}

    def test_throughput_getting_slower_is_a_regression(self) -> None:
        ok, message = perfrun.compare(
            self.report("throughput", 1.60), self.report("throughput", 1.80), 0.05)
        self.assertFalse(ok)
        self.assertIn("-11.1%", message)

    def test_throughput_getting_faster_is_never_a_regression(self) -> None:
        ok, _ = perfrun.compare(
            self.report("throughput", 2.20), self.report("throughput", 1.80), 0.05)
        self.assertTrue(ok)

    def test_load_going_up_is_a_regression(self) -> None:
        ok, _ = perfrun.compare(
            self.report("paced", 0.70), self.report("paced", 0.60), 0.05)
        self.assertFalse(ok)

    def test_load_going_down_is_never_a_regression(self) -> None:
        ok, _ = perfrun.compare(
            self.report("paced", 0.50), self.report("paced", 0.60), 0.05)
        self.assertTrue(ok)

    def test_a_change_inside_the_tolerance_passes_in_both_directions(self) -> None:
        for mode, old, new in (("throughput", 1.80, 1.78), ("paced", 0.60, 0.61)):
            ok, _ = perfrun.compare(
                self.report(mode, new), self.report(mode, old), 0.05)
            self.assertTrue(ok, f"{mode} {old} -> {new} should pass")

    def test_a_missing_headline_is_reported_rather_than_crashing(self) -> None:
        ok, message = perfrun.compare({"mode": "paced"}, self.report("paced", 0.6), 0.05)
        self.assertFalse(ok)
        self.assertIn("loadP50Median", message)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: FAIL with `AttributeError: module 'perfrun' has no attribute 'spread'`

- [ ] **Step 3: Implement**

Append to `scripts/dev/perfrun.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: PASS, 28 tests

- [ ] **Step 5: Wire into dev.py**

In `cmd_perf`, after the mode-specific aggregation, add:

```python
    report["spread"] = perfrun.spread(perfrun.headline_values(samples, args.mode))
    print(f"\n{cfg['model']} {args.mode}: {headline} "
          f"[spread {report['spread']:.1%} over {len(samples)} repeats]")
```

and delete the previous `print(f"\n{cfg['model']} {args.mode}: {headline}")` line.

Replace the `_compare` call in the `if args.check:` branch, and add the spread guard before it:

```python
        if report["spread"] > args.max_spread:
            fail(f"repeats spread {report['spread']:.1%}, above the "
                 f"{args.max_spread:.0%} limit: this run cannot resolve a "
                 f"{args.tolerance:.0%} tolerance. Re-run on an idle machine.")
        passed, message = perfrun.compare(report, baseline, args.tolerance)
        print(message)
        if not passed:
            print(f"REGRESSION: exceeds {args.tolerance:.0%} tolerance")
            return 1
        print("within tolerance")
        return 0
```

Then delete the whole `_compare` function from `dev.py`; `perfrun.compare` replaces it and is the tested version.

Add the argument beside `--tolerance`:

```python
    sp.add_argument("--max-spread", type=float, default=0.03,
                    help="refuse to compare when repeats vary more than this")
```

- [ ] **Step 6: Verify on the machine**

Run: `python3 scripts/dev/dev.py perf md --mode throughput --repeats 3 --check`
Expected: a spread around **1%** and `within tolerance`.

Run: `python3 scripts/dev/dev.py perf md --mode throughput --repeats 3 --check --max-spread 0.0001`
Expected: exits non-zero with the "cannot resolve" message. This proves the guard fires.

- [ ] **Step 7: Commit**

```bash
git add scripts/dev/perfrun.py scripts/dev/test_perfrun.py scripts/dev/dev.py
git commit -m "Refuse perf comparisons when repeats are too noisy"
```

---

### Task 5: Expose the workload axes latency_host already supports

Everything measured so far used one workload: three isolated notes at 48 kHz with 128-sample blocks. `latency_host` also accepts `chords`, `input` and `transport`, and any rate and block size. Nothing exercises dense polyphony, the audio input path, or transport handling. Building the argument vector is also currently inline and untested, which is how the `seconds < 20` rejection got discovered at runtime rather than at call time.

Note `input` changes the bus layout: `latency_host.cpp:95` calls `setPlayConfigDetails(input ? 2 : 0, 2, ...)`, so that scenario opens two input channels.

**Files:**
- Modify: `scripts/dev/perfrun.py`
- Modify: `scripts/dev/test_perfrun.py`
- Modify: `scripts/dev/dev.py`

**Interfaces:**
- Produces: `perfrun.SCENARIOS`, `perfrun.MIN_SECONDS`, `perfrun.host_command(host, plugin, prefix, mode, scenario, rate, block_frames, seconds, note) -> list[str]`, `perfrun.validate_run(scenario, rate, block_frames, seconds) -> str | None`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/dev/test_perfrun.py`:

```python
class HostCommandTest(unittest.TestCase):
    def command(self, **overrides) -> list[str]:
        kwargs = {"host": "/bin/host", "plugin": "/tmp/MD.vst3",
                  "prefix": "/tmp/cap", "mode": "throughput",
                  "scenario": "notes", "rate": 48000, "block_frames": 128,
                  "seconds": 20, "note": 36}
        kwargs.update(overrides)
        return perfrun.host_command(**kwargs)

    def test_positional_contract_matches_the_host_usage_string(self) -> None:
        self.assertEqual(
            self.command(),
            ["/bin/host", "/tmp/MD.vst3", "/tmp/cap", "48000", "128", "20",
             "-1", "fixed", "0", "fast", "36", "127", "notes", "messages", "-1"])

    def test_paced_mode_selects_pacing_and_disables_the_offline_switch(self) -> None:
        command = self.command(mode="paced")
        self.assertEqual(command[8], "-1")
        self.assertEqual(command[9], "paced")

    def test_scenario_is_passed_through(self) -> None:
        self.assertEqual(self.command(scenario="chords")[12], "chords")

    def test_phase_stays_inside_the_block(self) -> None:
        # latency_host requires 0 <= phase < block.
        self.assertEqual(self.command(block_frames=64)[11], "63")


class ValidateRunTest(unittest.TestCase):
    def test_short_runs_are_rejected_before_launching_the_host(self) -> None:
        self.assertIsNotNone(perfrun.validate_run("notes", 48000, 128, 10))
        self.assertIsNone(perfrun.validate_run("notes", 48000, 128, 20))

    def test_unknown_scenarios_are_rejected(self) -> None:
        self.assertIsNotNone(perfrun.validate_run("wobble", 48000, 128, 20))

    def test_a_block_of_one_leaves_no_room_for_a_nonzero_phase(self) -> None:
        self.assertIsNone(perfrun.validate_run("notes", 48000, 1, 20))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: FAIL with `AttributeError: module 'perfrun' has no attribute 'host_command'`

- [ ] **Step 3: Implement**

Append to `scripts/dev/perfrun.py`:

```python
SCENARIOS = ("notes", "chords", "input", "transport")

# latency_host rejects anything shorter; it needs a warm-up window before its
# measurement period means anything.
MIN_SECONDS = 20


def validate_run(scenario: str, rate: int, block_frames: int,
                 seconds: int) -> str | None:
    """Reasons latency_host would reject this run, checked before launching it."""
    if scenario not in SCENARIOS:
        return f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}"
    if seconds < MIN_SECONDS:
        return f"latency_host requires seconds >= {MIN_SECONDS}"
    if rate < 8000 or rate > 192000:
        return "latency_host requires 8000 <= rate <= 192000"
    if block_frames < 1 or block_frames > 8192:
        return "latency_host requires 1 <= block <= 8192"
    return None


def host_command(host, plugin, prefix, mode: str, scenario: str, rate: int,
                 block_frames: int, seconds: int, note: int) -> list[str]:
    """Build latency_host's positional argument vector.

    Order is fixed by its usage string:
      PLUGIN PREFIX RATE BLOCK SECONDS [reprepare] [variable] [offline_after]
      [paced|fast] [note] [phase] [scenario] [messages] [restore]
    """
    paced = mode == "paced"
    return [
        str(host), str(plugin), str(prefix),
        str(rate), str(block_frames), str(seconds),
        "-1",                        # no re-prepare mid-run
        "fixed",                     # fixed block size
        "-1" if paced else "0",      # unpaced renders offline
        "paced" if paced else "fast",
        str(note),
        str(block_frames - 1),       # phase, must stay below the block size
        scenario,
        "messages",
        "-1",                        # no state restore mid-run
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd scripts/dev && python3 test_perfrun.py -v`
Expected: PASS, 35 tests

- [ ] **Step 5: Use it in dev.py**

In `cmd_perf`, replace the inline `cmd = [...]` construction with:

```python
        cmd = perfrun.host_command(
            host, plugin, prefix, args.mode, args.scenario, args.rate,
            args.block, args.seconds, cfg["note"])
```

Replace the existing `if args.seconds < 20:` guard with:

```python
    problem = perfrun.validate_run(args.scenario, args.rate, args.block, args.seconds)
    if problem:
        fail(problem)
```

Replace the hardcoded `"scenario": "notes"` in the report dict with `"scenario": args.scenario`, and add the argument:

```python
    sp.add_argument("--scenario", default="notes", choices=perfrun.SCENARIOS,
                    help="workload: notes, chords, input (opens 2 input "
                         "channels), or transport")
```

- [ ] **Step 6: Record the wider matrix**

```bash
python3 scripts/dev/dev.py perf md --mode paced --scenario chords --repeats 3 --save-baseline
python3 scripts/dev/dev.py perf md --mode paced --block 512 --repeats 3 --save-baseline
python3 scripts/dev/dev.py perf mm --mode paced --scenario chords --repeats 3 --save-baseline
```

Expected: three new baseline files. Do **not** expect `chords` to raise `loadP50`. Measured on this machine it does not: `chords` drives 384 MIDI note events against roughly 6 for `notes`, yet median load is flat (0.576 vs 0.569 back to back, inside the repeat spread). MD/MM emulate fixed DSP hardware cycle-accurately, so per-block work is largely independent of how many voices are sounding. What does respond is the tail — the same A/B moved `loadP99` from 0.898 to 1.090 and overruns from 31 to 202. Verify the scenario is reaching the host by checking the printed command line for `chords` and, if you want positive confirmation, the MIDI event count in the capture's JSON receipt. Treat `loadP99` and overrun counts as directional only: back-to-back runs of the identical configuration swung 32 vs 202 overruns on this machine.

- [ ] **Step 7: Commit**

```bash
git add -A scripts/dev
git commit -m "Expose perf scenario, rate and block axes"
```

---

### Task 6: Standalone scenario library

The driver can launch the app and call individual MCP tools, but there is no way to express "run this sequence and tell me whether it held". Two scenarios are defined here; both use only tools verified working against a live instance (`get_front_panel`, `get_device_info`, `get_plugin_state`, `set_plugin_state`, `send_note`).

State save/restore is the deliberate choice for the second scenario: `.github/copilot-instructions.md` records that this exact path regressed in 2.1.2 for JE8086, Vavra and Xenia via the `assign()`/`insert()` mistake. It is a proven-recurring bug class and nothing currently exercises it through the shipping standalone app.

**Files:**
- Create: `scripts/dev/scenarios.py`
- Modify: `scripts/dev/dev.py` (add the `ui` subcommand)
- Modify: `doc/dev_workflow.md`

**Interfaces:**
- Consumes: `mcpclient.McpClient` from the existing module.
- Produces: `scenarios.SCENARIOS` (name -> callable), each callable taking `(client, log)` and raising `AssertionError` on failure.

- [ ] **Step 1: Discover whether a restored state is byte-identical**

Before writing the assertion, find out what is actually guaranteed. Launch an instance and check:

```bash
python3 scripts/dev/dev.py run md --fresh
# wait for "mcp ready", then:
python3 -c "
import sys; sys.path.insert(0, 'scripts/dev')
from mcpclient import McpClient
c = McpClient.connect(name_substring='MD'); c.initialize()
a = c.call('get_plugin_state')['data']
c.call('send_note', note=36, velocity=100, duration_ms=200)
c.call('set_plugin_state', data=a)
b = c.call('get_plugin_state')['data']
print('identical:', a == b, 'len:', len(a), len(b))
"
```

Record the answer. If `identical: True`, Step 3's scenario asserts byte equality. If `False`, it asserts equal length plus a live device afterwards, and a comment records that byte equality is not guaranteed. **Do not guess** — write whichever the run shows.

- [ ] **Step 2: Write the scenario module**

Create `scripts/dev/scenarios.py`:

```python
#!/usr/bin/env python3
"""Named scenarios driven against a running standalone instance over MCP.

Each scenario takes a connected McpClient and a log callable, and raises
AssertionError on failure. Assertions target decoded panel state and device
facts, never screenshots: the LCD is a rendered bitmap, so the RmlUi DOM
carries none of its content and pixel comparisons break on renderer, HiDPI
and skin changes.
"""

from __future__ import annotations

import time


def boot(client, log) -> None:
    """The machine boots far enough to draw its screen."""
    deadline = time.monotonic() + 60.0
    panel = None
    while time.monotonic() < deadline:
        panel = client.call("get_front_panel", lcd=True)
        if panel["litPixels"] > 0:
            break
        time.sleep(0.5)

    assert panel is not None, "no panel response"
    assert panel["litPixels"] > 0, (
        f"LCD still blank after 60s (tileWrites={panel['tileWrites']}, "
        f"panelBytes={panel['panelBytes']})")
    assert panel["lcdHeight"] == 64 and panel["lcdWidth"] == 128, (
        f"unexpected LCD geometry {panel['lcdWidth']}x{panel['lcdHeight']}")
    assert len(panel["lcd"]) == 64, f"got {len(panel['lcd'])} LCD rows"
    log(f"{panel['model']} drew {panel['litPixels']} pixels, "
        f"{panel['tileWrites']} tile writes")

    info = client.call("get_device_info")
    assert info["valid"], "device reports itself invalid after boot"


def state_roundtrip(client, log) -> None:
    """A DAW-level save, a change, and a restore leave the machine alive.

    This is the path that regressed in 2.1.2 across three products when
    getState used assign() instead of insert() and overwrote the version
    header the Plugin layer had already pushed.
    """
    boot(client, log)

    original = client.call("get_plugin_state")["data"]
    assert original, "get_plugin_state returned nothing"
    log(f"saved {len(original)} base64 chars of plugin state")

    client.call("send_note", note=36, velocity=100, duration_ms=200)
    time.sleep(1.0)

    client.call("set_plugin_state", data=original)
    time.sleep(2.0)

    restored = client.call("get_plugin_state")["data"]
    # Replace this assertion with byte equality if Step 1 showed the round
    # trip is exact; keep the length check and the liveness checks either way.
    assert len(restored) == len(original), (
        f"restored state is {len(restored)} chars, original was {len(original)}")

    info = client.call("get_device_info")
    assert info["valid"], "device is invalid after restoring its own state"

    panel = client.call("get_front_panel", lcd=False)
    assert panel["litPixels"] > 0, "LCD went blank after restore"
    log("state restored, device still valid with a live panel")


SCENARIOS = {
    "boot": boot,
    "state_roundtrip": state_roundtrip,
}
```

- [ ] **Step 3: Add the `ui` subcommand to dev.py**

Add the import beside the others:

```python
import scenarios  # noqa: E402
```

Add the command:

```python
def cmd_ui(args) -> int:
    """Launch the standalone app, run one scenario against it, shut it down."""
    if args.scenario not in scenarios.SCENARIOS:
        fail(f"unknown scenario {args.scenario!r}; "
             f"choose from {', '.join(sorted(scenarios.SCENARIOS))}")

    key = args.product
    roms = find_roms()
    if key not in roms:
        fail(f"no valid {PRODUCTS[key]['model']} ROM in {ROMS}")

    app = standalone_app(key, config_type(args))
    if not app.exists():
        fail(f"{app} not built")

    env = stage_devroot(key, roms[key], enable_mcp=True, fresh=True)
    binary = app / "Contents" / "MacOS" / PRODUCTS[key]["product"]
    proc = subprocess.Popen([str(binary)], env=env)
    print(f"launched pid {proc.pid}")

    try:
        client = McpClient.wait_for(pid=proc.pid, timeout=args.timeout)
        client.initialize()
        scenarios.SCENARIOS[args.scenario](client, lambda m: print(f"  {m}"))
    except AssertionError as e:
        print(f"FAIL {args.scenario}: {e}", file=sys.stderr)
        return 1
    except McpError as e:
        print(f"ERROR {args.scenario}: {e}", file=sys.stderr)
        return 2
    finally:
        # The MCP exit tool terminates only this host process, so parallel
        # instances are unaffected. Fall back to a signal if it does not land.
        try:
            McpClient.connect(pid=proc.pid).call("exit")
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"PASS {args.scenario}")
    return 0
```

Register it in `build_parser`:

```python
    sp = sub.add_parser("ui", help="run a scenario against the standalone app")
    add_config(sp)
    sp.add_argument("product", choices=sorted(PRODUCTS))
    sp.add_argument("scenario", choices=sorted(scenarios.SCENARIOS))
    sp.add_argument("--timeout", type=float, default=90.0,
                    help="seconds to wait for the MCP server")
    sp.set_defaults(func=cmd_ui)
```

- [ ] **Step 4: Verify both scenarios pass, on both products**

```bash
python3 scripts/dev/dev.py ui md boot
python3 scripts/dev/dev.py ui mm boot
python3 scripts/dev/dev.py ui md state_roundtrip
python3 scripts/dev/dev.py ui mm state_roundtrip
```

Expected: each prints `PASS <scenario>` and exits 0, and the app process is gone afterwards (`pgrep -f "Standalone/Gearmulator"` returns nothing).

- [ ] **Step 5: Verify a scenario can actually fail**

Temporarily change `boot`'s pixel assertion to `assert panel["litPixels"] > 99999999`, re-run `python3 scripts/dev/dev.py ui md boot`, confirm it prints `FAIL boot:` with the pixel and tile-write counts and exits 1, then revert the change. A test that cannot fail is not a test.

- [ ] **Step 6: Document it**

In `doc/dev_workflow.md`, under the "Drive it" section, add:

```markdown
### Scenario library

```sh
python3 scripts/dev/dev.py ui md boot
python3 scripts/dev/dev.py ui md state_roundtrip
```

Each scenario launches the standalone app in a fresh isolated data root,
drives it over MCP, asserts, and shuts the instance down again. Scenarios live
in `scripts/dev/scenarios.py` and assert on decoded panel state and device
facts, never on screenshots.

`state_roundtrip` covers the DAW save/restore path through the shipping app.
That path regressed in 2.1.2 across three products, and no other local check
exercises it end to end.
```

- [ ] **Step 7: Commit**

```bash
git add scripts/dev/scenarios.py scripts/dev/dev.py doc/dev_workflow.md
git commit -m "Add a standalone scenario library driven over MCP"
```

---

### Task 7: Run the harness unit tests in CI

`perfrun.py` is pure Python with no macOS or ROM dependency, so it runs anywhere. `.github/workflows/mdmm-core.yml` already builds on Ubuntu; adding a step costs seconds and stops the harness silently rotting. This follows the pattern at `.github/workflows/elektron-macos.yml:66`.

**Files:**
- Modify: `.github/workflows/mdmm-core.yml`

- [ ] **Step 1: Add the path trigger**

In `mdmm-core.yml`, add `- "scripts/dev/**"` to **both** the `pull_request.paths` and `push.paths` lists, after the `- "source/synthLib/**"` entry in each.

- [ ] **Step 2: Add the test step**

In the `fixture-free` job, immediately after the `Install build dependencies` step, add:

```yaml
      - name: Run dev harness unit tests
        working-directory: scripts/dev
        run: python3 test_perfrun.py -v
```

It goes before the configure step deliberately: it needs no build, so it should fail fast.

- [ ] **Step 3: Verify the workflow parses and the command works from that directory**

```bash
python3 -c "import json,sys; sys.exit(0)"   # sanity: python3 present
cd scripts/dev && python3 test_perfrun.py -v && cd -
```

Expected: 35 tests pass. Confirm the YAML is valid:

```bash
python3 -c "
import sys
try:
    import yaml
except ImportError:
    print('pyyaml absent; check indentation by eye against the neighbouring steps')
    sys.exit(0)
print('workflow parses:', bool(yaml.safe_load(open('.github/workflows/mdmm-core.yml'))))
"
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/mdmm-core.yml
git commit -m "Run dev harness unit tests in CI"
```

---

## Not in this plan

- **The MD startup transient.** `startupOverruns` is 29-51 per run at up to 2.9x budget in the first 8 seconds, after the ~150 ms lock waits were fixed in `2a713f83`. This is emulator work, not harness work, and it needs its own investigation and plan. It is also the natural next target once the measurements here are trustworthy.
- **PGO.** `GEARMULATOR_MDMM_APPLE_PGO_MODE` requires a generate/train/merge/use cycle per architecture, rejects universal builds (`optimization.cmake:38-41`), and carries provenance-record obligations documented in `doc/mdmm-apple-optimization.md`. Half a day with release-process consequences, worth doing only after the transient work.
- **Interleaved A/B measurement.** Proper A/B/A/B interleaving needs two build trees alive at once, which means teaching `dev.py` about multiple build directories. The spread guard in Task 4 plus the "re-measure the control last" protocol covers the current need; revisit if a sub-3% effect ever needs resolving.
- **Release tooling.** `build_mdmm.sh` and friends are deliberately untouched.
