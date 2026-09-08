"""Make closed material solids, then cut explicit underpasses. No slicer repair dependency."""
import argparse,gc,json,math,time,tempfile
from pathlib import Path
import numpy as np,geopandas as gpd,shapely,trimesh,manifold3d as md,mapbox_earcut,laspy
from shapely.geometry import Polygon,LineString,Point,box
from shapely.ops import substring
from scipy.ndimage import label,find_objects
from map_common import *
from crossings import (constrain_deck_to_visible_surface,minimum_crossing_length_mm,
    minimum_crossing_floor_mm,printable_tunnel_profile,structural_roof_thickness_mm)
from _material_layers import drawn_line_relief_mm
from mesh_precision import prepare_export_mesh
from road_symbols import TRAIL_HIGHWAYS,trail_width_mm

# Every road height this module builds -- a tunnel floor, a bridge deck, the
# baseline of the route running under one -- is the top of a drawn road line,
# not the pavement pad beside it. It has to be the height build_map_fields
# painted that same line at, or a crossing is cut against a surface the visible
# model never had.
LINE_RELIEF_MM=drawn_line_relief_mm(CFG)

class Phases:
    """Record wall-clock per construction phase into the mesh report.

    A chunk takes minutes and the phases differ by orders of magnitude, so
    attributing a slowdown without timings is guesswork. Boolean cost tracks
    the number of intersecting faces between operands, not their triangle
    count, which is why a change that adds 4% triangles can move the total a
    long way and a change that adds many more can be free.
    """
    def __init__(self):self.records={}
    def __call__(self,name):
        phases=self
        class Scope:
            def __enter__(self):self.t=time.monotonic();return self
            def __exit__(self,*exc):
                phases.records[name]=round(phases.records.get(name,0.)+time.monotonic()-self.t,3)
                print('Phase',name,f'{phases.records[name]:.1f}s',flush=True)
        return Scope()

# Vertex motion allowed when each material solid is simplified. Every boolean
# in the build costs by intersecting-face count, so this is the one constant
# that moves the whole pipeline. It is not a free dial: two materials sharing a
# wall are simplified independently, so raising it lets their two copies drift
# further apart -- which is exactly the drift the partition passes then have to
# repair. maximum_seam_thickness_mm() derives the repair bound from this value,
# so the tolerance and the defect it admits stay in step.
INITIAL_SIMPLIFY_MM=float(CFG.get('mesh_simplify_mm',.006))
# Base rasters are simplified before crossing construction. Independently
# simplifying colors again after CSG can pull a tunnel approach's shared road
# seam apart, leaving an enclosed wedge. Preserve the constructed interfaces.
FINAL_SIMPLIFY_MM=0.
MAXIMUM_BRIDGE_SPAN_NOZZLES=3.
OPENING_STATUSES={'cut','water bridge opening','road/path overpass opening'}

def maximum_bridge_span_mm(nozzle_mm):
    """Return the longest roof span allowed by the active print profile."""
    nozzle=float(nozzle_mm)
    if not math.isfinite(nozzle) or nozzle<=0:
        raise ValueError(f'nozzle_mm must be a finite positive number, got {nozzle_mm!r}')
    return nozzle*MAXIMUM_BRIDGE_SPAN_NOZZLES

def printable_roof_span(requested_span_mm,nozzle_mm):
    """Bound a crossing aperture without shrinking its mapped road/floor.

    Wide roads can legitimately produce cartographic symbols wider than the
    short bridges supported by the print profile. Narrow only the hidden
    opening in that case. The remaining shoulders become grounded side
    supports, while the full-width surface symbol remains geographically
    faithful and the reported roof span describes the geometry actually cut.
    """
    requested=float(requested_span_mm)
    if not math.isfinite(requested) or requested<=0:
        raise ValueError(
            f'requested roof span must be a finite positive number, got {requested_span_mm!r}')
    return min(requested,maximum_bridge_span_mm(nozzle_mm))

def audit_layer_support(crossings,nozzle_mm,layer_height_mm):
    """Reject openings whose roofs cannot be treated as short FDM bridges.

    Ordinary colored surfaces and reinforcements sit on a continuous ivory
    substrate. Tunnel and underpass roofs are the only deliberate horizontal
    spans, so make their printable span and thickness bounded invariants.
    """
    openings=[item for item in crossings if item.get('status') in OPENING_STATUSES]
    missing=[int(item.get('osm_id',-1)) for item in openings if 'roof_span_mm' not in item]
    if missing:
        raise RuntimeError(f'Opening records lack a roof span: {missing}')
    invalid=[]
    for item in openings:
        try:span=float(item['roof_span_mm'])
        except (TypeError,ValueError):span=float('nan')
        if not math.isfinite(span) or span<=0:
            invalid.append((int(item.get('osm_id',-1)),item['roof_span_mm']))
    if invalid:
        raise RuntimeError(f'Opening records have invalid roof spans: {invalid}')
    missing_thickness=[int(item.get('osm_id',-1)) for item in openings if 'roof_thickness_mm' not in item]
    if missing_thickness:
        raise RuntimeError(f'Opening records lack a structural roof thickness: {missing_thickness}')
    minimum_thickness=structural_roof_thickness_mm(nozzle_mm,layer_height_mm)
    thin=[]
    for item in openings:
        try:thickness=float(item['roof_thickness_mm'])
        except (TypeError,ValueError):thickness=float('nan')
        if not math.isfinite(thickness) or thickness+1e-9<minimum_thickness:
            thin.append((int(item.get('osm_id',-1)),item['roof_thickness_mm']))
    if thin:
        raise RuntimeError(
            f'Opening roofs are thinner than the {minimum_thickness:g} mm structural minimum: {thin}')
    limit=maximum_bridge_span_mm(nozzle_mm)
    excessive=[item for item in openings if float(item['roof_span_mm'])>limit+1e-9]
    if excessive:
        details=[(int(item.get('osm_id',-1)),float(item['roof_span_mm'])) for item in excessive]
        raise RuntimeError(f'Unsupported roof spans exceed {limit:g} mm: {details}')
    spans=[float(item['roof_span_mm']) for item in openings]
    return {
        'result':'passed','ordinary_geometry':'colored surface solids seated on a continuous ivory substrate',
        'explicit_bridge_roofs':len(openings),'maximum_bridge_span_mm':max(spans,default=0.),
        'maximum_allowed_bridge_span_mm':limit,
        'maximum_allowed_bridge_span_nozzle_widths':MAXIMUM_BRIDGE_SPAN_NOZZLES,
        'minimum_structural_roof_thickness_mm':minimum_thickness,
        'minimum_structural_roof_layers':minimum_thickness/float(layer_height_mm),
    }

def solid(mesh):
    if mesh.volume<0:mesh.invert()
    m=md.Manifold(md.Mesh64(np.ascontiguousarray(mesh.vertices,dtype=np.float64),np.ascontiguousarray(mesh.faces,dtype=np.uint64)))
    if m.status()!=md.Error.NoError:raise RuntimeError(f'Non-manifold input: {m.status()}')
    return m

def write_ply64(mesh,path):
    """Atomically write a minimal binary PLY without truncating vertices to float32.

    Trimesh's PLY exporter always declares vertex properties as ``float`` and
    casts them to 32 bits.  Boolean seam repairs are intentionally sub-micron,
    so that cast can merge distinct vertices and recreate zero-area triangles
    after the in-memory mesh has passed.  PLY supports ``double`` properties;
    use them and validate the exact file that downstream stages will consume.
    """
    path=Path(path);temporary=path.with_name(path.stem+'.tmp'+path.suffix)
    vertices=np.ascontiguousarray(mesh.vertices,dtype='<f8')
    face_dtype=np.dtype([('count','u1'),('indices','<i4',(3,))])
    faces=np.empty(len(mesh.faces),dtype=face_dtype)
    faces['count']=3;faces['indices']=np.asarray(mesh.faces,dtype='<i4')
    header=(
        'ply\nformat binary_little_endian 1.0\n'
        'comment 3D-NYC lossless intermediate mesh\n'
        f'element vertex {len(vertices)}\n'
        'property double x\nproperty double y\nproperty double z\n'
        f'element face {len(faces)}\n'
        'property list uchar int vertex_indices\nend_header\n'
    ).encode('ascii')
    with temporary.open('wb') as stream:
        stream.write(header);stream.write(vertices.tobytes());stream.write(faces.tobytes())
    return temporary

def export(m,path):
    path=Path(path)
    if m.status()!=md.Error.NoError:raise RuntimeError(f'Boolean error: {m.status()}')
    if m.num_tri()==0:
        if path.exists():path.unlink()
        return {'vertices':0,'triangles':0,'volume_mm3':0.,'bounds_mm':None,
            'watertight':True,'winding_consistent':True,'empty':True,
            'submicron_export_vertex_adjustments':0}
    shared_base=float(np.float32(CFG['base_mm']))
    try:
        mesh,adjusted,cleanup_tolerance=prepare_export_mesh(m,shared_base)
    except RuntimeError as error:
        raise RuntimeError(f'{path}: {error}') from error
    assert mesh.is_watertight and mesh.is_winding_consistent and mesh.volume>0,(path,mesh.is_watertight,mesh.volume)
    assert (mesh.area_faces>=1e-12).all(),(path,int((mesh.area_faces<1e-12).sum()))
    temporary=write_ply64(mesh,path)
    try:
        roundtrip=trimesh.load(temporary,process=False)
        roundtrip_bad=int((roundtrip.area_faces<1e-12).sum())
        assert np.isfinite(roundtrip.vertices).all(),f'Non-finite serialized vertices in {temporary}'
        assert roundtrip.is_watertight and roundtrip.is_winding_consistent and roundtrip.volume>0,(
            temporary,roundtrip.is_watertight,roundtrip.is_winding_consistent,roundtrip.volume)
        assert roundtrip_bad==0,(temporary,roundtrip_bad)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True);raise
    return {'vertices':len(roundtrip.vertices),'triangles':len(roundtrip.faces),'volume_mm3':float(roundtrip.volume),
        'bounds_mm':roundtrip.bounds.tolist(),'watertight':bool(roundtrip.is_watertight),'winding_consistent':bool(roundtrip.is_winding_consistent),
        'serialized_vertex_precision_bits':64,
        'export_topology_cleanup_tolerance_mm':cleanup_tolerance,
        'submicron_export_vertex_adjustments':adjusted}

def snap_for_export(m):
    """A binary grid avoids slivers collapsing when the slicer converts doubles to floats.

    1/16384 mm is ~61 nanometres, far below any printable feature. Manifold
    removes collapsed triangles while preserving the closed topology.
    """
    data=m.to_mesh64();index=np.arange(len(data.vert_properties))
    index[np.asarray(data.merge_from_vert,dtype=int)]=np.asarray(data.merge_to_vert,dtype=int)
    for _ in range(5):index=index[index]
    denominator=CFG.get('export_snap_denominator',16384)
    vertices=np.round(data.vert_properties[:,:3]*denominator)/denominator
    result=solid(trimesh.Trimesh(vertices,index[data.tri_verts],process=False))
    # Snapping can align three distinct vertices into a zero-area triangle.
    # Remove such redundant edges at 0.1 micrometre tolerance before export.
    return result.simplify(max(.00001,2/denominator))

def maximum_seam_thickness_mm():
    """Bound the overlap two independently simplified copies of one wall can make.

    Each closed solid is simplified once before crossing construction and its
    exported vertices are snapped to the export grid, so either copy of a
    shared surface can move by that much.  Twice the per-surface motion is the
    thickest purely numerical seam the pipeline can produce; anything thicker
    is a modeling collision.  ``validate_3mf.py`` reads this value back from
    the mesh report, so keep every seam decision on this one definition.
    """
    denominator=CFG.get('export_snap_denominator',16384)
    return 2*(INITIAL_SIMPLIFY_MM+FINAL_SIMPLIFY_MM+max(.00001,2/denominator))

def serialized_seams_acceptable(intersections,thicknesses,tolerance,maximum_thickness):
    """Apply the material-overlap rule that the strict 3MF validator applies.

    A pair is stable when its accumulated volume stays under the nozzle-volume
    threshold or when the overlap is thinner than the vertex motion the
    simplify and export passes are allowed to introduce.  Both measures are
    needed: a coincident seam that follows every road and building edge of a
    dense tile covers thousands of mm2, so it can pass the volume threshold
    while remaining hundreds of times thinner than one printed layer.
    """
    return all(volume<tolerance or thicknesses.get(key,float('inf'))<=maximum_thickness
        for key,volume in intersections.items())

def partition_materials(materials):
    """Make independently simplified color solids mutually exclusive.

    The raster cells form an exact partition, but simplifying each closed solid
    separately can move the two copies of a shared wall in opposite directions.
    On a detailed city tile, micron-deep slivers can accumulate into a material
    overlap large enough to fail the volume-based 3MF audit.  Resolve those
    seams in stable filament order after every operation that can move vertices.

    A genuine modeling collision must not be silently repaired.  For a thin
    overlap, ``2 * volume / surface_area`` estimates its thickness.  Bound that
    by the maximum relative motion allowed by the two independent simplify
    passes and export-grid cleanup; a thicker intersection remains an error.
    """
    maximum_expected_thickness=maximum_seam_thickness_mm()
    claimed=md.Manifold();cleaned=[];records=[]
    for color,material in enumerate(materials):
        overlap_volume=0.;overlap_area=0.;effective_thickness=0.
        if material.num_tri() and claimed.num_tri():
            overlap,material=material.split(claimed)
            if overlap.status()!=md.Error.NoError or material.status()!=md.Error.NoError:
                raise RuntimeError(f'Failed to partition material {color}')
            overlap_volume=abs(float(overlap.volume()))
            overlap_area=float(overlap.surface_area())
            if overlap_area>0:effective_thickness=2*overlap_volume/overlap_area
            if effective_thickness>maximum_expected_thickness:
                raise RuntimeError(
                    f'Material {color} has a real collision with higher-priority materials: '
                    f'{overlap_volume:g} mm3 at approximately {effective_thickness:g} mm thick; '
                    f'the simplification bound is {maximum_expected_thickness:g} mm'
                )
        cleaned.append(material)
        if material.num_tri():claimed=material if not claimed.num_tri() else claimed+material
        records.append({'material':color,'removed_overlap_mm3':overlap_volume,
            'overlap_surface_area_mm2':overlap_area,
            'effective_overlap_thickness_mm':effective_thickness})
        print('Partitioned material',color,'removed overlap mm3',overlap_volume,
            'effective thickness mm',effective_thickness,flush=True)
    return cleaned,{'priority':list(range(len(materials))),
        'maximum_expected_overlap_thickness_mm':maximum_expected_thickness,
        'parts':records}


def seat_colored_surfaces(materials):
    """Cut simplified colored skins out of ivory once, preserving exact support contacts."""
    colored=[material for material in materials[1:] if material.num_tri()]
    if not colored or not materials[0].num_tri():
        return materials,{'removed_overlap_mm3':0.,'effective_overlap_thickness_mm':0.}
    colors=md.Manifold.batch_boolean(colored,md.OpType.Add)
    overlap,ivory=materials[0].split(colors)
    if overlap.status()!=md.Error.NoError or ivory.status()!=md.Error.NoError:
        raise RuntimeError('Failed to seat colored surface materials on the ivory substrate')
    volume=abs(float(overlap.volume()));area=float(overlap.surface_area())
    thickness=2*volume/area if area else 0.
    # Independent raster-surface simplification can move either copy of a
    # shared boundary. Anything approaching one whole manufacturing cell is a
    # modeling error rather than numerical seating.
    limit=float(CFG['grid_step_mm'])/2
    if thickness>limit+1e-9:
        raise RuntimeError(
            f'Colored surfaces overlap the ivory substrate by approximately {thickness:g} mm; '
            f'the seating limit is {limit:g} mm')
    result=list(materials);result[0]=ivory
    return result,{'removed_overlap_mm3':volume,'overlap_surface_area_mm2':area,
        'effective_overlap_thickness_mm':thickness,'maximum_allowed_thickness_mm':limit}

def stabilize_serialized_materials(folder,parts):
    """Resolve coincident CSG seams against the actual serialized geometry.

    Repeated exact subtraction of reconstructed, coincident faces can generate
    more slivers on each pass. Break that coincidence with a bounded horizontal
    cutter translation (at most 0.000013 mm). Z stays exact so materials retain
    their contact with the base. Each trial starts from the original files and
    is committed only after export and an independent audit of every pair.
    """
    tolerance=max(.05,float(CFG['nozzle_mm'])**2*float(CFG['layer_height_mm'])*2)
    materials=[solid(trimesh.load(folder/f'material_{i}.ply',process=False))
        if not parts[str(i)].get('empty',False) else md.Manifold() for i in range(4)]
    def audit(solids):
        intersections={}
        thicknesses={}
        for i in range(4):
            for j in range(i+1,4):
                if not solids[i].num_tri() or not solids[j].num_tri():continue
                overlap=solids[i]^solids[j]
                if overlap.status()!=md.Error.NoError:
                    raise RuntimeError(f'Boolean error auditing materials {i}-{j}: {overlap.status()}')
                volume=abs(float(overlap.volume()))
                area=float(overlap.surface_area())
                intersections[f'{i}-{j}']=volume
                thicknesses[f'{i}-{j}']=2*volume/area if area>0 else (float('inf') if volume else 0.)
        return intersections,thicknesses
    initial,initial_thickness=audit(materials);history=[initial]
    report={'intersection_tolerance_mm3':tolerance,'roundtrip_intersections_mm3':history,
        'roundtrip_intersection_effective_thickness_mm':[initial_thickness],
        'cutter_translation_mm':[0.,0.,0.]}
    print('Serialized seam audit',0,initial,'effective thickness',initial_thickness,flush=True)
    maximum_thickness=maximum_seam_thickness_mm()
    report['maximum_seam_thickness_mm']=maximum_thickness
    # Volume accumulates along long seams. A seam thinner than the allowed
    # relative vertex motion cannot create a printable overlap, even when its
    # total volume exceeds the nozzle-volume threshold. Treat it as already
    # stable; repeated coincident-face subtraction can make it worse.
    if max(initial.values(),default=0.)<tolerance:return parts,report
    affected={i:[j for j in range(i) if initial.get(f'{j}-{i}',0.)>=tolerance] for i in range(4)}
    failures=[]
    for offset in ((1e-5,7.31e-6,0.),(-1e-5,-7.31e-6,0.),(7.31e-6,-1e-5,0.)):
        trial=list(materials);trial_parts=dict(parts)
        with tempfile.TemporaryDirectory(prefix='.seam-',dir=folder) as temporary:
            staging=Path(temporary)
            try:
                for i,cutters in affected.items():
                    if not cutters:continue
                    claimed=md.Manifold.batch_boolean([trial[j] for j in cutters],md.OpType.Add)
                    overlap,cleaned=materials[i].split(claimed.translate(offset))
                    if overlap.status()!=md.Error.NoError or cleaned.status()!=md.Error.NoError:
                        raise RuntimeError(f'Failed to repair serialized material {i}')
                    area=float(overlap.surface_area());volume=abs(float(overlap.volume()))
                    thickness=2*volume/area if area>0 else (float('inf') if volume else 0.)
                    if thickness>maximum_thickness:
                        raise RuntimeError(f'Material {i} has a real collision: {thickness:g} mm thick')
                    trial_parts[str(i)]=export(cleaned,staging/f'material_{i}.ply')
                    trial[i]=(solid(trimesh.load(staging/f'material_{i}.ply',process=False))
                        if not trial_parts[str(i)].get('empty',False) else md.Manifold())
                intersections,thicknesses=audit(trial);history.append(intersections)
                report['roundtrip_intersection_effective_thickness_mm'].append(thicknesses)
                print('Serialized seam audit offset',offset,intersections,
                    'effective thickness',thicknesses,flush=True)
                if not serialized_seams_acceptable(intersections,thicknesses,tolerance,maximum_thickness):continue
                report['accepted_by_thickness']=max(intersections.values(),default=0.)>=tolerance
            except (RuntimeError,AssertionError) as error:
                failures.append(str(error))
                print('Serialized seam candidate rejected',offset,str(error),flush=True)
                continue
            for i,cutters in affected.items():
                if not cutters:continue
                destination=folder/f'material_{i}.ply'
                if trial_parts[str(i)].get('empty',False):destination.unlink(missing_ok=True)
                else:(staging/destination.name).replace(destination)
            report['cutter_translation_mm']=list(offset)
            report['rejected_candidates']=failures
            return trial_parts,report
    # Every cutter translation failed.  Subtracting a reconstructed coincident
    # face can create slivers faster than the bounded export cleanup absorbs
    # them, and that gets likelier as a tile carries more shared boundary.  The
    # repair is an improvement, not a requirement: it is only fatal when the
    # geometry already on disk is unacceptable.  Keep the untouched originals
    # whenever they satisfy the same rule the packaged 3MF is validated by.
    if serialized_seams_acceptable(initial,initial_thickness,tolerance,maximum_thickness):
        report['accepted_by_thickness']=True
        report['accepted_serialized_originals']=True
        report['rejected_candidates']=failures
        print('Serialized seam repair unavailable; kept the exported geometry, whose seams '
            f'stay within {maximum_thickness:g} mm',flush=True)
        return parts,report
    raise RuntimeError(f'Serialized seams could not be repaired below {tolerance:g} mm3 or '
        f'{maximum_thickness:g} mm effective thickness; audits={history}; '
        f'effective thickness={report["roundtrip_intersection_effective_thickness_mm"]}; '
        f'rejected candidates={failures}')

def clip_to_tile(m,color):
    """Constrain explicit additions to the requested model footprint.

    Raster-derived solids already end at the grid boundary.  Vector additions
    such as bridge substrates and tunnel approaches can originate from source
    features that merely intersect the AOI and extend beyond it.  Keep the
    final boundary invariant here as a last line of defence for every layer.
    """
    if m.num_tri()==0:return m
    vertices=m.to_mesh64().vert_properties[:,:3]
    bounds=np.array([vertices.min(axis=0),vertices.max(axis=0)])
    tolerance=.00001
    rectangular=MODEL_AOI.symmetric_difference(box(0,0,W,H)).area<1e-6
    outside=(not rectangular or bounds[0,0] < -tolerance or bounds[0,1] < -tolerance or
             bounds[1,0] > W+tolerance or bounds[1,1] > H+tolerance)
    if not outside:return m
    zmax=max(1.,float(bounds[1,2])+1.)
    tile=polygon_prism(MODEL_AOI,0,zmax)
    clipped=md.Manifold.batch_boolean([m,tile],md.OpType.Intersect)
    if clipped.status()!=md.Error.NoError:
        raise RuntimeError(f'Failed to clip material {color} to the requested polygon: {clipped.status()}')
    print('Clipped material',color,'from XY bounds',bounds[:,:2].tolist(),'to polygon footprint',flush=True)
    return clipped

def cell_vertex_heights(values,mask,reducer='mean'):
    """Convert cell heights to corner heights with an explicit seam reducer."""
    values=np.asarray(values);mask=np.asarray(mask,dtype=bool)
    if values.shape!=mask.shape or values.ndim!=2:raise ValueError('Cell values and mask must be same-shaped 2-D arrays')
    ny,nx=mask.shape;n=np.zeros((ny+1,nx+1),np.uint8)
    if reducer=='mean':result=np.zeros((ny+1,nx+1),np.float32)
    elif reducer=='minimum':result=np.full((ny+1,nx+1),np.inf,np.float32)
    else:raise ValueError(f'Unknown cell-to-vertex reducer {reducer!r}')
    weighted=np.where(mask,values,0)
    for rows,cols in [(slice(0,ny),slice(0,nx)),(slice(1,ny+1),slice(0,nx)),
                      (slice(0,ny),slice(1,nx+1)),(slice(1,ny+1),slice(1,nx+1))]:
        n[rows,cols]+=mask
        if reducer=='mean':result[rows,cols]+=weighted
        else:
            window=result[rows,cols];np.minimum(window,np.where(mask,values,np.inf),out=window)
    use=n>0
    if reducer=='mean':result[use]/=n[use]
    result[~use]=np.nan
    return result,use


def raster_volume(top_cells,mask,bottom_vertices,step):
    """Build one closed raster volume between a material top and shared lower surface."""
    top_grid,use=cell_vertex_heights(top_cells,mask,'mean')
    if not mask.any():return md.Manifold()
    if bottom_vertices.shape!=top_grid.shape:raise ValueError('Bottom vertex grid does not match raster volume')
    if not np.isfinite(bottom_vertices[use]).all():raise ValueError('Raster volume has an undefined lower surface')
    ny,nx=mask.shape;rr,cc=np.where(use);nv=len(rr)
    top=np.column_stack([cc*step,H-rr*step,top_grid[use]]).astype(np.float32)
    bottom=top.copy();bottom[:,2]=bottom_vertices[use]
    if np.any(bottom[:,2]>=top[:,2]-1e-6):
        raise ValueError('Raster volume lower surface must stay below its material top')
    vertices=np.vstack([top,bottom]);del top,bottom,top_grid
    ids=np.full((ny+1,nx+1),-1,np.int32);ids[use]=np.arange(nv)
    tl=ids[:-1,:-1][mask];tr=ids[:-1,1:][mask];bl=ids[1:,:-1][mask];br=ids[1:,1:][mask]
    faces=np.vstack([np.column_stack([tl,bl,tr]),np.column_stack([tr,bl,br])]).astype(np.uint32)
    walls=[]
    west=mask&~np.pad(mask[:,:-1],((0,0),(1,0)))
    east=mask&~np.pad(mask[:,1:],((0,0),(0,1)))
    north=mask&~np.pad(mask[:-1,:],((1,0),(0,0)))
    south=mask&~np.pad(mask[1:,:],((0,1),(0,0)))
    for edge,a,b in [(west,ids[:-1,:-1],ids[1:,:-1]),(south,ids[1:,:-1],ids[1:,1:]),
                     (east,ids[1:,1:],ids[:-1,1:]),(north,ids[:-1,1:],ids[:-1,:-1])]:
        ia,ib=a[edge],b[edge]
        walls.extend([np.column_stack([ib,ia,ia+nv]),np.column_stack([ib,ia+nv,ib+nv])])
    faces=np.vstack([faces,faces[:,::-1]+nv,*walls]).astype(np.uint32)
    del ids,use,walls,tl,tr,bl,br
    mesh=trimesh.Trimesh(vertices,faces,process=False)
    m=solid(mesh);del mesh,vertices,faces;gc.collect()
    return m.simplify(INITIAL_SIMPLIFY_MM)


def material_solid(h,substrate,mat,aoi,color,stride=1):
    h=h[::stride,::stride];substrate=substrate[::stride,::stride]
    mat=mat[::stride,::stride];aoi=aoi[::stride,::stride].astype(bool)
    mask=mat==color
    if not mask.any() and color!=0:
        print('Material',color,'empty',flush=True);return md.Manifold()
    ny,nx=mask.shape;step=W/nx;base=float(np.float32(CFG['base_mm']))
    substrate_vertices,substrate_use=cell_vertex_heights(substrate,aoi,'minimum')
    base_vertices=np.full(substrate_vertices.shape,base,dtype=np.float32)
    substrate_solid=md.Manifold()
    if color==0:
        substrate_solid=raster_volume(substrate,aoi,base_vertices,step)
        substrate_solid=substrate_solid+polygon_prism(MODEL_AOI,0,base)
    surface_solid=raster_volume(h,mask,substrate_vertices,step) if mask.any() else md.Manifold()
    m=surface_solid if color else substrate_solid+surface_solid
    print('Material',color,'initial substrate vertices',int(substrate_use.sum()),flush=True)
    print('Material',color,'simplified',m.num_tri(),'triangles',m.status(),flush=True)
    return m

def strip_solid(line,width,bottom,top,steps=24,arch_rise=0):
    """Closed swept strip. Heights are arrays on a polyline, never ungrounded floating roofs."""
    ds=np.linspace(0,line.length,steps+1)
    floor=np.broadcast_to(bottom,(len(ds),));ceiling=np.broadcast_to(top,(len(ds),))
    verts=[]
    # Counter-clockwise cross section when viewed along negative tangent.
    section=[(-width/2,0),(width/2,0),(width/2,1),(0,2),(-width/2,1)] if arch_rise else [(-width/2,0),(width/2,0),(width/2,1),(-width/2,1)]
    for i,d in enumerate(ds):
        p=line.interpolate(d);a=line.interpolate(max(0,d-.01));b=line.interpolate(min(line.length,d+.01))
        dx,dy=b.x-a.x,b.y-a.y;norm=np.hypot(dx,dy);normal=np.array([-dy,dx])/max(norm,1e-12)
        for offset,level in section:
            z=floor[i] if level==0 else ceiling[i]-(arch_rise if level==1 else 0)
            verts.append([p.x+normal[0]*offset,p.y+normal[1]*offset,z])
    count=len(section);faces=[]
    for i in range(steps):
        for j in range(count):
            a=i*count+j;b=i*count+(j+1)%count;c=(i+1)*count+j;d=(i+1)*count+(j+1)%count
            faces.extend([[a,c,b],[b,c,d]])
    for j in range(1,count-1):faces.extend([[0,j,j+1],[steps*count,steps*count+j+1,steps*count+j]])
    mesh=trimesh.Trimesh(verts,faces,process=False);mesh.fix_normals();return solid(mesh)

def polygon_prism(geom,z0,z1):
    parts=[]
    for poly in shapely.get_parts(shapely.make_valid(geom)):
        if poly.geom_type!='Polygon' or poly.area<1e-8:continue
        rings=[np.asarray(poly.exterior.coords)[:-1,:2]]+[np.asarray(r.coords)[:-1,:2] for r in poly.interiors]
        verts=np.ascontiguousarray(np.vstack(rings),dtype=np.float64)
        ends=np.cumsum([len(r) for r in rings]).astype(np.uint32)
        faces=mapbox_earcut.triangulate_float64(verts,ends).reshape(-1,3)
        m=trimesh.creation.extrude_triangulation(verts,faces,z1-z0);m.apply_translation([0,0,z0]);parts.append(solid(m))
    return md.Manifold.batch_boolean(parts,md.OpType.Add) if parts else md.Manifold()

def ground_at(g,x,y,radius=.25):
    r=int((H-y)/STEP);c=int(x/STEP);d=max(1,int(radius/STEP))
    patch=g[max(0,r-d):min(NY,r+d+1),max(0,c-d):min(NX,c+d+1)]
    return float(np.percentile(patch,15)) if patch.size else float(g.min())

def portal_extension(line,fields,at_start,width):
    """Reach the exposed road beyond the rasterized tunnel's rounded end cap."""
    xy=np.asarray(line.coords)[:,:2]
    p,q=(xy[0],xy[1]) if at_start else (xy[-1],xy[-2])
    outward=(p-q)/np.linalg.norm(p-q)
    chosen=max(1.,width/2+.35)
    for distance in np.arange(chosen,3.01,.125):
        x,y=p+outward*distance;r=int((H-y)/STEP);c=int(x/STEP)
        if 0<=r<NY and 0<=c<NX and fields['material'][r,c]==3 and fields['height_mm'][r,c]-fields['ground_mm'][r,c]<.35:
            chosen=float(distance);break
    return p+outward*chosen,chosen

def strengthen_top_peaks(materials,fields,folder):
    """Keep the measured height of the tallest narrow peaks in a 0.4 mm slice.

    The initial Classic slice dropped the three small peaks above 23 mm and
    rejected the resulting empty top layers. Widen isolated peaks in the top
    band, without increasing their heights or globally swelling all buildings.
    """
    diameter=CFG.get('minimum_top_peak_diameter_mm',1.)
    separation=CFG.get('top_peak_minimum_separation_mm',diameter)
    band=CFG.get('top_peak_reinforcement_band_mm',2.)
    heights=fields['height_mm'];building_mask=fields['building_mask'];candidates={};records=[]
    # Sweep the top band: a narrow tank may sit on a broader high roof, so one
    # threshold at the band's bottom can incorrectly merge and skip the tank.
    reinforcement_step=CFG.get('top_peak_reinforcement_step_mm',CFG['layer_height_mm'])
    for level in np.arange(float(heights.max())-band,float(heights.max()),reinforcement_step):
        labels,count=label(building_mask&(heights>level))
        for i,sl in enumerate(find_objects(labels),1):
            r,c=sl;selection=labels[sl]==i
            if max((r.stop-r.start)*STEP,(c.stop-c.start)*STEP)>diameter:continue
            peak=float(heights[sl][selection].max())
            peak_cells=selection&(heights[sl]>=peak-.0001)
            rr,cc=np.where(peak_cells)
            x=float((cc+c.start+.5).mean()*STEP);y=float(H-(rr+r.start+.5).mean()*STEP)
            colors=fields['material'][sl][peak_cells]
            colors=colors[colors<4]
            material_index=int(np.bincount(colors,minlength=4).argmax()) if len(colors) else 0
            candidates[(round(x,4),round(y,4),round(peak,4))]=(x,y,peak,material_index)
    # A sloped or stepped feature can appear as a new tiny component at several
    # sweep levels. Keep its highest representative instead of building a
    # cluster of overlapping reinforcement posts around the same local peak.
    selected=[]
    for x,y,peak,material_index in sorted(candidates.values(),key=lambda item:item[2],reverse=True):
        if any(math.hypot(x-sx,y-sy)<separation for sx,sy,_,_ in selected):continue
        selected.append((x,y,peak,material_index))
    for i,(x,y,peak,material_index) in enumerate(selected,1):
        support=ground_at(fields['substrate_top_mm'],x,y,radius=.08)
        reinforcement=polygon_prism(Point(x,y).buffer(diameter/2,quad_segs=12),support,peak)
        for color in range(4):
            materials[color]=materials[color]+reinforcement if color==material_index else materials[color]-reinforcement
        export(reinforcement,folder/f'peak_reinforcement_{i}.ply')
        records.append({'center_mm':[x,y],'top_mm':peak,'diameter_mm':diameter,
            'minimum_separation_mm':separation,'material':material_index,'substrate_top_mm':support,
            'reason':'preserve measured peak height after the initial slicer omitted narrow top islands'})
    print('Strengthened top peaks',len(records),flush=True)
    return materials,records

def underpasses(materials,fields,folder):
    # Match the raster-solid base exactly. Mixing float32(1.8) and float64(1.8)
    # leaves a nanometre gap beneath narrow approach strips after exact CSG.
    report=[];ground=fields['ground_mm'];base=float(np.float32(CFG['base_mm']))
    structural_minimum=structural_roof_thickness_mm(CFG['nozzle_mm'],CFG['layer_height_mm'])
    tunnel_roof=max(structural_minimum,float(CFG['minimum_tunnel_cover_mm']))
    bridge_roof=max(structural_minimum,float(CFG['minimum_bridge_deck_thickness_mm']))
    color_depth=float(CFG.get('surface_color_depth_mm',CFG.get('minimum_surface_color_depth_mm',.24)))
    minimum_floor=minimum_crossing_floor_mm(base,CFG['layer_height_mm'])
    minimum_colored_floor=minimum_crossing_floor_mm(base,CFG['layer_height_mm'],color_depth)
    field_report=json.loads((OUT/'field_build_report.json').read_text())
    origin=field_report['vertical_origin_m_navd88']
    terrain_factor=float(field_report.get('terrain_relief',{}).get('factor',1.))
    vertical=CFG.get('vertical_exaggeration',1.)
    to_z=lambda elevation:(elevation-origin)*1000/CFG['scale_denominator']*vertical*terrain_factor+CFG['minimum_terrain_mm']
    cut_tunnels=[]
    if (OUT/'tunnels.parquet').exists() and CFG['infer_hidden_road_profiles']:
        for _,row in gpd.read_parquet(OUT/'tunnels.parquet').iterrows():
            line=local(row.geometry)
            minimum_length=minimum_crossing_length_mm(CFG)
            if line.geom_type!='LineString':
                report.append({'osm_id':int(row.osm_id),'status':'not cut: non-linear tunnel geometry',
                    'geometry_type':line.geom_type});continue
            if line.length<minimum_length:
                report.append({'osm_id':int(row.osm_id),'status':'not cut: below printable length',
                    'length_mm':float(line.length),'minimum_length_mm':minimum_length});continue
            steps=max(12,int(line.length/.25));p0=line.interpolate(0);p1=line.interpolate(line.length)
            if np.isfinite(row.get('road_start_elevation_m',np.nan)):
                floor0=to_z(float(row.road_start_elevation_m))+LINE_RELIEF_MM
            else:floor0=ground_at(ground,p0.x,p0.y)+LINE_RELIEF_MM
            if np.isfinite(row.get('road_end_elevation_m',np.nan)):
                floor1=to_z(float(row.road_end_elevation_m))+LINE_RELIEF_MM
            else:floor1=ground_at(ground,p1.x,p1.y)+LINE_RELIEF_MM
            baseline_road=np.linspace(floor0,floor1,steps+1);ds=np.linspace(0,line.length,steps+1)
            surface=np.array([ground_at(ground,line.interpolate(d).x,line.interpolate(d).y,radius=.08) for d in ds])
            profile=printable_tunnel_profile(surface,baseline_road,
                minimum_cover_mm=tunnel_roof,
                minimum_clearance_mm=CFG['minimum_tunnel_clearance_mm'],
                minimum_evidence_mm=CFG.get('minimum_tunnel_evidence_mm',.08),
                maximum_clearance_mm=CFG.get('maximum_tunnel_clearance_mm',1.40),
                portal_fraction=CFG.get('tunnel_portal_transition_fraction',.15))
            if not profile.accepted:
                report.append({'osm_id':int(row.osm_id),'status':'not cut: '+profile.reason,
                    'maximum_geographic_separation_mm':profile.maximum_geographic_separation_mm});continue
            if float(profile.road.min())<minimum_floor-1e-9:
                report.append({'osm_id':int(row.osm_id),'status':'not cut: lower road would enter the model base',
                    'minimum_inferred_road_mm':float(profile.road.min()),
                    'minimum_printable_floor_mm':minimum_floor});continue
            road=profile.road
            # Cutting only the covered interior leaves sealed chambers behind
            # the portal caps. Extend the floor into the exposed approach road,
            # and cut daylight openings at both ends of the covered segment.
            e0,reach0=portal_extension(line,fields,True,row.width_mm)
            e1,reach1=portal_extension(line,fields,False,row.width_mm)
            extended=LineString([e0,*list(line.coords),e1])
            ext_steps=max(12,int(extended.length/.25));ext_ds=np.linspace(0,extended.length,ext_steps+1)
            ef0=ground_at(ground,*e0)+LINE_RELIEF_MM;ef1=ground_at(ground,*e1)+LINE_RELIEF_MM
            ext_road=np.interp(ext_ds,np.r_[0.,reach0+ds,extended.length],np.r_[ef0,road,ef1])
            floor_shape=strip_solid(extended,row.width_mm,np.full(len(ext_ds),base),ext_road,ext_steps)
            low=road+.005
            ceiling=profile.ceiling
            requested_roof_span=float(row.width_mm)+.10
            roof_span=printable_roof_span(requested_roof_span,CFG['nozzle_mm'])
            void=strip_solid(line,roof_span,low,ceiling,steps,arch_rise=.12)
            open_portals=[]
            for d0,d1 in [(0,reach0+.20),(reach0+line.length-.20,extended.length)]:
                approach=substring(extended,d0,d1);n=max(3,int(approach.length/.125))
                positions=np.linspace(d0,d1,n+1)
                bottom=np.interp(positions,ext_ds,ext_road)+.004
                open_portals.append(strip_solid(approach,roof_span,bottom,np.full(n+1,float(fields['height_mm'].max())+1),n))
            void=md.Manifold.batch_boolean([void,*open_portals],md.OpType.Add)
            for i in range(4):
                materials[i]=materials[i]-void
                if i!=0:materials[i]=materials[i]-floor_shape
            materials[0]=materials[0]+floor_shape
            export(void,folder/f'tunnel_{int(row.osm_id)}_void.ply')
            cut_tunnels.append({'osm_id':int(row.osm_id),'corridor':line.buffer(roof_span/2),
                'void':void,'road':road,'line':line})
            report.append({'osm_id':int(row.osm_id),'status':'cut','name':row['name'],'length_mm':line.length,
                'source_road_width_mm':float(row.width_mm),'requested_roof_span_mm':requested_roof_span,
                'roof_span_mm':roof_span,'roof_span_reduction_mm':requested_roof_span-roof_span,
                'roof_thickness_mm':tunnel_roof,
                'road_end_heights_mm':[floor0,floor1],'maximum_clearance_mm':float((ceiling-low).max()),
                'maximum_geographic_separation_mm':profile.maximum_geographic_separation_mm,
                'maximum_hidden_floor_adjustment_mm':profile.maximum_hidden_floor_adjustment_mm,
                'maximum_portal_roof_overcut_mm':profile.maximum_portal_roof_overcut_mm,
                'source_road_profile_method':str(row.get('road_profile_method','LiDAR ground at mapped portal')),
                'portal_structure_count':int(row.get('portal_structure_count',0)),
                'portal_approach_extensions_mm':[reach0,reach1],
                'road_material':'ivory','road_surface_width_mm':float(row.width_mm),
                'method':'source-anchored linear hidden road profile; permanent structural roof; full-width ivory floor; both portals remain connected'})
            print('Tunnel',row.osm_id,'cut',float((ceiling-low).max()),flush=True)
    if (OUT/'bridges.parquet').exists():
        tile=MODEL_AOI
        bridges=gpd.read_parquet(OUT/'bridges.parquet')
        # Old cached fields may predate this mask. Reconstruct it from the same
        # recorded deck footprints without changing or reinterpreting sources.
        from rasterio.features import rasterize
        deck_shapes=[local(shapely.from_wkt(value)) for value in bridges.deck_geometry
            if isinstance(value,str)]
        deck_mask=rasterize([(shape,1) for shape in deck_shapes],out_shape=ground.shape,
            transform=Affine(STEP,0,0,0,-STEP,H),dtype='uint8').astype(bool) if deck_shapes else np.zeros_like(ground,dtype=bool)
        deck_mask&=np.isin(fields['material'],[0,3])

        def visible_deck_at(point,width):
            # Include the full swept width and a raster-cell margin: a cutter
            # that is safe at its center can still remove a sloping deck edge.
            r=int((H-point.y)/STEP);c=int(point.x/STEP)
            radius=max(1,int(np.ceil((width/2+STEP)/STEP)))
            rows=slice(max(0,r-radius),min(NY,r+radius+1))
            cols=slice(max(0,c-radius),min(NX,c+radius+1))
            selected=deck_mask[rows,cols]
            values=fields['height_mm'][rows,cols][selected]
            return float(values.min()) if values.size else np.nan
        water=read('planimetrics_HYDROGRAPHY')
        water_union=local(shapely.union_all(water.geometry)).intersection(tile)
        osm=gpd.read_parquet(OUT/'osm_detail.parquet') if (OUT/'osm_detail.parquet').exists() else gpd.GeoDataFrame()
        routes_path=PROCESSED/'road_symbol_routes.parquet'
        routes=gpd.read_parquet(routes_path) if routes_path.exists() else gpd.GeoDataFrame()
        route_by_id={int(r.osm_id):r for _,r in routes.iterrows()}
        tunnel_union=shapely.union_all([item['corridor'] for item in cut_tunnels]) if cut_tunnels else Polygon()

        def layer_number(value,default):
            try:return float(str(value).split(';')[0])
            except (TypeError,ValueError):return float(default)

        def direction_at(line,point):
            d=float(shapely.line_locate_point(line,point));delta=min(.10,max(.01,line.length*.05))
            a=line.interpolate(max(0.,d-delta));b=line.interpolate(min(line.length,d+delta))
            vector=np.asarray(b.coords[0])[:2]-np.asarray(a.coords[0])[:2]
            return vector/max(float(np.linalg.norm(vector)),1e-9)

        def lower_route(row,source_shape,upper_line):
            if len(osm)==0:return None
            upper_layer=layer_number(row.get('layer',1),1)
            candidates=osm[osm.highway.notna()&osm.geom_type.eq('LineString')&
                ~osm.osm_id.eq(int(row.osm_id))&osm.intersects(source_shape)]
            choices=[]
            for _,candidate in candidates.iterrows():
                try:tags=json.loads(candidate.tags) if isinstance(candidate.tags,str) else {}
                except json.JSONDecodeError:tags={}
                lower_layer=layer_number(tags.get('layer'),-1 if tags.get('tunnel') not in [None,'no'] else 0)
                if lower_layer>=upper_layer:continue
                line=local(candidate.geometry).intersection(tile)
                parts=[part for part in shapely.get_parts(line) if part.geom_type=='LineString' and part.length>.2]
                if not parts:continue
                line=max(parts,key=lambda part:part.length)
                crossing=line.intersection(local(source_shape))
                if crossing.is_empty:continue
                point=crossing.centroid
                upper_direction=direction_at(upper_line,point);lower_direction=direction_at(line,point)
                cross=float(abs(upper_direction[0]*lower_direction[1]-upper_direction[1]*lower_direction[0]))
                if cross<.25:continue
                semantic=route_by_id.get(int(candidate.osm_id))
                is_trail=(candidate.highway in TRAIL_HIGHWAYS or
                    (semantic is not None and str(semantic.classification)=='trail'))
                if is_trail:
                    width=trail_width_mm(candidate.highway,tags,CFG)
                else:
                    width=float(semantic.surface_width_mm) if semantic is not None and np.isfinite(semantic.surface_width_mm) else max(.5,float(row.width_mm))
                choices.append((lower_layer,-cross,int(candidate.osm_id),candidate,line,width,semantic))
            return min(choices,key=lambda item:item[:3]) if choices else None

        for _,row in bridges.iterrows():
            if not isinstance(row.get('deck_geometry'),str):continue
            source_shape=shapely.from_wkt(row.deck_geometry)
            shape=local(source_shape).intersection(tile)
            line=local(row.geometry).intersection(tile)
            if line.geom_type!='LineString' or line.length<minimum_crossing_length_mm(CFG):
                report.append({'osm_id':int(row.osm_id),'status':'bridge deck retained: below printable opening length',
                    'length_mm':float(getattr(line,'length',0.))});continue
            water_part=shape.intersection(water_union)
            deck=to_z(row.deck_elevation_m)+LINE_RELIEF_MM;lower=to_z(row.beneath_elevation_m)
            if water_part.area>=shape.area*.25:
                # Water supplies an observed, level lower surface.  Preserve it
                # below the separately measured bridge deck.
                central=substring(line,.3,line.length-.3)
                points=shapely.points(shapely.get_coordinates(shape));requested_roof_span=float(np.max(shapely.distance(points,line))*2)+.3
                roof_span=printable_roof_span(requested_roof_span,CFG['nozzle_mm'])
                aperture=central.buffer(roof_span/2,quad_segs=4)
                minimum_under=deck-bridge_roof-CFG.get('minimum_bridge_clearance_mm',CFG['minimum_tunnel_clearance_mm'])
                printable_lower=min(lower,minimum_under)
                if printable_lower<minimum_colored_floor-1e-9:
                    report.append({'osm_id':int(row.osm_id),
                        'status':'bridge deck retained: water opening would enter the model base',
                        'printed_lower_surface_mm':printable_lower,
                        'minimum_printable_floor_mm':minimum_colored_floor,'deck_top_mm':deck,
                        'method':row.height_method});continue
                void=polygon_prism(aperture,printable_lower+.005,deck-bridge_roof)
                skin_bottom=max(base,printable_lower-color_depth)
                substrate=polygon_prism(water_part,base,skin_bottom)
                water_skin=polygon_prism(water_part,skin_bottom,printable_lower)
                for i in range(4):
                    materials[i]=materials[i]-void
                    if i!=0:materials[i]=materials[i]-substrate
                    if i!=2:materials[i]=materials[i]-water_skin
                materials[0]=materials[0]+substrate
                materials[2]=materials[2]+water_skin
                export(void,folder/f'bridge_{int(row.osm_id)}_void.ply')
                report.append({'osm_id':int(row.osm_id),'status':'water bridge opening','deck_top_mm':deck,
                    'requested_roof_span_mm':requested_roof_span,'roof_span_mm':roof_span,
                    'roof_span_reduction_mm':requested_roof_span-roof_span,
                    'roof_thickness_mm':bridge_roof,
                    'geographic_lower_surface_mm':lower,'printed_lower_surface_mm':printable_lower,
                    'hidden_lowering_mm':max(0.,lower-printable_lower),
                    'clearance_mm':deck-bridge_roof-printable_lower,'deck_thickness_mm':bridge_roof,
                    'support':'grounded ivory shoulders and substrate; bounded central aperture',
                    'method':row.height_method})
                print('Water bridge',row.osm_id,'cut',flush=True);continue

            overlapping=[item for item in cut_tunnels if item['corridor'].intersects(shape)]
            if overlapping:
                report.append({'osm_id':int(row.osm_id),'status':'land overpass opened by lower tunnel',
                    'lower_tunnel_osm_ids':[item['osm_id'] for item in overlapping],
                    'deck_top_mm':deck,'method':row.height_method})
                continue

            choice=lower_route(row,source_shape,line)
            if choice is None:
                report.append({'osm_id':int(row.osm_id),'status':'bridge deck retained: no mapped lower surface',
                    'deck_top_mm':deck,'method':row.height_method});continue
            _,_,lower_id,candidate,lower_line,width,semantic=choice
            # Leave a full-width approach beyond the protected deck so the
            # hidden floor can return to the observed exposed road height.
            segment=lower_line.intersection(shape.buffer(max(.35,width/2+.35)))
            parts=[part for part in shapely.get_parts(segment) if part.geom_type=='LineString']
            if not parts:
                report.append({'osm_id':int(row.osm_id),'status':'bridge deck retained: lower route has no printable intersection',
                    'lower_osm_id':lower_id});continue
            segment=max(parts,key=lambda part:part.length)
            steps=max(8,int(segment.length/.125));ds=np.linspace(0,segment.length,steps+1)
            baseline=np.linspace(ground_at(ground,*segment.coords[0])+LINE_RELIEF_MM,
                ground_at(ground,*segment.coords[-1])+LINE_RELIEF_MM,steps+1)
            deck_start=to_z(float(row.get('deck_start_elevation_m',row.deck_elevation_m)))+LINE_RELIEF_MM
            deck_end=to_z(float(row.get('deck_end_elevation_m',row.deck_elevation_m)))+LINE_RELIEF_MM
            surface=[]
            protected=[]
            visible_surface=[]
            for distance in ds:
                point=segment.interpolate(distance)
                along=float(shapely.line_locate_point(line,point,normalized=True))
                surface.append(np.interp(along,[0.,1.],[deck_start,deck_end]))
                protected.append(shape.covers(point))
                visible_surface.append(visible_deck_at(point,width+.10))
            surface,visible_protected=constrain_deck_to_visible_surface(surface,visible_surface)
            protected=np.asarray(protected)|visible_protected
            profile=printable_tunnel_profile(surface,baseline,
                minimum_cover_mm=bridge_roof,
                minimum_clearance_mm=CFG.get('minimum_bridge_clearance_mm',CFG['minimum_tunnel_clearance_mm']),
                minimum_evidence_mm=CFG.get('minimum_bridge_evidence_mm',.08),
                maximum_clearance_mm=CFG.get('maximum_bridge_clearance_mm',1.40),
                portal_fraction=.18,protected_surface_mask=protected)
            if not profile.accepted:
                report.append({'osm_id':int(row.osm_id),'status':'bridge deck retained: '+profile.reason,
                    'lower_osm_id':lower_id,'maximum_geographic_separation_mm':profile.maximum_geographic_separation_mm});continue
            road_color=3 if (candidate.highway in TRAIL_HIGHWAYS or
                (semantic is not None and str(semantic.classification)=='trail')) else 0
            route_floor=minimum_colored_floor if road_color==3 else minimum_floor
            if float(profile.road.min())<route_floor-1e-9:
                report.append({'osm_id':int(row.osm_id),
                    'status':'bridge deck retained: lower route would enter the model base',
                    'lower_osm_id':lower_id,'minimum_inferred_road_mm':float(profile.road.min()),
                    'minimum_printable_floor_mm':route_floor,'deck_top_mm':deck,
                    'method':row.height_method});continue
            if road_color==3:
                colored_bottom=np.maximum(base,profile.road-color_depth)
                floor_support=strip_solid(segment,width,np.full(steps+1,base),colored_bottom,steps)
                floor=strip_solid(segment,width,colored_bottom,profile.road,steps)
            else:
                floor_support=md.Manifold()
                floor=strip_solid(segment,width,np.full(steps+1,base),profile.road,steps)
            requested_roof_span=width+.10
            roof_span=printable_roof_span(requested_roof_span,CFG['nozzle_mm'])
            void=strip_solid(segment,roof_span,profile.road+.005,profile.ceiling,steps,arch_rise=.08)
            for i in range(4):
                materials[i]=materials[i]-void
                if i!=road_color:materials[i]=materials[i]-floor
                if road_color==3 and i!=0:materials[i]=materials[i]-floor_support
            materials[road_color]=materials[road_color]+floor
            if road_color==3:materials[0]=materials[0]+floor_support
            export(void,folder/f'bridge_{int(row.osm_id)}_void.ply')
            report.append({'osm_id':int(row.osm_id),'status':'road/path overpass opening','lower_osm_id':lower_id,
                'lower_name':str(candidate['name']),'deck_top_mm':deck,
                'source_road_width_mm':width,'requested_roof_span_mm':requested_roof_span,
                'roof_span_mm':roof_span,'roof_span_reduction_mm':requested_roof_span-roof_span,
                'roof_thickness_mm':bridge_roof,
                'maximum_geographic_separation_mm':profile.maximum_geographic_separation_mm,
                'maximum_hidden_floor_adjustment_mm':profile.maximum_hidden_floor_adjustment_mm,
                'clearance_mm':float((profile.ceiling-profile.road).max()),
                'lower_road_material':'tan' if road_color==3 else 'ivory',
                'support':'grounded shoulders beside a bounded central aperture',
                'method':row.height_method})
            print('Land overpass',row.osm_id,'over',lower_id,'cut',flush=True)
    return materials,report

def main():
    p=argparse.ArgumentParser();p.add_argument('--stride',type=int,default=1);p.add_argument('--reuse-base',action='store_true');a=p.parse_args()
    folder=OUT/('mesh' if a.stride==1 else f'mesh_draft_{a.stride}');folder.mkdir(exist_ok=True)
    fields=np.load(OUT/'map_fields.npz');mats=[];report={'stride':a.stride,'parts':{}}
    phase=Phases()
    if 'substrate_top_mm' not in fields:
        raise RuntimeError('Map fields lack the required continuous ivory substrate surface')
    with phase('material_solids'):
        for i in range(4):
            path=folder/f'material_{i}_base.ply'
            if a.reuse_base and path.exists():m=solid(trimesh.load(path,process=False))
            else:
                m=material_solid(fields['height_mm'],fields['substrate_top_mm'],fields['material'],
                    fields['aoi_mask'],i,a.stride)
                export(m,path)
            mats.append(m)
    with phase('seat_colored_surfaces'):
        mats,seating=seat_colored_surfaces(mats)
    report['surface_seating']=seating
    with phase('crossings'):
        mats,cuts=underpasses(mats,fields,folder)
    report['crossings']=cuts
    report['layer_support']=audit_layer_support(cuts,CFG['nozzle_mm'],CFG['layer_height_mm'])
    report['material_layers']={'substrate_material':0,'substrate_color':'ivory',
        'surface_color_depth_mm':float(CFG.get('surface_color_depth_mm',CFG.get('minimum_surface_color_depth_mm',.24)))}
    with phase('top_peaks'):
        mats,peaks=strengthen_top_peaks(mats,fields,folder)
    report['top_peak_reinforcements']=peaks
    report['measured_landmarks']=[]
    with phase('clip_to_tile'):
        for i,m in enumerate(mats):
            m=clip_to_tile(m,i)
            # Keep exact CSG interfaces through the 64-bit PLY/3MF path. Snapping
            # and independently re-simplifying the colors here separates the two
            # copies of an approach wall and can create enclosed seam wedges.
            mats[i]=m
    with phase('partition'):
        mats,partition=partition_materials(mats)
    # A Boolean difference can leave a micron-thin numerical skin when its
    # result is rebuilt as a new manifold. A second, non-simplifying pass
    # removes that residual before serialization; the strict 3MF validator
    # independently measures the packaged parts again.
    stabilization=[]
    with phase('partition_stabilization'):
        for _ in range(1):
            mats,stabilized=partition_materials(mats);stabilization.append(stabilized)
    partition['post_boolean_stabilization_passes']=stabilization
    report['material_partition']=partition
    with phase('export'):
        for i,m in enumerate(mats):
            report['parts'][str(i)]=export(m,folder/f'material_{i}.ply')
            print('Final',i,report['parts'][str(i)],flush=True)
    with phase('serialized_stabilization'):
        report['parts'],report['serialized_material_partition']=stabilize_serialized_materials(folder,report['parts'])
    report['phase_seconds']=phase.records
    report['phase_seconds']['total']=round(sum(phase.records.values()),3)
    report['triangles']={str(i):int(m.num_tri()) for i,m in enumerate(mats)}
    print('Phase totals',json.dumps(report['phase_seconds']),flush=True)
    write_json(folder/'mesh_report.json',report)

if __name__=='__main__':main()
