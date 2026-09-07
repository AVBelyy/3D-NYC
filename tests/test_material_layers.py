import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _material_layers import surface_color_depth_mm,white_substrate_top
from build_map_meshes import material_solid
from _surface_styles import apply_street_palette,paint_bridge_decks,paint_trail_ribbons


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


class BridgeDeckPaintingTests(unittest.TestCase):
    """One surveyed deck is shared by the roadway and the sidewalks beside it.

    The manhattan_2m_A5 Riverside Drive Viaduct and the manhattan_2m_B6 Third
    Avenue Bridge both came out tan because the footways OSM maps alongside
    them happened to be painted last.
    """

    def decks(self):
        material=np.full((4,6),1,dtype=np.uint8);top=np.zeros((4,6))
        rows,cols=np.mgrid[1:3,0:6]
        carriageway={'rows':rows.ravel(),'cols':cols.ravel(),
            'values':np.full(rows.size,5.),'ivory':True}
        trail={'rows':np.full(6,2),'cols':np.arange(6),
            'values':np.full(6,5.),'ivory':False}
        return material,top,carriageway,trail

    def test_a_carriageway_deck_outranks_a_trail_deck_in_either_source_order(self):
        for order in ([0,1],[1,0]):
            material,top,carriageway,trail=self.decks()
            decks=[carriageway,trail]
            report=paint_bridge_decks(material,top,[decks[i] for i in order],relief_source=.16)
            self.assertTrue(np.all(material[1:3,:]==0),f'source order {order}')
            self.assertEqual(report['trail_deck_cells_reclaimed_by_carriageways'],6)
            np.testing.assert_allclose(top[1:3,:],5.16)

    def test_a_trail_deck_keeps_every_cell_no_carriageway_claims(self):
        # The symmetric case: a real footbridge must still print tan.
        material,top,_,trail=self.decks()
        report=paint_bridge_decks(material,top,[trail],relief_source=.16)
        self.assertTrue(np.all(material[2,:]==3))
        self.assertEqual(report['trail_deck_cells_reclaimed_by_carriageways'],0)
        self.assertEqual(report['trail_decks'],1)
        np.testing.assert_allclose(top[2,:],5.16)

    def test_decks_of_one_class_keep_their_source_order(self):
        material,top,_,_=self.decks()
        first={'rows':np.zeros(2,int),'cols':np.arange(2),'values':np.full(2,4.),'ivory':True}
        second={'rows':np.zeros(2,int),'cols':np.arange(2),'values':np.full(2,9.),'ivory':True}
        paint_bridge_decks(material,top,[first,second],relief_source=0.)
        np.testing.assert_allclose(top[0,:2],9.)

    def test_malformed_decks_are_rejected(self):
        material,top,carriageway,_=self.decks()
        broken={**carriageway,'values':carriageway['values'][:-1]}
        with self.assertRaises(ValueError):
            paint_bridge_decks(material,top,[broken],relief_source=.16)
        with self.assertRaises(ValueError):
            paint_bridge_decks(material,top[:-1],[carriageway],relief_source=.16)


class TrailRibbonPaintingTests(unittest.TestCase):
    """A footway ribbon overlapping a road must not perforate the carriageway.

    The manhattan_2m_C11 failure: mapped footways beside East 28th Street
    overlapped the ivory ribbon, and painting trails after the street palette
    scattered tan cells along a road the map had already coloured ivory.
    """

    def field(self):
        material=np.full((3,6),1,dtype=np.uint8)
        ground=np.full((3,6),4.);top=ground.copy()
        roads=np.zeros((3,6),dtype=bool);roads[1,:]=True
        material[roads]=0;top[roads]=ground[roads]+.16
        return material,top,ground,roads

    def test_a_trail_crossing_a_road_leaves_the_carriageway_ivory(self):
        material,top,ground,roads=self.field()
        trails=np.zeros((3,6),dtype=bool);trails[:,2]=True
        report=paint_trail_ribbons(material,top,ground,trail_mask=trails,
            road_mask=roads,relief_source=.16)
        self.assertTrue(np.all(material[1,:]==0))
        self.assertEqual(material[0,2],3);self.assertEqual(material[2,2],3)
        self.assertEqual(report['trail_cells_yielded_to_carriageways'],1)
        # The trail keeps its relief across the road it yields.
        np.testing.assert_allclose(top[:,2],ground[:,2]+.16)

    def test_a_trail_away_from_any_road_is_still_tan(self):
        # The symmetric case: an ordinary park path must still print tan.
        material,top,ground,roads=self.field()
        trails=np.zeros((3,6),dtype=bool);trails[0,:]=True
        report=paint_trail_ribbons(material,top,ground,trail_mask=trails,
            road_mask=roads,relief_source=.16)
        self.assertTrue(np.all(material[0,:]==3))
        self.assertEqual(report['trail_cells'],6)
        self.assertEqual(report['trail_cells_yielded_to_carriageways'],0)
        np.testing.assert_allclose(top[0,:],ground[0,:]+.16)

    def test_a_road_free_field_paints_every_trail_cell(self):
        material,top,ground,_=self.field()
        trails=np.ones((3,6),dtype=bool)
        paint_trail_ribbons(material,top,ground,trail_mask=trails,
            road_mask=np.zeros((3,6),dtype=bool),relief_source=.16)
        self.assertTrue(np.all(material==3))

    def test_malformed_trail_fields_are_rejected(self):
        material,top,ground,roads=self.field()
        with self.assertRaises(ValueError):
            paint_trail_ribbons(material,top,ground,trail_mask=roads[:-1],
                road_mask=roads,relief_source=.16)
        with self.assertRaises(ValueError):
            paint_trail_ribbons(material,top,ground,trail_mask=roads,
                road_mask=roads,relief_source=float('nan'))


if __name__=='__main__':unittest.main()
