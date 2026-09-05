"""Independently parse serialized 3MF meshes and audit geometry/material contacts."""
import argparse,json,zipfile,xml.parsers.expat,hashlib,gc
from pathlib import Path
import numpy as np,trimesh,manifold3d as md
from lxml import etree
from map_common import *
from build_map_meshes import solid
from package_3mf import validate_generation_metadata

def classify_positive_shells(volumes,nozzle_mm,layer_height_mm):
    """Separate printable components from sub-extrusion Boolean crumbs."""
    threshold=float(nozzle_mm)**2*float(layer_height_mm)
    return [v for v in volumes if v>threshold],sum(0<v<=threshold for v in volumes),threshold

def classify_cavity_shells(pieces,nozzle_mm,layer_height_mm):
    """Separate slicer-resolvable cavities from sub-extrusion seam wedges."""
    thickness_limit=min(float(nozzle_mm),float(layer_height_mm))
    printable=[];negligible=[]
    for piece in pieces:
        volume=float(piece.volume())
        if volume>=0:continue
        area=float(piece.surface_area())
        effective_thickness=2*abs(volume)/area if area>0 else float('inf')
        (printable if effective_thickness>=thickness_limit else negligible).append(
            (abs(volume),effective_thickness))
    return printable,negligible,thickness_limit

def validate_material_support_settings(project):
    """Require solid skins where one material starts on another material."""
    if str(project.get('interface_shells','0'))!='1':
        raise RuntimeError(
            'Material interface shells must be enabled so an upper color is not printed over sparse infill')
    bottom=int(project.get('bottom_shell_layers',0));top=int(project.get('top_shell_layers',0))
    if bottom<1 or top<1:
        raise RuntimeError('Material interface shells require positive top and bottom shell counts')
    return {'interface_shells':True,'bottom_shell_layers':bottom,'top_shell_layers':top}

def main():
    p=argparse.ArgumentParser();p.add_argument('--model',type=Path,required=True);p.add_argument('--skip-booleans',action='store_true')
    p.add_argument('--report-dir',type=Path,default=VALID)
    p.add_argument('--expected-generation-metadata',type=Path);a=p.parse_args();a.report_dir.mkdir(parents=True,exist_ok=True)
    mesh_report=json.loads((OUT/'mesh/mesh_report.json').read_text())
    layer_support=mesh_report.get('layer_support')
    if not layer_support or layer_support.get('result')!='passed':
        raise RuntimeError(
            'Mesh report is missing a passed all-layer support audit; '
            f'found {layer_support!r}')
    expected=mesh_report['parts']
    active=sorted(int(i) for i,part in expected.items() if not part.get('empty',False) and part.get('triangles',0)>0)
    overlap_tolerance=max(.05,float(CFG['nozzle_mm'])**2*float(CFG['layer_height_mm'])*2)
    seam_thickness_tolerance=float(mesh_report.get('material_partition',{}).get(
        'maximum_expected_overlap_thickness_mm',.02))
    report={'model':str(a.model),'parts':{},'intersections_mm3':{},
        'intersection_effective_thickness_mm':{},
        'intersection_tolerance_mm3':overlap_tolerance,
        'intersection_tolerance_basis':'max(0.05 mm3, two nozzle-width-squared layer volumes)',
        'distributed_seam_thickness_tolerance_mm':seam_thickness_tolerance,
        'distributed_seam_thickness_basis':'maximum relative motion allowed by mesh simplify/export passes',
        'layer_support':layer_support}
    bodies=[];state={};parsed=[]
    def start(name,attr):
        if name=='model' and attr.get('unit')!='millimeter':
            raise RuntimeError(
                f'3MF object model must use millimeter units; found {attr.get("unit")!r}')
        if name=='object':
            try:i=int(attr['id'])-1
            except (KeyError,TypeError,ValueError) as error:
                raise RuntimeError(f'3MF mesh object has invalid id {attr.get("id")!r}') from error
            e=expected.get(str(i))
            if e is None:
                raise RuntimeError(f'3MF contains unexpected material object id {i+1}')
            try:pindex=int(attr['pindex'])
            except (KeyError,TypeError,ValueError) as error:
                raise RuntimeError(
                    f'Material object {i} has invalid pindex {attr.get("pindex")!r}') from error
            if pindex!=i or attr.get('pid')!='1008':
                raise RuntimeError(
                    f'Material object {i} has invalid color assignment: '
                    f'pindex={pindex}, pid={attr.get("pid")!r}; expected pindex={i}, pid="1008"')
            state.update(id=i,v=np.empty((e['vertices'],3),np.float64),
                f=np.empty((e['triangles'],3),np.int64),nv=0,nf=0)
        elif name=='vertex':
            if 'nv' not in state:raise RuntimeError('3MF vertex appears outside a material object')
            n=state['nv']
            if n>=len(state['v']):
                raise RuntimeError(
                    f'Material {state["id"]} contains more vertices than mesh_report declared ({len(state["v"])})')
            try:state['v'][n]=[float(attr[k]) for k in ['x','y','z']]
            except (KeyError,TypeError,ValueError) as error:
                raise RuntimeError(f'Material {state["id"]} contains an invalid vertex at index {n}') from error
            state['nv']=n+1
        elif name=='triangle':
            if 'nf' not in state:raise RuntimeError('3MF triangle appears outside a material object')
            n=state['nf']
            if n>=len(state['f']):
                raise RuntimeError(
                    f'Material {state["id"]} contains more triangles than mesh_report declared ({len(state["f"])})')
            try:state['f'][n]=[int(attr[k]) for k in ['v1','v2','v3']]
            except (KeyError,TypeError,ValueError) as error:
                raise RuntimeError(f'Material {state["id"]} contains an invalid triangle at index {n}') from error
            state['nf']=n+1
    def end(name):
        if name!='object':return
        if 'id' not in state:raise RuntimeError('3MF object ended without a parsed material object')
        i=state['id'];v,f=state['v'],state['f']
        if state['nv']!=len(v) or state['nf']!=len(f):
            raise RuntimeError(
                f'Material {i} serialized counts differ from mesh_report: '
                f'vertices={state["nv"]}/{len(v)}, triangles={state["nf"]}/{len(f)}')
        if not np.isfinite(v).all():
            raise RuntimeError(f'Material {i} contains non-finite serialized vertex coordinates')
        if not len(f) or f.min()<0 or f.max()>=len(v):
            bounds=(int(f.min()),int(f.max())) if len(f) else None
            raise RuntimeError(
                f'Material {i} contains invalid triangle vertex indices {bounds}; vertex count={len(v)}')
        m=trimesh.Trimesh(v,f,process=False)
        zero=int((m.area_faces<1e-12).sum())
        if not m.is_watertight or not m.is_winding_consistent or m.volume<=0 or zero:
            raise RuntimeError(
                f'Material {i} serialized mesh failed geometry validation: '
                f'watertight={m.is_watertight}, winding_consistent={m.is_winding_consistent}, '
                f'volume_mm3={m.volume:g}, zero_area_triangles={zero}')
        if not (m.bounds[0,0]>=-.002 and m.bounds[0,1]>=-.002 and m.bounds[0,2]>=-.002 and
                m.bounds[1,0]<=W+.002 and m.bounds[1,1]<=H+.002):
            raise RuntimeError(
                f'Part {i} lies outside the local {W:g}x{H:g} mm tile or below Z=0: '
                f'bounds={m.bounds.tolist()}'
            )
        report['parts'][str(i)]={'vertices':len(v),'triangles':len(f),'watertight':True,'consistent_winding':True,
            'zero_area_triangles':zero,'volume_mm3':float(m.volume),'bounds_mm':m.bounds.tolist()}
        m.export(a.report_dir/f'roundtrip_material_{i}.ply')
        if not a.skip_booleans:bodies.append(solid(m))
        parsed.append(i);state.clear();del m;gc.collect();print('Serialized part',i,'passed',flush=True)
    with zipfile.ZipFile(a.model) as z:
        corrupt_member=z.testzip()
        if corrupt_member is not None:
            raise RuntimeError(f'3MF ZIP integrity check failed for member {corrupt_member!r}')
        generation_member='Metadata/generation_command.json'
        if generation_member in z.namelist():
            generation=validate_generation_metadata(
                json.loads(z.read(generation_member)),f'{a.model}:{generation_member}')
            if a.expected_generation_metadata:
                expected_generation=validate_generation_metadata(
                    json.loads(a.expected_generation_metadata.read_text()),
                    str(a.expected_generation_metadata))
                if generation!=expected_generation:
                    raise RuntimeError(
                        'Embedded generation command metadata does not match '
                        f'{a.expected_generation_metadata}')
            report['generation_command_metadata']={
                'archive_path':generation_member,
                'shell_command':generation['shell_command'],
                'matches_expected':bool(a.expected_generation_metadata),
            }
        else:
            if a.expected_generation_metadata:
                raise RuntimeError(
                    f'3MF is missing required hidden metadata member {generation_member}')
            report['generation_command_metadata']={'archive_path':None,'legacy_archive':True}
        wrapper=etree.fromstring(z.read('3D/3dmodel.model'));components=wrapper.findall('.//{*}component')
        try:component_ids=sorted(int(c.get('objectid')) for c in components)
        except (TypeError,ValueError) as error:
            raise RuntimeError('3MF wrapper contains a component with an invalid objectid') from error
        expected_component_ids=[i+1 for i in active]
        if component_ids!=expected_component_ids:
            raise RuntimeError(
                f'3MF wrapper component IDs {component_ids} do not match active materials '
                f'{expected_component_ids}')
        production_path='{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path'
        object_paths={c.get(production_path) for c in components}
        if None in object_paths or len(object_paths)!=1:
            raise RuntimeError(
                f'3MF wrapper components must reference one shared production object path; found {object_paths}')
        object_member=next(iter(object_paths)).lstrip('/')
        if object_member not in z.namelist():
            raise RuntimeError(f'3MF wrapper references missing object member {object_member!r}')
        project=json.loads(z.read('Metadata/project_settings.config'))
        if project.get('filament_colour')!=CFG['colors']:
            raise RuntimeError(
                f'3MF filament colors {project.get("filament_colour")!r} do not match configured '
                f'colors {CFG["colors"]!r}')
        if project.get('filament_map')!=['1']*4:
            raise RuntimeError(
                f'3MF filament_map must be four single-nozzle assignments; found {project.get("filament_map")!r}')
        report['material_support_settings']=validate_material_support_settings(project)
        variants=project.get('filament_extruder_variant');ids=project.get('filament_self_index')
        if not isinstance(variants,list) or not isinstance(ids,list) or len(variants)!=len(ids):
            raise RuntimeError(
                '3MF filament_extruder_variant and filament_self_index must be equal-length lists; '
                f'got variants={type(variants).__name__}/{len(variants) if isinstance(variants,list) else "n/a"}, '
                f'indices={type(ids).__name__}/{len(ids) if isinstance(ids,list) else "n/a"}')
        missing_standard=[
            i for i in range(1,5)
            if not any(v=='Direct Drive Standard' and k==str(i) for v,k in zip(variants,ids))
        ]
        if missing_standard:
            raise RuntimeError(
                f'3MF lacks a Direct Drive Standard extruder variant for filament(s) {missing_standard}')
        metadata=etree.fromstring(z.read('Metadata/model_settings.config'))
        try:
            metadata_extruders=[
                int(p.find("metadata[@key='extruder']").get('value'))
                for p in metadata.findall('object/part')
            ]
        except (AttributeError,TypeError,ValueError) as error:
            raise RuntimeError('3MF model settings contain an invalid part extruder assignment') from error
        if metadata_extruders!=expected_component_ids:
            raise RuntimeError(
                f'3MF model-settings extruders {metadata_extruders} do not match active materials '
                f'{expected_component_ids}')
        filament_maps=metadata.find("plate/metadata[@key='filament_maps']")
        filament_maps_value=filament_maps.get('value') if filament_maps is not None else None
        if filament_maps_value!='1 1 1 1':
            raise RuntimeError(
                f'3MF model settings filament_maps must be "1 1 1 1"; found {filament_maps_value!r}')
        parser=xml.parsers.expat.ParserCreate();parser.StartElementHandler=start;parser.EndElementHandler=end
        with z.open(object_member) as stream:parser.ParseFile(stream)
    if sorted(parsed)!=active:
        raise RuntimeError(
            f'Parsed material objects {sorted(parsed)} do not match expected active materials {active}')
    if not a.skip_booleans:
        for left in range(len(active)):
            for right in range(left+1,len(active)):
                i,j=active[left],active[right]
                overlap=md.Manifold.batch_boolean([bodies[left],bodies[right]],md.OpType.Intersect)
                volume=abs(float(overlap.volume()))
                area=float(overlap.surface_area())
                effective_thickness=2*volume/area if area>0 else float('inf')
                key=f'{i}-{j}';report['intersections_mm3'][key]=volume
                report['intersection_effective_thickness_mm'][key]=effective_thickness
                print('Intersection',i,j,volume,'effective thickness',effective_thickness,flush=True)
                # A long, sub-micron seam can exceed the accumulated volume
                # threshold without containing a printable overlap. The mesh
                # builder uses the same thickness bound; keep this independent
                # audit consistent with that rule while still rejecting real
                # (thicker) material collisions.
                if not (volume<overlap_tolerance or
                        effective_thickness<=seam_thickness_tolerance):
                    raise RuntimeError(
                        f'Materials {i} and {j} overlap by {volume:g} mm3 at approximately '
                        f'{effective_thickness:g} mm effective thickness; limits are '
                        f'{overlap_tolerance:g} mm3 or {seam_thickness_tolerance:g} mm thickness')
        assembly=md.Manifold.batch_boolean(bodies,md.OpType.Add)
        if assembly.status()!=md.Error.NoError:
            raise RuntimeError(f'Assembly union failed with manifold status {assembly.status()}')
        pieces=assembly.decompose();volumes=sorted([float(m.volume()) for m in pieces],reverse=True)
        report['assembly_boundary_shells']=len(pieces);report['assembly_shell_signed_volumes_mm3']=volumes
        report['assembly_volume_mm3']=float(assembly.volume())
        # A negative shell encloses a cavity, not a detached solid. The union
        # may contain microscopic enclosed seams after float32 CSG. Report
        # them separately rather than mislabelling them as loose print pieces.
        # Exact surface contacts between independently serialized materials can
        # produce small signed shells during this diagnostic Boolean. A shell
        # smaller than one nozzle-width-square layer cannot form one extrusion
        # voxel, so do not mislabel it as a disconnected printable component.
        positive,negligible_positive,negligible_shell_volume_mm3=classify_positive_shells(
            volumes,CFG['nozzle_mm'],CFG['layer_height_mm'])
        cavities=[v for v in volumes if v < -1e-6]
        printable_cavities,negligible_cavities,cavity_thickness_limit=classify_cavity_shells(
            pieces,CFG['nozzle_mm'],CFG['layer_height_mm'])
        report['assembly_positive_volume_components']=len(positive)
        report['enclosed_cavity_shells']=len(cavities)
        report['enclosed_cavity_volume_mm3']=-sum(cavities)
        report['largest_enclosed_cavity_mm3']=-min(cavities) if cavities else 0
        report['printable_enclosed_cavities']=len(printable_cavities)
        report['maximum_enclosed_cavity_effective_thickness_mm']=max(
            (item[1] for item in negligible_cavities),default=0.)
        report['enclosed_cavity_effective_thickness_limit_mm']=cavity_thickness_limit
        report['boolean_negligible_shell_volume_mm3']=negligible_shell_volume_mm3
        report['boolean_negligible_positive_shells']=negligible_positive
        report['boolean_negligible_shells']=sum(abs(v)<=negligible_shell_volume_mm3 for v in volumes)
        print('Assembly shells:',len(positive),'solid,',len(cavities),'cavities; total cavity mm3',-sum(cavities),flush=True)
        if len(positive)!=1:
            raise RuntimeError(
                f'Assembly contains {len(positive)} disconnected printable components; '
                f'largest component volumes={positive[:20]} mm3')
        # Full tunnel apertures must connect to the outside. Thin wedges along
        # independently serialized material seams cannot occupy one extrusion
        # thickness and are not slicer-resolvable chambers.
        if printable_cavities:
            raise RuntimeError(
                f'Assembly contains {len(printable_cavities)} sealed printable chambers; '
                f'largest cavity measurements={printable_cavities[:20]}')
    with a.model.open('rb') as f:report['sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    report['bytes']=a.model.stat().st_size;report['result']='passed'
    write_json(a.report_dir/'3mf_validation.json',report)
    print('3MF validation passed',flush=True)

if __name__=='__main__':main()
