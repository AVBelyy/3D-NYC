import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Polygon, box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generate_3mf import Pipeline, count_osm_semantic_features  # noqa: E402


class SemanticCoverageTests(unittest.TestCase):
    def test_osm_reference_counts_use_rotated_source_polygon_not_its_bbox(self):
        diamond = Polygon([(0, 5), (5, 10), (10, 5), (5, 0)])
        geometries = (
            [box(4, 4, 5, 5), box(5, 5, 6, 6)]
            + [box(0.1, 0.1, 0.4, 0.4)] * 10
            + [LineString([(4, 5), (6, 5)]), LineString([(0.1, 0.2), (0.3, 0.2)])]
        )
        osm = gpd.GeoDataFrame(
            {
                "building": ["yes"] * 12 + [None, None],
                "highway": [None] * 12 + ["primary", "primary"],
            },
            geometry=geometries,
            crs=2263,
        )

        counts = count_osm_semantic_features(osm, diamond)

        self.assertEqual(counts["osm_building_footprints"], 2)
        self.assertEqual(counts["osm_building_footprints_unfiltered"], 12)
        self.assertEqual(counts["osm_motor_road_segments"], 1)
        self.assertEqual(counts["osm_motor_road_segments_unfiltered"], 2)


class _RecordingLog:
    """Collect stage events, and optionally interrupt the output loop."""

    def __init__(self, interrupt_on=None):
        self.events = []
        self.interrupt_on = interrupt_on

    def _record(self, event, **_fields):
        self.events.append(event)
        if event == self.interrupt_on:
            raise KeyboardInterrupt

    info = warning = error = _record


class StageChildProcessTests(unittest.TestCase):
    """Every stage runs through ``run_command``, so it owns the child cleanup."""

    def pipeline(self, logs, interrupt_on=None):
        runner = object.__new__(Pipeline)
        runner.logs = logs
        runner.env = os.environ.copy()
        runner.log = _RecordingLog(interrupt_on)
        return runner

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_interrupted_stage_kills_a_child_that_ignores_termination(self):
        # A long mesh boolean does not reach a Python signal handler until it
        # returns from C++, so emulate the worst case: a child that ignores
        # SIGTERM outright. Interrupting while the parent reads output is what
        # used to strand it.
        child = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('PROGRESS running', flush=True)\n"
            "time.sleep(600)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            runner = self.pipeline(Path(directory), interrupt_on="command_progress")
            pids = []
            real_popen = subprocess.Popen

            def capture(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                pids.append(process.pid)
                return process

            subprocess.Popen = capture
            try:
                with self.assertRaises(KeyboardInterrupt):
                    runner.run_command("interrupted", [sys.executable, "-c", child])
            finally:
                subprocess.Popen = real_popen

        self.assertFalse(self.alive(pids[0]), "stage child outlived the interrupted pipeline")
        self.assertIn("command_abandoned", runner.log.events)
        self.assertIn("command_terminated", runner.log.events)

    def test_successful_stage_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.pipeline(Path(directory))
            runner.run_command("quick", [sys.executable, "-c", "print('done')"])
            self.assertIn("command_completed", runner.log.events)
            self.assertNotIn("command_abandoned", runner.log.events)
            self.assertEqual((Path(directory) / "quick.log").read_text(), "done\n")

    def test_failing_stage_still_reports_its_return_code(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.pipeline(Path(directory))
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                runner.run_command("broken", [sys.executable, "-c", "raise SystemExit(3)"])
            self.assertEqual(raised.exception.returncode, 3)
            self.assertIn("command_failed", runner.log.events)

    def test_child_leads_its_own_process_group(self):
        # ``reap`` signals the group, so a stage that forks workers takes them
        # all with it rather than leaving them behind.
        with tempfile.TemporaryDirectory() as directory:
            runner = self.pipeline(Path(directory))
            runner.run_command("group", [
                sys.executable, "-c", "import os; print(os.getpid() == os.getpgid(0))",
            ])
            self.assertEqual((Path(directory) / "group.log").read_text(), "True\n")


if __name__ == "__main__":
    unittest.main()
