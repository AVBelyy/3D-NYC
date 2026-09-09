"""Write a four-part Bambu-compatible 3MF with explicit filament assignments.

The supplied project's palette/settings are a starting point. Executable hooks are
cleared, and machine G-code templates are taken from the installed official P2S preset.
This script exports a project only; it never connects to or starts a printer.
"""
import argparse,io,json,re,shlex,uuid,zipfile
from pathlib import Path
from xml.sax.saxutils import escape
import numpy as np,trimesh
from lxml import etree
from PIL import Image
from _material_layers import FIRST_LAYER_HEIGHT_MM
from map_common import *

CORE='http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
PROD='http://schemas.microsoft.com/3dmanufacturing/production/2015/06'
MAT='http://schemas.microsoft.com/3dmanufacturing/material/2015/02'
ID='1 0 0 0 1 0 0 0 1 0 0 0'
PLATE_THUMBNAIL_PX=2400                 # longest edge of the preview embedded for the slicer
SURFACE_ROLES=['Ivory - road ribbons, default buildings and structural bridge roofs',
    'Green - terrain, recreation, gardens, grass, measured canopy relief and selected buildings',
    'Blue - level water surfaces and selected buildings',
    'Tan - sidewalks, paths, plazas, surface parking and selected buildings']


# Bambu Studio does not reject an unknown enum value: it silently substitutes
# the option's default and reports the project as written by a newer slicer, so
# a misspelled value reads as a working setting that quietly does nothing.
# These are the values the installed 2.8 build accepts, labelled "No ironing",
# "Top surfaces", "Topmost surface" and "All solid layer"; note they are not
# PrusaSlicer's, which spells the first "none" and has no "no ironing".
IRONING_TYPES=('no ironing','top','topmost','solid')


def part_names():
    """Name every part for the surfaces it draws, and say which carries the substrate.

    The substrate is the bulk of the print and is assigned by supply rather
    than by cartography, so the part list has to state where it went; reading
    the plate in Bambu Studio is how the operator checks the filament mapping
    before starting a multi-day print.
    """
    names=list(SURFACE_ROLES)
    foundation=int(CFG.get('foundation_material',0))
    if not 0<=foundation<len(names):
        raise ValueError(f'foundation_material {foundation} is not one of the configured filaments')
    names[foundation]+=', and the continuous substrate below every visible surface'
    return names


def validate_generation_metadata(generation,source='generation metadata'):
    """Validate the hidden command record before it is packaged into a 3MF."""
    if not isinstance(generation,dict):
        raise ValueError(f'{source} must be a JSON object')
    if generation.get('schema_version')!=1:
        raise ValueError(
            f'{source} has unsupported schema_version {generation.get("schema_version")!r}; expected 1')
    argv=generation.get('argv')
    if not isinstance(argv,list) or not argv or not all(isinstance(value,str) and value for value in argv):
        raise ValueError(f'{source}.argv must be a non-empty list of non-empty strings')
    shell_command=generation.get('shell_command')
    if not isinstance(shell_command,str) or not shell_command:
        raise ValueError(f'{source}.shell_command must be a non-empty string')
    expected_command=shlex.join(argv)
    if shell_command!=expected_command:
        raise ValueError(
            f'{source}.shell_command does not match its argv; expected {expected_command!r}')
    working_directory=generation.get('working_directory')
    if not isinstance(working_directory,str) or not working_directory:
        raise ValueError(f'{source}.working_directory must be a non-empty string')
    return generation


def preset(profiles,kind,name):
    path=profiles/kind/(name+'.json');d=json.loads(path.read_text());out={}
    if d.get('inherits'):out.update(preset(profiles,kind,d['inherits']))
    for include in d.get('include',[]):out.update(preset(profiles,kind,include))
    out.update(d);return out

def settings(template,profiles):
    # The process and machine presets are named for the nozzle, so both come
    # from the config rather than being fixed here.  A missing preset is
    # reported by name: it means Bambu Studio does not ship that combination,
    # or ships it under a name this build does not know.
    nozzle=float(CFG['nozzle_mm'])
    machine_name=CFG.get('machine_preset',f'Bambu Lab P2S {nozzle:g} nozzle')
    process_name=CFG['process_preset']
    for kind,name in (('process',process_name),('machine',machine_name)):
        if not (profiles/kind/(name+'.json')).exists():
            raise ValueError(
                f'No installed {kind} preset {name!r} for a {nozzle:g} mm nozzle under {profiles}; '
                'install it in Bambu Studio or select a nozzle and layer height it ships')
    d=json.loads(template.read_text())
    process=preset(profiles,'process',process_name)
    for k,v in process.items():
        if k not in ['type','name','inherits','include','from','setting_id','instantiation','compatible_printers']:d[k]=v
    machine=preset(profiles,'machine',machine_name)
    for k,v in machine.items():
        if 'gcode' in k:d[k]=v
    filament=preset(profiles,'filament','Bambu PLA Matte @BBL P2S')
    for k,v in filament.items():
        if 'gcode' in k:d[k]=v*4 if isinstance(v,list) and len(v)==1 else v
    # Bambu stores each filament's Standard/High Flow variants consecutively.
    # The reference incorrectly assigns every variant to filament 4. Follow the
    # official PresetBundle::full_config serialization layout for four filaments.
    variants=d.get('filament_extruder_variant')
    if not isinstance(variants,list) or not variants or len(variants)%4:
        raise ValueError(
            'Project settings must contain a non-empty filament_extruder_variant list '
            f'whose length is divisible by four; got {variants!r}')
    per_filament=len(variants)//4
    if not all(variants[i*per_filament:(i+1)*per_filament]==variants[:per_filament] for i in range(4)):
        raise ValueError(
            'Project settings filament_extruder_variant entries must contain four identical '
            'per-filament variant blocks')
    d['filament_self_index']=[str(i+1) for i in range(4) for _ in range(per_filament)]
    d['filament_map']=['1']*4
    d['filament_nozzle_map']=['0']*4
    prime=bool(CFG.get('prime_tower',True));tower=CFG.get('prime_tower_position_mm',[214,80])
    ironing=str(CFG.get('ironing_type','top'))
    if ironing not in IRONING_TYPES:
        raise ValueError(
            f'ironing_type {ironing!r} is not one of the values Bambu Studio accepts '
            f'({", ".join(IRONING_TYPES)}); an unrecognized value is silently replaced by its default')
    d.update({'print_settings_id':f'NYC map - {CFG["layer_height_mm"]:g}mm detail @BBL P2S {nozzle:g} nozzle',
        'printer_settings_id':machine_name,'printer_model':'Bambu Lab P2S','printer_variant':f'{nozzle:g}',
        'layer_height':str(CFG['layer_height_mm']),
        # The mesh was quantized onto the planes this height sets the phase of,
        # so the two cannot be chosen independently.
        'initial_layer_print_height':f'{CFG.get("first_layer_height_mm",FIRST_LAYER_HEIGHT_MM):g}',
        'filament_colour':CFG['colors'],'filament_type':['PLA']*4,
        'filament_settings_id':['Bambu PLA Matte @BBL P2S']*4,
        'wall_generator':CFG['wall_generator'],'detect_thin_wall':'1','wall_loops':str(CFG.get('wall_loops',2)),
        # Every map color is a separate volume. Without interface shells the
        # slicer treats a horizontal color change as an ordinary internal
        # infill transition, so the first layer of the upper color can be
        # deposited directly over sparse infill. Force solid top/bottom skins
        # at those shared material boundaries.
        'interface_shells':'1',
        'sparse_infill_density':f'{CFG.get("infill_percent",12):g}%',
        # The map's ground is one large, nearly flat top surface, so it shows
        # every solid-infill line and any sag between sparse-infill ribs.
        # Iron it, and keep the top line no wider than the outer wall so the
        # two meet without a ridge between them.
        'ironing_type':ironing,'top_surface_line_width':str(CFG.get('top_surface_line_width_mm',round(1.05*nozzle,10))),
        'bottom_shell_layers':str(CFG.get('bottom_shell_layers',5)),
        'top_shell_layers':str(CFG.get('top_shell_layers',7)),'enable_support':'0',
        'brim_type':'outer_only','brim_width':str(CFG.get('brim_width_mm',3)),
        'brim_object_gap':str(CFG.get('brim_gap_mm',.1)),
        'enable_prime_tower':'1' if prime else '0','prime_tower_width':str(CFG.get('prime_tower_width_mm',35)),
        'prime_tower_brim_width':str(CFG.get('prime_tower_brim_width_mm',2)),
        'prime_tower_rib_wall':'0','prime_tower_enable_framework':'0',
        'wipe_tower_x':[str(tower[0])],'wipe_tower_y':[str(tower[1])],'wipe_tower_rotation_angle':'0',
        'timelapse_type':'0','post_process':'','print_host':'','printhost_apikey':'',
        'notes':f'Personal NYC map. Build report: {OUT / "field_build_report.json"}. Review filament mapping before printing.'})
    # A project file must not carry external post-processing commands from another project.
    for k in list(d):
        if 'post_process' in k:d[k]=''
    return d

def metadata_config(meshes,source_file):
    names=part_names()
    root=etree.Element('config');obj=etree.SubElement(root,'object',id='9')
    etree.SubElement(obj,'metadata',key='name',value=CFG['name']);etree.SubElement(obj,'metadata',key='extruder',value='1')
    etree.SubElement(obj,'metadata',face_count=str(sum(len(m.faces) for _,m in meshes)))
    for i,m in meshes:
        part=etree.SubElement(obj,'part',id=str(i+1),subtype='normal_part')
        for k,v in [('name',names[i]),('matrix','1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1'),('source_file',source_file),('source_object_id','0'),('source_volume_id',str(i)),('extruder',str(i+1))]:
            etree.SubElement(part,'metadata',key=k,value=v)
        etree.SubElement(part,'mesh_stat',face_count=str(len(m.faces)),edges_fixed='0',degenerate_facets='0',facets_removed='0',facets_reversed='0',backwards_edges='0')
    plate=etree.SubElement(root,'plate')
    # These are physical nozzle assignments, not AMS slot numbers: P2S has one nozzle.
    for k,v in [('plater_id','1'),('plater_name',CFG['name']),('locked','false'),('filament_map_mode','Auto For Flush'),('filament_maps','1 1 1 1'),('filament_volume_maps','0 0 0 0')]:etree.SubElement(plate,'metadata',key=k,value=v)
    inst=etree.SubElement(plate,'model_instance')
    for k,v in [('object_id','9'),('instance_id','0'),('identify_id','1')]:etree.SubElement(inst,'metadata',key=k,value=v)
    assemble=etree.SubElement(root,'assemble')
    transform='1 0 0 0 1 0 0 0 1 '+' '.join(map(str,CFG['plate_translation_mm']))
    etree.SubElement(assemble,'assemble_item',object_id='9',instance_id='0',transform=transform,offset='0 0 0')
    return etree.tostring(root,xml_declaration=True,encoding='UTF-8',pretty_print=True)

def plate_thumbnail(path):
    """The preview at slicer-thumbnail size; the full-resolution file stays on disk.

    A slicer shows this small, so embedding a print-quality render would grow
    every project file for nothing.
    """
    with Image.open(path) as im:
        im=im.convert('RGB')
        if max(im.size)>PLATE_THUMBNAIL_PX:
            scale=PLATE_THUMBNAIL_PX/max(im.size)
            im=im.resize((round(im.width*scale),round(im.height*scale)),Image.Resampling.LANCZOS)
        buffer=io.BytesIO();im.save(buffer,'PNG');return buffer.getvalue()


def main():
    p=argparse.ArgumentParser();p.add_argument('--mesh-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--preview',type=Path)
    p.add_argument('--project-settings-template',type=Path,required=True);p.add_argument('--profiles-dir',type=Path,required=True)
    p.add_argument('--generation-metadata',type=Path,required=True);a=p.parse_args()
    generation=validate_generation_metadata(
        json.loads(a.generation_metadata.read_text()),str(a.generation_metadata))
    meshes=[(i,trimesh.load(a.mesh_dir/f'material_{i}.ply',process=False)) for i in range(4) if (a.mesh_dir/f'material_{i}.ply').exists()]
    if not meshes:raise RuntimeError(f'No non-empty material meshes found in {a.mesh_dir}')
    for material,mesh in meshes:
        if not isinstance(mesh,trimesh.Trimesh):
            raise RuntimeError(
                f'Material {material} did not load as one mesh from {a.mesh_dir / f"material_{material}.ply"}')
        finite=bool(np.isfinite(mesh.vertices).all())
        if not mesh.is_watertight or not mesh.is_winding_consistent or not finite:
            raise RuntimeError(
                f'Material {material} mesh failed package validation: '
                f'watertight={mesh.is_watertight}, winding_consistent={mesh.is_winding_consistent}, '
                f'finite_vertices={finite}')
        bad=int((mesh.area_faces<1e-12).sum())
        if bad:
            raise RuntimeError(
                f'Material {material} has {bad} degenerate triangles in its serialized PLY')
    config=settings(a.project_settings_template,a.profiles_dir);write_json(OUT/'project_settings.generated.json',config)
    identity=f'{CFG["name"]}|{CFG["center_wgs84"]}|{CFG["size_mm"]}|{CFG["scale_denominator"]}'
    U=lambda name:str(uuid.uuid5(uuid.NAMESPACE_URL,'3d-nyc/'+identity+'/'+name))
    transform='1 0 0 0 1 0 0 0 1 '+' '.join(map(str,CFG['plate_translation_mm']))
    xml_name=escape(CFG['name'])
    member_slug=re.sub(r'[^A-Za-z0-9_.-]+','_',a.output.stem).strip('._') or 'nyc_map'
    object_member=f'3D/Objects/{member_slug}.model'
    wrapper=f'''<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{CORE}" xmlns:p="{PROD}" unit="millimeter" requiredextensions="p" xml:lang="en-US">
<metadata name="Application">BambuStudio-02.08.02.61</metadata><metadata name="BambuStudio:3mfVersion">1</metadata>
<metadata name="Title">{xml_name}</metadata><metadata name="Description">Detailed NYC map; 1:{CFG['scale_denominator']:g}; measured sources with documented print adjustments and optional inferred underpasses.</metadata>
<metadata name="Designer">Personal NYC map project</metadata>
<metadata name="Copyright">NYC public geospatial data; © OpenStreetMap contributors, ODbL. Source notices retained with project.</metadata>
<resources><object id="9" type="model" name="{xml_name}" p:UUID="{U('assembly')}"><components>
'''+''.join(f'<component objectid="{i+1}" p:path="/{object_member}" transform="{ID}" p:UUID="{U("component"+str(i))}"/>\n' for i,_ in meshes)+f'''</components></object></resources>
<build p:UUID="{U('build')}"><item objectid="9" transform="{transform}" printable="1" p:UUID="{U('instance')}"/></build></model>'''
    a.output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(a.output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        z.writestr('[Content_Types].xml','''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/><Default Extension="png" ContentType="image/png"/><Default Extension="json" ContentType="application/json"/></Types>''')
        z.writestr('_rels/.rels','''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>''')
        z.writestr('3D/3dmodel.model',wrapper)
        z.writestr('3D/_rels/3dmodel.model.rels',f'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/{object_member}" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>''')
        with z.open(object_member,'w',force_zip64=True) as raw:
            f=io.TextIOWrapper(raw,encoding='utf-8',newline='\n')
            f.write(f'<?xml version="1.0" encoding="UTF-8"?><model xmlns="{CORE}" xmlns:p="{PROD}" xmlns:m="{MAT}" unit="millimeter" requiredextensions="p m"><metadata name="BambuStudio:3mfVersion">1</metadata><resources><m:colorgroup id="1008">')
            for color in CFG['colors']:f.write(f'<m:color color="{color}FF"/>')
            f.write('</m:colorgroup>')
            names=part_names()
            for i,m in meshes:
                f.write(f'<object id="{i+1}" name="{escape(names[i])}" type="model" pid="1008" pindex="{i}" p:UUID="{U("mesh"+str(i))}"><mesh><vertices>\n')
                for start in range(0,len(m.vertices),10000):
                    f.writelines(f'<vertex x="{x:.17g}" y="{y:.17g}" z="{zz:.17g}"/>\n' for x,y,zz in m.vertices[start:start+10000])
                f.write('</vertices><triangles>\n')
                for start in range(0,len(m.faces),10000):f.writelines(f'<triangle v1="{int(v[0])}" v2="{int(v[1])}" v3="{int(v[2])}"/>\n' for v in m.faces[start:start+10000])
                f.write('</triangles></mesh></object>\n')
            f.write('</resources><build/></model>');f.flush();f.detach()
        z.writestr('Metadata/model_settings.config',metadata_config(meshes,a.output.name))
        z.writestr('Metadata/project_settings.config',json.dumps(config,indent=2))
        # Deliberately stored as an archive metadata part, not a display/title field.
        z.writestr('Metadata/generation_command.json',json.dumps(generation,indent=2))
        z.writestr('Metadata/slice_info.config','<config><header><header_item key="X-BBL-Client-Type" value="slicer"/><header_item key="X-BBL-Client-Version" value="02.08.02.61"/></header></config>')
        if a.preview and a.preview.exists():z.writestr('Metadata/plate_1.png',plate_thumbnail(a.preview))
    print(a.output,a.output.stat().st_size,'bytes',flush=True)

if __name__=='__main__':main()
