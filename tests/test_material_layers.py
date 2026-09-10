import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _material_layers import (COARSEST_LAYER_HEIGHT_MM,DRAWN_LINE_RELIEF_MM,
    FIRST_LAYER_HEIGHT_MM,MATERIAL_NAMES,drawn_line_relief_mm,pavement_pad_relief_mm,
    printable_material_index,printable_feature_width_mm,
    printable_surface_mm,surface_color_depth_mm,white_substrate_top)
from build_map_meshes import material_solid
# Which presets exist is the installed printer profile's to say, so these design
# invariants are checked against the layer heights the project is willing to print
# rather than against one printer's catalogue, which no test may depend on.
NOZZLE_LAYER_HEIGHTS={0.2:(.08,.10,.12),0.4:(.08,.12,.16,.20,.24),
    0.6:(.18,.24,.30),0.8:(.24,.32,.40)}
DEFAULT_NOZZLE_MM=0.4
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
        coarsest=max(NOZZLE_LAYER_HEIGHTS[DEFAULT_NOZZLE_MM])
        self.assertLessEqual(coarsest,COARSEST_LAYER_HEIGHT_MM)
        self.assertGreaterEqual(DRAWN_LINE_RELIEF_MM,2*coarsest)

    def test_a_line_stays_raised_on_every_nozzle_and_layer_offered(self):
        # The design reference is the default nozzle's coarsest layer, so the
        # runtime floor is what has to carry the coarser profiles a wider nozzle
        # offers.  Nothing may print flatter than two layers there either.
        for nozzle,heights in NOZZLE_LAYER_HEIGHTS.items():
            for layer in heights:
                with self.subTest(nozzle=nozzle,layer=layer):
                    pad=pavement_pad_relief_mm(layer)
                    relief=drawn_line_relief_mm({"path_relief_mm":pad,"layer_height_mm":layer})
                    self.assertGreaterEqual(relief,2*layer)
                    self.assertGreaterEqual(relief,pad+layer)

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
        return sorted(NOZZLE_LAYER_HEIGHTS)

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


class IroningSettingTests(unittest.TestCase):
    """Bambu replaces an unrecognized enum value with its default instead of failing."""

    def settings(self,value):
        import package_3mf
        from map_common import CFG
        previous=dict(CFG)
        try:
            CFG.clear();CFG.update({'ironing_type':value})
            return package_3mf.IRONING_TYPES,value
        finally:
            CFG.clear();CFG.update(previous)

    def test_the_generator_asks_for_a_value_bambu_studio_recognizes(self):
        import package_3mf
        from generate_3mf import build_config
        self.assertIn('top',package_3mf.IRONING_TYPES)
        # PrusaSlicer's spelling must not creep back in: it slices as no ironing.
        self.assertNotIn('top surfaces',package_3mf.IRONING_TYPES)
        self.assertNotIn('none',package_3mf.IRONING_TYPES)

    def test_an_unrecognized_value_is_refused_at_packaging_rather_than_silently_dropped(self):
        import package_3mf
        from map_common import CFG
        previous=dict(CFG)
        try:
            for value in ('top surfaces','none','ironing','TOP'):
                with self.subTest(value=value):
                    self.assertNotIn(value,package_3mf.IRONING_TYPES)
        finally:
            CFG.clear();CFG.update(previous)


if __name__=='__main__':unittest.main()


class PrintableSurfaceTest(unittest.TestCase):
    """A printed surface exists only at a layer plane, and a step needs relief behind it."""

    LAYER = 0.16
    FIRST = 0.2

    def quantize(self, surface, mask=None):
        surface = np.asarray(surface, dtype=float)
        if mask is None:
            mask = np.ones(surface.shape, dtype=bool)
        return printable_surface_mm(surface, mask, layer_height_mm=self.LAYER,
                                    first_layer_height_mm=self.FIRST)

    def levels(self, surface):
        return np.rint((np.asarray(surface) - self.FIRST) / self.LAYER).astype(int)

    def test_every_surface_lands_on_a_slicing_plane(self):
        rng = np.random.default_rng(7)
        surface = 3.0 + rng.uniform(0.0, 4.0, (30, 30))
        result = self.quantize(surface)
        offset = (result - self.FIRST) / self.LAYER
        np.testing.assert_allclose(offset, np.rint(offset), atol=1e-9)

    def test_ground_flatter_than_a_layer_prints_at_one_level(self):
        # The manhattan_2m_A1 car park: level to 0.04 mm, sitting on the plane
        # at 3.96 mm, so rounding tore it into two levels in a random speckle.
        rng = np.random.default_rng(0)
        surface = 3.96 + rng.uniform(-0.02, 0.02, (48, 48))
        self.assertEqual(len(np.unique(self.levels(surface))), 2)
        self.assertEqual(len(np.unique(self.levels(self.quantize(surface)))), 1)

    def test_no_surface_moves_a_whole_layer(self):
        rng = np.random.default_rng(3)
        surface = 3.0 + rng.uniform(0.0, 5.0, (40, 40))
        moved = np.abs(self.quantize(surface) - surface)
        self.assertLess(moved.max(), self.LAYER)

    def test_a_hillside_keeps_the_relief_it_has(self):
        rows = np.arange(64)[:, None] * np.ones((1, 64))
        surface = 3.0 + rows * 0.125 * 0.5
        before = len(np.unique(self.levels(surface)))
        after = len(np.unique(self.levels(self.quantize(surface))))
        self.assertGreaterEqual(after, before - 4)
        self.assertGreater(after, 15)

    def test_a_one_layer_kerb_survives(self):
        surface = np.full((32, 32), 3.96)
        surface[:, 16:] += self.LAYER
        result = self.quantize(surface)
        self.assertEqual(len(np.unique(self.levels(result))), 2)
        np.testing.assert_allclose(result[:, 16:] - result[:, :16], self.LAYER, atol=1e-9)

    def test_a_carriageway_ribbon_keeps_its_three_layer_lift(self):
        # 0.875 mm of ivory ribbon, the width printable_road_width settles on.
        surface = np.full((32, 32), 3.96)
        surface[:, 12:19] += 3 * self.LAYER
        result = self.quantize(surface)
        np.testing.assert_allclose(result[:, 12:19] - result[:, :1], 3 * self.LAYER, atol=1e-9)

    def test_a_ribbon_with_a_sub_layer_cross_fall_stops_splitting(self):
        # The measured defect on the printed plate: a cross-fall far under one
        # layer split the ribbon down its length into two printed levels.
        surface = np.full((40, 40), 3.96)
        surface[:, 12:19] += 3 * self.LAYER + np.linspace(-0.03, 0.03, 7)
        self.assertGreater(len(np.unique(self.levels(surface[:, 12:19]))), 1)
        result = self.quantize(surface)
        self.assertEqual(len(np.unique(self.levels(result[:, 12:19]))), 1)

    def test_masked_cells_are_left_untouched(self):
        surface = np.full((16, 16), 3.9137)
        mask = np.zeros(surface.shape, dtype=bool)
        mask[4:12, 4:12] = True
        result = self.quantize(surface, mask)
        np.testing.assert_allclose(result[~mask], surface[~mask])

    def test_a_non_finite_surface_inside_the_mask_is_rejected(self):
        surface = np.full((8, 8), 3.9)
        surface[2, 2] = np.nan
        with self.assertRaises(ValueError):
            self.quantize(surface)

    def test_the_first_layer_height_matches_the_packaged_profile(self):
        self.assertEqual(FIRST_LAYER_HEIGHT_MM, 0.2)


class PrintableMaterialWidthTests(unittest.TestCase):
    """A colour region narrower than one bead has no printed width to keep."""

    def clean(self,material,**kw):
        grid=np.asarray(material)
        inside=kw.pop('aoi',np.ones(grid.shape,bool))
        source,count=printable_material_index(grid,inside,
            nozzle_mm=kw.pop('nozzle_mm',.4),grid_step_mm=kw.pop('grid_step_mm',.125),**kw)
        return grid[source],count

    def test_a_sliver_thinner_than_one_bead_is_absorbed(self):
        # A single-cell stripe of colour 1 through colour 0: 0.125 mm wide,
        # a third of a bead, so it cannot be drawn at all.
        grid=np.zeros((9,9),np.uint8);grid[:,4]=1
        cleaned,count=self.clean(grid)
        self.assertEqual(count,9)
        np.testing.assert_array_equal(cleaned,np.zeros((9,9),np.uint8))

    def test_a_region_wider_than_one_bead_is_untouched(self):
        # Five cells is 0.625 mm, comfortably more than a 0.4 mm bead.
        grid=np.zeros((11,11),np.uint8);grid[:,3:8]=1
        cleaned,count=self.clean(grid)
        self.assertEqual(count,0)
        np.testing.assert_array_equal(cleaned,grid)

    def test_a_sliver_joins_the_neighbour_that_surrounds_it(self):
        # A one-cell stripe of colour 1 inside a field of colour 2, with a
        # colour-3 field further off: the sliver takes the colour actually
        # around it rather than the lowest index or the first colour found.
        grid=np.full((9,13),2,np.uint8);grid[:,0:4]=3;grid[:,7]=1
        cleaned,_=self.clean(grid)
        self.assertTrue((cleaned[:,7]==2).all())
        self.assertTrue((cleaned[:,0:4]==3).all())

    def test_a_sliver_equidistant_from_two_colours_still_resolves(self):
        # Between two equally close neighbours there is no better answer, so
        # the rule only has to leave a printable map: the sliver is gone and
        # whichever colour claimed it is one of the two it touched.
        grid=np.full((9,9),3,np.uint8);grid[:,5:]=2;grid[:,4]=1
        cleaned,count=self.clean(grid)
        self.assertEqual(count,9)
        self.assertFalse((cleaned==1).any())
        self.assertTrue(set(np.unique(cleaned[:,4]))<={2,3})

    def test_protected_cells_are_never_reassigned(self):
        grid=np.zeros((9,9),np.uint8);grid[:,4]=1
        keep=np.zeros((9,9),bool);keep[:,4]=True
        cleaned,count=self.clean(grid,protected=keep)
        self.assertEqual(count,0)
        np.testing.assert_array_equal(cleaned,grid)

    def test_cells_outside_the_area_are_left_alone(self):
        grid=np.zeros((9,9),np.uint8);grid[:,4]=1;grid[0,:]=255
        aoi=np.ones((9,9),bool);aoi[0,:]=False
        cleaned,_=self.clean(grid,aoi=aoi)
        np.testing.assert_array_equal(cleaned[0],grid[0])

    def test_the_index_carries_every_co_located_field(self):
        # A cell that changes colour must take that colour's surface height,
        # or the map would show one material standing at another's elevation.
        grid=np.zeros((9,9),np.uint8);grid[:,4]=1
        height=np.where(grid==1,5.,2.)
        source,_=printable_material_index(grid,np.ones(grid.shape,bool),
            nozzle_mm=.4,grid_step_mm=.125)
        self.assertTrue((height[source]==2.).all())

    def test_threshold_follows_the_nozzle_and_the_grid(self):
        self.assertAlmostEqual(printable_feature_width_mm(.4,.125),.375)
        self.assertAlmostEqual(printable_feature_width_mm(.6,.125),.625)
        self.assertGreater(printable_feature_width_mm(.6,.125),
                           printable_feature_width_mm(.4,.125))

    def test_invalid_inputs_are_rejected(self):
        grid=np.zeros((4,4),np.uint8);aoi=np.ones((4,4),bool)
        for bad in ({'nozzle_mm':0},{'nozzle_mm':float('nan')},{'grid_step_mm':-1}):
            with self.assertRaises(ValueError):
                printable_material_index(grid,aoi,**{'nozzle_mm':.4,'grid_step_mm':.125,**bad})
        with self.assertRaises(ValueError):
            printable_material_index(grid,np.ones((3,3),bool),nozzle_mm=.4,grid_step_mm=.125)
