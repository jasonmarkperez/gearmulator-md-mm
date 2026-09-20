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
                "rate": 48000, "block": 128, "seconds": 20,
                "warmupSeconds": 8.0}
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

    def test_a_different_seconds_value_is_refused(self) -> None:
        # A longer/shorter render is a different workload: comparing its
        # xRealtime (which folds in fixed process/boot startup cost) against
        # a differently-timed baseline produces a spurious delta.
        self.assertEqual(
            perfrun.config_mismatch(self.config(), self.config(seconds=60)),
            ["seconds"])

    def test_config_keys_includes_seconds(self) -> None:
        self.assertIn("seconds", perfrun.CONFIG_KEYS)


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



if __name__ == "__main__":
    unittest.main()
