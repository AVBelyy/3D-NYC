"""Real-data planner checks.

Skipped unless ``NYC_CHUNK_INTEGRATION=1``, because these need the complete
LiDAR, Planimetrics, building-footprint and OpenStreetMap caches. The default
suite stays hermetic; run this after provisioning ``data/cache``.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

ENABLED = os.environ.get("NYC_CHUNK_INTEGRATION") == "1"


@unittest.skipUnless(ENABLED, "set NYC_CHUNK_INTEGRATION=1 with populated data/cache")
class CentralParkPlanTests(unittest.TestCase):
    """Plan the smallest tracked polygon end to end and inspect the result."""

    @classmethod
    def setUpClass(cls):
        cls.directory = Path(tempfile.mkdtemp(prefix="chunk-plan-"))
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "plan_map_chunks.py"),
             "--bounding-polygon", f"@{ROOT / 'data/polygons/central_park.geojson'}",
             "--scale", "5400", "--max-chunks", "8", "--plan-id", "integration",
             "--output-dir", str(cls.directory), "--no-preview"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if completed.returncode:
            raise AssertionError(f"planning failed:\n{completed.stdout}\n{completed.stderr}")
        cls.plan = json.loads(
            (cls.directory / "plans/integration/plan.json").read_text()
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def test_the_partition_covers_the_target_exactly(self):
        coverage = self.plan["coverage"]
        self.assertLessEqual(coverage["uncovered_area_ft2"], coverage["tolerance_ft2"])
        self.assertLessEqual(coverage["excess_area_ft2"], coverage["tolerance_ft2"])
        self.assertLessEqual(
            coverage["maximum_pairwise_overlap_ft2"], coverage["tolerance_ft2"]
        )

    def test_every_plate_fits_the_envelope_and_the_elevation_limit(self):
        for chunk in self.plan["chunks"]:
            width, height = chunk["size_mm"]
            self.assertLessEqual(max(width, height), 235.0 + 1e-9)
            self.assertGreaterEqual(min(width, height), 20.0)
            self.assertLessEqual(chunk["elevation_cells"], 30_000_000)

    def test_seams_agree_between_neighbours(self):
        self.assertTrue(self.plan["seams"])
        for seam in self.plan["seams"]:
            self.assertLess(seam["boundary_offset_ft"], 1e-6)

    def test_no_seam_runs_far_along_a_keep_out(self):
        self.assertLessEqual(self.plan["quality"]["longest_crossing_mm"], 6.0)

    def test_seams_prefer_low_open_ground(self):
        quality = self.plan["quality"]
        self.assertLess(quality["building_fraction"], 0.05)
        self.assertLess(quality["mean_above_ground_m"], 12.0)

    def test_every_chunk_shares_one_frame_and_terrain_datum(self):
        shared = self.plan["shared_generation"]
        frame = self.plan["frame"]
        for chunk in self.plan["chunks"]:
            self.assertEqual(chunk["print_frame"]["x_axis"], frame["x_axis"])
            self.assertEqual(chunk["print_frame"]["y_axis"], frame["y_axis"])
            command = chunk["command"]
            self.assertEqual(
                command[command.index("--terrain-origin-m") + 1],
                f"{shared['terrain_origin_m']:.4f}",
            )

    def test_the_generator_accepts_every_emitted_chunk_configuration(self):
        from generate_3mf import build_config, parser

        for chunk in self.plan["chunks"]:
            arguments = parser().parse_args(chunk["command"][2:])
            config, _, _ = build_config(arguments)
            self.assertEqual(config["frame_epsg2263"]["x_axis"], self.plan["frame"]["x_axis"])
            self.assertEqual(config["size_mm"], chunk["size_mm"])
            self.assertAlmostEqual(
                config["terrain_origin_m"],
                round(self.plan["shared_generation"]["terrain_origin_m"], 4),
            )


if __name__ == "__main__":
    unittest.main()
