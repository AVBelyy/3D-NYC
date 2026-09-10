"""Run a local Bambu Studio CLI validation slice; never send anything to a printer."""
import argparse, datetime, hashlib, json, re, shutil, subprocess, time
from pathlib import Path
from map_common import CACHE_DIR, VALID, write_json


EXTRUSION=re.compile(r'(?:^|\s)E([-+]?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|$)')
SLICER=Path('/Applications/BambuStudio.app/Contents/MacOS/BambuStudio')

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
