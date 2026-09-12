import json
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import compute_print_stats as print_stats
from compute_print_stats import (aggregate,band_lines,chunk_caption,chunk_label,collect_all,
    embed_stats,format_duration,parse_duration,parse_gcode,parse_result,parse_sliced_3mf,part_labels,
    read_embedded_stats,sha256,sliced_path,strip_band,unique_models,update_preview)

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

PREVIEW='''<?xml version="1.0" encoding="utf-8" standalone="no"?>
<svg width="400pt" height="300pt" viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg" version="1.1">
 <g id="figure_1">
  <g id="patch_1"><path d="M 0 300 L 400 300 L 400 0 L 0 0 z" style="fill: #ffffff"/></g>
  <g id="text_5">
   <g id="patch_9">
    <path d="M 100 200 L 180 200 L 180 170 L 100 170 z" style="fill: #ffffff; opacity: 0.82; stroke: #c0392b; stroke-width: 1.13; stroke-linejoin: miter"/>
   </g>
   <!-- A1 -->
   <g style="fill: #1a1a1a" transform="translate(120 190) scale(0.141421 -0.141421)"/>
  </g>
 </g>
</svg>
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


class SlicedProjectTests(unittest.TestCase):
    def project(self,folder,digest):
        model=write_3mf(folder,**{'Metadata/generation_command.json':'{"argv":["generate"]}'})
        project=Path(folder)/'sliced.gcode.3mf'
        with zipfile.ZipFile(project,'w') as archive:archive.writestr('Metadata/plate_1.gcode','G1\n')
        embed_stats(project,model,{'schema_version':1,'source_sha256':digest,
            'stats':{'weight_g':[13.68],'seconds':600},'extra':{'model_g':{1:5.19},'changes':4}})
        return model,project

    def test_names_the_project_beside_its_model(self):
        self.assertEqual(sliced_path(Path('output/models/a.3mf')),Path('output/models/a.gcode.3mf'))

    def test_reads_back_the_totals_it_embedded(self):
        with tempfile.TemporaryDirectory() as folder:
            _,project=self.project(folder,'abc')
            stats,extra=read_embedded_stats(project,'abc')
        self.assertEqual(stats['weight_g'],[13.68])
        self.assertEqual(extra['model_g'],{1:5.19})  # keys survive the JSON round trip as integers
        self.assertEqual(extra['changes'],4)

    def test_carries_the_model_provenance_into_the_project(self):
        with tempfile.TemporaryDirectory() as folder:
            _,project=self.project(folder,'abc')
            with zipfile.ZipFile(project) as archive:
                self.assertEqual(json.loads(archive.read('Metadata/generation_command.json')),
                    {'argv':['generate']})
                self.assertEqual(archive.read('Metadata/plate_1.gcode'),b'G1\n')

    def test_rejects_totals_left_by_a_different_model(self):
        with tempfile.TemporaryDirectory() as folder:
            _,project=self.project(folder,'abc')
            self.assertIsNone(read_embedded_stats(project,'other'))

    def test_ignores_a_project_without_embedded_totals(self):
        with tempfile.TemporaryDirectory() as folder:
            path=write_3mf(folder,**{'Metadata/slice_info.config':SLICED_INFO})
            self.assertIsNone(read_embedded_stats(path))
            self.assertIsNone(read_embedded_stats(Path(folder)/'missing.gcode.3mf'))


class InputListTests(unittest.TestCase):
    def test_drops_a_sliced_project_whose_model_is_also_listed(self):
        models=[Path('out/a.3mf'),Path('out/a.gcode.3mf'),Path('out/b.gcode.3mf')]
        self.assertEqual(unique_models(models),[Path('out/a.3mf'),Path('out/b.gcode.3mf')])


class BatchTests(unittest.TestCase):
    """A batch resolves its cache before slicing, so the bar counts slices alone."""

    STATS={'weight_g':[13.68],'length_mm':[4308.72],'seconds':3600.,'colour':['#F2F0E8'],
        'cost_per_kg':['24.99'],'type':['PLA'],'layers':98}

    def batch(self,folder,cached,uncached):
        """Write models, giving only the cached ones a sliced project that carries their totals."""
        models=[]
        for name in [*cached,*uncached]:
            model=Path(folder)/f'{name}.3mf'
            with zipfile.ZipFile(model,'w') as archive:
                archive.writestr('Metadata/model_settings.config',MODEL_SETTINGS)
            if name in cached:
                project=sliced_path(model)
                with zipfile.ZipFile(project,'w') as archive:archive.writestr('keep','')
                embed_stats(project,model,{'schema_version':1,'source_sha256':sha256(model),
                    'stats':self.STATS,'extra':{}})
            models.append(model)
        return models

    def run_batch(self,models):
        """Collect the batch with the slicer stubbed out, reporting how the bar was sized."""
        def fake_slice(model,digest,slicer):
            return self.STATS,{},sliced_path(model)
        with patch.object(print_stats,'slice_model',side_effect=fake_slice) as sliced, \
                patch.object(print_stats,'Progress') as progress:
            entries=collect_all(models,False,False,Path('slicer'))
        return entries,sliced,progress

    def test_bar_counts_only_the_models_that_need_slicing(self):
        with tempfile.TemporaryDirectory() as folder:
            models=self.batch(folder,['a','b'],['c'])
            entries,sliced,progress=self.run_batch(models)
        self.assertEqual(sliced.call_count,1)
        self.assertEqual(progress.call_args.args,('slicing',1))
        self.assertEqual([entry['reused_slice'] for entry in entries],[True,True,False])

    def test_a_fully_cached_batch_shows_no_bar(self):
        with tempfile.TemporaryDirectory() as folder:
            models=self.batch(folder,['a','b'],[])
            entries,sliced,progress=self.run_batch(models)
        self.assertEqual(sliced.call_count,0)
        progress.assert_not_called()
        self.assertEqual(len(entries),2)


class PreviewBandTests(unittest.TestCase):
    """The caption the planner's preview carries once a batch has been estimated."""

    ENTRIES=[{'model':'output/models/plan_2m_A1.3mf','seconds':3600.,'plates':1,'filaments':[
        {'extruder':1,'label':'Ivory','colour':'#F2F0E8','total_g':10.,'model_g':8.,'purge_g':2.,'cost_usd':0.25},
        {'extruder':2,'label':'Green','colour':'#5FAA72','total_g':4.,'model_g':3.,'purge_g':1.,'cost_usd':0.1}]},
        {'model':'output/models/plan_2m_A2.3mf','seconds':1800.,'plates':1,'filaments':[
        {'extruder':1,'label':'Ivory','colour':'#F2F0E8','total_g':6.,'model_g':5.,'purge_g':1.,'cost_usd':0.15}]}]

    def preview(self,folder):
        path=Path(folder)/'preview.svg';path.write_text(PREVIEW)
        return path

    def test_folds_a_batch_into_one_print(self):
        summary=aggregate(self.ENTRIES)
        self.assertEqual((summary['models'],summary['plates']),(2,2))
        self.assertAlmostEqual(summary['seconds'],5400.)
        self.assertEqual([f['total_g'] for f in summary['filaments']],[16.,4.])
        self.assertEqual([f['model_g'] for f in summary['filaments']],[13.,3.])

    def test_caption_carries_time_and_the_weight_split(self):
        head,chips=band_lines(aggregate(self.ENTRIES))
        self.assertIn('1h 30m',head)
        self.assertIn('20 g = 16 g model + 4 g purge',head)
        self.assertIn('#5FAA72',chips)
        self.assertIn('Ivory 16 g',chips)

    def test_grams_without_a_split_are_counted_apart(self):
        entries=[{'model':'output/models/plan_2m_A3.3mf','seconds':60.,'plates':1,'filaments':[
            {'extruder':1,'label':'Ivory','colour':None,'total_g':5.,'model_g':None,'purge_g':None,'cost_usd':None},
            *self.ENTRIES[1]['filaments']]}]
        head,_=band_lines(aggregate(entries))
        self.assertIn('11 g = 5 g model + 1 g purge + 5 g unsplit',head)

    def test_band_grows_the_canvas_without_covering_the_figure(self):
        with tempfile.TemporaryDirectory() as folder:
            path=self.preview(folder)
            update_preview(path,self.ENTRIES,aggregate(self.ENTRIES))
            text=path.read_text()
        self.assertIn('<g id="print-stats"',text)
        self.assertIn('print-stats-shift" transform="translate(0 ',text)
        self.assertGreater(float(text.split('height="',2)[1].split('pt')[0]),300)
        self.assertEqual(text.split('viewBox="')[1].split('"')[0].split()[:3],['0','0','400'])
        ET.fromstring(text.encode())

    def test_reads_a_plate_label_from_its_model_name(self):
        self.assertEqual(chunk_label({'model':'output/models/manhattan_2m_240_C12.2.3mf'}),'C12.2')
        self.assertEqual(chunk_label({'model':'output/models/manhattan_2m_240_A1.3mf'}),'A1')
        self.assertIsNone(chunk_label({'model':'output/models/manhattan_2m_240.3mf'}))

    def test_plate_caption_carries_its_own_time_and_weight(self):
        caption,length=chunk_caption(self.ENTRIES[0])
        self.assertIn('1h 00m',caption)
        self.assertIn('14 g',caption)
        self.assertGreater(length,len('1h 00m 14 g'))
        # The model and purge split is the band's to carry, not the plate's.
        self.assertNotIn('purge',caption)

    def test_a_plate_with_no_split_is_captioned_the_same_way(self):
        entry={'model':'output/models/plan_2m_A3.3mf','seconds':60.,'plates':1,'filaments':[
            {'extruder':1,'label':'Ivory','colour':None,'total_g':5.,'model_g':None,
            'purge_g':None,'cost_usd':None}]}
        self.assertIn('5 g',chunk_caption(entry)[0])

    def test_every_labelled_plate_is_captioned_where_the_preview_draws_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path=self.preview(folder)
            captioned,missing=update_preview(path,self.ENTRIES,aggregate(self.ENTRIES))
            text=path.read_text()
        self.assertEqual((captioned,missing),(['A1'],['A2']))
        chunks=text.split('<g id="print-stats-chunks"')[1]
        self.assertIn('1h 00m',chunks)
        # Centred on the label box and written below its old lower edge.
        self.assertIn('<text x="140.00" y="20',chunks)
        # The label's own box grew to hold them, keeping its corner radius.
        box=re.search(r'<rect data-print-stats="A1"[^>]*/>',text).group(0)
        self.assertGreater(float(re.search(r'height="([\d.]+)"',box).group(1)),30.)
        self.assertNotIn('<path d="M 100 200',text)

    def test_a_batch_reports_grams_it_could_not_split(self):
        entries=[{'model':'output/models/plan_2m_A3.3mf','seconds':60.,'plates':1,'filaments':[
            {'extruder':1,'label':'Filament 1','colour':None,'total_g':5.,'model_g':None,
            'purge_g':None,'cost_usd':None}]},*self.ENTRIES]
        summary=aggregate(entries)
        self.assertEqual(summary['unattributed']['entries'],1)
        self.assertAlmostEqual(summary['unattributed']['grams'],5.)
        self.assertEqual(summary['unattributed']['models'],['plan_2m_A3.3mf'])
        # A real role and colour win over the placeholder an unlabelled model carries.
        self.assertEqual(summary['filaments'][0]['label'],'Ivory')
        self.assertEqual(summary['filaments'][0]['colour'],'#F2F0E8')

    def test_rerunning_replaces_the_caption_instead_of_stacking_one(self):
        with tempfile.TemporaryDirectory() as folder:
            path=self.preview(folder)
            update_preview(path,self.ENTRIES,aggregate(self.ENTRIES))
            once=path.read_text()
            update_preview(path,self.ENTRIES,aggregate(self.ENTRIES))
            self.assertEqual(path.read_text(),once)
            self.assertEqual(strip_band(once),PREVIEW)


if __name__=='__main__':unittest.main()
