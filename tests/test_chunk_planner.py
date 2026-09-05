import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, box, mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_3mf import build_config, parser as generator_parser  # noqa: E402
from plan_map_chunks import (  # noqa: E402
    ELEVATION_CELL_M,
    MAX_ELEVATION_CELLS,
    SemanticRouteCost,
    SeamScorer,
    axes_for_orientation,
    main as planner_main,
    maximum_safe_scale,
    optimize_axis,
    parse_polygon,
    route_semantic_edge,
)


class FrameIntegrationTests(unittest.TestCase):
    def test_explicit_frame_preserves_shared_axes_size_and_terrain_origin(self):
        origin = [990000.0, 200000.0]
        scale = 6286.5
        k = 0.3048006096012192 * 1000 / scale
        frame = {
            "origin_ft": origin,
            "x_axis": [1.0, 0.0],
            "y_axis": [0.0, 1.0],
            "size_mm": [100.0, 120.0],
        }
        projected = box(origin[0] + 10, origin[1] + 20, origin[0] + 90 / k, origin[1] + 110 / k)
        wgs = gpd.GeoSeries([projected], crs=2263).to_crs(4326).iloc[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            polygon_path = root / "chunk.geojson"
            frame_path = root / "frame.json"
            polygon_path.write_text(json.dumps(mapping(wgs)))
            frame_path.write_text(json.dumps(frame))
            args = generator_parser().parse_args([
                "--bounding-polygon", "@" + str(polygon_path),
                "--print-frame", "@" + str(frame_path),
                "--scale", str(scale),
                "--terrain-origin-m", "-7.5",
                "--terrain-relief-factor", "1.25",
                "--prime-tower", "off",
                "--output", str(root / "chunk.3mf"),
            ])
            config, _, _ = build_config(args)
        self.assertEqual(config["size_mm"], frame["size_mm"])
        self.assertEqual(config["frame_epsg2263"], {
            key: frame[key] for key in ["origin_ft", "x_axis", "y_axis"]
        })
        self.assertEqual(config["terrain_origin_m"], -7.5)
        self.assertEqual(config["terrain_relief_factor"], 1.25)

    def test_explicit_frame_rejects_a_polygon_outside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            polygon_path = root / "chunk.geojson"
            frame_path = root / "frame.json"
            polygon_path.write_text(json.dumps(mapping(box(-74.00, 40.75, -73.99, 40.76))))
            frame_path.write_text(json.dumps({
                "origin_ft": [900000, 100000], "x_axis": [1, 0], "y_axis": [0, 1],
                "size_mm": [20, 20],
            }))
            args = generator_parser().parse_args([
                "--bounding-polygon", "@" + str(polygon_path),
                "--print-frame", "@" + str(frame_path), "--scale", "6286.5",
            ])
            with self.assertRaisesRegex(ValueError, "not covered"):
                build_config(args)


class PartitionTests(unittest.TestCase):
    def test_orientation_axes_are_right_handed(self):
        x_axis, y_axis = axes_for_orientation(29.0)
        self.assertAlmostEqual(float(x_axis @ y_axis), 0.0)
        self.assertAlmostEqual(float(x_axis[0] * y_axis[1] - x_axis[1] * y_axis[0]), 1.0)
        self.assertGreater(y_axis[1], 0)

    def test_dynamic_partition_obeys_span_constraints_and_avoids_building(self):
        aoi = box(0, 0, 300, 100)
        buildings = gpd.GeoDataFrame(
            {"height_roof": [30.0], "geometry": [box(95, 0, 110, 100)]}, crs=2263
        )
        empty = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=2263), crs=2263)
        roads = gpd.GeoDataFrame({"geometry": [box(145, 0, 155, 100)]}, crs=2263)
        scorer = SeamScorer(
            aoi, buildings=buildings, roads=roads, parks=empty, water=empty,
            hard_layers=[], clearance_ft=5,
        )
        cuts, reports = optimize_axis(
            axis="x", total_cells=300, divisions=3, maximum_cells=120,
            minimum_cells=20, candidate_every_cells=1, step_ft=1,
            origin=shapely.get_coordinates(box(0, 0, 1, 1))[0],
            x_axis=axes_for_orientation(0)[0], y_axis=axes_for_orientation(0)[1],
            other_extent_ft=100, scorer=scorer,
        )
        self.assertEqual(cuts[0], 0)
        self.assertEqual(cuts[-1], 300)
        self.assertTrue(all(20 <= right - left <= 120 for left, right in zip(cuts, cuts[1:])))
        self.assertTrue(all(report["buildings_cut"] == 0 for report in reports))

    def test_semantic_route_bends_around_building_and_uses_road(self):
        aoi = box(0, 0, 100, 100)
        buildings = gpd.GeoDataFrame(
            {"height_roof": [30.0], "geometry": [box(40, 45, 60, 55)]}, crs=2263
        )
        roads = gpd.GeoDataFrame(
            {"geometry": [box(5, 59, 95, 63)]}, crs=2263
        )
        empty = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=2263), crs=2263)
        scorer = SeamScorer(
            aoi, buildings=buildings, roads=roads, trails=empty,
            parks=empty, water=empty, hard_layers=[], clearance_ft=2,
        )
        costs = SemanticRouteCost(
            aoi, buildings=buildings, roads=roads, trails=empty,
            parks=empty, water=empty, hard_layers=[], sample_ft=1,
        )
        line, report = route_semantic_edge(
            axis="y", anchor_cells=50, major_start_cells=0, major_end_cells=100,
            minimum_deviation_cells=-20, maximum_deviation_cells=20,
            sample_every_cells=1, origin=np.asarray([0.0, 0.0]),
            x_axis=np.asarray([1.0, 0.0]), y_axis=np.asarray([0.0, 1.0]),
            step_ft=1.0, grid_step_mm=1.0, cost_surface=costs,
        )
        self.assertGreater(report["maximum_absolute_deviation_mm"], 0)
        self.assertEqual(scorer.details(line)["buildings_cut"], 0)
        self.assertGreater(scorer.details(line)["road_fraction"], 0.25)

    def test_feature_collection_input_is_dissolved(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": mapping(box(-74.0, 40.7, -73.99, 40.71))},
                {"type": "Feature", "properties": {}, "geometry": mapping(box(-73.99, 40.7, -73.98, 40.71))},
            ],
        }
        geometry = parse_polygon(json.dumps(payload))
        self.assertEqual(geometry.geom_type, "Polygon")
        self.assertAlmostEqual(geometry.area, 0.0002)

    def test_safe_scale_includes_elevation_grid_ceil_snapping(self):
        scale = maximum_safe_scale(235.0, 235.0, 20.0)
        width_cells = math.ceil((0.235 * scale + 40.0) / ELEVATION_CELL_M)
        height_cells = math.ceil((0.235 * scale + 40.0) / ELEVATION_CELL_M)
        self.assertLessEqual(width_cells * height_cells, MAX_ELEVATION_CELLS)

    def test_invalid_generator_parameter_fails_before_planning(self):
        argv = [
            "plan_map_chunks.py",
            "--bounding-polygon", "POLYGON ((-74 40.7, -73.99 40.7, -73.99 40.71, -74 40.71, -74 40.7))",
            "--max-chunks", "1", "--vertical-exaggeration", "0",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaisesRegex(SystemExit, "vertical-exaggeration"):
                planner_main()


class PlannerEndToEndTests(unittest.TestCase):
    def test_geometric_plan_has_exact_coverage_and_preflightable_commands(self):
        polygon = "POLYGON ((-74.0 40.76, -73.96 40.76, -73.96 40.79, -74.0 40.79, -74.0 40.76))"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan"
            argv = [
                "plan_map_chunks.py", "--bounding-polygon", polygon,
                "--max-chunks", "9", "--scale", "6286.5",
                "--orientation-deg", "0", "--geometric-only",
                "--skip-terrain-scan", "--plan-id", "test_plan",
                "--output-dir", str(output),
            ]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                planner_main()
            plan = json.loads((output / "plan.json").read_text())
            self.assertEqual(plan["coverage"]["result"], "passed")
            self.assertLessEqual(plan["chunk_count"], 9)
            self.assertLessEqual(plan["coverage"]["missing_area_sq_ft"], plan["coverage"]["tolerance_sq_ft"])
            self.assertLessEqual(plan["coverage"]["overlap_area_sq_ft"], plan["coverage"]["tolerance_sq_ft"])
            axes = {
                (
                    tuple(chunk["frame"]["x_axis"]),
                    tuple(chunk["frame"]["y_axis"]),
                )
                for chunk in plan["chunks"]
            }
            self.assertEqual(len(axes), 1)
            for chunk in plan["chunks"]:
                payload = json.loads(Path(chunk["polygon_file"]).read_text())
                self.assertEqual(payload["type"], "Polygon")
                self.assertNotEqual(payload["type"], "FeatureCollection")
                width, height = chunk["frame"]["size_mm"]
                self.assertTrue(20 <= width <= 235 and 20 <= height <= 235)
                self.assertAlmostEqual(width / 0.125, round(width / 0.125))
                self.assertAlmostEqual(height / 0.125, round(height / 0.125))
                parsed = generator_parser().parse_args(chunk["argv"][2:])
                config, _, _ = build_config(parsed)
                self.assertEqual(config["size_mm"], chunk["frame"]["size_mm"])
                self.assertEqual(config["terrain_origin_m"], -50.0)
            self.assertTrue((output / "commands.sh").stat().st_mode & 0o111)
            self.assertTrue((output / "preview.svg").is_file())
            events = [json.loads(line) for line in (output / "logs/planner.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "planner_completed")
            self.assertTrue(any(event["event"] == "generator_command_preflighted" for event in events))


if __name__ == "__main__":
    unittest.main()
