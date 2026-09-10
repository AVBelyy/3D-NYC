"""Run a local Bambu Studio CLI validation slice; never send anything to a printer."""
import argparse, datetime, hashlib, json, os, re, shutil, subprocess, time, zipfile
from pathlib import Path
from map_common import CACHE_DIR, VALID, write_json


EXTRUSION=re.compile(r'(?:^|\s)E([-+]?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|$)')
GEOMETRY=re.compile(r'^3D/(Objects/|_rels/)')
SLICER=Path('/Applications/BambuStudio.app/Contents/MacOS/BambuStudio')


def printer_model_id(printer_model,slicer=SLICER):
    """Resolve a printer's vendor model id from the profiles shipped beside the slicer."""
    for path in sorted((slicer.parents[1]/'Resources'/'profiles').glob(f'*/machine/{printer_model}.json')):
        found=json.loads(path.read_text()).get('model_id')
        if found:return found
    return ''


def as_sliced_file(project,slicer=SLICER):
    """Rewrite an exported project into the geometry-free shape Studio prints without reslicing.

    A 3MF that still carries its objects opens as a project to edit, so Studio discards the
    embedded G-code and reslices. Its own sliced file keeps the plate and the toolpaths and
    drops the mesh, which is what the model beside it is for.
    """
    with zipfile.ZipFile(project) as archive:
        members={item.filename:archive.read(item.filename) for item in archive.infolist()
            if not GEOMETRY.match(item.filename)}
    printer=re.search(r'"printer_model":\s*"([^"]*)"',members['Metadata/project_settings.config'].decode())
    identifier=printer_model_id(printer.group(1),slicer) if printer else ''
    model=members['3D/3dmodel.model'].decode()
    model=re.sub(r'<resources>.*?</resources>','<resources>\n </resources>',model,flags=re.S)
    members['3D/3dmodel.model']=re.sub(r'<build[ >].*?</build>','<build/>',model,flags=re.S).encode()
    plate=re.search(r'\s*<plate>.*?</plate>',members['Metadata/model_settings.config'].decode(),flags=re.S)
    if plate is None:raise RuntimeError(f'Exported project carries no plate to print: {project}')
    block=re.sub(r'\s*<model_instance>.*?</model_instance>','',plate.group(0),flags=re.S)
    if 'pattern_bbox_file' not in block:
        block=re.sub(r'(<metadata key="pick_file".*?/>)',
            r'\1\n    <metadata key="pattern_bbox_file" value="Metadata/plate_1.json"/>',block,count=1)
    members['Metadata/model_settings.config']=(
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>'+block+'\n</config>\n').encode()
    members['Metadata/slice_info.config']=re.sub(r'(key="printer_model_id" value=")[^"]*(")',
        rf'\g<1>{identifier}\g<2>',members['Metadata/slice_info.config'].decode()).encode()
    staging=project.with_suffix('.tmp')
    with zipfile.ZipFile(staging,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in members.items():archive.writestr(name,data)
    os.replace(staging,project)
    return project

def audit_sliced_gcode(path):
    """Confirm that every elevated filament begins with a solid interface skin."""
    interface_shells=False;z=None;tool=0;feature=None;wipe_tower=False;starts={}
    with Path(path).open(errors='replace') as stream:
        for raw in stream:
            line=raw.strip()
            if line=='; interface_shells = 1':interface_shells=True
            elif line.startswith('; Z_HEIGHT:'):
                z=float(line.split(':',1)[1])
            elif re.fullmatch(r'T\d+',line):tool=int(line[1:])
            elif line.startswith('; FEATURE:'):feature=line.split(':',1)[1].strip()
            elif line=='; WIPE_TOWER_START':wipe_tower=True
            elif line=='; WIPE_TOWER_END':wipe_tower=False
            elif z is not None and not wipe_tower and line.startswith(('G1 ','G2 ','G3 ')):
                match=EXTRUSION.search(line)
                if not match or float(match.group(1))<=0 or feature in {'Prime tower','Brim','Custom','Flush'}:
                    continue
                current=starts.get(tool)
                if current is None or z<current['z']-1e-6:
                    starts[tool]={'z':z,'features':{feature} if feature else set()}
                elif abs(z-current['z'])<=1e-6 and feature:
                    current['features'].add(feature)
    if not interface_shells:
        raise RuntimeError('Sliced G-code does not enable material interface shells')
    missing={tool:value for tool,value in starts.items()
        if tool>0 and 'Bottom surface' not in value['features']}
    if missing:
        raise RuntimeError(f'Elevated materials do not start with solid bottom surfaces: {missing}')
    return {'result':'passed','interface_shells':True,
        'material_starts':{str(tool):{'z_mm':value['z'],'features':sorted(value['features'])}
            for tool,value in sorted(starts.items())}}


def run_slice(model,output,slicer=SLICER,export_project=False,announce=True):
    """Slice one model offline into output and record the invocation; never contacts a printer."""
    output.mkdir(parents=True,exist_ok=False)
    command=[str(slicer),'--datadir',str(CACHE_DIR/'bambu_profile'),
        '--debug','3','--arrange','0','--orient','0','--slice','1',
        '--mtcpp','10000000','--mstpp','7200','--outputdir',str(output)]
    # --export-3mf is resolved under --outputdir, so an absolute path here yields a bad concatenation.
    if export_project:command+=['--export-3mf',f'{model.stem}.gcode.3mf']
    command.append(str(model.resolve()))
    with model.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
    record={'command':command,'input':str(model.resolve()),'input_sha256':sha,
        'input_bytes':model.stat().st_size,
        'started':datetime.datetime.now().astimezone().isoformat(),'result':'running',
        'purpose':'Offline validation only. No printer connection or print command.'}
    write_json(output/'run.json',record)
    if announce:print('Slicer log:',output/'slice.log',flush=True)
    started=time.monotonic()
    with (output/'slice.log').open('w') as log:
        result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
    record.update(exit_code=result.returncode,elapsed_seconds=time.monotonic()-started,
        finished=datetime.datetime.now().astimezone().isoformat(),
        result='completed' if result.returncode==0 else 'failed')
    exported=output/f'{model.stem}.gcode.3mf'
    if export_project and result.returncode==0 and exported.exists():
        record['sliced_file']=str(as_sliced_file(exported,slicer))
    write_json(output/'run.json',record)
    return record


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--slicer',type=Path,default=SLICER)
    parser.add_argument('--name',default='slice_'+datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
    parser.add_argument('--replace',action='store_true',help='Replace an existing derived slice directory')
    parser.add_argument('--export-project',action='store_true',help='Also write <model>.gcode.3mf, a sliced project that Bambu Studio opens ready to print')
    args=parser.parse_args()
    output=VALID/args.name
    if output.exists() and args.replace:shutil.rmtree(output)
    record=run_slice(args.model,output,args.slicer,args.export_project)
    code=record['exit_code']
    if code==0:
        try:record['support_audit']=audit_sliced_gcode(output/'plate_1.gcode')
        except (OSError,RuntimeError) as error:
            record.update(result='failed',support_audit={'result':'failed','error':str(error)});code=1
    write_json(output/'run.json',record)
    print(json.dumps(record,indent=2),flush=True)
    raise SystemExit(code)


if __name__=='__main__':main()
