import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manifold3d as md
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_map_meshes as build


class SerializedMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def write_parts(self, overlap):
        materials = [md.Manifold.cube([100, 100, 100]), md.Manifold(), md.Manifold(),
                     md.Manifold.cube([100, 100, 100]).translate([100 - overlap, 0, 0])]
        return {str(i): build.export(m, self.folder / f'material_{i}.ply')
                for i, m in enumerate(materials)}

    def fingerprint(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.folder.glob('*.ply')}

    def test_thin_distributed_overlap_is_repaired_and_reloaded(self):
        parts = self.write_parts(1e-5)
        before = self.fingerprint()
        parts, report = build.stabilize_serialized_materials(self.folder, parts)
        self.assertGreater(report['roundtrip_intersections_mm3'][0]['0-3'],
                           report['intersection_tolerance_mm3'])
        self.assertEqual(report['cutter_translation_mm'][2], 0.)
        meshes = [trimesh.load(self.folder / f'material_{i}.ply', process=False) for i in (0, 3)]
        for i, mesh in zip((0, 3), meshes):
            self.assertTrue(mesh.is_watertight and mesh.is_winding_consistent)
            self.assertTrue((mesh.area_faces >= 1e-12).all())
            self.assertEqual(len(mesh.faces), parts[str(i)]['triangles'])
        overlap = abs((build.solid(meshes[0]) ^ build.solid(meshes[1])).volume())
        self.assertLess(overlap, report['intersection_tolerance_mm3'])
        self.assertEqual(before['material_0.ply'], self.fingerprint()['material_0.ply'])

    def test_valid_files_are_unchanged(self):
        parts = self.write_parts(-1.)
        before = self.fingerprint()
        _, report = build.stabilize_serialized_materials(self.folder, parts)
        self.assertEqual(report['cutter_translation_mm'], [0., 0., 0.])
        self.assertEqual(before, self.fingerprint())

    def test_real_collision_rejected_without_changing_originals(self):
        parts = self.write_parts(1.)
        before = self.fingerprint()
        with self.assertRaisesRegex(RuntimeError, 'real collision'):
            build.stabilize_serialized_materials(self.folder, parts)
        self.assertEqual(before, self.fingerprint())
        self.assertFalse(list(self.folder.glob('.seam-*')))

    def test_failed_export_never_replaces_original_files(self):
        parts = self.write_parts(1e-5)
        before = self.fingerprint()
        with patch.object(build, 'export', side_effect=RuntimeError('export rejected')):
            with self.assertRaisesRegex(RuntimeError, 'export rejected'):
                build.stabilize_serialized_materials(self.folder, parts)
        self.assertEqual(before, self.fingerprint())
        self.assertFalse(list(self.folder.glob('.seam-*')))


if __name__ == '__main__':
    unittest.main()
