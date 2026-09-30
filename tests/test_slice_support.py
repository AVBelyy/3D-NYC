import contextlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import slice_3mf
from slice_3mf import as_sliced_file,audit_sliced_gcode,printer_model_id


class SliceSupportTests(unittest.TestCase):
    def audit(self,text):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'plate.gcode';path.write_text(text)
            return audit_sliced_gcode(path)

    def test_elevated_material_starts_with_bottom_surface(self):
        report=self.audit('''
; interface_shells = 1
; Z_HEIGHT: 0.2
; FEATURE: Bottom surface
G1 X1 E1
; Z_HEIGHT: 2.12
; WIPE_TOWER_START
T1
; FEATURE: Prime tower
G1 X2 E1
; WIPE_TOWER_END
; FEATURE: Outer wall
G1 X3 E1
; FEATURE: Bottom surface
G1 X4 E1
''')
        self.assertEqual(report['result'],'passed')
        self.assertIn('Bottom surface',report['material_starts']['1']['features'])

    def test_missing_interface_shell_setting_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'interface shells'):
            self.audit('; Z_HEIGHT: 0.2\n; FEATURE: Bottom surface\nG1 X1 E1\n')

    def test_floating_material_start_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'solid bottom'):
            self.audit('''
; interface_shells = 1
; Z_HEIGHT: 2.12
T1
; FEATURE: Outer wall
G1 X3 E1
''')


PROJECT_MODEL='''<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter">
 <metadata name="Title">NYC polygon map</metadata>
 <resources>
  <object id="5" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="1"/></components></object>
 </resources>
 <build><item objectid="5" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build>
</model>
'''

PROJECT_SETTINGS='''<config><plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>
    <metadata key="pick_file" value="Metadata/pick_1.png"/>
    <model_instance>
      <metadata key="object_id" value="5"/>
    </model_instance>
  </plate>
  <object id="5"><part id="1"><metadata key="name" value="Ivory - roads"/></part></object>
</config>
'''


class SlicedFileTests(unittest.TestCase):
    def app(self,folder,model_id='X9'):
        """A stand-in for the slicer bundle, whose profiles carry the vendor model ids."""
        machine=Path(folder)/'Contents/Resources/profiles/BBL/machine'
        machine.mkdir(parents=True)
        (machine/'Test Printer.json').write_text(json.dumps({'name':'Test Printer','model_id':model_id}))
        binary=Path(folder)/'Contents/MacOS/BambuStudio'
        binary.parent.mkdir(parents=True);binary.write_text('')
        return binary

    def project(self,folder):
        path=Path(folder)/'chunk.gcode.3mf'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('3D/3dmodel.model',PROJECT_MODEL)
            archive.writestr('3D/Objects/object_1.model','<model>mesh</model>')
            archive.writestr('3D/_rels/3dmodel.model.rels','<Relationships/>')
            archive.writestr('Metadata/model_settings.config',PROJECT_SETTINGS)
            archive.writestr('Metadata/project_settings.config',json.dumps({'printer_model':'Test Printer'}))
            archive.writestr('Metadata/slice_info.config',
                '<config><plate><metadata key="printer_model_id" value=""/></plate></config>')
            archive.writestr('Metadata/plate_1.gcode','G1 X1 E1\n')
        return path

    def rewrite(self,folder):
        binary=self.app(folder)
        path=self.project(folder)
        as_sliced_file(path,binary)
        return zipfile.ZipFile(path)

    def test_drops_the_mesh_a_project_carries(self):
        with tempfile.TemporaryDirectory() as folder:
            names=self.rewrite(folder).namelist()
        self.assertNotIn('3D/Objects/object_1.model',names)
        self.assertNotIn('3D/_rels/3dmodel.model.rels',names)
        self.assertIn('Metadata/plate_1.gcode',names)

    def test_empties_the_resources_studio_would_reslice(self):
        with tempfile.TemporaryDirectory() as folder:
            model=self.rewrite(folder).read('3D/3dmodel.model').decode()
        self.assertNotIn('<object id="5"',model)
        self.assertIn('<build/>',model)
        self.assertIn('NYC polygon map',model)  # the project's own metadata survives

    def test_keeps_only_the_plate_block(self):
        with tempfile.TemporaryDirectory() as folder:
            config=self.rewrite(folder).read('Metadata/model_settings.config').decode()
        self.assertIn('key="gcode_file"',config)
        self.assertIn('key="pattern_bbox_file"',config)
        self.assertNotIn('<model_instance>',config)
        self.assertNotIn('<part id',config)

    def test_stamps_the_resolved_printer_model_id(self):
        with tempfile.TemporaryDirectory() as folder:
            info=self.rewrite(folder).read('Metadata/slice_info.config').decode()
        self.assertIn('value="X9"',info)

    def test_reports_an_unknown_printer_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(printer_model_id('No Such Printer',self.app(folder)),'')

    def test_leaves_no_staging_file_behind(self):
        with tempfile.TemporaryDirectory() as folder:
            self.rewrite(folder)
            self.assertEqual(sorted(p.name for p in Path(folder).glob('*.3mf*')),['chunk.gcode.3mf'])


class ExportProjectTests(unittest.TestCase):
    """``--export-project`` writes ``<model>.gcode.3mf`` beside the model, as its help says.

    The reported defect: a run outside a job slices into the scratch directory map_common
    deletes when the process exits, and the sliced project went with it.
    """

    PASSING='; interface_shells = 1\n; Z_HEIGHT: 0.2\n; FEATURE: Bottom surface\nG1 X1 E1\n'

    def run_main(self,folder,*flags,gcode=PASSING,exit_code=0):
        """Run the command line against a stand-in for the slicer; return code, model, record."""
        model=Path(folder)/'models'/'chunk.3mf'
        model.parent.mkdir();model.write_bytes(b'model')

        def slicer(model,output,slicer,export_project,announce=True):
            output.mkdir(parents=True)
            (output/'plate_1.gcode').write_text(gcode)
            record={'exit_code':exit_code,'result':'completed' if exit_code==0 else 'failed'}
            if export_project and exit_code==0:
                exported=output/f'{model.stem}.gcode.3mf'
                exported.write_bytes(b'sliced')
                record['sliced_file']=str(exported)
            return record

        valid=Path(folder)/'valid'
        argv=['slice_3mf.py','--model',str(model),'--name','slice',*flags]
        with patch.object(slice_3mf,'run_slice',side_effect=slicer), \
                patch.object(slice_3mf,'VALID',valid),patch.object(sys,'argv',argv), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                slice_3mf.main()
        return raised.exception.code,model,json.loads((valid/'slice'/'run.json').read_text())

    def test_the_sliced_project_lands_beside_the_model(self):
        with tempfile.TemporaryDirectory() as folder:
            code,model,record=self.run_main(folder,'--export-project')
            project=model.parent/'chunk.gcode.3mf'
            self.assertEqual(code,0)
            self.assertEqual(project.read_bytes(),b'sliced')
            self.assertEqual(record['sliced_file'],str(project))
            self.assertFalse((Path(folder)/'valid'/'slice'/'chunk.gcode.3mf').exists())

    def test_a_failed_support_audit_still_delivers_the_file(self):
        """The audit judges the G-code and fails the run; it does not unmake what Bambu wrote."""
        with tempfile.TemporaryDirectory() as folder:
            code,model,record=self.run_main(folder,'--export-project',gcode='G1 X1 E1\n')
            self.assertEqual(code,1)
            self.assertEqual(record['support_audit']['result'],'failed')
            self.assertTrue((model.parent/'chunk.gcode.3mf').exists())

    def test_nothing_lands_beside_the_model_unless_asked(self):
        with tempfile.TemporaryDirectory() as folder:
            code,model,record=self.run_main(folder)
            self.assertEqual(code,0)
            self.assertNotIn('sliced_file',record)
            self.assertEqual(sorted(p.name for p in model.parent.iterdir()),['chunk.3mf'])

    def test_a_failed_slice_leaves_nothing_beside_the_model(self):
        with tempfile.TemporaryDirectory() as folder:
            code,model,_=self.run_main(folder,'--export-project',exit_code=155)
            self.assertEqual(code,155)
            self.assertEqual(sorted(p.name for p in model.parent.iterdir()),['chunk.3mf'])


if __name__=='__main__':unittest.main()