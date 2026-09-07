import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _surface_styles import LAND_COVER_BARE_SOIL,vegetated_ground_mask


GRASS=2
TREE=1
ROAD=6
IMPERVIOUS=7
BUILDING=5
WATER=4


def field_with_bare_stipple(size=40,seed=0):
    """A land-cover lawn delivered the way the 2017 survey delivers one."""
    grid=np.full((size,size),GRASS,np.uint8)
    rng=np.random.default_rng(seed)
    for _ in range(24):
        r,c=rng.integers(2,size-4,2);h,w=rng.integers(1,4,2)
        grid[r:r+h,c:c+w]=LAND_COVER_BARE_SOIL
    return grid


class VegetatedGroundTests(unittest.TestCase):
    def test_bare_stipple_inside_a_lawn_is_the_same_ground(self):
        """The reported defect: bare pixels punched ivory holes through green."""
        grid=field_with_bare_stipple()
        mask=vegetated_ground_mask(grid)
        self.assertTrue(mask.all())
        self.assertGreater((grid==LAND_COVER_BARE_SOIL).sum(),0)

    def test_lawn_with_no_park_polygon_needs_no_other_evidence(self):
        """Land cover is the only evidence for green outside mapped parks.

        The measured case is a fenced brownfield lot the survey reads as
        grass/shrub: 1,549 of its 1,657 ivory cells were bare soil, and the
        remainder were genuinely impervious.
        """
        grid=np.full((30,30),GRASS,np.uint8)
        grid[10:20,10:20]=LAND_COVER_BARE_SOIL
        grid[5,5]=IMPERVIOUS
        mask=vegetated_ground_mask(grid)
        self.assertTrue(mask[10:20,10:20].all())
        self.assertFalse(mask[5,5])

    def test_predominantly_unvegetated_ground_stays_ivory(self):
        """The symmetric case: a dirt lot must not become a lawn.

        A construction site keeps its own surface even when it shares an edge
        with a park, because the region it belongs to is not a field.
        """
        grid=np.full((40,40),ROAD,np.uint8)
        grid[5:35,5:25]=LAND_COVER_BARE_SOIL   # 600-cell lot
        grid[5:35,25:31]=GRASS                 # 180-cell verge along one side
        mask=vegetated_ground_mask(grid)
        self.assertFalse(mask[5:35,5:25].any())
        self.assertTrue(mask[5:35,25:31].all())

    def test_isolated_dirt_lot_ringed_by_pavement_stays_ivory(self):
        grid=np.full((20,20),IMPERVIOUS,np.uint8)
        grid[6:14,6:14]=LAND_COVER_BARE_SOIL
        self.assertFalse(vegetated_ground_mask(grid).any())

    def test_a_lot_is_not_greened_through_a_diagonal_touch(self):
        """Contact is judged on shared edges, as manufacturability is."""
        grid=np.full((8,8),BUILDING,np.uint8)
        grid[1:4,1:4]=LAND_COVER_BARE_SOIL
        grid[4:7,4:7]=GRASS
        mask=vegetated_ground_mask(grid)
        self.assertFalse(mask[1:4,1:4].any())
        self.assertTrue(mask[4:7,4:7].all())

    def test_water_and_built_classes_are_never_ground(self):
        grid=np.array([[WATER,BUILDING,ROAD],[IMPERVIOUS,8,0]],np.uint8)
        self.assertFalse(vegetated_ground_mask(grid).any())

    def test_vegetation_alone_is_unchanged_by_the_bare_soil_rule(self):
        grid=np.array([[TREE,GRASS,ROAD],[ROAD,TREE,BUILDING]],np.uint8)
        np.testing.assert_array_equal(vegetated_ground_mask(grid),
            np.array([[True,True,False],[False,True,False]]))

    def test_degenerate_rasters(self):
        self.assertFalse(vegetated_ground_mask(np.full((4,4),LAND_COVER_BARE_SOIL,np.uint8)).any())
        self.assertTrue(vegetated_ground_mask(np.full((4,4),GRASS,np.uint8)).all())
        self.assertEqual(vegetated_ground_mask(np.zeros((0,3),np.uint8)).shape,(0,3))
        with self.assertRaises(ValueError):
            vegetated_ground_mask(np.zeros(5,np.uint8))

    def test_a_region_is_judged_on_the_extent_it_is_sampled_over(self):
        """Why the padded halo, not the print frame, is classified.

        A field that is mostly dirt within one crop can be mostly lawn once its
        surroundings are included, so the classifier must see every cell the
        neighbouring plate will also see.
        """
        wide=np.full((20,40),GRASS,np.uint8)
        wide[:,4:18]=LAND_COVER_BARE_SOIL
        cropped=wide[:,:20]
        self.assertTrue(vegetated_ground_mask(wide)[:,4:18].all())
        self.assertFalse(vegetated_ground_mask(cropped)[:,4:18].any())

    def test_a_resampled_float_raster_with_nodata_classifies(self):
        """The pipeline hands over a float32 halo whose nodata is NaN."""
        grid=np.full((10,10),float(GRASS),np.float32)
        grid[3:6,3:6]=LAND_COVER_BARE_SOIL
        grid[0,:]=np.nan
        mask=vegetated_ground_mask(grid)
        self.assertTrue(mask[3:6,3:6].all())
        self.assertFalse(mask[0,:].any())


if __name__=='__main__':
    unittest.main()
