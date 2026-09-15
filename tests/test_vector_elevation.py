"""Terrain and canopy built from vector sources, with no LiDAR to read.

These are the invariants the substitution has to keep: only the spot codes
that describe the ground are used, the triangulated surface honours the values
it was given, the floor it reports really is a lower bound for a multi-plate
datum, and the canopy model states plainly that it measured nothing.
"""

import sys
import unittest
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from _vector_elevation import (  # noqa: E402
    FT,
    SPOT_BRIDGE,
    SPOT_ROADBED,
    SPOT_ROOF,
    SPOT_WATER,
    VectorElevationError,
    canopy_model_report,
    control_point_floor,
    crown_canopy_height,
    ground_control_points,
    interpolate_terrain,
    interpolate_terrain_grid,
    spot_elevation_points,
)
from affine import Affine  # noqa: E402


def elevations(records):
    """A Planimetrics ELEVATION frame: (x, y, elevation_ft, sub_feature_code)."""
    return gpd.GeoDataFrame(
        {
            "ELEVATION": [record[2] for record in records],
            "SUB_FEATURE_CODE": [record[3] for record in records],
        },
        geometry=[Point(record[0], record[1]) for record in records],
        crs=2263,
    )


def footprints(records):
    """A Building Footprints frame: (x, y, size, ground_elevation_ft)."""
    return gpd.GeoDataFrame(
        {"ground_elevation": [record[3] for record in records]},
        geometry=[
            Polygon([
                (record[0], record[1]), (record[0] + record[2], record[1]),
                (record[0] + record[2], record[1] + record[2]), (record[0], record[1] + record[2]),
            ]) for record in records
        ],
        crs=2263,
    )


class SpotSelectionTests(unittest.TestCase):
    def test_only_ground_describing_codes_are_terrain(self):
        """A roof spot is a roof, and a bridge spot is a deck."""
        frame = elevations([
            (0, 0, 100.0, SPOT_ROADBED),
            (10, 0, 500.0, SPOT_ROOF),
            (20, 0, 300.0, SPOT_BRIDGE),
            (30, 0, 10.0, SPOT_WATER),
        ])
        x, _, z = spot_elevation_points(frame)
        self.assertEqual(sorted(np.round(z / FT).tolist()), [10.0, 100.0])
        self.assertEqual(sorted(x.tolist()), [0.0, 30.0])

    def test_feet_are_converted_to_metres(self):
        frame = elevations([(0, 0, 100.0, SPOT_ROADBED)])
        _, _, z = spot_elevation_points(frame)
        self.assertAlmostEqual(float(z[0]), 100.0 * FT, places=9)

    def test_placeholder_and_out_of_range_values_are_rejected(self):
        """A -999 no-data marker must not become a crater in the terrain."""
        frame = elevations([
            (0, 0, -999.0, SPOT_ROADBED),
            (10, 0, 40.0, SPOT_ROADBED),
            (20, 0, float("nan"), SPOT_ROADBED),
        ])
        _, _, z = spot_elevation_points(frame)
        self.assertEqual(len(z), 1)

    def test_an_empty_or_absent_frame_contributes_nothing(self):
        for value in (None, elevations([])):
            with self.subTest(value=type(value).__name__):
                x, y, z = spot_elevation_points(value)
                self.assertEqual((len(x), len(y), len(z)), (0, 0, 0))


class ControlPointTests(unittest.TestCase):
    def test_building_ground_elevations_supplement_the_spot_elevations(self):
        points = ground_control_points(
            elevations([(0, 0, 30.0, SPOT_ROADBED), (100, 0, 30.0, SPOT_ROADBED)]),
            footprints([(40, 40, 20, 60.0), (70, 70, 20, 90.0)]),
        )
        self.assertEqual(len(points), 4)
        self.assertEqual(points.provenance["planimetric_spot_elevations"], 2)
        self.assertEqual(points.provenance["building_ground_elevations"], 2)

    def test_a_building_point_lands_inside_its_own_footprint(self):
        """Representative points, not centroids: an L-shape must not fall out."""
        frame = gpd.GeoDataFrame(
            {"ground_elevation": [50.0]},
            geometry=[Polygon([(0, 0), (30, 0), (30, 10), (10, 10), (10, 30), (0, 30)])],
            crs=2263,
        )
        points = ground_control_points(elevations([
            (0, 0, 10.0, SPOT_ROADBED), (60, 0, 10.0, SPOT_ROADBED),
            (0, 60, 10.0, SPOT_ROADBED),
        ]), frame)
        placed = Point(points.x[-1], points.y[-1])
        self.assertTrue(frame.geometry.iloc[0].covers(placed))

    def test_too_few_control_points_is_refused_rather_than_guessed(self):
        with self.assertRaises(VectorElevationError):
            ground_control_points(elevations([(0, 0, 30.0, SPOT_ROADBED)]), None)


class InterpolationTests(unittest.TestCase):
    def points(self):
        return ground_control_points(elevations([
            (0, 0, 0.0, SPOT_ROADBED), (100, 0, 0.0, SPOT_ROADBED),
            (0, 100, 0.0, SPOT_ROADBED), (100, 100, 100.0, SPOT_ROADBED),
        ]), None)

    def test_a_control_point_keeps_its_own_value(self):
        points = self.points()
        sampled = interpolate_terrain(points, points.x, points.y)
        np.testing.assert_allclose(sampled, points.z_m, atol=1e-4)

    def test_interpolation_stays_between_the_values_it_was_given(self):
        points = self.points()
        xs, ys = np.meshgrid(np.linspace(0, 100, 37), np.linspace(0, 100, 41))
        sampled = interpolate_terrain(points, xs, ys)
        self.assertTrue(np.isfinite(sampled).all())
        self.assertGreaterEqual(float(sampled.min()), float(points.z_m.min()) - 1e-4)
        self.assertLessEqual(float(sampled.max()), float(points.z_m.max()) + 1e-4)

    def test_outside_the_hull_the_nearest_surveyed_value_is_carried(self):
        points = self.points()
        sampled = interpolate_terrain(points, np.array([[-500.0]]), np.array([[-500.0]]))
        self.assertTrue(np.isfinite(sampled).all())
        self.assertAlmostEqual(float(sampled[0, 0]), 0.0, places=4)

    def test_chunked_evaluation_matches_a_single_pass(self):
        """Chunking exists for memory, so it must not change the surface."""
        points = self.points()
        xs, ys = np.meshgrid(np.linspace(0, 100, 23), np.linspace(0, 100, 29))
        np.testing.assert_allclose(
            interpolate_terrain(points, xs, ys, chunk_rows=1),
            interpolate_terrain(points, xs, ys, chunk_rows=10_000),
            atol=1e-6,
        )

    def test_the_grid_helper_samples_cell_centres(self):
        """Banding is an implementation detail; the surface is not."""
        points = self.points()
        transform = Affine(10.0, 0.0, 0.0, 0.0, -10.0, 100.0)
        shape = (10, 10)
        gridded = interpolate_terrain_grid(points, transform, shape, chunk_rows=3)
        columns = np.arange(shape[1]) + 0.5
        rows = (np.arange(shape[0]) + 0.5)[:, None]
        expected = interpolate_terrain(
            points,
            transform.c + transform.a * columns[None, :] + transform.b * rows,
            transform.f + transform.d * columns[None, :] + transform.e * rows,
        )
        self.assertEqual(gridded.shape, shape)
        np.testing.assert_allclose(gridded, expected, atol=1e-6)

    def test_the_reported_floor_bounds_every_sample(self):
        """The multi-plate datum relies on this: no plate may sample below it."""
        points = self.points()
        xs, ys = np.meshgrid(np.linspace(-200, 300, 61), np.linspace(-200, 300, 61))
        self.assertLessEqual(
            control_point_floor(points), float(interpolate_terrain(points, xs, ys).min()) + 1e-4
        )


class CrownCanopyTests(unittest.TestCase):
    def test_nothing_outside_the_canopy_is_raised(self):
        mask = np.zeros((20, 20), dtype=bool)
        mask[5:15, 5:15] = True
        height = crown_canopy_height(mask, cell_m=1.0)
        self.assertEqual(float(height[~mask].max()), 0.0)
        self.assertTrue((height[mask] > 0).all())

    def test_a_lone_tree_keeps_the_edge_height_and_a_stand_grows_inward(self):
        lone = np.zeros((9, 9), dtype=bool)
        lone[4, 4] = True
        stand = np.zeros((81, 81), dtype=bool)
        stand[1:80, 1:80] = True
        edge, mature = 8.0, 21.5
        street = crown_canopy_height(lone, cell_m=1.0, edge_height_m=edge,
                                     mature_height_m=mature, crown_scale_m=5.0)
        forest = crown_canopy_height(stand, cell_m=1.0, edge_height_m=edge,
                                     mature_height_m=mature, crown_scale_m=5.0)
        # A lone tree sits near the edge height, nowhere near a closed stand.
        self.assertGreaterEqual(float(street[4, 4]), edge)
        self.assertLess(float(street[4, 4]) - edge, mature - float(street[4, 4]))
        self.assertGreater(float(forest[40, 40]), 0.95 * mature)
        self.assertLessEqual(float(forest.max()), mature)

    def test_height_never_exceeds_the_mature_height(self):
        mask = np.ones((60, 60), dtype=bool)
        height = crown_canopy_height(mask, cell_m=0.5, mature_height_m=21.5)
        self.assertLessEqual(float(height.max()), 21.5)

    def test_a_coarser_cell_reaches_the_same_height_at_the_same_distance(self):
        """The model is stated in source metres, not cells."""
        fine = np.zeros((161, 161), dtype=bool)
        fine[1:160, 1:160] = True
        coarse = np.zeros((41, 41), dtype=bool)
        coarse[1:40, 1:40] = True
        at_ten_metres_fine = float(crown_canopy_height(fine, cell_m=0.5)[80 - 20, 80])
        at_ten_metres_coarse = float(crown_canopy_height(coarse, cell_m=2.0)[20 - 5, 20])
        self.assertAlmostEqual(at_ten_metres_fine, at_ten_metres_coarse, delta=0.6)

    def test_an_empty_canopy_is_flat_rather_than_an_error(self):
        height = crown_canopy_height(np.zeros((8, 8), dtype=bool), cell_m=1.0)
        self.assertEqual(float(np.abs(height).max()), 0.0)

    def test_invalid_model_parameters_are_refused(self):
        mask = np.ones((4, 4), dtype=bool)
        for kwargs in ({"cell_m": 0.0}, {"cell_m": 1.0, "crown_scale_m": -1.0},
                       {"cell_m": 1.0, "edge_height_m": 30.0, "mature_height_m": 20.0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                crown_canopy_height(mask, **kwargs)

    def test_the_report_says_the_canopy_was_not_measured(self):
        report = canopy_model_report(0.5, edge_height_m=8.0, mature_height_m=21.5,
                                     crown_scale_m=5.0, cells=12)
        self.assertFalse(report["measured"])
        self.assertIn("model", report["method"])


if __name__ == "__main__":
    unittest.main()
