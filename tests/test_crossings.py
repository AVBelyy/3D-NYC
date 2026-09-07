import sys
import unittest
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Polygon, box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from crossings import (  # noqa: E402
    carried_structures,
    constrain_deck_to_visible_surface,
    fit_linear_elevation_profile,
    minimum_crossing_floor_mm,
    minimum_crossing_length_mm,
    printable_tunnel_profile,
    structural_roof_thickness_mm,
    tunnel_surface_masks,
)


class ElevationProfileTests(unittest.TestCase):
    def test_bridge_profile_retains_grade_and_rejects_outlier(self):
        line = LineString([(0, 0), (100, 0)])
        x = np.linspace(0, 100, 11)
        z = 20 + x * 0.03
        z[5] += 20
        result = fit_linear_elevation_profile(
            line, np.column_stack([x, np.zeros_like(x)]), z, 0, "surveyed bridge points"
        )
        self.assertAlmostEqual(result.start, 20.0, delta=0.1)
        self.assertAlmostEqual(result.end, 23.0, delta=0.1)
        self.assertEqual(result.rejected_outliers, 1)

    def test_sparse_profile_uses_constant_median(self):
        line = LineString([(0, 0), (100, 0)])
        result = fit_linear_elevation_profile(
            line, [[49, 0], [51, 0]], [12, 14], 0, "polygon Z"
        )
        self.assertEqual(result.start, 13)
        self.assertEqual(result.end, 13)


class PrintableCrossingTests(unittest.TestCase):
    def test_structural_roof_is_layer_aligned_and_at_least_three_layers(self):
        self.assertAlmostEqual(structural_roof_thickness_mm(.4,.24),.72)
        self.assertAlmostEqual(structural_roof_thickness_mm(.4,.08),.64)

    def test_crossing_floor_retains_one_layer_above_base(self):
        self.assertAlmostEqual(minimum_crossing_floor_mm(1.8,.24),2.04)
        self.assertAlmostEqual(minimum_crossing_floor_mm(1.8,.24,.48),2.52)

    def test_untagged_surface_road_over_tunnel_is_not_repainted_green(self):
        tunnel=np.ones((3,3),dtype=bool)
        park=np.ones_like(tunnel)
        bridges=np.zeros_like(tunnel)
        upper=np.zeros_like(tunnel);upper[:,1]=True
        restore,protected=tunnel_surface_masks(tunnel,park,bridges,upper)
        self.assertTrue(np.all(protected[:,1]))
        self.assertFalse(np.any(restore[:,1]))
        self.assertTrue(np.all(restore[:,[0,2]]))

    def test_overlapping_bridge_grade_cannot_cut_the_visible_deck(self):
        # A shared structure's other mapped way fitted a higher grade. The
        # selected visible top, not that higher fit, must constrain its void.
        surface,protected=constrain_deck_to_visible_surface(
            [6.5,6.5,6.5],[np.nan,6.17,6.20])
        result=printable_tunnel_profile(surface,np.array([5.8,5.8,5.8]),
            minimum_cover_mm=.24,minimum_clearance_mm=.48,
            minimum_evidence_mm=.08,protected_surface_mask=protected)
        self.assertLessEqual(result.ceiling[1],6.17-.24+1e-9)
        self.assertLessEqual(result.ceiling[2],6.20-.24+1e-9)
        self.assertAlmostEqual(result.road[0],5.8)

    def test_small_scale_underpass_gets_hidden_only_clearance(self):
        # About 0.35 mm of scaled geographic separation: real, but too small
        # for a 0.48 mm opening plus a 0.32 mm printable roof.
        road = np.zeros(21)
        surface = 0.35 * np.sin(np.linspace(0, np.pi, 21))
        result = printable_tunnel_profile(
            surface, road,
            minimum_cover_mm=0.32,
            minimum_clearance_mm=0.48,
            minimum_evidence_mm=0.08,
        )
        self.assertTrue(result.accepted)
        self.assertAlmostEqual(result.road[0], road[0])
        self.assertAlmostEqual(result.road[-1], road[-1])
        self.assertGreater(result.maximum_hidden_floor_adjustment_mm, 0.4)
        core = slice(3, -3)
        self.assertTrue(np.all(result.ceiling[core] - result.road[core] >= 0.48 - 1e-9))
        self.assertGreater(result.maximum_portal_roof_overcut_mm, 0)

    def test_flat_false_positive_is_rejected(self):
        road = np.zeros(11)
        result = printable_tunnel_profile(
            np.full(11, 0.04), road,
            minimum_cover_mm=0.32,
            minimum_clearance_mm=0.48,
            minimum_evidence_mm=0.08,
        )
        self.assertFalse(result.accepted)
        self.assertIn("insufficient", result.reason)

    def test_bridge_deck_is_never_treated_as_a_daylight_portal(self):
        surface=np.full(21,3.5)
        baseline=np.full(21,3.3)
        protected=np.ones(21,dtype=bool)
        result=printable_tunnel_profile(surface,baseline,
            minimum_cover_mm=.24,minimum_clearance_mm=.48,
            minimum_evidence_mm=.08,protected_surface_mask=protected)
        self.assertTrue(result.accepted)
        self.assertTrue(np.all(result.ceiling<=surface-.24+1e-9))
        self.assertTrue(np.all(result.ceiling-result.road>=.48-1e-9))
        self.assertAlmostEqual(result.maximum_portal_roof_overcut_mm,0)

    def test_short_real_crossing_uses_two_nozzle_minimum(self):
        self.assertEqual(minimum_crossing_length_mm({"nozzle_mm": 0.4}), 0.8)
        self.assertEqual(minimum_crossing_length_mm({"nozzle_mm": 0.4, "minimum_crossing_length_mm": 1.2}), 1.2)


class CarriedStructureTests(unittest.TestCase):
    """Which surveyed transport structure belongs to which mapped bridge way.

    The fixture is the Riverside Drive Viaduct shape: one long surveyed deck
    carrying a roadway, with the sidewalks OSM maps as their own bridge ways
    running along it.
    """

    VIADUCT = box(0, 0, 400, 80)
    ROADWAY = LineString([(0, 40), (400, 40)])
    SIDEWALK = LineString([(20, 74), (380, 74)])
    CROSSING_STUB = LineString([(200, 6), (200, 74)])

    def test_a_viaduct_sidewalk_yields_the_deck_to_the_roadway_it_flanks(self):
        keep = carried_structures([self.VIADUCT], self.SIDEWALK, self.ROADWAY,
                                  is_carriageway=False)
        self.assertFalse(bool(keep[0]))

    def test_a_short_crossing_stub_cannot_claim_the_whole_viaduct(self):
        # 1097594161 in manhattan_2m_A5: 48 ft of footway claiming a 149,746
        # square-foot deck, then repainting the Riverside Drive Viaduct tan.
        keep = carried_structures([self.VIADUCT], self.CROSSING_STUB, self.ROADWAY,
                                  is_carriageway=False)
        self.assertFalse(bool(keep[0]))

    def test_a_footbridge_keeps_the_structure_it_actually_carries(self):
        # The symmetric case: a pedestrian span over a road. The carriageway
        # only crosses this structure, so the footway still owns its own deck.
        overpass_structure = box(190, 0, 210, 120)
        overpass = LineString([(200, 0), (200, 120)])
        road_beneath = LineString([(0, 60), (400, 60)])
        keep = carried_structures([overpass_structure], overpass, road_beneath,
                                  is_carriageway=False)
        self.assertTrue(bool(keep[0]))

    def test_a_carriageway_always_keeps_every_structure_it_matches(self):
        keep = carried_structures([self.VIADUCT], self.CROSSING_STUB, self.ROADWAY,
                                  is_carriageway=True)
        self.assertTrue(bool(keep[0]))

    def test_a_trail_keeps_its_deck_when_no_carriageway_bridge_exists(self):
        for other in (None, Polygon()):
            keep = carried_structures([self.VIADUCT], self.SIDEWALK, other,
                                      is_carriageway=False)
            self.assertTrue(bool(keep[0]))

    def test_each_structure_is_judged_on_its_own(self):
        own = box(420, 0, 440, 120)
        keep = carried_structures([self.VIADUCT, own],
                                  LineString([(20, 74), (430, 74)]),
                                  self.ROADWAY, is_carriageway=False)
        self.assertEqual(list(map(bool, keep)), [False, True])

    def test_no_structures_selects_nothing(self):
        self.assertEqual(len(carried_structures([], self.SIDEWALK, self.ROADWAY,
                                                is_carriageway=False)), 0)


if __name__ == "__main__":
    unittest.main()
