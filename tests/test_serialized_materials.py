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
        # An unrepairable seam is only fatal when the geometry already on disk
        # is unacceptable. Here it is a sub-micron seam the validator accepts,
        # so every candidate failing must leave the originals in place and
        # report the attempts rather than fail the build.
        parts = self.write_parts(1e-5)
        before = self.fingerprint()
        with patch.object(build, 'export', side_effect=RuntimeError('export rejected')):
            kept, report = build.stabilize_serialized_materials(self.folder, parts)
        self.assertEqual(kept, parts)
        self.assertTrue(report['accepted_serialized_originals'])
        self.assertEqual(report['cutter_translation_mm'], [0., 0., 0.])
        self.assertEqual(len(report['rejected_candidates']), 3)
        self.assertTrue(all('export rejected' in f for f in report['rejected_candidates']))
        self.assertEqual(before, self.fingerprint())
        self.assertFalse(list(self.folder.glob('.seam-*')))

    def test_unrepairable_thick_seam_still_fails(self):
        parts = self.write_parts(1.)
        before = self.fingerprint()
        with patch.object(build, 'export', side_effect=RuntimeError('export rejected')):
            with self.assertRaisesRegex(RuntimeError, 'could not be repaired'):
                build.stabilize_serialized_materials(self.folder, parts)
        self.assertEqual(before, self.fingerprint())

    def test_long_thin_seam_outranks_its_accumulated_volume(self):
        # A coincident seam that follows every road and building edge of a
        # dense tile covers thousands of mm2, so its volume passes the
        # nozzle-volume threshold while staying far below one printed layer.
        maximum = build.maximum_seam_thickness_mm()
        self.assertTrue(build.serialized_seams_acceptable(
            {'0-1': 2.06e-3, '0-3': 1.169}, {'0-1': 1.27e-6, '0-3': 2.83e-4}, .0768, maximum))
        # The same volume spread over a genuinely thick collision is rejected.
        self.assertFalse(build.serialized_seams_acceptable(
            {'0-3': 1.169}, {'0-3': .5}, .0768, maximum))
        # Below the volume threshold the validator's volume rule still applies.
        self.assertTrue(build.serialized_seams_acceptable(
            {'0-3': 1e-3}, {'0-3': .5}, .0768, maximum))

    def test_seam_thickness_bound_matches_the_partition_report(self):
        materials = [md.Manifold.cube([10, 10, 10]), md.Manifold(), md.Manifold(), md.Manifold()]
        _, partition = build.partition_materials(materials)
        self.assertEqual(partition['maximum_expected_overlap_thickness_mm'],
                         build.maximum_seam_thickness_mm())


if __name__ == '__main__':
    unittest.main()
