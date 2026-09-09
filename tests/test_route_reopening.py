import sys
import unittest
from pathlib import Path

from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_map_meshes import (REOPENED_ROUTE_FRACTION,merge_span,route_span,span_covered)


class RouteSpanTests(unittest.TestCase):
    """An interchange re-cuts one stretch of one street once per stacked deck.

    Every repeat leaves another floor slab whose walls coincide laterally with
    the last one and whose top sits microns away, and exact CSG answers that
    with point-sized triangles no bounded export repair can widen.
    """

    def test_span_is_measured_along_the_route_not_in_the_plane(self):
        route = LineString([(0, 0), (10, 0), (10, 10)])
        segment = LineString([(10, 2), (10, 6)])
        self.assertEqual(route_span(route, segment), (12., 16.))

    def test_span_ignores_the_direction_the_segment_was_built_in(self):
        route = LineString([(0, 0), (10, 0)])
        self.assertEqual(route_span(route, LineString([(7, 0), (3, 0)])),
                         route_span(route, LineString([(3, 0), (7, 0)])))

    def test_repeated_openings_of_one_stretch_collapse_to_a_single_interval(self):
        spans = merge_span(merge_span([], (2., 6.)), (5., 9.))
        self.assertEqual(spans, [(2., 9.)])

    def test_disjoint_openings_stay_separate(self):
        spans = merge_span(merge_span([], (0., 1.)), (4., 5.))
        self.assertEqual(spans, [(0., 1.), (4., 5.)])

    def test_touching_openings_coalesce(self):
        self.assertEqual(merge_span([(0., 1.)], (1., 2.)), [(0., 2.)])

    def test_stacked_decks_over_one_street_reopen_it_once(self):
        # Seventeen bridge ways resolving to the same short piece of the same
        # lower route: the first cuts, none of the rest may cut again.
        spans, cut = [], 0
        for _ in range(17):
            span = (12., 13.302)
            if span_covered(spans, span) > REOPENED_ROUTE_FRACTION * (span[1] - span[0]):
                continue
            spans = merge_span(spans, span)
            cut += 1
        self.assertEqual(cut, 1)
        self.assertEqual(spans, [(12., 13.302)])

    def test_a_genuinely_different_stretch_of_the_same_street_still_opens(self):
        spans = merge_span([], (0., 4.))
        span = (20., 24.)
        self.assertEqual(span_covered(spans, span), 0.)
        self.assertLessEqual(span_covered(spans, span),
                             REOPENED_ROUTE_FRACTION * (span[1] - span[0]))

    def test_a_mostly_new_stretch_still_opens(self):
        spans = merge_span([], (0., 10.))
        span = (9., 19.)
        self.assertEqual(span_covered(spans, span), 1.)
        self.assertLessEqual(span_covered(spans, span),
                             REOPENED_ROUTE_FRACTION * (span[1] - span[0]))

    def test_the_threshold_is_a_fraction_so_it_is_scale_free(self):
        # The same pair of openings, judged identically at any model scale.
        for scale in (1e-3, 1., 1e3):
            spans = merge_span([], (0., 10. * scale))
            span = (6. * scale, 16. * scale)
            covered = span_covered(spans, span)
            self.assertAlmostEqual(covered / (span[1] - span[0]), .4)


if __name__ == "__main__":
    unittest.main()
