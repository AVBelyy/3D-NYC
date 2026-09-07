import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _material_layers import surface_color_depth_mm,white_substrate_top
from build_map_meshes import material_solid
from _surface_styles import apply_street_palette


class MaterialLayerTests(unittest.TestCase):
    def test_color_depth_is_layer_aligned_and_never_one_layer(self):
        self.assertAlmostEqual(surface_color_depth_mm(.24),.48)
        self.assertAlmostEqual(surface_color_depth_mm(.16),.32)
        self.assertAlmostEqual(surface_color_depth_mm(.20),.40)
        self.assertAlmostEqual(surface_color_depth_mm(.08),.24)

    def test_substrate_is_continuous_ivory_below_ground(self):
        ground=np.array([[2.4,2.8],[3.0,3.2]])
        result=white_substrate_top(ground,np.ones_like(ground,dtype=bool),base_mm=1.8,color_depth_mm=.48)
        np.testing.assert_allclose(result,ground-.48,rtol=0,atol=1e-6)

    def test_ivory_substrate_exists_even_without_visible_ivory_cells(self):
        height=np.full((2,2),2.4,dtype=np.float32)
        substrate=np.full((2,2),2.16,dtype=np.float32)
        material=np.ones((2,2),dtype=np.uint8)
        solid=material_solid(height,substrate,material,np.ones((2,2),bool),0)
        self.assertGreater(solid.volume(),0)

    def test_colored_surface_starts_at_substrate_not_model_base(self):
        height=np.full((2,2),2.4,dtype=np.float32)
        substrate=np.full((2,2),2.16,dtype=np.float32)
        material=np.ones((2,2),dtype=np.uint8)
        solid=material_solid(height,substrate,material,np.ones((2,2),bool),1)
        vertices=solid.to_mesh64().vert_properties[:,:3]
        self.assertAlmostEqual(float(vertices[:,2].min()),2.16,places=5)

    def test_ivory_road_ribbon_overrides_tan_street_field(self):
        material=np.ones((3,3),dtype=np.uint8);ground=np.full((3,3),2.4);top=ground.copy()
        sidewalks=np.zeros((3,3),bool);sidewalks[:,0]=True
        roads=np.zeros((3,3),bool);roads[:,1:]=True;roads[1,0]=True
        report=apply_street_palette(material,top,ground,sidewalk_mask=sidewalks,
            tan_pavement_mask=np.zeros_like(sidewalks),road_mask=roads,relief_source=.16)
        self.assertTrue(np.all(material[:,1:]==0))
        self.assertEqual(material[1,0],0)
        self.assertEqual(material[0,0],3)
        self.assertEqual(report['road_overlaps_repainted_ivory'],1)
        np.testing.assert_allclose(top,ground+.16)


if __name__=='__main__':unittest.main()
