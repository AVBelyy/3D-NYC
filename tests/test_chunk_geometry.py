import math
import sys
import unittest
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Point, Polygon, box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _chunk_geometry as geometry  # noqa: E402
from _chunk_geometry import Frame, PlanGeometryError  # noqa: E402


def a_frame(bearing_deg=27.0, scale=10000.0, origin=(980000.0, 200000.0)):
    return Frame.from_bearing(math.radians(bearing_deg), origin, scale, 0.125)


def an_l_shape():
    """A concave target with a hole, big enough to need cuts on both axes."""
    outer = Polygon([(0, 0), (26000, 0), (26000, 9000), (11000, 9000), (11000, 30000), (0, 30000)])
    return outer.difference(Point(5000, 20000).buffer(2500))


class FrameTests(unittest.TestCase):
    def test_axes_must_be_orthonormal_and_right_handed(self):
        with self.assertRaises(PlanGeometryError):
            Frame((0, 0), (1, 0), (1, 0), 10000.0, 0.125)
        with self.assertRaises(PlanGeometryError):
            Frame((0, 0), (2, 0), (0, 1), 10000.0, 0.125)
        with self.assertRaises(PlanGeometryError):
            Frame((0, 0), (0, 1), (1, 0), 10000.0, 0.125)

    def test_scale_and_grid_step_are_bounded(self):
        with self.assertRaises(PlanGeometryError):
            a_frame(scale=999.0)
        with self.assertRaises(PlanGeometryError):
            Frame((0, 0), (1, 0), (0, 1), 10000.0, 0.9)

    def test_frame_round_trip_preserves_geometry(self):
        frame = a_frame()
        original = box(981000, 201000, 982000, 203000)
        restored = frame.to_world(frame.to_frame(original))
        self.assertLess(
            float(np.abs(
                shapely.get_coordinates(restored) - shapely.get_coordinates(original)
            ).max()),
            1e-6,
        )

    def test_millimetre_conversion_matches_the_scale(self):
        frame = a_frame(scale=10000.0)
        # 10000 ft on the ground is 10000 * 0.3048006 m, printed at 1:10000.
        self.assertAlmostEqual(frame.mm(10000.0), 304.8006096012192, places=6)
        self.assertAlmostEqual(frame.feet(frame.mm(1234.5)), 1234.5, places=6)

    def test_print_frame_origin_sits_on_the_shared_lattice(self):
        frame = a_frame()
        polygon = box(1000.0, 2000.0, 5000.0, 9000.0)
        bounds = geometry.chunk_frame_bounds(frame, polygon)
        payload = frame.print_frame(bounds)
        offset = np.asarray(payload["origin_ft"]) - np.asarray(frame.origin_ft)
        for axis in (frame.x_axis, frame.y_axis):
            steps = float(offset @ np.asarray(axis)) / frame.cell_ft
            self.assertAlmostEqual(steps, round(steps), places=6)
        for side in payload["size_mm"]:
            self.assertAlmostEqual(side / frame.grid_step_mm, round(side / frame.grid_step_mm))

    def test_chunk_frame_bounds_cover_the_polygon(self):
        frame = a_frame()
        polygon = Polygon([(101.3, 202.7), (5003.1, 300.2), (4000.0, 8000.9)])
        minx, miny, maxx, maxy = geometry.chunk_frame_bounds(frame, polygon)
        self.assertTrue(box(minx, miny, maxx, maxy).covers(polygon))


class LimitTests(unittest.TestCase):
    def test_generator_limits_match_generate_3mf(self):
        import generate_3mf

        self.assertEqual(geometry.FT, generate_3mf.FT)
        self.assertEqual(geometry.MAX_PRINT_MM, 250.0)
        self.assertEqual(geometry.MIN_PRINT_MM, 20.0)

    def test_elevation_cell_rule_matches_the_generator_formula(self):
        cells = geometry.elevation_cells(235.0, 235.0, 10000.0, 20.0)
        expected = ((235.0 / 1000 * 10000 + 40) / 0.5) ** 2
        self.assertAlmostEqual(cells, expected)

    def test_max_scale_for_envelope_is_the_binding_scale(self):
        ceiling = geometry.max_scale_for_envelope((235.0, 235.0), 20.0)
        self.assertLessEqual(
            geometry.elevation_cells(235.0, 235.0, ceiling, 20.0),
            geometry.MAX_ELEVATION_CELLS + 1,
        )
        self.assertGreater(
            geometry.elevation_cells(235.0, 235.0, ceiling * 1.01, 20.0),
            geometry.MAX_ELEVATION_CELLS,
        )

    def test_cut_count_reserves_room_for_the_deviation_budget(self):
        self.assertEqual(geometry.plan_cut_count(100.0, 100.0, 0.0), 1)
        self.assertEqual(geometry.plan_cut_count(100.0, 100.0, 25.0), 2)
        with self.assertRaises(PlanGeometryError):
            geometry.plan_cut_count(100.0, 40.0, 20.0)


class RectilinearPathTests(unittest.TestCase):
    def setUp(self):
        self.u = np.linspace(0, 1000, 201)
        rng = np.random.default_rng(0)
        self.v = 50 * np.sin(self.u / 120) + rng.normal(0, 3, self.u.size)

    def test_output_is_axis_parallel_and_monotone(self):
        u, v = geometry.rectilinear_path(
            self.u, self.v, snap_ft=25, min_run_ft=120, min_jog_ft=25
        )
        self.assertTrue(bool((np.diff(u) >= -1e-9).all()))
        deltas = np.diff(np.column_stack([u, v]), axis=0)
        self.assertTrue(bool(np.all((np.abs(deltas[:, 0]) < 1e-9) | (np.abs(deltas[:, 1]) < 1e-9))))

    def test_no_zero_length_edges(self):
        u, v = geometry.rectilinear_path(
            self.u, self.v, snap_ft=25, min_run_ft=120, min_jog_ft=25
        )
        deltas = np.diff(np.column_stack([u, v]), axis=0)
        self.assertTrue(bool((np.hypot(deltas[:, 0], deltas[:, 1]) > 1e-9).all()))

    def test_runs_meet_the_minimum_and_span_the_request(self):
        u, v = geometry.rectilinear_path(
            self.u, self.v, snap_ft=25, min_run_ft=200, min_jog_ft=25
        )
        self.assertAlmostEqual(u[0], self.u[0])
        self.assertAlmostEqual(u[-1], self.u[-1])
        runs = [
            u[index + 1] - u[index] for index in range(len(u) - 1)
            if abs(v[index + 1] - v[index]) < 1e-9
        ]
        self.assertTrue(all(run >= 200 - 1e-6 for run in runs), runs)

    def test_levels_land_on_the_snap_lattice(self):
        _, v = geometry.rectilinear_path(
            self.u, self.v, snap_ft=25, min_run_ft=120, min_jog_ft=25
        )
        self.assertTrue(bool(np.abs(v / 25 - np.round(v / 25)).max() < 1e-9))

    def test_jogs_land_on_the_snap_lattice_too(self):
        """Where a cut jogs matters as much as which level it jogs to.

        The along-coordinate comes from evenly spaced samples, so an unsnapped
        jog can sit a fraction of a step from a seam already running the other
        way, stranding a strip of plate between the two too narrow to print.
        The ends stay where the region ends; only the jogs move.
        """
        u, _ = geometry.rectilinear_path(
            self.u, self.v, snap_ft=25, min_run_ft=120, min_jog_ft=25
        )
        self.assertGreater(len(u), 2, "this fixture should produce a jog")
        interior = u[1:-1]
        self.assertTrue(bool(np.abs(interior / 25 - np.round(interior / 25)).max() < 1e-9))
        self.assertAlmostEqual(u[0], self.u[0])
        self.assertAlmostEqual(u[-1], self.u[-1])

    def test_snapping_a_jog_keeps_the_path_monotone(self):
        u, v = geometry.rectilinear_path(
            self.u, self.v, snap_ft=25, min_run_ft=120, min_jog_ft=25
        )
        self.assertTrue(bool(np.all(np.diff(u) >= -1e-9)))

    def test_mismatched_inputs_are_rejected(self):
        with self.assertRaises(PlanGeometryError):
            geometry.rectilinear_path(
                np.arange(5.0), np.arange(4.0), snap_ft=1, min_run_ft=1, min_jog_ft=1
            )


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.frame = a_frame()
        self.target = an_l_shape()
        self.limits = (self.frame.feet(235.0), self.frame.feet(235.0))

    def partition(self, deviation_mm=12.0):
        return geometry.partition(
            self.target,
            limits_ft=self.limits,
            deviation_ft=self.frame.feet(deviation_mm),
            snap_ft=self.frame.feet(1.0),
            min_run_ft=self.frame.feet(8.0),
            min_jog_ft=self.frame.feet(2.0),
            sample_step_ft=40.0,
        )

    def test_union_equals_the_target_with_no_overlaps(self):
        result = self.partition()
        report = geometry.validate_partition(self.target, result.polygons, tolerance_ft2=1e-6)
        self.assertEqual(report["uncovered_area_ft2"], 0.0)
        self.assertLess(report["excess_area_ft2"], 1e-6)
        self.assertLess(report["maximum_pairwise_overlap_ft2"], 1e-6)

    def test_every_chunk_is_a_single_polygon(self):
        for polygon in self.partition().polygons:
            self.assertEqual(polygon.geom_type, "Polygon")
            self.assertTrue(polygon.is_valid)
            self.assertGreater(polygon.area, 0)

    def test_every_chunk_fits_the_printer_envelope(self):
        result = self.partition()
        plates = geometry.validate_plates(
            self.frame, result.polygons, envelope_mm=(235.0, 235.0), padding_m=20.0
        )
        self.assertEqual(len(plates), len(result.polygons))
        for plate in plates:
            width, height = plate["size_mm"]
            self.assertLessEqual(width, 235.0 + 1e-9)
            self.assertLessEqual(height, 235.0 + 1e-9)

    def test_oversized_plates_are_rejected(self):
        result = self.partition()
        with self.assertRaises(PlanGeometryError):
            geometry.validate_plates(
                self.frame, result.polygons, envelope_mm=(60.0, 60.0), padding_m=20.0
            )

    def test_seams_lie_on_both_neighbouring_boundaries(self):
        result = self.partition()
        seams = geometry.shared_edges(result.polygons)
        self.assertTrue(seams)
        for seam in seams:
            self.assertLess(seam["boundary_offset_ft"], 1e-6)

    def test_a_deliberate_overlap_is_detected(self):
        square = box(0.0, 0.0, 100.0, 100.0)
        overlapping = [box(0.0, 0.0, 60.0, 100.0), box(40.0, 0.0, 100.0, 100.0)]
        with self.assertRaises(PlanGeometryError) as caught:
            geometry.validate_partition(square, overlapping, tolerance_ft2=1e-6)
        self.assertIn("overlap", str(caught.exception))

    def test_a_gap_is_detected(self):
        square = box(0.0, 0.0, 100.0, 100.0)
        with self.assertRaises(PlanGeometryError) as caught:
            geometry.validate_partition(
                square, [box(0.0, 0.0, 40.0, 100.0), box(60.0, 0.0, 100.0, 100.0)],
                tolerance_ft2=1e-6,
            )
        self.assertIn("miss", str(caught.exception))

    def test_straight_cuts_use_fewer_plates_than_wandering_ones(self):
        # Reserving deviation on both sides of every cut costs plate width.
        self.assertLessEqual(
            len(self.partition(deviation_mm=0.0).polygons),
            len(self.partition(deviation_mm=12.0).polygons),
        )

    def test_an_empty_target_is_rejected(self):
        with self.assertRaises(PlanGeometryError):
            geometry.partition(Polygon(), limits_ft=self.limits, deviation_ft=0.0)


class CompactionTests(unittest.TestCase):
    def test_neighbours_that_share_a_plate_are_combined(self):
        limits = (100.0, 100.0)
        pieces = [box(0, 0, 40, 100), box(40, 0, 80, 100)]
        combined, notes = geometry.compact_chunks(pieces, limits_ft=limits)
        self.assertEqual(len(combined), 1)
        self.assertTrue(notes)
        self.assertAlmostEqual(combined[0].area, 8000.0)

    def test_neighbours_that_would_overflow_the_plate_are_left_alone(self):
        limits = (100.0, 100.0)
        pieces = [box(0, 0, 80, 100), box(80, 0, 160, 100)]
        combined, _ = geometry.compact_chunks(pieces, limits_ft=limits)
        self.assertEqual(len(combined), 2)

    def test_chunks_are_not_joined_across_a_pinch(self):
        """Fill is blind to a sliver glued on by its short end.

        Area over bounding box barely moves whichever neighbour absorbs a
        sliver, so the pass will hang one off a contact a millimetre wide. That
        sliver becomes a finger too thin to print on one plate and a slot of
        the same width on the plate wrapped around it.
        """
        sliver = box(100.0, 0.0, 101.0, 60.0)
        above = box(0.0, 60.0, 200.0, 130.0)        # meets the sliver end-on
        merged, notes = geometry.compact_chunks(
            [sliver, above], limits_ft=(260.0, 260.0), min_contact_ft=4.0
        )
        self.assertEqual(len(merged), 2, "a 1-wide contact must not be merged")
        self.assertEqual(notes, [])

    def test_a_long_enough_contact_still_merges(self):
        left, right = box(0.0, 0.0, 100.0, 100.0), box(100.0, 0.0, 200.0, 100.0)
        merged, notes = geometry.compact_chunks(
            [left, right], limits_ft=(260.0, 260.0), min_contact_ft=4.0
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(notes), 1)

    def test_the_guard_is_off_by_default(self):
        sliver = box(100.0, 0.0, 101.0, 60.0)
        above = box(0.0, 60.0, 200.0, 130.0)
        merged, _ = geometry.compact_chunks([sliver, above], limits_ft=(260.0, 260.0))
        self.assertEqual(len(merged), 1)

    def test_compaction_preserves_total_area(self):
        limits = (100.0, 100.0)
        pieces = [box(0, 0, 30, 90), box(30, 0, 60, 90), box(60, 0, 90, 90)]
        combined, _ = geometry.compact_chunks(pieces, limits_ft=limits)
        self.assertAlmostEqual(
            sum(piece.area for piece in combined), sum(piece.area for piece in pieces)
        )

    def test_a_sliver_is_merged_into_its_longest_neighbour(self):
        limits = (100.0, 100.0)
        pieces = [box(0, 0, 60, 90), box(60, 0, 62, 90)]
        merged, notes = geometry.merge_small_chunks(
            pieces, limits_ft=limits, min_side_ft=10.0, min_area_ft2=500.0, min_fill=0.1
        )
        self.assertEqual(len(merged), 1)
        self.assertTrue(notes)

    def test_undersized_reason_names_the_failing_rule(self):
        self.assertIn("printable minimum", geometry.undersized_reason(
            box(0, 0, 2, 90), min_side_ft=10.0, min_area_ft2=1.0, min_fill=0.0
        ))
        self.assertIn("area", geometry.undersized_reason(
            box(0, 0, 20, 20), min_side_ft=1.0, min_area_ft2=1e6, min_fill=0.0
        ))
        self.assertIsNone(geometry.undersized_reason(
            box(0, 0, 90, 90), min_side_ft=10.0, min_area_ft2=100.0, min_fill=0.5
        ))


class LabelTests(unittest.TestCase):
    def test_a_grid_reads_top_left_first(self):
        frame = a_frame()
        limits = (100.0, 100.0)
        chunks = [
            box(0, 100, 100, 200), box(100, 100, 200, 200),
            box(0, 0, 100, 100), box(100, 0, 200, 100),
        ]
        self.assertEqual(
            geometry.grid_labels(frame, chunks, limits), ["A1", "B1", "A2", "B2"]
        )

    def test_a_single_column_does_not_collide(self):
        frame = a_frame()
        chunks = [box(0, index * 100, 100, index * 100 + 100) for index in range(4)]
        labels = geometry.grid_labels(frame, chunks, (100.0, 100.0))
        self.assertEqual(sorted(labels), sorted(set(labels)))
        self.assertEqual(len(labels), 4)

    def test_column_names_continue_past_z(self):
        self.assertEqual(geometry._column_name(0), "A")
        self.assertEqual(geometry._column_name(25), "Z")
        self.assertEqual(geometry._column_name(26), "AA")


class CutChooserTests(unittest.TestCase):
    def test_the_default_chooser_returns_the_straight_nominal_line(self):
        request = geometry.CutRequest(
            geometry.AXIS_X, 0.0, 100.0, 50.0, 10.0, 5.0,
            snap_ft=1.0, min_run_ft=10.0, min_jog_ft=2.0,
        )
        path = geometry.StraightCuts().choose(request)
        self.assertEqual(path.shape, (2, 2))
        self.assertTrue(bool(np.allclose(path[:, 1], 50.0)))

    def test_a_path_outside_the_allowance_is_clamped(self):
        request = geometry.CutRequest(
            geometry.AXIS_X, 0.0, 100.0, 50.0, 5.0, 5.0,
            snap_ft=1.0, min_run_ft=10.0, min_jog_ft=2.0,
        )
        _, across = geometry._validated_path([[0.0, 0.0], [100.0, 999.0]], request)
        self.assertTrue(bool(np.all(across >= 45.0 - 1e-9)))
        self.assertTrue(bool(np.all(across <= 55.0 + 1e-9)))

    def test_a_path_that_doubles_back_is_rejected(self):
        request = geometry.CutRequest(
            geometry.AXIS_X, 0.0, 100.0, 50.0, 5.0, 5.0,
            snap_ft=1.0, min_run_ft=10.0, min_jog_ft=2.0,
        )
        with self.assertRaises(PlanGeometryError):
            geometry._validated_path([[0.0, 50.0], [60.0, 50.0], [30.0, 50.0]], request)

    def test_a_degenerate_path_is_rejected(self):
        request = geometry.CutRequest(
            geometry.AXIS_X, 0.0, 100.0, 50.0, 5.0, 5.0,
            snap_ft=1.0, min_run_ft=10.0, min_jog_ft=2.0,
        )
        with self.assertRaises(PlanGeometryError):
            geometry._validated_path([[0.0, 50.0]], request)


class ContinuationTests(unittest.TestCase):
    """Which already-placed cut a new cut is picking up, and at which end."""

    def placed(self, axis=geometry.AXIS_X, u_start=0.0, u_end=100.0,
               v_start=50.0, v_end=50.0):
        return [geometry.PlacedCut(axis, u_start, u_end, v_start, v_end)]

    def levels(self, placed, axis=geometry.AXIS_X, u_start=100.0, u_end=200.0,
               v_nominal=52.0, deviation_ft=20.0):
        return geometry.continuing_levels(
            placed, axis, u_start, u_end, v_nominal, deviation_ft
        )

    def test_a_cut_ending_where_this_one_starts_is_met_at_the_start(self):
        self.assertEqual(self.levels(self.placed(v_end=45.0)), (45.0, None))

    def test_a_cut_starting_where_this_one_ends_is_met_at_the_end(self):
        placed = self.placed(u_start=200.0, u_end=300.0, v_start=45.0)
        self.assertEqual(self.levels(placed), (None, 45.0))

    def test_a_cut_on_the_other_axis_never_meets(self):
        self.assertEqual(self.levels(self.placed(axis=geometry.AXIS_Y)), (None, None))

    def test_a_cut_elsewhere_along_the_axis_is_a_different_street(self):
        self.assertEqual(self.levels(self.placed(u_start=-500.0, u_end=-400.0)),
                         (None, None))

    def test_a_level_out_of_reach_is_not_one_this_cut_can_meet(self):
        self.assertEqual(self.levels(self.placed(v_end=900.0)), (None, None))

    def test_the_staircase_between_two_regions_is_within_tolerance(self):
        # Sibling bounding boxes overlap by however far the cut separating them
        # wandered, which is bounded by twice the deviation budget.
        self.assertEqual(self.levels(self.placed(u_end=135.0)), (50.0, None))
        self.assertEqual(self.levels(self.placed(u_end=145.0)), (None, None))

    def test_a_cut_beside_this_one_over_the_same_ground_is_a_second_cut(self):
        # Same span, so the two run side by side through one band rather than
        # continuing each other, and neither anchors the other.
        placed = self.placed(u_start=100.0, u_end=200.0, v_start=50.0, v_end=50.0)
        self.assertEqual(self.levels(placed), (None, None))

    def test_the_nearest_cut_wins_when_several_meet_one_end(self):
        placed = (self.placed(u_end=90.0, v_end=45.0)
                  + self.placed(u_end=100.0, v_end=55.0))
        self.assertEqual(self.levels(placed), (55.0, None))


class SplitTests(unittest.TestCase):
    def test_a_staircase_splitter_divides_a_square_exactly(self):
        square = box(0.0, 0.0, 100.0, 100.0)
        line = shapely.LineString([(-10, 40), (50, 40), (50, 60), (110, 60)])
        low, high = geometry.split_region(square, line, geometry.AXIS_Y)
        self.assertEqual((len(low), len(high)), (1, 1))
        self.assertAlmostEqual(low[0].area + high[0].area, square.area)
        self.assertAlmostEqual(low[0].intersection(high[0]).area, 0.0)

    def test_a_splitter_that_misses_leaves_one_side_empty(self):
        square = box(0.0, 0.0, 100.0, 100.0)
        line = shapely.LineString([(-10, 200), (110, 200)])
        low, high = geometry.split_region(square, line, geometry.AXIS_Y)
        self.assertEqual(len(low) + len(high), 1)


if __name__ == "__main__":
    unittest.main()
