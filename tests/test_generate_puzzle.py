"""The puzzle cut's geometry contract, independent of any map or cache.

Three things here are worth more than the rest.  ``--pieces`` is exact, so a
count that cannot be divided into acceptable pieces has to fail loudly rather
than quietly produce ribbons.  The undercut is a physical fit allowance -- the
interference two printed pieces must flex past -- so the finished curve has to
have that interference and no more, whatever the control points behind it are
doing.  And because both halves of a joint are eroded by half the clearance,
the lock that survives is ``undercut - clearance``: an undercut sized on its own
leaves a puzzle that falls apart in the hand.
"""

import json
import sys
import unittest
from pathlib import Path

import numpy as np
import shapely.affinity
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_puzzle  # noqa: E402
from generate_puzzle import (Grid, PrintProfile, PuzzleError, choose_grid,  # noqa: E402
                             cut_curves, floor_polygons, knob_interference, knob_profile,
                             piece_label, piece_rectangles, print_profile)

# A 0.4 mm nozzle at 0.24 mm layers with two 0.45 mm walls on a 256 mm plate --
# the profile the tracked example model was resolved against.
P2S = PrintProfile(0.4, 0.24, 2, 0.45, 0.42, (256.0, 256.0), 3.0, 0.1, "by layer")


def square_cut(pieces=25, size=235.0, clearance=P2S.clearance_mm, seed=0,
               tab=generate_puzzle.DEFAULT_TAB_SIZE, neck=generate_puzzle.DEFAULT_TAB_NECK,
               undercut=P2S.undercut_mm):
    grid = choose_grid(size, size, pieces, 1.6)
    curves = cut_curves(grid, np.random.default_rng(seed), size=tab, neck=neck,
                        undercut=undercut)
    return grid, curves, floor_polygons(grid, curves, clearance)


class GridTests(unittest.TestCase):
    def test_the_piece_count_is_exact(self):
        for pieces in (1, 4, 12, 24, 25, 36, 100):
            grid = choose_grid(235.0, 235.0, pieces, 3.0)
            self.assertEqual(grid.pieces, pieces, pieces)

    def test_the_squarest_factorisation_wins(self):
        self.assertEqual((choose_grid(200.0, 200.0, 36, 3.0).rows,
                          choose_grid(200.0, 200.0, 36, 3.0).cols), (6, 6))
        # A 2:1 map wants a 2:1 grid to get square pieces back out of it.
        grid = choose_grid(200.0, 100.0, 8, 3.0)
        self.assertEqual((grid.rows, grid.cols), (2, 4))
        self.assertAlmostEqual(grid.aspect, 1.0)

    def test_a_count_that_cannot_be_divided_fails_and_names_alternatives(self):
        with self.assertRaises(PuzzleError) as raised:
            choose_grid(235.0, 235.0, 23, 1.6)
        message = str(raised.exception)
        self.assertIn("23 pieces", message)
        self.assertIn("1 x 23", message)
        for suggestion in ("20", "24", "25"):
            if suggestion in message:
                break
        else:
            self.fail(f"No workable piece count suggested: {message}")

    def test_a_workable_count_of_the_same_size_is_not_rejected(self):
        """The symmetric case: 24 is next door to 23 and must pass."""
        grid = choose_grid(235.0, 235.0, 24, 1.6)
        self.assertEqual(grid.pieces, 24)
        self.assertLessEqual(grid.aspect, 1.6)

    def test_piece_labels_are_unique_and_spreadsheet_shaped(self):
        labels = [piece_label(row, col) for row in range(3) for col in range(30)]
        self.assertEqual(len(set(labels)), len(labels))
        self.assertEqual(piece_label(0, 0), "A1")
        self.assertEqual(piece_label(2, 26), "AA3")


class KnobTests(unittest.TestCase):
    def test_the_finished_curve_has_the_interference_that_was_asked_for(self):
        for length in (20.0, 47.0, 90.0):
            for undercut in (0.0, 0.15, 0.25, 0.6):
                profile = knob_profile(length, 0.24 * length, 0.20 * length, undercut)
                self.assertAlmostEqual(knob_interference(profile), undercut, delta=0.01,
                                       msg=f"{length} mm edge, {undercut} mm undercut")

    def test_the_neck_is_the_only_throat(self):
        """The reported bug: a shoulder that sweeps out and back is a second,
        wider interference that no parameter names, so a joint built to 0.25 mm
        was really built to 0.75 mm and would not have gone together."""
        profile = knob_profile(47.0, 0.24 * 47.0, 0.20 * 47.0, 0.25)
        peak = int(np.argmax(profile[:, 1]))
        rising = profile[:peak + 1]
        neck_height = generate_puzzle.TAB_NECK_HEIGHT * profile[peak, 1]
        shoulder = rising[rising[:, 1] <= neck_height]
        # Below the neck the knob may only get narrower, so its left boundary
        # may only move right.
        self.assertTrue(np.all(np.diff(shoulder[:, 0]) >= -1e-9),
                        "the shoulder doubles back and pinches the joint")

    def test_a_profile_with_a_second_pinch_is_measured_as_worse(self):
        """The symmetric case: a curve that really does have a hidden waist has
        to report the larger number, or the measurement proves nothing."""
        good = knob_profile(47.0, 0.24 * 47.0, 0.20 * 47.0, 0.25)
        peak = int(np.argmax(good[:, 1]))
        neck_height = generate_puzzle.TAB_NECK_HEIGHT * good[peak, 1]
        pinched = good.copy()
        waist = np.zeros(len(good), dtype=bool)
        waist[:peak + 1] = (good[:peak + 1, 1] > 1.0) & (good[:peak + 1, 1] < neck_height)
        self.assertTrue(waist.any(), "no shoulder samples to pinch")
        pinched[waist, 0] += 0.8          # squeeze the shoulder below the neck
        self.assertGreater(knob_interference(pinched), knob_interference(good) + 0.15)

    def test_a_knob_too_big_for_its_edge_is_refused(self):
        with self.assertRaises(PuzzleError):
            knob_profile(10.0, 9.0, 2.0, 0.25)
        with self.assertRaises(PuzzleError):
            knob_profile(47.0, 0.24 * 47.0, 0.2 * 47.0, 40.0)


class ProfileTests(unittest.TestCase):
    """Every clearance the cut defends comes off the project's own settings."""

    SETTINGS = json.dumps({
        "nozzle_diameter": ["0.4"], "layer_height": "0.24", "wall_loops": "2",
        "inner_wall_line_width": "0.45", "outer_wall_line_width": "0.42",
        "brim_type": "outer_only", "brim_width": "3.0", "brim_object_gap": "0.1",
        "printable_area": ["0x0", "256x0", "256x256", "0x256"],
        "print_sequence": "by layer",
    }).encode()

    def test_a_resolved_project_yields_every_bound(self):
        profile = print_profile(self.SETTINGS)
        self.assertTrue(profile.complete())
        self.assertEqual(profile.plate_mm, (256.0, 256.0))
        self.assertAlmostEqual(profile.brim_margin_mm, 3.1)
        self.assertAlmostEqual(profile.clearance_mm, 0.84)      # two outer wall lines
        self.assertAlmostEqual(profile.interference_mm, 0.2)    # half a nozzle
        # Both halves of a joint are eroded by half the gap, so the undercut has
        # to cover the whole gap before any lock is left.
        self.assertAlmostEqual(profile.undercut_mm, 0.84 + 0.2)
        self.assertGreater(profile.undercut_mm, profile.clearance_mm)
        self.assertAlmostEqual(profile.narrowest_knob_neck_mm, 1.8)   # 2 * 2 walls * 0.45
        self.assertAlmostEqual(profile.crumb_mm3, 0.4 ** 2 * 0.24)
        self.assertAlmostEqual(profile.vertical_clearance_mm, 0.48)   # two layers

    def test_a_brim_is_kept_only_when_it_cannot_reach_across_a_seam(self):
        """A brim is laid outward from every object and clipped back from the
        others by the object gap. Where an extrusion still fits in what is left,
        the first layer of the print welds the whole puzzle into a tile."""
        profile = print_profile(self.SETTINGS)
        # 0.84 - 2*0.1 = 0.64 mm free, and a 0.42 mm line fits.
        self.assertTrue(profile.brim_bridges_gap(profile.clearance_mm))
        # 0.4 - 2*0.1 = 0.2 mm free, and it does not.
        self.assertFalse(profile.brim_bridges_gap(0.4))

    def test_a_project_without_a_brim_never_bridges(self):
        settings = json.loads(self.SETTINGS)
        settings["brim_type"] = "no_brim"
        profile = print_profile(json.dumps(settings).encode())
        self.assertFalse(profile.brim_bridges_gap(5.0))
        self.assertEqual(profile.brim_margin_mm, 0.0)

    def test_the_crumb_bound_is_the_validators_own(self):
        """A shell the map's validator would call debris must not be a loose
        puzzle fragment, and the reverse; one definition serves both."""
        from mesh_precision import minimum_printable_shell_volume_mm3
        from validate_3mf import classify_positive_shells
        profile = print_profile(self.SETTINGS)
        _, _, threshold = classify_positive_shells(
            [1.0], profile.nozzle_mm, profile.layer_height_mm)
        self.assertEqual(profile.crumb_mm3, threshold)
        self.assertEqual(threshold, minimum_printable_shell_volume_mm3(
            profile.nozzle_mm, profile.layer_height_mm))

    def test_settings_that_do_not_resolve_are_reported_incomplete(self):
        self.assertFalse(print_profile(None).complete())
        self.assertFalse(print_profile(b"not json").complete())
        self.assertFalse(print_profile(b'{"nozzle_diameter": ["0.4"]}').complete())


class CutTests(unittest.TestCase):
    def test_one_curve_per_interior_edge(self):
        grid = Grid(4, 5, 200.0, 160.0)
        curves = cut_curves(grid, np.random.default_rng(1), size=0.2, neck=0.24, undercut=0.25)
        expected = grid.rows * (grid.cols - 1) + grid.cols * (grid.rows - 1)
        self.assertEqual(len(curves), expected)

    def test_exactly_n_single_pieces_come_out(self):
        for pieces in (4, 9, 25, 48):
            grid, _, polygons = square_cut(pieces)
            self.assertEqual(len(polygons), pieces)
            for polygon in polygons:
                self.assertEqual(polygon.geom_type, "Polygon")
                self.assertTrue(polygon.is_valid)
                self.assertEqual(len(polygon.interiors), 0)

    def test_the_clearance_is_the_gap_between_neighbours(self):
        clearance = 0.24
        grid, _, polygons = square_cut(25, clearance=clearance)
        for row in range(grid.rows):
            for col in range(grid.cols):
                here = polygons[row * grid.cols + col]
                for neighbour in ((row, col + 1), (row + 1, col)):
                    if neighbour[0] >= grid.rows or neighbour[1] >= grid.cols:
                        continue
                    other = polygons[neighbour[0] * grid.cols + neighbour[1]]
                    self.assertAlmostEqual(here.distance(other), clearance, delta=0.02)

    def test_the_outer_edge_keeps_the_models_dimensions(self):
        """Only interior cuts are widened; a puzzle that shrank by a clearance
        on every side would no longer be the map it was cut from."""
        grid, _, polygons = square_cut(25, clearance=0.4)
        union_bounds = np.array([polygon.bounds for polygon in polygons])
        self.assertAlmostEqual(union_bounds[:, 0].min(), 0.0, places=9)
        self.assertAlmostEqual(union_bounds[:, 1].min(), 0.0, places=9)
        self.assertAlmostEqual(union_bounds[:, 2].max(), grid.width, places=9)
        self.assertAlmostEqual(union_bounds[:, 3].max(), grid.height, places=9)

    def test_pieces_tile_the_map_apart_from_the_gaps(self):
        grid, curves, polygons = square_cut(25, clearance=0.2)
        covered = sum(polygon.area for polygon in polygons)
        gap = sum(curve.length for curve in curves) * 0.2
        self.assertLess(grid.width * grid.height - covered, gap * 1.3)
        self.assertGreater(grid.width * grid.height - covered, gap * 0.5)

    def test_pieces_do_not_overlap(self):
        _, _, polygons = square_cut(16)
        for index, polygon in enumerate(polygons):
            for other in polygons[index + 1:]:
                self.assertLess(polygon.intersection(other).area, 1e-9)

    def test_every_piece_is_inside_the_map(self):
        grid, _, polygons = square_cut(25)
        outline = box(0.0, 0.0, grid.width, grid.height)
        for polygon in polygons:
            self.assertLess(polygon.difference(outline).area, 1e-9)

    def test_knobs_that_reach_far_enough_to_collide_are_refused(self):
        """Two knobs that overlap merge their pieces, which would silently
        deliver fewer pieces than were asked for."""
        with self.assertRaises(PuzzleError) as raised:
            square_cut(25, tab=0.9, neck=0.5)
        self.assertIn("regions", str(raised.exception))

    def test_a_knob_that_stays_on_its_own_edge_is_not_refused(self):
        """The symmetric case, one notch below the failure above."""
        _, _, polygons = square_cut(25, tab=0.30, neck=0.28)
        self.assertEqual(len(polygons), 25)

    def test_a_joint_locks_only_when_the_undercut_outreaches_the_gap(self):
        """The reported trap: both halves of a joint are eroded by half the
        clearance, so an undercut sized on its own -- half a nozzle, say --
        vanishes entirely once a gap wide enough to separate the pieces is
        subtracted, and the puzzle falls apart in the hand.

        Measured the way the hand does it: pull one piece straight away from its
        neighbour and see whether the knob's head is caught on the way out."""
        clearance = P2S.clearance_mm

        def lock_area(undercut):
            _, _, polygons = square_cut(4, size=200.0, clearance=clearance,
                                        undercut=undercut, seed=3)
            pulls = np.linspace(0.05, 6.0, 60)
            return max(
                max(shapely.affinity.translate(polygons[0], -d * dx, -d * dy)
                    .intersection(polygons[n]).area for d in pulls)
                for n, (dx, dy) in ((1, (1, 0)), (2, (0, 1))))

        self.assertGreater(lock_area(clearance + P2S.interference_mm), 0.05)
        self.assertEqual(lock_area(clearance), 0.0)            # exactly the boundary
        self.assertEqual(lock_area(clearance / 2), 0.0)        # the trap

    def test_a_pieces_own_cell_separates_its_seat_from_its_knobs(self):
        """The floor is extruded to two heights -- full under the piece's own
        surface, recessed under a neighbour's -- and the piece's own rectangle
        is the line between them, so it has to partition the footprint exactly.

        A piece may have no knobs at all: when all four of its edges point
        inward it is all sockets, which is an ordinary jigsaw piece and locks
        just as well. What no piece may lack is a seat."""
        grid, _, polygons = square_cut(25, clearance=P2S.clearance_mm)
        rectangles = piece_rectangles(grid, (0.0, 0.0), P2S.clearance_mm)
        self.assertEqual(len(rectangles), len(polygons))
        knobbed = 0
        for polygon, rectangle in zip(polygons, rectangles):
            seated = polygon.intersection(rectangle)
            knobs = polygon.difference(rectangle)
            self.assertGreater(seated.area, 0.0, "a piece with no seat has nothing to stand on")
            self.assertAlmostEqual(seated.area + knobs.area, polygon.area, places=6)
            knobbed += knobs.area > 0
        self.assertGreater(knobbed, 0, "no piece reaches into another; nothing would interlock")

    def test_the_seed_decides_the_cut(self):
        _, _, first = square_cut(16, seed=7)
        _, _, same = square_cut(16, seed=7)
        _, _, other = square_cut(16, seed=8)
        self.assertTrue(all(a.equals(b) for a, b in zip(first, same)))
        self.assertFalse(all(a.equals(b) for a, b in zip(first, other)))


if __name__ == "__main__":
    unittest.main()
