import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _material_layers import (COARSEST_LAYER_HEIGHT_MM,DRAWN_LINE_RELIEF_MM,MATERIAL_NAMES,
    drawn_line_relief_mm,pavement_pad_relief_mm,surface_color_depth_mm,white_substrate_top)
from build_map_meshes import material_solid
from _surface_styles import apply_street_palette,paint_bridge_decks,paint_trail_ribbons,stair_tread_mask


class DrawnLineReliefTests(unittest.TestCase):
    """A drawn road is a raised line, not a colour painted onto the pavement."""

    def config(self,layer,**overrides):
        return {"path_relief_mm":.16,"layer_height_mm":layer,**overrides}

    def test_the_symbol_keeps_one_height_on_every_supported_profile(self):
        heights={drawn_line_relief_mm(self.config(layer))
            for layer in [.08,.12,.16,.20,.24]}
        self.assertEqual(heights,{DRAWN_LINE_RELIEF_MM})

    def test_the_default_prints_raised_at_the_coarsest_supported_layer(self):
        from generate_3mf import PROCESS_PRESETS
        self.assertLessEqual(max(PROCESS_PRESETS),COARSEST_LAYER_HEIGHT_MM)
        self.assertGreaterEqual(DRAWN_LINE_RELIEF_MM,2*max(PROCESS_PRESETS))

    def test_a_line_always_out_tops_the_pavement_pad_beside_it(self):
        for layer in [.08,.12,.16,.20,.24]:
            with self.subTest(layer=layer):
                config=self.config(layer,road_line_relief_mm=.01)
                relief=drawn_line_relief_mm(config)
                self.assertGreaterEqual(relief,config["path_relief_mm"]+layer)
                self.assertGreaterEqual(relief,2*layer)

    def test_a_taller_cartographic_line_is_honoured(self):
        self.assertAlmostEqual(drawn_line_relief_mm(self.config(.24,road_line_relief_mm=.96)),.96)

    def test_malformed_relief_inputs_are_rejected(self):
        for config in [
            {"path_relief_mm":.16,"layer_height_mm":0.},
            {"path_relief_mm":-1.,"layer_height_mm":.24},
            {"path_relief_mm":.16,"layer_height_mm":.24,"road_line_relief_mm":0.},
            {"path_relief_mm":.16,"layer_height_mm":.24,"road_line_relief_mm":float("nan")},
        ]:
            with self.subTest(config=config),self.assertRaises(ValueError):
                drawn_line_relief_mm(config)


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
            tan_pavement_mask=np.zeros_like(sidewalks),road_mask=roads,
            relief_source=.16,line_relief_source=.48)
        self.assertTrue(np.all(material[:,1:]==0))
        self.assertEqual(material[1,0],0)
        self.assertEqual(material[0,0],3)
        self.assertEqual(report['road_overlaps_repainted_ivory'],1)
        # The carriageway is a raised line; only the sidewalk keeps the pad.
        np.testing.assert_allclose(top[material==0],ground[material==0]+.48)
        np.testing.assert_allclose(top[material==3],ground[material==3]+.16)


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
            report=paint_bridge_decks(material,top,[decks[i] for i in order],line_relief_source=.16)
            self.assertTrue(np.all(material[1:3,:]==0),f'source order {order}')
            self.assertEqual(report['trail_deck_cells_reclaimed_by_carriageways'],6)
            np.testing.assert_allclose(top[1:3,:],5.16)

    def test_a_trail_deck_keeps_every_cell_no_carriageway_claims(self):
        # The symmetric case: a real footbridge must still print tan.
        material,top,_,trail=self.decks()
        report=paint_bridge_decks(material,top,[trail],line_relief_source=.16)
        self.assertTrue(np.all(material[2,:]==3))
        self.assertEqual(report['trail_deck_cells_reclaimed_by_carriageways'],0)
        self.assertEqual(report['trail_decks'],1)
        np.testing.assert_allclose(top[2,:],5.16)

    def test_decks_of_one_class_keep_their_source_order(self):
        material,top,_,_=self.decks()
        first={'rows':np.zeros(2,int),'cols':np.arange(2),'values':np.full(2,4.),'ivory':True}
        second={'rows':np.zeros(2,int),'cols':np.arange(2),'values':np.full(2,9.),'ivory':True}
        paint_bridge_decks(material,top,[first,second],line_relief_source=0.)
        np.testing.assert_allclose(top[0,:2],9.)

    def test_malformed_decks_are_rejected(self):
        material,top,carriageway,_=self.decks()
        broken={**carriageway,'values':carriageway['values'][:-1]}
        with self.assertRaises(ValueError):
            paint_bridge_decks(material,top,[broken],line_relief_source=.16)
        with self.assertRaises(ValueError):
            paint_bridge_decks(material,top[:-1],[carriageway],line_relief_source=.16)


class TrailRibbonPaintingTests(unittest.TestCase):
    """A footway ribbon overlapping a road must not perforate the carriageway.

    The manhattan_2m_C11 failure: mapped footways beside East 28th Street
    overlapped the ivory ribbon, and painting trails after the street palette
    scattered tan cells along a road the map had already coloured ivory.
    """

    def field(self,road_relief=.16):
        material=np.full((3,6),1,dtype=np.uint8)
        ground=np.full((3,6),4.);top=ground.copy()
        roads=np.zeros((3,6),dtype=bool);roads[1,:]=True
        material[roads]=0;top[roads]=ground[roads]+road_relief
        return material,top,ground,roads

    def test_a_trail_crossing_a_road_leaves_the_carriageway_ivory(self):
        material,top,ground,roads=self.field()
        trails=np.zeros((3,6),dtype=bool);trails[:,2]=True
        report=paint_trail_ribbons(material,top,ground,trail_mask=trails,
            road_mask=roads,line_relief_source=.16)
        self.assertTrue(np.all(material[1,:]==0))
        self.assertEqual(material[0,2],3);self.assertEqual(material[2,2],3)
        self.assertEqual(report['trail_cells_yielded_to_carriageways'],1)
        # The trail and the road stand at one height, so the crossing is level.
        np.testing.assert_allclose(top[:,2],ground[:,2]+.16)

    def test_a_marked_crossing_never_notches_the_road_line_it_reaches(self):
        # A marked crossing runs kerb to kerb at every intersection on the map.
        # If a trail could write its own height onto the carriageway, each one
        # would cut a trench across the raised road line it crosses.
        material,top,ground,roads=self.field(road_relief=.48)
        trails=np.zeros((3,6),dtype=bool);trails[:,2]=True
        paint_trail_ribbons(material,top,ground,trail_mask=trails,
            road_mask=roads,line_relief_source=.16)
        np.testing.assert_allclose(top[1,:],ground[1,:]+.48)
        np.testing.assert_allclose(top[0,2],ground[0,2]+.16)

    def test_a_trail_away_from_any_road_is_still_tan(self):
        # The symmetric case: an ordinary park path must still print tan.
        material,top,ground,roads=self.field()
        trails=np.zeros((3,6),dtype=bool);trails[0,:]=True
        report=paint_trail_ribbons(material,top,ground,trail_mask=trails,
            road_mask=roads,line_relief_source=.16)
        self.assertTrue(np.all(material[0,:]==3))
        self.assertEqual(report['trail_cells'],6)
        self.assertEqual(report['trail_cells_yielded_to_carriageways'],0)
        np.testing.assert_allclose(top[0,:],ground[0,:]+.16)

    def test_a_footway_inside_the_street_field_is_raised_like_any_other(self):
        # The reference map raises the footway along each block edge into its
        # own tan ridge beside the ivory carriageway line, so a street reads as
        # three parallel lines rather than one flat field with a painted stripe.
        material,top,ground,roads=self.field()
        trails=np.zeros((3,6),dtype=bool);trails[0,:]=True;trails[2,:]=True
        report=paint_trail_ribbons(material,top,ground,trail_mask=trails,
            road_mask=roads,line_relief_source=.16)
        np.testing.assert_allclose(top[0,:],ground[0,:]+.16)
        np.testing.assert_allclose(top[2,:],ground[2,:]+.16)
        self.assertEqual(report['raised_trail_line_cells'],12)

    def test_a_road_free_field_paints_every_trail_cell(self):
        material,top,ground,_=self.field()
        trails=np.ones((3,6),dtype=bool)
        paint_trail_ribbons(material,top,ground,trail_mask=trails,
            road_mask=np.zeros((3,6),dtype=bool),line_relief_source=.16)
        self.assertTrue(np.all(material==3))

    def test_malformed_trail_fields_are_rejected(self):
        material,top,ground,roads=self.field()
        with self.assertRaises(ValueError):
            paint_trail_ribbons(material,top,ground,trail_mask=roads[:-1],
                road_mask=roads,line_relief_source=.16)
        with self.assertRaises(ValueError):
            paint_trail_ribbons(material,top,ground,trail_mask=roads,
                road_mask=roads,line_relief_source=float('nan'))


class StairTreadPriorityTests(unittest.TestCase):
    """A mapped stair run must not cut treads through a road or a deck.

    The manhattan_2m_A3 failure: a staircase beside the ramp at the George
    Washington Bridge approach painted tan treads across the ramp's bridge
    deck, and the tread height -- interpolated from the street far below the
    crossing -- dropped the deck with it.
    """

    def field(self):
        empty=lambda:np.zeros((3,6),dtype=bool)
        run=empty();run[:,2]=True
        return run,empty(),empty(),empty(),empty()

    def test_a_run_crossing_a_carriageway_yields_the_road_cells(self):
        run,buildings,water,roads,protected=self.field()
        roads[1,:]=True
        kept,yielded=stair_tread_mask(run,building_mask=buildings,water_mask=water,
            road_mask=roads,protected_transport_mask=protected)
        self.assertFalse(kept[1,2]);self.assertEqual(yielded,1)
        self.assertTrue(kept[0,2] and kept[2,2])

    def test_a_run_beside_a_bridge_deck_never_steps_the_deck_down(self):
        # The deck stands at its surveyed elevation. A tread is interpolated
        # from the terrain the crossing spans, so painting one onto the deck
        # would drop the road to the street beneath it.
        run,buildings,water,roads,protected=self.field()
        protected[1,:]=True
        kept,yielded=stair_tread_mask(run,building_mask=buildings,water_mask=water,
            road_mask=roads,protected_transport_mask=protected)
        self.assertFalse(kept[1,2]);self.assertEqual(yielded,1)

    def test_a_run_clear_of_every_higher_surface_keeps_all_its_treads(self):
        run,buildings,water,roads,protected=self.field()
        kept,yielded=stair_tread_mask(run,building_mask=buildings,water_mask=water,
            road_mask=roads,protected_transport_mask=protected)
        np.testing.assert_array_equal(kept,run);self.assertEqual(yielded,0)

    def test_buildings_and_water_still_outrank_a_run(self):
        # Both were already excluded before roads and decks were, so neither
        # counts against the priority this yield figure reports.
        run,buildings,water,roads,protected=self.field()
        buildings[0,2]=True;water[2,2]=True
        kept,yielded=stair_tread_mask(run,building_mask=buildings,water_mask=water,
            road_mask=roads,protected_transport_mask=protected)
        self.assertEqual(kept.sum(),1);self.assertTrue(kept[1,2]);self.assertEqual(yielded,0)

    def test_a_road_beneath_a_building_is_not_counted_against_the_run_twice(self):
        run,buildings,water,roads,protected=self.field()
        buildings[0,2]=True;roads[0,:]=True;protected[2,:]=True
        _,yielded=stair_tread_mask(run,building_mask=buildings,water_mask=water,
            road_mask=roads,protected_transport_mask=protected)
        self.assertEqual(yielded,1)

    def test_malformed_stair_masks_are_rejected(self):
        run,buildings,water,roads,protected=self.field()
        with self.assertRaises(ValueError):
            stair_tread_mask(run,building_mask=buildings[:-1],water_mask=water,
                road_mask=roads,protected_transport_mask=protected)
        with self.assertRaises(ValueError):
            stair_tread_mask(run[0],building_mask=buildings[0],water_mask=water[0],
                road_mask=roads[0],protected_transport_mask=protected[0])


class PavementPadReliefTests(unittest.TestCase):
    """A pad shallower than one layer prints intermittently, not shallowly."""

    def profiles(self):
        from generate_3mf import PROCESS_PRESETS
        return sorted(PROCESS_PRESETS)

    def test_the_pad_is_a_whole_number_of_layers_on_every_supported_profile(self):
        for layer in self.profiles():
            with self.subTest(layer=layer):
                pad=pavement_pad_relief_mm(layer)
                self.assertAlmostEqual(pad/layer,round(pad/layer),places=9)
                self.assertGreaterEqual(pad,layer)

    def test_the_pad_is_never_shrunk_below_the_height_the_map_is_drawn_against(self):
        # A symbol may grow to stay printable; it must never quietly shrink out
        # of the print, so rounding is up rather than to nearest.
        for layer in self.profiles():
            with self.subTest(layer=layer):
                self.assertGreaterEqual(pavement_pad_relief_mm(layer),.16-1e-9)

    def test_a_drawn_line_still_clears_the_layer_aligned_pad(self):
        for layer in self.profiles():
            with self.subTest(layer=layer):
                pad=pavement_pad_relief_mm(layer)
                line=drawn_line_relief_mm({"path_relief_mm":pad,"layer_height_mm":layer})
                self.assertGreaterEqual(line-pad,layer-1e-9)

    def test_a_nonsense_pad_or_layer_height_is_rejected(self):
        for layer,requested in [(0,.16),(-.1,.16),(float("nan"),.16),(.2,0),(.2,-1)]:
            with self.subTest(layer=layer,requested=requested):
                with self.assertRaises(ValueError):
                    pavement_pad_relief_mm(layer,requested)


class FoundationMaterialTests(unittest.TestCase):
    """The substrate is bulk, not cartography, so its filament is a supply choice."""

    def resolve(self,value):
        import build_map_meshes
        from map_common import CFG
        previous=dict(CFG)
        try:
            CFG.clear();CFG.update({"colors":["#000000"]*4,**({} if value is None else {"foundation_material":value})})
            return build_map_meshes.foundation_material()
        finally:
            CFG.clear();CFG.update(previous)

    def test_the_substrate_defaults_to_the_first_filament(self):
        self.assertEqual(self.resolve(None),MATERIAL_NAMES.index("ivory"))

    def test_any_configured_filament_may_carry_the_substrate(self):
        for index in range(len(MATERIAL_NAMES)):
            with self.subTest(index=index):
                self.assertEqual(self.resolve(index),index)

    def test_a_filament_the_palette_does_not_have_is_rejected(self):
        for index in (-1,4,99):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    self.resolve(index)

    def test_the_named_part_says_which_filament_carries_the_substrate(self):
        import package_3mf
        from map_common import CFG
        previous=dict(CFG)
        try:
            for index in range(len(MATERIAL_NAMES)):
                with self.subTest(index=index):
                    CFG.clear();CFG.update({"foundation_material":index})
                    names=package_3mf.part_names()
                    carrying=[name for name in names if "substrate" in name]
                    self.assertEqual(len(carrying),1)
                    self.assertIs(carrying[0],names[index])
        finally:
            CFG.clear();CFG.update(previous)


if __name__=='__main__':unittest.main()
