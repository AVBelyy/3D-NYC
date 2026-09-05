import sys
import unittest
from pathlib import Path

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from terrain_relief import absolute_elevation_to_mm, choose_terrain_relief  # noqa: E402


class TerrainReliefTests(unittest.TestCase):
    def test_naturally_printable_city_relief_is_not_enlarged(self):
        result = choose_terrain_relief(
            np.linspace(0, 30, 1001),
            scale_denominator=16761,
            vertical_exaggeration=1,
            layer_height_mm=0.24,
        )
        self.assertEqual(result.factor, 1)
        self.assertGreaterEqual(result.adjusted_levels, 6)
        self.assertIn("already printable", result.mode)

    def test_real_but_small_relief_uses_capped_citywide_factor(self):
        result = choose_terrain_relief(
            np.linspace(10, 13, 1001),
            scale_denominator=10000,
            vertical_exaggeration=1,
            layer_height_mm=0.24,
        )
        self.assertEqual(result.factor, 3)
        self.assertGreater(result.adjusted_levels, result.unadjusted_levels)

    def test_genuinely_flat_source_is_not_given_invented_relief(self):
        result = choose_terrain_relief(
            np.linspace(10, 10.4, 1001),
            scale_denominator=1000,
            vertical_exaggeration=1,
            layer_height_mm=0.24,
        )
        self.assertEqual(result.factor, 1)
        self.assertIn("flat", result.mode)

    def test_absolute_terrain_scales_but_relative_feature_height_does_not(self):
        ground = absolute_elevation_to_mm(
            np.array([10.0]), origin_m=0, scale_denominator=10000,
            vertical_exaggeration=1, terrain_factor=2, minimum_terrain_mm=2.4,
        )[0]
        relative_building_height = 8 * 1000 / 10000
        self.assertAlmostEqual(ground, 4.4)
        self.assertAlmostEqual((ground + relative_building_height) - ground, 0.8)


if __name__ == "__main__":
    unittest.main()
