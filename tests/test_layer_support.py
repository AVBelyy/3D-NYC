import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from build_map_meshes import audit_layer_support,printable_roof_span


class LayerSupportTests(unittest.TestCase):
    def test_short_explicit_bridges_pass(self):
        report=audit_layer_support([
            {'osm_id':1,'status':'cut','roof_span_mm':1.6},
            {'osm_id':2,'status':'road/path overpass opening','roof_span_mm':.6},
            {'osm_id':3,'status':'bridge deck retained: no mapped lower surface'},
        ],.4)
        self.assertEqual(report['result'],'passed')
        self.assertEqual(report['explicit_bridge_roofs'],2)
        self.assertEqual(report['maximum_bridge_span_mm'],1.6)
        self.assertEqual(report['maximum_allowed_bridge_span_mm'],2.)

    def test_unmeasured_opening_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'lack a roof span'):
            audit_layer_support([{'osm_id':1,'status':'cut'}],.4)

    def test_excessive_bridge_span_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'exceed'):
            audit_layer_support([
                {'osm_id':1,'status':'water bridge opening','roof_span_mm':2.01}],.4)

    def test_wide_mapped_road_gets_a_printable_hidden_aperture(self):
        # Keep the 2.25 mm road/floor, but the 0.10 mm cutter clearance must
        # not turn its overpass into a roof wider than five nozzle diameters.
        self.assertEqual(printable_roof_span(2.25+.10,.4),2.0)
        report=audit_layer_support([{
            'osm_id':5669194,'status':'road/path overpass opening',
            'source_road_width_mm':2.25,'requested_roof_span_mm':2.35,
            'roof_span_mm':2.0,'roof_span_reduction_mm':.35,
        }],.4)
        self.assertEqual(report['maximum_bridge_span_mm'],2.0)

    def test_invalid_span_or_nozzle_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError,'invalid roof spans'):
            audit_layer_support([{'osm_id':1,'status':'cut','roof_span_mm':float('nan')}],.4)
        with self.assertRaisesRegex(ValueError,'nozzle_mm'):
            printable_roof_span(1.0,0)


if __name__=='__main__':unittest.main()
