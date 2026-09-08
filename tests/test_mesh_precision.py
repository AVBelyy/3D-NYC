import sys
import unittest
from unittest.mock import Mock,patch
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import trimesh

from mesh_precision import (prepare_export_mesh,repair_export_vertices,seam_nudge_axis,
    seam_nudge_distance,weld_microscopic_faces)


class MeshPrecisionTests(unittest.TestCase):
    def test_simplifier_residual_sliver_reaches_guarded_vertex_repair(self):
        import manifold3d as md

        # A closed, truncated tetrahedron whose small cap survives simplify.
        # Keep the simplifier fixed so this regression does not depend on
        # which redundant faces a particular Manifold version removes.
        e=2e-8
        vertices=np.array([[e,0,0],[0,e,0],[0,0,e],[1,0,0],[0,1,0],[0,0,1]],dtype=float)
        faces=np.array([[0,2,1],[3,4,5],[0,4,3],[0,1,4],
                        [0,3,5],[0,5,2],[1,2,5],[1,5,4]],dtype=np.uint64)
        source=Mock(spec=md.Manifold)
        source.status.return_value=md.Error.NoError
        source.num_tri.return_value=len(faces)
        source.get_tolerance.return_value=0.
        source.to_mesh64.return_value=md.Mesh64(vertices,faces)
        source.as_original.return_value=source
        source.simplify.return_value=source
        with patch('mesh_precision.repair_export_vertices',wraps=repair_export_vertices) as repair:
            mesh,adjusted,tolerance=prepare_export_mesh(source,0.)
        self.assertGreater(repair.call_count,0)
        self.assertGreater(adjusted,0)
        self.assertGreater(tolerance,0)
        self.assert_valid_areas(mesh.vertices,mesh.faces)
        self.assertTrue(mesh.is_watertight and mesh.is_winding_consistent)
        self.assertGreater(mesh.volume,0)

        # A surviving tiny face still fails if bounded vertex repair cannot
        # fix it; reaching the fallback must not bypass validation.
        with patch('mesh_precision.repair_export_vertices',side_effect=RuntimeError('blocked repair')) as repair:
            with self.assertRaisesRegex(RuntimeError,'blocked repair'):
                prepare_export_mesh(source,0.)
        # Three simplify tolerances reach repair.  The weld candidates find
        # no coincident cluster in this solid, so they stop before repair.
        self.assertEqual(repair.call_count,3)

    def test_submicron_film_sliver_is_collapsed_rather_than_widened(self):
        # A degenerate triangle whose vertices sit half a nanometre apart
        # inside a film a micron thick cannot be grown to 1e-12 mm2 without
        # inverting the film, so the export must weld it away instead.
        vertices=np.array([[0.,0.,1.8],[3e-6,0.,1.8],[0.,3e-6,1.8],
            [1e-6,1e-6,1.8000011],[1.00039e-6,0.99970e-6,1.8000011],
            [1.00004e-6,1.00004e-6,1.8000011]])
        faces=np.array([[0,2,1],[3,4,5],[0,1,3],[1,4,3],[1,2,4],[2,5,4],[2,0,5],[0,3,5]])
        welded_vertices,welded_faces,welded=weld_microscopic_faces(vertices,faces,1.8)
        self.assertEqual(welded,2)
        self.assertEqual(len(welded_vertices),4)
        mesh=trimesh.Trimesh(welded_vertices,welded_faces,process=False)
        self.assertTrue(mesh.is_watertight and mesh.is_winding_consistent)
        self.assertGreater(mesh.volume,0)
        self.assert_valid_areas(welded_vertices,welded_faces)

    def test_weld_keeps_base_contact_and_leaves_sound_meshes_alone(self):
        # A cluster straddling the base plane stays exactly on it.
        vertices=np.array([[0.,0.,1.8],[1.,0.,1.8],[0.,1.,1.8],
            [1e-10,1e-10,1.8+4e-10],[2e-10,0.,1.8-3e-10],[0.,2e-10,1.8+1e-10]])
        faces=np.array([[3,4,5],[0,1,2]])
        welded_vertices,_,welded=weld_microscopic_faces(vertices,faces,1.8)
        self.assertEqual(welded,2)
        self.assertTrue(np.all(welded_vertices[:,2]==1.8))
        sound=np.array([[0.,0.,1.8],[1.,0.,1.8],[0.,1.,1.8]])
        unchanged,faces,welded=weld_microscopic_faces(sound,np.array([[0,1,2]]),1.8)
        self.assertEqual(welded,0)
        np.testing.assert_array_equal(unchanged,sound)

    def test_closed_solid_with_nanometre_corner_sliver(self):
        import manifold3d as md

        # Truncate a tetrahedron's corner, leaving a sub-nanometre face.
        # Enlarging that face would fold the adjacent planar surfaces.
        e=2e-8
        vertices=np.array([[e,0,0],[0,e,0],[0,0,e],[1,0,0],[0,1,0],[0,0,1]],dtype=float)
        faces=np.array([[0,2,1],[3,4,5],[0,4,3],[0,1,4],
                        [0,3,5],[0,5,2],[1,2,5],[1,5,4]],dtype=np.uint64)
        solid=md.Manifold(md.Mesh64(vertices,faces))
        self.assertEqual(solid.status(),md.Error.NoError)
        mesh,_,tolerance=prepare_export_mesh(solid,0.)
        self.assert_valid_areas(mesh.vertices,mesh.faces)
        self.assertTrue(mesh.is_watertight and mesh.is_winding_consistent)
        self.assertAlmostEqual(mesh.volume,1/6,places=8)
        self.assertLessEqual(tolerance,1e-5)
        self.assertTrue(np.all(mesh.vertices[:,2]>=0))

    def assert_valid_areas(self, vertices, faces):
        triangles=vertices[faces]
        areas=np.linalg.norm(np.cross(triangles[:,1]-triangles[:,0],
                                      triangles[:,2]-triangles[:,0]),axis=1)/2
        self.assertTrue(np.all(areas>=1e-12),areas)

    def test_fully_collapsed_triangle_needs_multiple_vertex_moves(self):
        vertices=np.array([[0,0,1.8]]*3,dtype=float)
        faces=np.array([[0,1,2]])
        repaired,adjusted=repair_export_vertices(vertices,faces,1.8)
        self.assert_valid_areas(repaired,faces)
        self.assertGreaterEqual(adjusted,2)
        self.assertTrue(np.all(repaired[:,2]==1.8))
        self.assertLessEqual(np.linalg.norm(repaired-vertices,axis=1).max(),1e-4)
        again,_=repair_export_vertices(vertices,faces,1.8)
        np.testing.assert_array_equal(repaired,again)

    def test_side_face_repair_keeps_base_vertices_and_valid_neighbor(self):
        vertices=np.array([[0,0,1.8],[0,0,1.8],[1,1,3],[-1,0,1.8],[0,-1,1.8]])
        faces=np.array([[0,1,2],[0,3,4]])
        repaired,_=repair_export_vertices(vertices,faces,1.8)
        self.assert_valid_areas(repaired,faces)
        self.assertTrue(np.all(repaired[[0,1,3,4],2]==1.8))
        normal=np.cross(repaired[3]-repaired[0],repaired[4]-repaired[0])
        self.assertGreater(normal[2],0)
        np.testing.assert_array_equal(repaired[3:],vertices[3:])

    def test_shared_collapsed_edges_do_not_undo_neighbor_repairs(self):
        vertices=np.array([[0,0,1.8],[0,0,1.8],[0,0,1.8],[1,0,1.8],[0,1,1.8]])
        faces=np.array([[0,1,3],[1,2,4],[0,2,3],[0,3,4]])
        repaired,_=repair_export_vertices(vertices,faces,1.8)
        self.assert_valid_areas(repaired,faces)
        self.assertLessEqual(np.linalg.norm(repaired-vertices,axis=1).max(),1e-4)

    def test_valid_mesh_is_unchanged(self):
        vertices=np.array([[0,0,1.8],[1,0,1.8],[0,1,1.8]])
        repaired,adjusted=repair_export_vertices(vertices,np.array([[0,1,2]]),1.8)
        np.testing.assert_array_equal(repaired,vertices)
        self.assertEqual(adjusted,0)

    def test_invalid_connectivity_is_not_repaired_by_moving_vertices(self):
        with self.assertRaisesRegex(RuntimeError,'Duplicate vertex index'):
            repair_export_vertices(np.array([[0,0,1.8],[1,0,1.8]]),
                                   np.array([[0,0,1]]),1.8)

    def test_diagonal_base_seam_does_not_move_off_contact_plane(self):
        base=float(np.float32(1.8))
        triangle=np.array([[0,0,base],[50,75,base],[100,150,base]])
        axis=seam_nudge_axis(triangle,triangle[2]-triangle[0],base)
        self.assertIn(axis,[0,1])
        triangle[1,axis]+=.00001
        self.assertTrue(np.all(triangle[:,2]==base))
        self.assertGreater(np.linalg.norm(np.cross(triangle[1]-triangle[0],triangle[2]-triangle[0])),0)

    def test_axis_aligned_base_seam_uses_perpendicular_xy_axis(self):
        triangle=np.array([[0,0,1.8],[1,0,1.8],[2,0,1.8]])
        self.assertEqual(seam_nudge_axis(triangle,[2,0,0],1.8),1)

    def test_other_seams_keep_general_noncollinear_displacement(self):
        triangle=np.array([[0,0,3.],[1,1,3.],[2,2,3.]])
        self.assertEqual(seam_nudge_axis(triangle,[2,2,0],1.8),2)

    def test_area_scaled_nudge_is_small_for_long_seams(self):
        self.assertEqual(seam_nudge_distance([100,150,0],0),1e-9)

    def test_small_seam_still_exceeds_minimum_triangle_area(self):
        direction=np.array([.001,0,0])
        offset=seam_nudge_distance(direction,1)
        area=np.linalg.norm(np.cross(direction,[0,offset,0]))/2
        self.assertGreater(area,1e-12)
        self.assertLess(offset,1e-5)


if __name__ == "__main__":
    unittest.main()
