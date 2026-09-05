"""Numerical seam cleanup that preserves the shared horizontal base plane."""
import numpy as np


def prepare_export_mesh(manifold, base_mm):
    """Remove microscopic Boolean slivers before repairing collinear seams.

    A tiny triangle cannot always be enlarged without folding its neighbors.
    Manifold's simplifier removes edges using a subset of existing vertices.
    Reset Boolean face provenance so it can collapse artificial CSG boundaries.
    Each candidate starts from the original solid, not a previously simplified
    or nudged result; the maximum surface tolerance is 0.01 micrometre.
    """
    import manifold3d as md
    import trimesh

    last_error = None
    cleanup_source = None
    for tolerance in (0., 1e-7, 1e-6, 1e-5):
        candidate = manifold
        if tolerance:
            # simplify() uses at least the solid's existing tolerance.
            if manifold.get_tolerance() > 1e-5:
                raise RuntimeError('Existing Manifold tolerance exceeds export cleanup bound')
            if cleanup_source is None:
                cleanup_source = manifold.as_original()
            candidate = cleanup_source.simplify(tolerance)
        if candidate.status() != md.Error.NoError or candidate.num_tri() == 0:
            raise RuntimeError('Export topology cleanup produced an invalid or empty solid')
        data = candidate.to_mesh64()
        index = np.arange(len(data.vert_properties))
        index[np.asarray(data.merge_from_vert, dtype=int)] = np.asarray(data.merge_to_vert, dtype=int)
        while not np.array_equal(index, index[index]):
            index = index[index]
        mesh = trimesh.Trimesh(data.vert_properties[:, :3], index[data.tri_verts], process=False)
        mesh.remove_unreferenced_vertices()
        vertices = mesh.vertices.copy()
        vertices[np.abs(vertices[:, 2] - base_mm) < 1e-8, 2] = base_mm
        mesh.vertices = vertices
        bad = mesh.area_faces < 1e-12
        # Prefer removing microscopic slivers before trying to enlarge them.
        # This is only a routing decision for the original mesh: simplify()
        # bounds surface error, not minimum face area. Its residual slivers
        # must reach guarded vertex repair rather than being rejected here.
        if tolerance == 0. and np.any(bad):
            triangles = vertices[mesh.faces[bad]]
            edges = triangles - np.roll(triangles, 1, axis=1)
            tiny = np.max(np.linalg.norm(edges, axis=2), axis=1) < 2e-6
            if np.any(tiny):
                last_error = RuntimeError(
                    f'{int(tiny.sum())} microscopic export triangles remain after '
                    f'{tolerance:g} mm topology cleanup')
                continue
        try:
            mesh.vertices, adjusted = repair_export_vertices(vertices, mesh.faces, base_mm)
        except RuntimeError as error:
            last_error = error
            continue
        if not np.all(mesh.area_faces >= 1e-12):
            last_error = RuntimeError('Export vertex repair left degenerate faces')
            continue
        if not (mesh.is_watertight and mesh.is_winding_consistent and mesh.volume > 0):
            raise RuntimeError('Export cleanup did not preserve a closed, positive-volume solid')
        return mesh, adjusted, max(tolerance, manifold.get_tolerance()) if tolerance else 0.
    raise RuntimeError(f'Export topology cleanup exhausted its 1e-5 mm bound: {last_error}') from last_error


def repair_export_vertices(vertices, faces, base_mm, minimum_area=1e-12,
                           maximum_motion=1e-4):
    """Repair numerical slivers without changing connectivity or valid neighbors.

    Each accepted edit eliminates a bad face and preserves every already valid
    incident face, so repairs cannot oscillate. Motion is bounded relative to
    the original vertices (0.1 micrometre by default), including across edits.
    Base vertices stay on the base even when the repaired face is a side wall.
    """
    original = np.asarray(vertices, dtype=float)
    vertices = original.copy()
    faces = np.asarray(faces, dtype=np.int64)
    if not np.isfinite(vertices).all():
        raise RuntimeError('Non-finite export vertices')

    def normals(triangles):
        return np.cross(triangles[:, 1] - triangles[:, 0],
                        triangles[:, 2] - triangles[:, 0])

    areas = np.linalg.norm(normals(vertices[faces]), axis=1) / 2
    if not np.any(areas < minimum_area):
        return vertices, 0
    if np.any((faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) |
              (faces[:, 2] == faces[:, 0])):
        raise RuntimeError('Duplicate vertex index in export face')
    # Compact vertex-to-face adjacency; candidate checks touch only the patch.
    order = np.argsort(faces.ravel(), kind='stable')
    offsets = np.r_[0, np.cumsum(np.bincount(faces.ravel(), minlength=len(vertices)))]
    incident = order // 3
    on_base = np.abs(original[:, 2] - base_mm) < 1e-8
    rng = np.random.default_rng(0)
    adjusted = 0
    while np.any(areas < minimum_area):
        progress = False
        for fi in np.flatnonzero(areas < minimum_area):
            if areas[fi] >= minimum_area:
                continue
            ids = faces[fi]
            affected = np.unique(np.concatenate([
                incident[offsets[v]:offsets[v + 1]] for v in ids]))
            local_faces = faces[affected]
            before = vertices[local_faces].copy()
            old_normals = normals(before)
            valid = areas[affected] >= minimum_area
            target = int(np.searchsorted(affected, fi))
            current = vertices[ids].copy()

            def accept(candidate):
                nonlocal adjusted
                if np.any(np.linalg.norm(candidate - original[ids], axis=1) > maximum_motion):
                    return False
                after = before.copy()
                for index, vertex in enumerate(ids):
                    after[local_faces == vertex] = candidate[index]
                new_normals = normals(after)
                new_areas = np.linalg.norm(new_normals, axis=1) / 2
                if (not np.isfinite(new_areas).all() or
                        new_areas[target] < 2 * minimum_area or
                        np.any(new_areas[valid] < minimum_area) or
                        np.any(np.einsum('ij,ij->i', old_normals[valid], new_normals[valid]) <= 0)):
                    return False
                adjusted += int(np.any(candidate != current, axis=1).sum())
                vertices[ids] = candidate
                areas[affected] = new_areas
                return True

            repaired = False
            # Use the edge opposite the vertex actually being moved. Try both
            # signs: adding a fixed positive offset can cancel existing area.
            for moving in range(3):
                a, b = [i for i in range(3) if i != moving]
                direction = current[b] - current[a]
                axes = (0, 1) if on_base[ids[moving]] else (0, 1, 2)
                for axis in sorted(axes, key=lambda axis: abs(direction[axis])):
                    perpendicular = np.linalg.norm(np.delete(direction, axis))
                    if perpendicular == 0:
                        continue
                    distance = max(1e-9, 8 * minimum_area / perpendicular)
                    for sign in (1, -1):
                        candidate = current.copy()
                        candidate[moving, axis] += sign * distance
                        if accept(candidate):
                            repaired = True
                            break
                    if repaired:
                        break
                if repaired:
                    break
            if not repaired:
                # Entirely collapsed triangles need two independent edges;
                # moving a single endpoint can never give them nonzero area.
                # Deterministic bounded patch candidates also handle coupled
                # slivers sharing vertices. Every candidate is audited above.
                for scale in maximum_motion * np.array([1e-4, 1e-3, 1e-2, .1, .5]):
                    for _ in range(32):
                        delta = rng.uniform(-scale, scale, size=(3, 3))
                        delta[on_base[ids], 2] = 0
                        if accept(current + delta):
                            repaired = True
                            break
                    if repaired:
                        break
            progress |= repaired
        if not progress:
            bad = np.flatnonzero(areas < minimum_area)
            raise RuntimeError(
                f'Cannot repair {len(bad)} export faces within {maximum_motion:g} mm '
                f'without damaging adjacent faces; minimum area={areas[bad].min():g} mm2; '
                f'first face={int(bad[0])}, vertices={vertices[faces[bad[0]]].tolist()}'
            )
    return vertices, adjusted


def seam_nudge_axis(triangle, direction, base_mm):
    """Choose a non-collinear displacement without opening the base contact.

    Every material rests on the same base. Moving a collinear bottom triangle
    in Z creates a thin sheet across a potentially very large contact face;
    repeated Boolean repairs can amplify that sheet. An XY displacement keeps
    the shared plane exact while giving the degenerate triangle nonzero area.
    """
    triangle = np.asarray(triangle, dtype=float)
    direction = np.asarray(direction, dtype=float)
    if np.all(np.abs(triangle[:, 2] - base_mm) < 1e-7):
        return int(np.argmin(np.abs(direction[:2])))
    return int(np.argmin(np.abs(direction)))


def seam_nudge_distance(direction, axis):
    """Move only far enough to clear the 1e-12 mm² degenerate-face threshold.

    A fixed offset unnecessarily moves long seam triangles and can accumulate
    appreciable overlap over a detailed city tile. Triangle area grows as half
    the perpendicular edge length times the displacement. Aim for twice the
    validation threshold, with a nanometre-scale numerical floor in model units.
    """
    direction = np.asarray(direction, dtype=float)
    perpendicular = np.linalg.norm(np.delete(direction, axis))
    return max(1e-9, min(1e-4, 4e-12 / max(perpendicular, 1e-12)))
