import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from generate_3mf import parser,prime_tower_layout,tower_free_layout


class PlateLayoutTests(unittest.TestCase):
    def test_narrow_model_is_centered_with_prime_tower(self):
        layout=prime_tower_layout(54.375,250.)
        self.assertTrue(layout['fits'])
        self.assertEqual(layout['translation_mm'],[100.8125,3.,0.])
        self.assertAlmostEqual(layout['model_to_tower_clearance_mm'],53.9125)

    def test_tower_free_model_is_centered_in_both_axes(self):
        translation,brim=tower_free_layout(54.375,250.)
        self.assertEqual(translation,[100.8125,3.,0.])
        self.assertAlmostEqual(brim,2.8)

    def test_centering_is_not_sacrificed_to_force_tower_fit(self):
        layout=prime_tower_layout(200.,200.)
        self.assertFalse(layout['fits'])
        self.assertEqual(layout['translation_mm'],[28.,28.,0.])


class CommandLineTests(unittest.TestCase):
    def test_slicing_is_opt_in(self):
        self.assertFalse(parser().parse_args([]).slice)
        self.assertTrue(parser().parse_args(['--slice']).slice)


if __name__=='__main__':unittest.main()
