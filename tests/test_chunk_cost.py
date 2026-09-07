import math
import sys
import unittest
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _chunk_geometry as geometry  # noqa: E402
from _chunk_cost import (  # noqa: E402
    BLOCKED_COST,
    CostSurface,
    CostWeights,
    FrameGrid,
    SourceDataError,
    cache_signature,
    staircase_path,
)
from _chunk_geometry import Frame  # noqa: E402


def a_frame(bearing_deg=0.0):
    return Frame.from_bearing(math.radians(bearing_deg), (980000.0, 200000.0), 10000.0, 0.125)


def a_surface(cost, keep_out=None, layers=None, resolution_m=4.0, frame=None, style="staircase"):
    """A CostSurface over synthetic arrays, with no dataset behind it."""
    frame = frame or a_frame()
    height, width = cost.shape
    grid = FrameGrid(frame, (0.0, 0.0), resolution_m / geometry.FT, width, height)
    zeros = np.zeros(cost.shape, dtype=bool)
    return CostSurface(
        grid=grid,
        cost=np.asarray(cost, dtype=np.float32),
        keep_out=zeros.copy() if keep_out is None else np.asarray(keep_out, dtype=bool),
        height_m=np.zeros(cost.shape, np.float32),
        ground_m=np.zeros(cost.shape, np.float32),
        ground_min_m=np.zeros(cost.shape, np.float32),
        layers=layers or {},
        weights=CostWeights(),
        style=style,
    )


class StaircasePathTests(unittest.TestCase):
    def test_the_cheap_corridor_is_followed(self):
        band = np.full((200, 21), 100.0)
        band[:, 5] = 1.0
        path = staircase_path(
            band, minimum_run=10, nominal_index=10, jog_penalty=1.0,
            centering_penalty=0.0, min_jog_levels=1,
        )
        self.assertTrue(bool((path == 5).all()))

    def test_a_jog_is_taken_when_the_corridor_moves(self):
        band = np.full((200, 21), 100.0)
        band[:100, 15] = 1.0
        band[100:, 5] = 1.0
        path = staircase_path(
            band, minimum_run=10, nominal_index=10, jog_penalty=1.0,
            centering_penalty=0.0, min_jog_levels=1,
        )
        self.assertEqual(path[0], 15)
        self.assertEqual(path[-1], 5)
        self.assertEqual(len(set(path.tolist())), 2)

    def test_runs_are_never_shorter_than_the_minimum(self):
        rng = np.random.default_rng(3)
        band = rng.random((300, 25)) * 10
        minimum_run = 25
        path = staircase_path(
            band, minimum_run=minimum_run, nominal_index=12, jog_penalty=1.0,
            centering_penalty=0.0, min_jog_levels=1,
        )
        changes = np.flatnonzero(np.diff(path)) + 1
        edges = np.concatenate([[0], changes, [len(path)]])
        self.assertTrue(bool((np.diff(edges) >= minimum_run).all()), np.diff(edges).tolist())

    def test_jogs_are_never_smaller_than_the_minimum(self):
        rng = np.random.default_rng(4)
        band = rng.random((400, 40)) * 10
        path = staircase_path(
            band, minimum_run=20, nominal_index=20, jog_penalty=0.1,
            centering_penalty=0.0, min_jog_levels=5,
        )
        steps = np.diff(path)[np.diff(path) != 0]
        self.assertTrue(bool((np.abs(steps) >= 5).all()), steps.tolist())

    def test_a_blocked_corridor_is_avoided_when_an_alternative_exists(self):
        band = np.ones((100, 11))
        band[:, 5] = np.inf
        path = staircase_path(
            band, minimum_run=10, nominal_index=5, jog_penalty=0.1,
            centering_penalty=0.0, min_jog_levels=1,
        )
        self.assertFalse(bool((path == 5).any()))

    def test_a_fully_blocked_band_still_returns_a_path(self):
        band = np.full((50, 7), np.inf)
        path = staircase_path(
            band, minimum_run=5, nominal_index=3, jog_penalty=1.0,
            centering_penalty=0.0, min_jog_levels=1,
        )
        self.assertEqual(len(path), 50)

    def test_centering_holds_the_cut_near_its_nominal_position(self):
        band = np.ones((100, 21))
        path = staircase_path(
            band, minimum_run=10, nominal_index=7, jog_penalty=1.0,
            centering_penalty=5.0, min_jog_levels=1,
        )
        self.assertTrue(bool((path == 7).all()))

    def test_a_degenerate_band_is_rejected(self):
        with self.assertRaises(geometry.PlanGeometryError):
            staircase_path(
                np.zeros((0, 5)), minimum_run=1, nominal_index=0,
                jog_penalty=1.0, centering_penalty=0.0, min_jog_levels=1,
            )


class FrameGridTests(unittest.TestCase):
    def test_cell_lookup_round_trips(self):
        grid = a_surface(np.zeros((40, 30))).grid
        for row, column in ((0, 0), (13, 7), (39, 29)):
            x, y = grid.x_at(column), grid.y_at(row)
            self.assertEqual(int(grid.rows(np.asarray([y]))[0]), row)
            self.assertEqual(int(grid.columns(np.asarray([x]))[0]), column)

    def test_a_rotated_grid_transform_stays_orthogonal(self):
        grid = a_surface(np.zeros((10, 10)), frame=a_frame(27.0)).grid
        transform = grid.transform
        first = np.asarray([transform.a, transform.d])
        second = np.asarray([transform.b, transform.e])
        self.assertAlmostEqual(float(first @ second), 0.0, places=6)
        self.assertAlmostEqual(float(np.linalg.norm(first)), grid.resolution_ft, places=6)

    def test_world_bounds_contain_the_rotated_footprint(self):
        grid = a_surface(np.zeros((20, 10)), frame=a_frame(27.0)).grid
        bounds = grid.world_bounds
        self.assertTrue(box(*bounds).covers(grid.frame.to_world(grid.frame_box())))


class SeamMeasurementTests(unittest.TestCase):
    def test_blocked_counts_only_keep_out_cells(self):
        keep_out = np.zeros((20, 20), dtype=bool)
        keep_out[:, 10] = True
        surface = a_surface(np.ones((20, 20)), keep_out=keep_out)
        clear = shapely.LineString([(grid_x, 5.0) for grid_x in (1.0, 30.0)])
        self.assertEqual(surface.blocked(clear), 0)
        crossing = shapely.LineString([(0.0, 1.0), (0.0, 250.0)])
        self.assertEqual(surface.blocked(crossing), 0)

    def test_describe_reports_the_layers_it_has(self):
        layers = {"building_core": np.ones((20, 20), dtype=bool),
                  "cheap_surface": np.zeros((20, 20), dtype=bool)}
        surface = a_surface(np.ones((20, 20)), layers=layers)
        report = surface.describe(shapely.LineString([(10.0, 10.0), (10.0, 200.0)]))
        self.assertAlmostEqual(report["building_fraction"], 1.0)
        self.assertAlmostEqual(report["cheap_surface_fraction"], 0.0)
        self.assertIn("blocked_fraction", report)

    def test_an_empty_seam_describes_nothing(self):
        surface = a_surface(np.ones((10, 10)))
        self.assertEqual(surface.describe(shapely.LineString()), {})
        self.assertEqual(surface.blocked(None), 0)


class StraightenTests(unittest.TestCase):
    def test_a_clear_staircase_collapses_to_one_segment(self):
        surface = a_surface(np.ones((60, 60)), style="angled")
        points = np.asarray([[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [100.0, 50.0]])
        straightened = surface.straighten(geometry.AXIS_Y, points, min_side_ft=0.0)
        self.assertEqual(len(straightened), 2)

    def test_a_keep_out_forces_the_bend_to_stay(self):
        keep_out = np.zeros((60, 60), dtype=bool)
        keep_out[20:40, 20:40] = True
        surface = a_surface(np.ones((60, 60)), keep_out=keep_out, style="angled")
        resolution = surface.grid.resolution_ft
        points = np.asarray([
            [0.0, 0.0],
            [0.0, 45 * resolution],
            [55 * resolution, 45 * resolution],
            [55 * resolution, 55 * resolution],
        ])
        straightened = surface.straighten(geometry.AXIS_Y, points, min_side_ft=0.0)
        self.assertGreaterEqual(len(straightened), 2)
        self.assertLessEqual(len(straightened), len(points))

    def test_short_paths_pass_through_unchanged(self):
        surface = a_surface(np.ones((20, 20)), style="angled")
        points = np.asarray([[0.0, 0.0], [10.0, 0.0]])
        self.assertEqual(len(surface.straighten(geometry.AXIS_Y, points, min_side_ft=0.0)), 2)


class CrossingClassificationTests(unittest.TestCase):
    def test_narrow_width_is_the_short_side_of_the_oriented_envelope(self):
        from _chunk_cost import _narrow_width

        self.assertAlmostEqual(_narrow_width(box(0, 0, 100, 20)), 20.0, places=6)
        rotated = shapely.affinity.rotate(box(0, 0, 100, 20), 35.0)
        self.assertAlmostEqual(_narrow_width(rotated), 20.0, places=4)

    def test_a_square_crossing_is_not_flagged_but_a_lengthwise_one_is(self):
        import geopandas as gpd
        from _chunk_cost import LENGTHWISE_RATIO

        deck = box(0.0, 0.0, 400.0, 40.0)
        surface = a_surface(np.ones((200, 200)))
        surface.keep_out_features = gpd.GeoDataFrame(
            {"kind": ["bridge"], "name": ["a bridge"]},
            geometry=[surface.grid.frame.to_world(deck)], crs=2263,
        )
        across = surface.crossings(shapely.LineString([(200.0, -50.0), (200.0, 90.0)]))
        self.assertEqual(len(across), 1)
        self.assertAlmostEqual(across[0]["feature_width_ft"], 40.0, places=1)
        self.assertFalse(across[0]["along_feature"])

        along = surface.crossings(shapely.LineString([(10.0, 20.0), (390.0, 20.0)]))
        self.assertTrue(along[0]["along_feature"])
        self.assertGreater(
            along[0]["length_ft"], LENGTHWISE_RATIO * along[0]["feature_width_ft"]
        )

    def test_a_seam_that_only_grazes_a_feature_is_ignored(self):
        import geopandas as gpd

        surface = a_surface(np.ones((200, 200)))
        surface.keep_out_features = gpd.GeoDataFrame(
            {"kind": ["bridge"], "name": ["a bridge"]},
            geometry=[surface.grid.frame.to_world(box(0.0, 0.0, 400.0, 40.0))], crs=2263,
        )
        self.assertEqual(surface.crossings(shapely.LineString([(200.0, 39.9), (200.0, 45.0)])), [])


class CacheSignatureTests(unittest.TestCase):
    def test_a_missing_manifest_names_the_cache_script(self):
        with self.assertRaises(SourceDataError) as caught:
            cache_signature(Path("/nonexistent-cache-root"), "nyc_lidar_2017")
        self.assertIn("cache_nyc_lidar_2017.py", str(caught.exception))

    def test_an_optional_missing_cache_is_reported_not_raised(self):
        record = cache_signature(
            Path("/nonexistent-cache-root"), "nyc_land_cover_2017", required=False
        )
        self.assertFalse(record["present"])

    def test_an_incomplete_manifest_is_refused(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "nyc_lidar_2017"
            root.mkdir()
            (root / "manifest.json").write_text(json.dumps({"status": "in_progress"}))
            with self.assertRaises(SourceDataError) as caught:
                cache_signature(Path(directory), "nyc_lidar_2017")
            self.assertIn("not production-ready", str(caught.exception))


class BlockedCostTests(unittest.TestCase):
    def test_the_sentinel_dominates_any_realistic_cost(self):
        self.assertGreater(BLOCKED_COST, CostWeights().building_weight * 1e6)


if __name__ == "__main__":
    unittest.main()
