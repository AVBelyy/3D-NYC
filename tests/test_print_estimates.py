import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from estimate_print_stats import (format_duration,parse_duration,parse_gcode,parse_result,
    parse_sliced_3mf,part_labels)

GCODE='''; HEADER_BLOCK_START
; model printing time: 6h 19m 15s; total estimated time: 6h 26m 17s
; total layer number: 98
; total filament length [mm] : 4308.72,8878.39,99.58,16269.14
; total filament weight [g] : 13.68,28.19,0.32,51.65
; filament_colour = #F2F0E8;#5FAA72;#A9D5DF;#C79A61
; filament_cost = 24.99,24.99,24.99,24.99
; filament_type = PLA;PLA;PLA;PLA
; layer_height = 0.16
; nozzle_diameter = 0.4
; printer_model = Bambu Lab P2S
G1 X1 Y1 E1
'''

MODEL_SETTINGS='''<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="9">
    <part id="1" subtype="normal_part">
      <metadata key="name" value="Ivory - roads and substrate"/>
      <metadata key="extruder" value="1"/>
    </part>
    <part id="2" subtype="normal_part">
      <metadata key="name" value="Green - terrain and canopy"/>
      <metadata key="extruder" value="2"/>
    </part>
  </object>
</config>
'''

SLICED_INFO='''<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="23177"/>
    <filament id="1" type="PLA" color="#F2F0E8" used_m="4.31" used_g="13.68"/>
    <filament id="2" type="PLA" color="#5FAA72" used_m="8.88" used_g="28.19"/>
  </plate>
</config>
'''


def write_3mf(folder,**members):
    path=Path(folder)/'model.3mf'
    with zipfile.ZipFile(path,'w') as archive:
        for name,text in members.items():archive.writestr(name,text)
    return path


class DurationTests(unittest.TestCase):
    def test_parses_slicer_duration_units(self):
        self.assertEqual(parse_duration(' 6h 26m 17s'),6*3600+26*60+17)
        self.assertEqual(parse_duration('1d 2h'),26*3600)
        self.assertEqual(parse_duration('45s'),45)

    def test_rejects_text_without_a_duration(self):
        with self.assertRaises(ValueError):parse_duration('unknown')

    def test_formats_hours_and_minutes(self):
        self.assertEqual(format_duration(6*3600+26*60+17),'6h 26m')
        self.assertEqual(format_duration(95),'1m 35s')


class GcodeTests(unittest.TestCase):
    def parse(self,text):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'plate_1.gcode';path.write_text(text)
            return parse_gcode(path)

    def test_reads_totals_and_presets(self):
        stats=self.parse(GCODE)
        self.assertEqual(stats['seconds'],6*3600+26*60+17)
        self.assertEqual(stats['model_seconds'],6*3600+19*60+15)
        self.assertEqual(stats['layers'],98)
        self.assertEqual(stats['weight_g'],[13.68,28.19,.32,51.65])
        self.assertEqual(stats['colour'],['#F2F0E8','#5FAA72','#A9D5DF','#C79A61'])
        self.assertEqual(stats['cost_per_kg'],['24.99']*4)
        self.assertEqual(stats['printer'],['Bambu Lab P2S'])

    def test_rejects_gcode_without_totals(self):
        with self.assertRaisesRegex(RuntimeError,'filament or time totals'):
            self.parse('; total layer number: 98\nG1 X1 E1\n')


class ProjectTests(unittest.TestCase):
    def test_reads_totals_from_an_already_sliced_project(self):
        with tempfile.TemporaryDirectory() as folder:
            path=write_3mf(folder,**{'Metadata/slice_info.config':SLICED_INFO})
            stats=parse_sliced_3mf(path)
        self.assertEqual(stats['seconds'],23177)
        self.assertEqual(stats['weight_g'],[13.68,28.19])
        self.assertAlmostEqual(stats['length_mm'][1],8880)
        self.assertEqual(stats['colour'],['#F2F0E8','#5FAA72'])

    def test_unsliced_project_reports_no_stats(self):
        header='<config><header><header_item key="X-BBL-Client-Type" value="slicer"/></header></config>'
        with tempfile.TemporaryDirectory() as folder:
            path=write_3mf(folder,**{'Metadata/slice_info.config':header,
                'Metadata/model_settings.config':MODEL_SETTINGS})
            self.assertIsNone(parse_sliced_3mf(path))

    def test_labels_extruders_by_material_role(self):
        with tempfile.TemporaryDirectory() as folder:
            path=write_3mf(folder,**{'Metadata/model_settings.config':MODEL_SETTINGS})
            self.assertEqual(part_labels(path),{1:'Ivory',2:'Green'})


class ResultTests(unittest.TestCase):
    def test_separates_model_material_from_purge(self):
        payload={'sliced_plates':[{'filament_change_times':108,'warning_message':'floating regions',
            'feature_type_times':{'Flush':9057.9},
            'filaments':[{'id':1,'main_used_g':5.19,'total_used_g':13.68},
                {'id':2,'main_used_g':3.3,'total_used_g':28.19}]}]}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'result.json';path.write_text(json.dumps(payload))
            summary=parse_result(path)
        self.assertEqual(summary['changes'],108)
        self.assertEqual(summary['warnings'],['floating regions'])
        self.assertAlmostEqual(summary['model_g'][2],3.3)
        self.assertAlmostEqual(summary['flush_seconds'],9057.9)


if __name__=='__main__':unittest.main()
