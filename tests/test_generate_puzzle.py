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
import math
import re
import sys
import unittest
from pathlib import Path

import numpy as np
import shapely
import shapely.affinity
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_puzzle  # noqa: E402
from generate_puzzle import (Grid, Layout, PrintProfile, PuzzleError,  # noqa: E402
                             plate_colouring, plate_offsets, plate_extent, offset_transform,
                             choose_layout, cut_curves, floor_polygons, knob_interference,
                             cell_box, knob_profile, layout_for, matches_cavity, piece_label,
                             seat_polygons,
                             best_layout, derive_undercut, print_profile,
                             printed_head_ratio)

# A 0.4 mm nozzle at 0.24 mm layers over a 0.2 mm first layer, two 0.45 mm
# walls and an 85%-of-nozzle minimum bead, on a 256 mm plate -- the profile the
# tracked example model was resolved against.
P2S = PrintProfile(0.4, 0.24, 0.2, 0.34, 2, 0.45, 0.42, (256.0, 256.0), 3.0, 0.1, "by layer")


def square_cut(pieces=25, size=235.0, clearance=P2S.clearance_mm, seed=0,
               tab=generate_puzzle.DEFAULT_TAB_SIZE, neck=generate_puzzle.DEFAULT_TAB_NECK,
               undercut=None, footprint=None):
    outline = box(0.0, 0.0, size, size) if footprint is None else footprint
    layout = choose_layout(outline, size, size, pieces, 1.6, 0.35)
    if undercut is None:
        undercut = derive_undercut(
            neck * min(layout.grid.cell_width, layout.grid.cell_height),
            clearance, P2S.interference_mm)
    curves = cut_curves(layout.grid, np.random.default_rng(seed), size=tab, neck=neck,
                        undercut=undercut)
    return layout, curves, floor_polygons(layout, curves, clearance, outline)


class GridTests(unittest.TestCase):
    @staticmethod
    def square(size=235.0):
        return box(0.0, 0.0, size, size)

    def test_the_piece_count_is_exact(self):
        for pieces in (1, 4, 12, 24, 25, 36, 100):
            layout = choose_layout(self.square(), 235.0, 235.0, pieces, 3.0, 0.35)
            self.assertEqual(layout.pieces, pieces, pieces)

    def test_the_squarest_grid_wins(self):
        layout = choose_layout(self.square(200.0), 200.0, 200.0, 36, 3.0, 0.35)
        self.assertEqual((layout.grid.rows, layout.grid.cols), (6, 6))
        # A 2:1 map wants a 2:1 grid to get square pieces back out of it.
        layout = choose_layout(box(0.0, 0.0, 200.0, 100.0), 200.0, 100.0, 8, 3.0, 0.35)
        self.assertEqual((layout.grid.rows, layout.grid.cols), (2, 4))
        self.assertAlmostEqual(layout.grid.aspect, 1.0)

    def test_a_count_a_rectangle_cannot_be_divided_into_fails_with_alternatives(self):
        with self.assertRaises(PuzzleError) as raised:
            choose_layout(self.square(), 235.0, 235.0, 23, 1.6, 0.35)
        message = str(raised.exception)
        self.assertIn("23 pieces", message)
        for suggestion in ("20", "24", "25"):
            if suggestion in message:
                break
        else:
            self.fail(f"No workable piece count suggested: {message}")

    def test_a_workable_count_of_the_same_size_is_not_rejected(self):
        """The symmetric case: 24 is next door to 23 and must pass."""
        layout = choose_layout(self.square(), 235.0, 235.0, 24, 1.6, 0.35)
        self.assertEqual(layout.pieces, 24)
        self.assertLessEqual(layout.grid.aspect, 1.6)

    # An L, the shape a street-following plate actually comes out as: it fills
    # its own bounding box and leaves one corner empty.
    L_SHAPE = box(0.0, 0.0, 200.0, 200.0).difference(box(120.0, 120.0, 200.0, 200.0))

    def test_an_irregular_outline_is_cut_to_the_count_it_was_asked_for(self):
        """A generated chunk is whatever polygon the planner cut, so the grid is
        no longer tied to the piece count: what has to come out exactly is the
        number of cells the model actually reaches."""
        reachable = [n for n in range(8, 40)
                     if best_layout(self.L_SHAPE, 200.0, 200.0, n, 1.6, 0.35)]
        self.assertGreater(len(reachable), 5, "an L this simple should offer many counts")
        for pieces in reachable:
            layout = choose_layout(self.L_SHAPE, 200.0, 200.0, pieces, 1.6, 0.35)
            self.assertEqual(layout.pieces, pieces, pieces)
            self.assertLess(layout.pieces, layout.grid.rows * layout.grid.cols,
                            "the empty corner should cost the grid some cells")

    def test_a_count_the_outline_cannot_make_names_counts_it_can(self):
        """Not every count is reachable on an irregular outline, so the refusal
        has to hand back ones that are -- and they have to actually work."""
        unreachable = [n for n in range(8, 40)
                       if not best_layout(self.L_SHAPE, 200.0, 200.0, n, 1.6, 0.35)]
        self.assertTrue(unreachable, "this L should not offer every count")
        with self.assertRaises(PuzzleError) as raised:
            choose_layout(self.L_SHAPE, 200.0, 200.0, unreachable[0], 1.6, 0.35)
        for suggestion in re.findall(r"\b\d+\b", str(raised.exception).split("Try", 1)[1]):
            self.assertIsNotNone(
                best_layout(self.L_SHAPE, 200.0, 200.0, int(suggestion), 1.6, 0.35),
                f"suggested {suggestion} pieces, which does not work either")

    def test_a_cell_the_outline_clips_to_a_crumb_joins_its_neighbour(self):
        """Rejecting those grids instead does not survive a real plate: over the
        tracked Manhattan plan, a plate whose outline carries a thousand vertices
        has almost no grid that escapes clipping something, and the count asked
        for becomes unreachable at every size. So a crumb is absorbed, which is
        what gives an irregular jigsaw its odd border pieces."""
        # A 5 x 5 grid has 40 mm cells starting at x=120; a notch cut back to
        # 125 leaves those cells holding a quarter of themselves or less.
        notched = box(0.0, 0.0, 200.0, 200.0).difference(box(125.0, 125.0, 200.0, 200.0))
        grid = Grid(5, 5, 200.0, 200.0)
        layout, stranded = layout_for(grid, notched, 0.35)
        self.assertEqual(stranded, 0, "every crumb should have found a host")
        self.assertTrue(any(len(group) > 1 for group in layout.groups),
                        "the clipped cells should have been absorbed, not kept")
        # Every cell the model reaches belongs to exactly one piece, and no
        # piece is a crumb.
        owner = layout.owner()
        cell_area = grid.cell_width * grid.cell_height
        for group in layout.groups:
            area = shapely.union_all([cell_box(grid, cell) for cell in group])
            self.assertGreaterEqual(area.intersection(notched).area, 0.35 * cell_area)
        for row in range(grid.rows):
            for col in range(grid.cols):
                covered = cell_box(grid, (row, col)).intersection(notched).area
                self.assertEqual(covered > 1e-6, (row, col) in owner)

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

    def test_the_lock_is_guaranteed_whatever_it_costs_the_knobs_looks(self):
        """The reversal: the undercut used to be capped to keep the printed head
        slim, which on a small piece at a sliceable gap drove the lock to
        nothing. A puzzle that will not hold together is a worse object than one
        with chunky knobs, so the proportion gives instead."""
        clearance, machine = P2S.clearance_mm, P2S.interference_mm
        for neck in (4.70, 5.64, 7.05, 11.28):
            undercut = derive_undercut(neck, clearance, machine)
            self.assertAlmostEqual(undercut - clearance, machine, msg=f"{neck} mm neck")

    def test_a_smaller_piece_buys_its_lock_with_a_chunkier_knob(self):
        """Both halves of a joint lose half the gap, so the same lock on a
        smaller neck prints a fatter head. That is reported, not prevented --
        until it is genuinely a lump on a stalk."""
        clearance = P2S.clearance_mm
        ratios = [printed_head_ratio(neck, clearance,
                                     derive_undercut(neck, clearance, P2S.interference_mm))
                  for neck in (11.28, 7.05, 5.64)]
        self.assertEqual(ratios, sorted(ratios), "a smaller neck must print a fatter head")
        self.assertLess(ratios[0], generate_puzzle.TAB_TARGET_HEAD_RATIO)   # 25 pieces: slim
        self.assertLess(ratios[-1], generate_puzzle.TAB_MAX_HEAD_RATIO)     # 100 pieces: allowed
        # ... but a neck small enough makes a head the run should refuse.
        tiny = 4.03
        self.assertGreater(
            printed_head_ratio(tiny, clearance,
                               derive_undercut(tiny, clearance, P2S.interference_mm)),
            generate_puzzle.TAB_MAX_HEAD_RATIO)

    def test_the_gap_clears_a_bead_laid_on_a_sub_bead_feature(self):
        """Why the gap is two line widths and not one nozzle.

        A city at this scale carries thousands of details finer than one bead --
        the uncut example model has 133 sub-bead islands at the layer that first
        failed. The slicer still lays a full bead down the middle of each, and
        Arachne widens a lone bead up to about twice nominal, so it can spill a
        line width past the feature. Two facing across a seam need two line
        widths between them.

        Measured against the real slicer on a hundred-piece cut: 0.40 mm and
        0.50 mm are refused, 0.84 mm slices clean."""
        self.assertAlmostEqual(P2S.clearance_mm, 2 * P2S.outer_wall_line_width_mm)
        self.assertGreater(P2S.clearance_mm, P2S.nozzle_mm)
        self.assertGreaterEqual(P2S.clearance_mm, 2 * 0.42 - 1e-9)

    def test_a_knob_too_big_for_its_edge_is_refused(self):
        with self.assertRaises(PuzzleError):
            knob_profile(10.0, 9.0, 2.0, 0.25)
        with self.assertRaises(PuzzleError):
            knob_profile(47.0, 0.24 * 47.0, 0.2 * 47.0, 40.0)


class ProfileTests(unittest.TestCase):
    """Every clearance the cut defends comes off the project's own settings."""

    SETTINGS = json.dumps({
        "nozzle_diameter": ["0.4"], "layer_height": "0.24", "wall_loops": "2",
        "initial_layer_print_height": "0.2", "min_bead_width": "85%",
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
        self.assertAlmostEqual(
            derive_undercut(11.28, profile.clearance_mm, profile.interference_mm), 0.84 + 0.2)
        self.assertAlmostEqual(profile.narrowest_knob_neck_mm, 1.8)   # 2 * 2 walls * 0.45
        self.assertAlmostEqual(profile.crumb_mm3, 0.4 ** 2 * 0.24)
        self.assertAlmostEqual(profile.vertical_clearance_mm, 0.48)   # two layers

    def test_the_narrowest_printable_section_is_two_minimum_beads(self):
        """Measured against Bambu on a hundred-piece cut of the tracked model:
        every layer it dropped was 0.644 mm across or narrower, every layer it
        kept was 0.677 mm or wider. Two minimum beads is 0.68 mm, and it is the
        bound the empty-layer check has to use -- a piece with an empty layer is
        refused outright, where the uncut map never notices because the layer
        belongs to one object spanning the whole city."""
        profile = print_profile(self.SETTINGS)
        self.assertAlmostEqual(profile.min_bead_width_mm, 0.34)      # 85% of nozzle
        self.assertAlmostEqual(profile.minimum_printable_width_mm, 0.68)
        # A profile stating the bead in millimetres is read the same way.
        settings = json.loads(self.SETTINGS)
        settings["min_bead_width"] = "0.3"
        self.assertAlmostEqual(
            print_profile(json.dumps(settings).encode()).minimum_printable_width_mm, 0.6)

    def test_layers_are_numbered_the_way_the_slicer_numbers_them(self):
        """The first layer has its own height, so a section measured against an
        evenly spaced ladder would be half a layer out all the way up."""
        profile = print_profile(self.SETTINGS)
        self.assertAlmostEqual(profile.layer_top_mm(1), 0.44)        # 0.2 + 0.24
        self.assertAlmostEqual(profile.layer_top_mm(50), 12.2)
        # A layer counts when its top is above the floor and at or below the peak.
        self.assertEqual(list(profile.layer_indices(1.64, 2.6)), [7, 8, 9, 10])
        self.assertEqual(list(profile.layer_indices(12.2, 12.2)), [])

    def test_a_brim_is_kept_only_when_it_cannot_reach_across_a_seam(self):
        """A brim is laid outward from every object and clipped back from the
        others by the object gap. Where an extrusion still fits in what is left,
        the first layer of the print welds the whole puzzle into a tile."""
        profile = print_profile(self.SETTINGS)
        # 0.84 - 2*0.1 = 0.64 mm free, and a 0.42 mm line fits, so it is dropped.
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


class CavityTests(unittest.TestCase):
    """A sealed chamber inside a piece is only the cut's fault if the cut made it.

    `generate_3mf` runs the assembly Boolean only under `--full-validation`, so a
    model generated without it has never had the check applied. Plate A5 of the
    tracked Manhattan plan turns out to carry three sealed chambers of its own,
    and the puzzle cut from it reported one of them verbatim -- (0.459 mm3,
    0.234 mm). Failing the puzzle for that blames the cut for the map.
    """

    A5_SOURCE = [(1.710, 0.265), (0.474, 0.241), (0.459, 0.234)]

    def test_a_chamber_the_map_already_had_is_recognised(self):
        self.assertTrue(matches_cavity((0.459, 0.234), self.A5_SOURCE))
        self.assertTrue(matches_cavity((1.710, 0.265), self.A5_SOURCE))

    def test_a_chamber_the_map_does_not_have_is_not_excused(self):
        """The symmetric case: a void the cut sealed off must still be caught."""
        self.assertFalse(matches_cavity((6.2, 0.51), self.A5_SOURCE))
        self.assertFalse(matches_cavity((0.459, 0.90), self.A5_SOURCE))   # same size, far deeper
        self.assertFalse(matches_cavity((0.90, 0.234), self.A5_SOURCE))   # same depth, far bigger

    def test_matching_survives_the_wobble_of_a_second_boolean(self):
        """The piece is unioned separately from the whole map, so the same void
        measures slightly differently; matching has to tolerate that and no more."""
        volume, thickness = self.A5_SOURCE[2]
        self.assertTrue(matches_cavity((volume * 1.01, thickness * 0.99), self.A5_SOURCE))
        self.assertFalse(matches_cavity((volume * 1.30, thickness), self.A5_SOURCE))

    def test_nothing_is_excused_when_the_map_has_no_chambers(self):
        self.assertFalse(matches_cavity((0.459, 0.234), []))


class PlateTests(unittest.TestCase):
    """The map keeps every millimetre of itself, and the plate pays instead.

    A gap wide enough for the slicer has to exist between two pieces printed
    side by side. If they are neighbours in the map it can only come out of the
    map surface, the joint clearance or the lock -- all three measurably bad. So
    neighbours are not printed together, and then there is nothing to pay.
    """

    @staticmethod
    def square(size=235.0):
        return box(0.0, 0.0, size, size)

    def test_no_two_pieces_on_a_plate_are_neighbours(self):
        for pieces in (4, 25, 63, 100):
            layout = choose_layout(self.square(), 235.0, 235.0, pieces, 1.6, 0.35)
            plates = plate_colouring(layout)
            self.assertEqual(len(plates), layout.pieces)
            owner = layout.owner()
            for (row, col), piece in owner.items():
                for cell in ((row, col + 1), (row + 1, col)):
                    other = owner.get(cell)
                    if other is not None and other != piece:
                        self.assertNotEqual(
                            plates[piece], plates[other],
                            f"{pieces} pieces: {piece} and {other} interlock but share a plate")

    def test_a_grid_takes_two_plates(self):
        """A grid is bipartite, so a rectangle never needs a third plate."""
        for pieces in (4, 16, 25, 100):
            layout = choose_layout(self.square(), 235.0, 235.0, pieces, 1.6, 0.35)
            self.assertLessEqual(len(set(plate_colouring(layout))), 2, pieces)

    def test_an_absorbed_crumb_still_gets_a_workable_colouring(self):
        """A piece that swallowed a clipped neighbour spans cells of both
        colours, so cell parity is not a colouring and the real adjacency graph
        has to be coloured instead. It may need a third plate; it must never
        leave two interlocking pieces together."""
        notched = box(0.0, 0.0, 200.0, 200.0).difference(box(125.0, 125.0, 200.0, 200.0))
        layout, _ = layout_for(Grid(5, 5, 200.0, 200.0), notched, 0.35)
        plates = plate_colouring(layout)
        owner = layout.owner()
        for (row, col), piece in owner.items():
            for cell in ((row, col + 1), (row + 1, col)):
                other = owner.get(cell)
                if other is not None and other != piece:
                    self.assertNotEqual(plates[piece], plates[other])

    def test_the_step_parts_two_diagonal_corners_by_the_clearance(self):
        """Neighbours are on other plates, so the closest pair left on a plate
        is two pieces meeting at a corner. Stepping every piece out by one
        `spacing` per cell parts that corner along the diagonal, so the step
        only has to be the clearance over root two."""
        layout = choose_layout(self.square(), 235.0, 235.0, 100, 1.6, 0.35)
        clearance = P2S.clearance_mm
        spacing = clearance / math.sqrt(2)
        offsets = plate_offsets(layout, spacing)
        owner = layout.owner()
        a, b = owner[(4, 4)], owner[(5, 5)]
        (ax, ay), (bx, by) = offsets[a], offsets[b]
        self.assertAlmostEqual(math.hypot(bx - ax, by - ay), clearance, places=9)

    def test_the_plate_grows_by_one_step_a_seam_and_the_map_does_not(self):
        layout = choose_layout(self.square(), 235.0, 235.0, 100, 1.6, 0.35)
        spacing = P2S.clearance_mm / math.sqrt(2)
        width, height = plate_extent(layout, 235.0, 235.0, spacing)
        self.assertAlmostEqual(width, 235.0 + 9 * spacing)
        self.assertAlmostEqual(height, 235.0 + 9 * spacing)
        # The seats are what the map surface is cut to. At zero kerf they are
        # the whole of the grid, so nothing of the map is thrown away.
        seats = seat_polygons(layout, (0.0, 0.0), 0.0)
        self.assertAlmostEqual(shapely.union_all(seats).area, 235.0 * 235.0, places=6)
        # And at a kerf they are not: that is the map the old cut deleted.
        eaten = seat_polygons(layout, (0.0, 0.0), P2S.clearance_mm)
        self.assertLess(shapely.union_all(eaten).area, 235.0 * 235.0 - 3000.0)

    def test_placing_a_piece_moves_it_and_nothing_else(self):
        moved = offset_transform("1 0 0 0 1 0 0 0 1 10.5 10.5 0.0", 2.5, -1.25)
        self.assertEqual(moved.split()[:9], "1 0 0 0 1 0 0 0 1".split())
        self.assertAlmostEqual(float(moved.split()[9]), 13.0)
        self.assertAlmostEqual(float(moved.split()[10]), 9.25)
        self.assertAlmostEqual(float(moved.split()[11]), 0.0)
        with self.assertRaises(PuzzleError):
            offset_transform("1 0 0", 1.0, 1.0)


class CutTests(unittest.TestCase):
    def test_one_curve_per_interior_edge(self):
        grid = Grid(4, 5, 200.0, 160.0)
        curves = cut_curves(grid, np.random.default_rng(1), size=0.2, neck=0.24, undercut=0.25)
        expected = grid.rows * (grid.cols - 1) + grid.cols * (grid.rows - 1)
        self.assertEqual(len(curves), expected)
        # Every key names the two cells the edge separates, both on the grid.
        for (a, b) in curves:
            for row, col in (a, b):
                self.assertTrue(0 <= row < grid.rows and 0 <= col < grid.cols)

    def test_exactly_n_single_pieces_come_out(self):
        for pieces in (4, 9, 25, 48):
            layout, _, polygons = square_cut(pieces)
            self.assertEqual(len(polygons), pieces)
            for polygon in polygons:
                self.assertEqual(polygon.geom_type, "Polygon")
                self.assertTrue(polygon.is_valid)
                self.assertEqual(len(polygon.interiors), 0)

    def test_the_clearance_is_the_gap_between_neighbours(self):
        clearance = 0.24
        layout, _, polygons = square_cut(25, clearance=clearance)
        grid = layout.grid
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
        layout, _, polygons = square_cut(25, clearance=0.4)
        union_bounds = np.array([polygon.bounds for polygon in polygons])
        self.assertAlmostEqual(union_bounds[:, 0].min(), 0.0, places=9)
        self.assertAlmostEqual(union_bounds[:, 1].min(), 0.0, places=9)
        self.assertAlmostEqual(union_bounds[:, 2].max(), layout.grid.width, places=9)
        self.assertAlmostEqual(union_bounds[:, 3].max(), layout.grid.height, places=9)

    def test_pieces_tile_the_map_apart_from_the_gaps(self):
        layout, curves, polygons = square_cut(25, clearance=0.2)
        covered = sum(polygon.area for polygon in polygons)
        gap = sum(curve.length for curve in curves.values()) * 0.2
        area = layout.grid.width * layout.grid.height
        self.assertLess(area - covered, gap * 1.3)
        self.assertGreater(area - covered, gap * 0.5)

    def test_pieces_do_not_overlap(self):
        _, _, polygons = square_cut(16)
        for index, polygon in enumerate(polygons):
            for other in polygons[index + 1:]:
                self.assertLess(polygon.intersection(other).area, 1e-9)

    def test_every_piece_is_inside_the_map(self):
        layout, _, polygons = square_cut(25)
        outline = box(0.0, 0.0, layout.grid.width, layout.grid.height)
        for polygon in polygons:
            self.assertLess(polygon.difference(outline).area, 1e-9)

    def test_knobs_that_reach_far_enough_to_collide_are_refused(self):
        """Two knobs that overlap merge their pieces, which would silently
        deliver fewer pieces than were asked for."""
        with self.assertRaises(PuzzleError) as raised:
            square_cut(25, tab=0.9, neck=0.5)
        self.assertIn("knobs overlapped", str(raised.exception))

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
        layout, _, polygons = square_cut(25, clearance=P2S.clearance_mm)
        rectangles = seat_polygons(layout, (0.0, 0.0), P2S.clearance_mm)
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
