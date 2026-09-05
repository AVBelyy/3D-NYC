import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from slice_3mf import audit_sliced_gcode


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


if __name__=='__main__':unittest.main()
