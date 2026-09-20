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


if __name__ == "__main__":
    unittest.main()
