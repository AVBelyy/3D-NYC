import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from validate_3mf import (classify_cavity_shells,classify_positive_shells,
                          validate_material_support_settings)
from package_3mf import validate_generation_metadata


class Cavity:
    def __init__(self,volume,area):self._volume=volume;self._area=area
    def volume(self):return -self._volume
    def surface_area(self):return self._area


class ThreeMfValidationTests(unittest.TestCase):
    def test_generation_metadata_records_one_exact_shell_command(self):
        metadata={
            'schema_version':1,
            'argv':['/tmp/python','/tmp/generate_3mf.py','--job-id','map chunk'],
            'shell_command':"/tmp/python /tmp/generate_3mf.py --job-id 'map chunk'",
            'working_directory':'/tmp',
        }
        self.assertIs(validate_generation_metadata(metadata),metadata)

    def test_generation_metadata_rejects_a_command_that_disagrees_with_argv(self):
        metadata={
            'schema_version':1,
            'argv':['/tmp/python','/tmp/generate_3mf.py','--job-id','actual'],
            'shell_command':'/tmp/python /tmp/generate_3mf.py --job-id different',
            'working_directory':'/tmp',
        }
        with self.assertRaisesRegex(ValueError,'does not match its argv'):
            validate_generation_metadata(metadata,'fixture metadata')

    def test_generation_metadata_rejects_missing_schema_under_explicit_validation(self):
        with self.assertRaisesRegex(ValueError,'schema_version'):
            validate_generation_metadata({
                'argv':['python','generate_3mf.py'],
                'shell_command':'python generate_3mf.py',
                'working_directory':'/tmp',
            })

    def test_sub_extrusion_boolean_crumbs_are_not_physical_components(self):
        positive,negligible,threshold=classify_positive_shells(
            [67010.,.0326,.0077,-.01],.4,.24)
        self.assertAlmostEqual(threshold,.0384)
        self.assertEqual(positive,[67010.])
        self.assertEqual(negligible,2)

    def test_printable_disconnected_component_is_still_rejected(self):
        positive,negligible,_=classify_positive_shells([100.,.04],.4,.24)
        self.assertEqual(positive,[100.,.04])
        self.assertEqual(negligible,0)

    def test_material_interfaces_require_solid_shells(self):
        report=validate_material_support_settings({
            'interface_shells':'1','bottom_shell_layers':'3','top_shell_layers':'4'})
        self.assertEqual(report,{
            'interface_shells':True,'bottom_shell_layers':3,'top_shell_layers':4})

    def test_sparse_material_interface_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'upper color'):
            validate_material_support_settings({
                'interface_shells':'0','bottom_shell_layers':'3','top_shell_layers':'4'})

    def test_sub_layer_cavity_wedge_is_not_printable(self):
        # A 0.05 mm slit cannot form a missing 0.24 mm printed layer.
        cavity=Cavity(5.,202.)
        printable,negligible,limit=classify_cavity_shells([cavity],.4,.24)
        self.assertEqual(printable,[])
        self.assertEqual(len(negligible),1)
        self.assertEqual(limit,.24)

    def test_extrusion_scale_cavity_is_rejected(self):
        cavity=Cavity(1.,6.)
        printable,_,_=classify_cavity_shells([cavity],.4,.24)
        self.assertEqual(len(printable),1)


if __name__=='__main__':unittest.main()
