import math
import sys
import unittest
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Polygon, box


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


class CutContinuityTests(unittest.TestCase):
    """Two cuts down one street have to meet where they touch.

    A guillotine splits an avenue between sibling regions, so the same street
    is chosen twice from nominals that can differ by less than one snap step.
    Centering outweighs the cost difference between two lanes of that street,
    so without an anchor the two halves land a single level apart and the plate
    they share grows a stubby side.
    """

    CORRIDOR = 180.0        # the cheaper of the street's two lanes
    NEIGHBOUR = 190.0       # the other one, a single level away

    def a_corridor(self, cheap=18, dearer=19):
        cost = np.full((40, 40), 50.0)
        cost[:, cheap] = 1.0
        if dearer is not None:
            cost[:, dearer] = 1.2
        return a_surface(cost, resolution_m=10.0 * geometry.FT)

    def a_request(self, nominal, **meets):
        return geometry.CutRequest(
            geometry.AXIS_X, 0.0, 300.0, nominal, 60.0, 10.0,
            snap_ft=10.0, min_run_ft=50.0, min_jog_ft=30.0, **meets,
        )

    def level_chosen(self, surface, nominal, **meets):
        path = surface.choose(self.a_request(nominal, **meets))
        levels = sorted(set(path[:, 1].tolist()))
        self.assertEqual(len(levels), 1, f"expected one level, got {levels}")
        return levels[0]

    def test_sibling_nominals_pull_one_street_onto_two_levels(self):
        # The defect: the northern half's nominal sits nearer the dearer lane
        # and centering outweighs the difference in cost between the two.
        surface = self.a_corridor()
        self.assertEqual(self.level_chosen(surface, 196.0), self.NEIGHBOUR)
        self.assertEqual(self.level_chosen(surface, 184.0), self.CORRIDOR)

    def test_a_cut_meets_the_one_it_continues_at_either_end(self):
        surface = self.a_corridor()
        self.assertEqual(
            self.level_chosen(surface, 196.0, continues_from=self.CORRIDOR),
            self.CORRIDOR,
        )
        self.assertEqual(
            self.level_chosen(surface, 196.0, continues_into=self.CORRIDOR),
            self.CORRIDOR,
        )

    def test_an_anchor_out_of_reach_is_ignored(self):
        # Beyond the deviation budget there is no level to meet, and the cut
        # falls back on its own corridor.
        surface = self.a_corridor()
        self.assertEqual(
            self.level_chosen(surface, 196.0, continues_from=1000.0), self.NEIGHBOUR
        )

    def test_a_corridor_worth_a_real_jog_still_wins(self):
        # Meeting is a jog's worth of preference, not a constraint: a street
        # that is genuinely elsewhere is still followed.
        surface = self.a_corridor(cheap=30, dearer=None)
        self.assertEqual(
            self.level_chosen(surface, 300.0, continues_from=self.CORRIDOR), 300.0
        )

    def test_a_jog_clear_of_the_anchor_stays_available(self):
        # Only steps too small to read as deliberate are refused; this corridor
        # is a full min_jog_ft away and remains reachable.
        surface = self.a_corridor(cheap=21, dearer=None)
        self.assertEqual(
            self.level_chosen(surface, 196.0, continues_from=self.CORRIDOR), 210.0
        )

    def test_a_blocked_anchor_is_left_rather_than_cut_through(self):
        # Meeting a cut must never be worth driving a seam through a keep-out.
        cost = np.full((40, 40), 50.0)
        cost[:, 18] = np.inf
        cost[:, 22] = 1.0
        surface = a_surface(cost, resolution_m=10.0 * geometry.FT)
        self.assertEqual(
            self.level_chosen(surface, 196.0, continues_from=self.CORRIDOR), 220.0
        )

    def test_an_axis_cut_meets_the_same_anchor(self):
        surface = self.a_corridor()
        surface.style = "axis"
        self.assertEqual(
            self.level_chosen(surface, 196.0, continues_from=self.CORRIDOR),
            self.CORRIDOR,
        )

    def test_anchors_a_sub_minimum_step_apart_still_yield_a_cut(self):
        # Both ends pinned to levels no legal jog can join: the cut still has
        # to divide its region, so it makes the best seam it can.
        surface = self.a_corridor()
        path = surface.choose(self.a_request(
            196.0, continues_from=self.CORRIDOR, continues_into=self.NEIGHBOUR
        ))
        self.assertGreaterEqual(len(path), 2)
        self.assertTrue(bool(np.all(np.isfinite(path))))


class PartitionContinuityTests(unittest.TestCase):
    """The rule over a whole partition, not one cut in isolation.

    A guillotine cuts one street from several regions, so what matters is the
    finished plates: two that meet down one avenue either share a full edge or
    only a corner.  Sharing a sliver means the two cuts chose different lanes
    of the same street, and the plate that later absorbs both grows a side too
    short to read.
    """

    LEVEL_FT = 10.0          # one raster cell, and one snap level
    MIN_JOG_FT = 40.0
    STREET_PITCH = 20        # cells between street centre lines
    EXTENT_FT = 6000.0
    LIMITS_FT = (1500.0, 1500.0)

    def a_city(self, cells=600):
        """A regular grid whose lanes are cheap but not equally cheap.

        Near-ties are the point: where two lanes of one street cost almost the
        same, the choice falls to centering, which is anchored on a nominal
        that differs between the regions sharing that street.
        """
        cost = np.full((cells, cells), 100.0)
        for offset, lane in enumerate((1.3, 1.0, 1.05, 1.25)):
            for axis in (0, 1):
                view = cost[offset::self.STREET_PITCH, :] if axis == 0 \
                    else cost[:, offset::self.STREET_PITCH]
                np.minimum(view, lane, out=view)
        return a_surface(cost, resolution_m=self.LEVEL_FT * geometry.FT, style="angled")

    def a_target(self, insets=(0.0, 91.0, 44.0, 17.0)):
        """A rectangle with a ragged edge, the way a shoreline is ragged.

        A square splits into regions that are mirror images, so every sibling
        shares one nominal and the tie never arises. Real coastlines give
        siblings nominals a fraction of a level apart, which is the whole
        problem, so the target has to be irregular to exercise it.
        """
        band = self.EXTENT_FT / len(insets)
        edge = [point for index, inset in enumerate(insets)
                for point in ((inset, index * band), (inset, (index + 1) * band))]
        return Polygon([(self.EXTENT_FT, 0.0), (self.EXTENT_FT, self.EXTENT_FT)]
                       + edge[::-1])

    def a_partition(self):
        return geometry.partition(
            self.a_target(), limits_ft=self.LIMITS_FT, deviation_ft=150.0,
            chooser=self.a_city(), snap_ft=self.LEVEL_FT, min_run_ft=150.0,
            min_jog_ft=self.MIN_JOG_FT, sample_step_ft=self.LEVEL_FT,
        )

    def slivers(self, polygons):
        """Pairs sharing a boundary too short to be an edge at all."""
        return sorted(
            round(float(left.intersection(right).length), 2)
            for index, left in enumerate(polygons) for right in polygons[index + 1:]
            if 1e-6 < left.intersection(right).length < self.MIN_JOG_FT - 1e-6
        )

    def seam_steps(self, polygons):
        """Interior sides of a shared edge shorter than a readable jog.

        Interior only: a short side at the end of a shared edge is a T-junction
        with a third plate, which is a different shape of problem.
        """
        steps = []
        for index, left in enumerate(polygons):
            for right in polygons[index + 1:]:
                shared = left.intersection(right)
                for part in getattr(shared, "geoms", [shared]):
                    if part.geom_type != "LineString" or len(part.coords) < 4:
                        continue
                    sides = np.hypot(*np.diff(np.asarray(part.coords), axis=0).T)
                    steps += [round(float(side), 2) for side in sides[1:-1]
                              if 1e-6 < side < self.MIN_JOG_FT - 1e-6]
        return sorted(steps)

    def test_plates_down_one_street_share_an_edge_or_a_corner(self):
        result = self.a_partition()
        self.assertGreater(len(result.cuts), 8)      # a real partition, not one cut
        self.assertEqual(self.slivers(result.polygons), [])

    def test_no_seam_steps_by_less_than_a_readable_jog(self):
        result = self.a_partition()
        plates, _ = geometry.compact_chunks(result.polygons, limits_ft=self.LIMITS_FT)
        self.assertEqual(self.seam_steps(plates), [])

    def test_the_partition_is_still_exact(self):
        result = self.a_partition()
        coverage = geometry.validate_partition(
            self.a_target(), result.polygons, tolerance_ft2=1.0
        )
        self.assertLessEqual(coverage["uncovered_area_ft2"], coverage["tolerance_ft2"])
        self.assertLessEqual(coverage["excess_area_ft2"], coverage["tolerance_ft2"])

    def test_seams_still_follow_the_streets(self):
        # Straightness must not be bought by routing a seam through the blocks.
        for cut in self.a_partition().cuts:
            self.assertEqual(cut["blocked_samples"], 0)


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
