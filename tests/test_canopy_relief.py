import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _canopy_relief import measured_canopy_relief


class MeasuredCanopyReliefTests(unittest.TestCase):
    def build(self,heights,vegetation,eligible=None,**overrides):
        if eligible is None:eligible=np.ones_like(heights,dtype=bool)
        kwargs=dict(grid_step_mm=.125,scale_denominator=5600,smoothing_m=0,
            maximum_gap_mm=.75,edge_roll_mm=.5)
        kwargs.update(overrides)
        return measured_canopy_relief(heights,vegetation,eligible,**kwargs)

    def test_measured_heights_remain_varied(self):
        heights=np.full((25,25),8.,dtype=float);heights[:,13:]=24
        relief,mask,report=self.build(heights,np.ones_like(heights,bool))
        self.assertTrue(mask[12,6])
        self.assertTrue(mask[12,18])
        self.assertGreater(relief[12,18],relief[12,6]*2)
        self.assertGreater(report['maximum_height_m'],report['median_height_m'])

    def test_narrow_classification_crack_is_closed(self):
        heights=np.full((25,25),16.,dtype=float)
        vegetation=np.ones_like(heights,bool);vegetation[:,12]=False
        relief,mask,report=self.build(heights,vegetation)
        self.assertTrue(mask[12,12])
        self.assertGreater(relief[12,12],0)
        self.assertGreater(report['closed_gap_cells'],0)

    def test_explicit_trail_exclusion_survives_gap_closing(self):
        heights=np.full((25,25),16.,dtype=float)
        eligible=np.ones_like(heights,bool);eligible[:,11:14]=False
        relief,mask,_=self.build(heights,np.ones_like(heights,bool),eligible)
        self.assertFalse(mask[:,11:14].any())
        self.assertEqual(np.count_nonzero(relief[:,11:14]),0)
        self.assertGreater(relief[12,5],0)


if __name__=='__main__':unittest.main()
