import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Polygon, box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _chunk_geometry as geometry  # noqa: E402
import plan_map_chunks as planner  # noqa: E402


def parsed(*arguments):
    return planner.parser().parse_args(
        ["--bounding-polygon", "POLYGON ((-74.01 40.70, -74.00 40.70, -74.00 40.71, -74.01 40.71, -74.01 40.70))",
         *arguments]
    )


class ArgumentTests(unittest.TestCase):
    def test_defaults_are_a_printable_plate(self):
        resolved = planner.resolve(parsed())
        self.assertEqual(resolved["envelope_mm"], (235.0, 235.0))

    def test_an_envelope_off_the_grid_step_is_rejected(self):
        with self.assertRaises(planner.PlanError) as caught:
            planner.resolve(parsed("--max-chunk-size-mm", "235.03x235"))
        self.assertIn("grid-step", str(caught.exception))

    def test_an_oversized_envelope_is_rejected(self):
        with self.assertRaises(planner.PlanError):
            planner.resolve(parsed("--max-chunk-size-mm", "300x300"))

    def test_a_scale_over_the_elevation_ceiling_names_the_fix(self):
        with self.assertRaises(planner.PlanError) as caught:
            planner.resolve(parsed("--scale", "40000"))
        message = str(caught.exception)
        self.assertIn("elevation", message)
        self.assertIn("--scale", message)

    def test_a_polygon_outside_nyc_is_rejected(self):
        arguments = planner.parser().parse_args([
            "--bounding-polygon",
            "POLYGON ((-80 40, -79 40, -79 41, -80 41, -80 40))",
        ])
        with self.assertRaises(planner.PlanError) as caught:
            planner.resolve(arguments)
        self.assertIn("NYC bounding box", str(caught.exception))

    def test_nonsense_knobs_are_rejected(self):
        for flag, value in (("--max-chunks", "0"), ("--cut-snap-mm", "0"),
                            ("--min-edge-mm", "-1"), ("--cost-resolution-m", "0"),
                            ("--min-chunk-fill", "1.5")):
            with self.subTest(flag=flag):
                with self.assertRaises(planner.PlanError):
                    planner.resolve(parsed(flag, value))


class OrientationTests(unittest.TestCase):
    def setUp(self):
        self.target = box(980000.0, 200000.0, 990000.0, 260000.0)

    def test_north_is_used_verbatim(self):
        chosen = planner.choose_bearing(Path("/nonexistent"), self.target, "north")
        self.assertEqual(chosen["bearing_rad"], 0.0)
        self.assertEqual(chosen["source"], "north")

    def test_an_explicit_bearing_is_used_verbatim(self):
        chosen = planner.choose_bearing(Path("/nonexistent"), self.target, "27.5")
        self.assertAlmostEqual(math.degrees(chosen["bearing_rad"]), 27.5)
        self.assertEqual(chosen["source"], "explicit")

    def test_an_unparseable_bearing_is_rejected(self):
        with self.assertRaises(planner.PlanError):
            planner.choose_bearing(Path("/nonexistent"), self.target, "sideways")

    def test_auto_without_the_osm_cache_names_the_cache_script(self):
        from _chunk_cost import SourceDataError

        with self.assertRaises(SourceDataError) as caught:
            planner.choose_bearing(Path("/nonexistent-cache-root"), self.target, "auto")
        self.assertIn("cache_new_york_osm.py", str(caught.exception))

    def test_the_mode_holds_the_main_grid_when_a_second_grid_competes(self):
        # Manhattan's shape: a dominant grid plus a smaller neighbourhood on a
        # different one. Averaging lands between them, which is what drifts a
        # cut off its street; the mode stays on the grid most cuts will follow.
        angle = np.concatenate([np.full(300, 29.0), np.full(100, 12.0)])
        weight = np.concatenate([np.full(300, 3.0), np.ones(100)])
        peak, coherence = planner.dominant_bearing(angle, weight)
        self.assertAlmostEqual(peak, 29.0, places=1)
        self.assertGreater(coherence, 0.5)
        mean = math.degrees(
            np.angle(np.sum(weight * np.exp(4j * np.radians(angle)))) / 4
        ) % 90.0
        self.assertGreater(abs(mean - 29.0), 0.5)

    def test_a_bearing_near_the_wrap_is_not_split_across_bins(self):
        angle = np.concatenate([np.full(100, 89.6), np.full(100, 0.3)])
        peak, coherence = planner.dominant_bearing(angle, np.ones(200))
        self.assertTrue(min(peak, 90.0 - peak) < 1.0, peak)
        self.assertGreater(coherence, 0.9)

    def test_scattered_directions_report_low_coherence(self):
        angle = np.linspace(0.0, 90.0, 900, endpoint=False)
        _, coherence = planner.dominant_bearing(angle, np.ones(900))
        self.assertLess(coherence, planner.MINIMUM_GRID_COHERENCE)

    def test_the_envelope_bearing_follows_the_long_axis(self):
        # A rectangle twice as tall as it is wide: its long axis points north.
        self.assertAlmostEqual(planner.envelope_bearing(self.target) % math.pi, 0.0, places=6)

    def test_the_frame_origin_sits_at_the_target_corner(self):
        frame = planner.build_frame(self.target, 0.0, 10000.0, 0.125)
        local = frame.to_frame(self.target)
        self.assertAlmostEqual(local.bounds[0], 0.0, places=6)
        self.assertAlmostEqual(local.bounds[1], 0.0, places=6)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.shared = {
            "plan_id": "demo", "python": "/usr/bin/python3", "scale": 10533.0,
            "grid_step_mm": 0.125, "layer_height": 0.24, "vertical_exaggeration": 1.0,
            "terrain_origin_m": -11.4712, "terrain_relief_factor": 1.0,
            "source_padding_m": 20.0, "prime_tower": "auto",
            "offline": False, "no_preview": False, "model_dir": "output/models",
        }
        self.paths = {"polygon": "output/plans/demo/chunks/demo_A1.geojson",
                      "frame": "output/plans/demo/chunks/demo_A1.frame.json",
                      "model": "output/models/demo_A1.3mf"}

    def test_every_cross_chunk_value_is_pinned_on_the_command_line(self):
        command = planner.command_for({"label": "A1"}, self.shared, self.paths)
        for flag in ("--print-frame", "--scale", "--grid-step-mm", "--layer-height",
                     "--vertical-exaggeration", "--terrain-origin-m",
                     "--terrain-relief-factor", "--source-padding-m"):
            self.assertIn(flag, command)

    def test_the_polygon_and_frame_are_passed_as_files(self):
        command = planner.command_for({"label": "A1"}, self.shared, self.paths)
        self.assertIn(f"@{self.paths['polygon']}", command)
        self.assertIn(f"@{self.paths['frame']}", command)

    def test_offline_and_preview_switches_are_forwarded(self):
        self.shared.update(offline=True, no_preview=True)
        command = planner.command_for({"label": "A1"}, self.shared, self.paths)
        self.assertIn("--offline", command)
        self.assertIn("--no-preview", command)

    def test_the_terrain_datum_keeps_enough_precision_to_match_across_plates(self):
        command = planner.command_for({"label": "A1"}, self.shared, self.paths)
        value = command[command.index("--terrain-origin-m") + 1]
        self.assertEqual(float(value), round(self.shared["terrain_origin_m"], 4))


class SummaryTests(unittest.TestCase):
    def test_quality_is_weighted_by_seam_length(self):
        seams = [
            {"length_ft": 100.0, "building_fraction": 1.0},
            {"length_ft": 300.0, "building_fraction": 0.0},
        ]
        summary = planner.summarise([], seams, 10000.0)
        self.assertAlmostEqual(summary["building_fraction"], 0.25)

    def test_crossings_are_listed_per_feature_not_pooled_by_name(self):
        seams = [
            {"length_ft": 100.0, "between": ["A1", "A2"],
             "crosses": [{"kind": "bridge", "name": "unnamed", "length_ft": 60.0},
                         {"kind": "bridge", "name": "unnamed", "length_ft": 30.0}]},
        ]
        summary = planner.summarise([], seams, 10000.0)
        self.assertEqual(len(summary["keep_out_crossings"]), 2)
        self.assertAlmostEqual(summary["keep_out_length_ft"], 90.0)
        self.assertEqual(summary["keep_out_crossings"][0]["between"], ["A1", "A2"])

    def test_crossing_lengths_are_converted_to_printed_millimetres(self):
        seams = [{"length_ft": 100.0, "between": ["A1", "A2"],
                  "crosses": [{"kind": "bridge", "name": "x", "length_ft": 1000.0}]}]
        summary = planner.summarise([], seams, 10000.0)
        self.assertAlmostEqual(summary["longest_crossing_mm"], 1000.0 * geometry.FT * 1000 / 10000)

    def test_no_seams_yields_no_weighted_values(self):
        summary = planner.summarise([], [], 10000.0)
        self.assertIsNone(summary["building_fraction"])
        self.assertEqual(summary["keep_out_crossings"], [])

    def test_quality_is_measured_on_final_seams_not_search_cuts(self):
        """Compaction can absorb an early cut, so cuts are not seams."""
        import numpy as np
        from test_chunk_cost import a_surface

        surface = a_surface(np.ones((40, 40)),
                            layers={"building_core": np.zeros((40, 40), dtype=bool)})
        seams = [{"chunks": [0, 1], "length_ft": 50.0,
                  "geometry": shapely.LineString([(10.0, 10.0), (10.0, 300.0)])}]
        measured = planner.measure_seams(surface, seams, ["A1", "A2"])
        self.assertEqual(measured[0]["between"], ["A1", "A2"])
        self.assertIn("blocked_samples", measured[0])
        self.assertIn("building_fraction", measured[0])


class PlanOutputTests(unittest.TestCase):
    """End-to-end writing with straight cuts, so no dataset is required."""

    def setUp(self):
        self.frame = planner.build_frame(
            box(980000.0, 200000.0, 990000.0, 230000.0), 0.0, 10000.0, 0.125
        )
        self.target_ft = self.frame.to_frame(box(980000.0, 200000.0, 990000.0, 230000.0))
        limits = (self.frame.feet(235.0), self.frame.feet(235.0))
        self.polygons = geometry.partition(
            self.target_ft, limits_ft=limits, deviation_ft=0.0, sample_step_ft=50.0
        ).polygons
        self.labels = geometry.grid_labels(self.frame, self.polygons, limits)
        self.shared = {
            "plan_id": "demo", "python": sys.executable, "scale": 10000.0,
            "grid_step_mm": 0.125, "layer_height": 0.24, "vertical_exaggeration": 1.0,
            "terrain_origin_m": -1.5, "terrain_relief_factor": 1.0,
            "source_padding_m": 20.0, "prime_tower": "auto", "offline": True,
            "no_preview": True, "model_dir": "output/models",
        }

    def test_chunk_records_carry_a_covering_print_frame(self):
        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        self.assertEqual(len(records), len(self.polygons))
        for record in records:
            payload = record["print_frame"]
            self.assertEqual(sorted(payload), ["origin_ft", "size_mm", "x_axis", "y_axis"])
            self.assertTrue(all(side > 0 for side in payload["size_mm"]))
            self.assertGreater(record["area_km2"], 0)

    def test_written_polygons_round_trip_through_geojson_exactly(self):
        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        for record in records:
            text = shapely.to_geojson(record["polygon_wgs84"])
            restored = shapely.from_geojson(text)
            self.assertEqual(
                shapely.get_coordinates(restored).tolist(),
                shapely.get_coordinates(record["polygon_wgs84"]).tolist(),
            )

    def test_the_plan_directory_holds_everything_needed_to_build(self):
        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = planner.write_plan(root, {"planner_version": 1}, records, self.shared)
            self.assertTrue((root / "plan.json").is_file())
            self.assertTrue((root / "commands.sh").is_file())
            self.assertTrue((root / "chunks.geojson").is_file())
            self.assertEqual(len(manifest["chunks"]), len(records))
            for record in records:
                stem = f"demo_{record['label']}"
                self.assertTrue((root / "chunks" / f"{stem}.geojson").is_file())
                self.assertTrue((root / "chunks" / f"{stem}.frame.json").is_file())
            script = (root / "commands.sh").read_text()
            self.assertIn("generate_3mf.py", script)
            self.assertEqual(script.count("--print-frame"), len(records))
            self.assertTrue(script.startswith("#!/bin/sh"))

    def test_the_written_frame_files_satisfy_the_generator_parser(self):
        from generate_3mf import parse_print_frame

        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner.write_plan(root, {"planner_version": 1}, records, self.shared)
            for record in records:
                path = root / "chunks" / f"demo_{record['label']}.frame.json"
                self.assertEqual(parse_print_frame(str(path)), record["print_frame"])

    def test_the_written_polygons_satisfy_the_generator_parser(self):
        from generate_3mf import parse_bounding_polygon

        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner.write_plan(root, {"planner_version": 1}, records, self.shared)
            for record in records:
                path = root / "chunks" / f"demo_{record['label']}.geojson"
                self.assertEqual(parse_bounding_polygon(f"@{path}").geom_type, "Polygon")

    def test_assembled_size_reports_the_whole_map(self):
        width, height = planner.assembled_size_mm(self.frame, self.target_ft)
        self.assertAlmostEqual(width, self.frame.mm(10000.0), places=6)
        self.assertAlmostEqual(height, self.frame.mm(30000.0), places=6)


if __name__ == "__main__":
    unittest.main()
