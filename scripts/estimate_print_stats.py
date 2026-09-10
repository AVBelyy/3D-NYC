"""Report per-filament use and print time for a 3MF, slicing it locally when it is not sliced."""
import argparse,hashlib,json,re,shutil,tempfile,zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from cache_common import Progress
from slice_3mf import SLICER,run_slice

SPOOL_G=1000.
SLICED_SUFFIX='.gcode.3mf'
STATS_MEMBER='Metadata/print_stats.json'
CARRIED=('Metadata/generation_command.json',)
DURATION=re.compile(r'(\d+(?:\.\d+)?)\s*([dhms])')
UNITS={'d':86400.,'h':3600.,'m':60.,'s':1.}
LIST_KEYS={'filament_colour':'colour','filament_type':'type','filament_cost':'cost_per_kg',
    'filament_settings_id':'settings','printer_model':'printer','nozzle_diameter':'nozzle_mm',
    'layer_height':'layer_mm'}


def parse_duration(text):
    """Convert a slicer duration such as '6h 26m 17s' to seconds."""
    parts=DURATION.findall(text)
    if not parts:raise ValueError(f'Unparsed slicer duration: {text!r}')
    return sum(float(value)*UNITS[unit] for value,unit in parts)


def format_duration(seconds):
    seconds=int(round(seconds));hours,rest=divmod(seconds,3600);days,hours=divmod(hours,24)
    if days:return f'{days}d {hours:02d}h {rest//60:02d}m'
    return f'{hours}h {rest//60:02d}m' if hours else f'{rest//60}m {rest%60:02d}s'


def scan_comments(path,head=4<<20,tail=1<<20):
    """Read the comment lines of a G-code file's header and footer without loading the toolpaths."""
    size=path.stat().st_size
    with path.open('rb') as stream:
        data=stream.read(min(size,head))
        if size>head+tail:
            stream.seek(-tail,2);data+=b'\n'+stream.read()
    return [line[1:].strip() for line in data.decode('utf-8','replace').splitlines()
        if line.startswith(';')]


def parse_gcode(path):
    """Collect the slicer's own totals from a sliced plate."""
    out={}
    for line in scan_comments(path):
        if line.startswith('model printing time:'):
            for part in line.split(';'):
                name,_,value=part.partition(':')
                if name.strip()=='model printing time':out['model_seconds']=parse_duration(value)
                elif name.strip()=='total estimated time':out['seconds']=parse_duration(value)
        elif line.startswith('total layer number:'):out['layers']=int(line.split(':',1)[1])
        elif line.startswith('total filament length [mm]'):
            out['length_mm']=[float(v) for v in line.split(':',1)[1].split(',')]
        elif line.startswith('total filament weight [g]'):
            out['weight_g']=[float(v) for v in line.split(':',1)[1].split(',')]
        elif ' = ' in line:
            key,_,value=line.partition(' = ')
            if key in LIST_KEYS:
                out[LIST_KEYS[key]]=[v.strip().strip('"') for v in re.split('[;,]',value)]
    if 'weight_g' not in out or 'seconds' not in out:
        raise RuntimeError(f'Sliced G-code is missing filament or time totals: {path}')
    return out


def parse_sliced_3mf(model):
    """Read totals from an already sliced project, so a Bambu Studio export needs no reslice."""
    with zipfile.ZipFile(model) as archive:
        if 'Metadata/slice_info.config' not in archive.namelist():return None
        root=ET.fromstring(archive.read('Metadata/slice_info.config'))
    plates=[plate for plate in root.iter('plate') if plate.findall('filament')]
    if not plates:return None
    meta=[{item.get('key'):item.get('value') for item in plate.findall('metadata')} for plate in plates]
    filaments=[filament for plate in plates for filament in plate.findall('filament')]
    indices=sorted({int(f.get('id')) for f in filaments})
    def total(index,key):
        return sum(float(f.get(key,0) or 0) for f in filaments if int(f.get('id'))==index)
    return {'seconds':sum(float(m.get('prediction',0) or 0) for m in meta),
        'plates':len(plates),
        'weight_g':[total(i,'used_g') for i in indices],
        'length_mm':[total(i,'used_m')*1000 for i in indices],
        'colour':[next(f.get('color') for f in filaments if int(f.get('id'))==i) for i in indices],
        'type':[next(f.get('type') for f in filaments if int(f.get('id'))==i) for i in indices]}


def parse_result(path):
    """Split the slicer's per-filament totals into model material and tool-change purge."""
    plates=json.loads(path.read_text())['sliced_plates']
    model={};changes=0;warnings=[]
    for plate in plates:
        changes+=plate.get('filament_change_times',0)
        if plate.get('warning_message'):warnings.append(plate['warning_message'])
        for filament in plate['filaments']:
            model[int(filament['id'])]=model.get(int(filament['id']),0.)+filament['main_used_g']
    return {'model_g':model,'changes':changes,'warnings':sorted(set(warnings)),
        'flush_seconds':sum(p.get('feature_type_times',{}).get('Flush',0.) for p in plates)}


def part_labels(model):
    """Name each extruder after the material role the generator assigned it."""
    labels={}
    with zipfile.ZipFile(model) as archive:
        if 'Metadata/model_settings.config' not in archive.namelist():return labels
        root=ET.fromstring(archive.read('Metadata/model_settings.config'))
    for part in root.iter('part'):
        meta={item.get('key'):item.get('value') for item in part.findall('metadata')}
        index=int(meta.get('extruder') or 0)
        if index and index not in labels:
            labels[index]=meta.get('name','').split(' - ')[0].strip() or f'Filament {index}'
    return labels


def sha256(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def sliced_path(model):
    """The sliced project belongs beside the model it was sliced from."""
    return model.with_suffix(SLICED_SUFFIX)


def read_embedded_stats(project,digest=None):
    """Return the totals embedded when we sliced, unless they describe some other model."""
    if not project.exists():return None
    with zipfile.ZipFile(project) as archive:
        if STATS_MEMBER not in archive.namelist():return None
        payload=json.loads(archive.read(STATS_MEMBER))
    if digest is not None and payload.get('source_sha256')!=digest:return None
    extra=payload.get('extra') or {}
    if 'model_g' in extra:extra['model_g']={int(key):value for key,value in extra['model_g'].items()}
    return payload['stats'],extra


def embed_stats(project,model,payload):
    """Carry our own totals and the model's provenance into the sliced project.

    Bambu's export keeps neither, and a rerun that can read both needs no reslice.
    """
    with zipfile.ZipFile(model) as archive:
        carried={name:archive.read(name) for name in CARRIED if name in archive.namelist()}
    with zipfile.ZipFile(project,'a',zipfile.ZIP_DEFLATED) as archive:
        present=set(archive.namelist())
        for name,data in {STATS_MEMBER:json.dumps(payload,indent=2).encode(),**carried}.items():
            if name not in present:archive.writestr(name,data)


def slice_model(model,digest,slicer):
    """Slice into a throwaway directory, keeping only the sliced project beside the model.

    The G-code is not written twice: the copy inside the exported project is the one we keep.
    A failed slice leaves its temporary directory behind, because its log is the evidence.
    """
    folder=Path(tempfile.mkdtemp(prefix='print-stats-'))
    output=folder/'slice'
    record=run_slice(model,output,slicer,export_project=True,announce=False)
    if record['exit_code']!=0:
        raise RuntimeError(f"Slicing failed ({record['exit_code']}); see {output/'slice.log'}")
    gcodes=sorted(output.glob('plate_*.gcode'))
    stats=parse_gcode(gcodes[0]);stats['plates']=len(gcodes)
    extra=parse_result(output/'result.json') if (output/'result.json').exists() else {}
    project=sliced_path(model)
    shutil.move(str(output/f'{model.stem}{SLICED_SUFFIX}'),project)
    embed_stats(project,model,{'schema_version':1,'source_sha256':digest,'stats':stats,'extra':extra})
    shutil.rmtree(folder,ignore_errors=True)
    return stats,extra,project


def unique_models(models):
    """Drop a sliced project whose own model is on the same command line, which a *.3mf glob picks up twice."""
    given={model.resolve() for model in models}
    return [model for model in models if not (model.name.endswith(SLICED_SUFFIX)
        and model.with_suffix('').with_suffix('.3mf').resolve() in given)]


def cached_stats(model,digest,replace,force_slice):
    """Find totals that need no slicer: ours, a matching sliced project, or the model's own metadata.

    Every lookup is a hash and a zip read, so a whole batch resolves in well
    under a second and only the models that truly need a slice reach the bar.
    """
    if force_slice:return None,None
    found=read_embedded_stats(model)
    if found is not None:return found,str(model)
    if not replace:
        project=sliced_path(model)
        found=read_embedded_stats(project,digest)
        if found is not None:return found,str(project)
    direct=parse_sliced_3mf(model)
    if direct is not None:return (direct,{}),f'{model} slice metadata'
    return None,None


def build_entry(model,digest,stats,extra,source,reused):
    """Shape one model's totals into the record the reports and the JSON share."""
    labels=part_labels(model)
    model_g=extra.get('model_g',{})
    project=sliced_path(model)
    filaments=[]
    for position,weight in enumerate(stats['weight_g'],start=1):
        colour=(stats.get('colour') or [])[position-1:position]
        cost=(stats.get('cost_per_kg') or [])[position-1:position]
        main=model_g.get(position)
        filaments.append({'extruder':position,'label':labels.get(position,f'Filament {position}'),
            'colour':colour[0] if colour else None,
            'type':(stats.get('type') or [])[position-1] if position<=len(stats.get('type') or []) else None,
            'total_g':weight,'model_g':main,'purge_g':None if main is None else weight-main,
            'length_m':stats['length_mm'][position-1]/1000 if position<=len(stats.get('length_mm',[])) else None,
            'cost_usd':weight/SPOOL_G*float(cost[0]) if cost else None})
    return {'model':str(model),'sha256':digest,'source':source,'reused_slice':reused,
        'sliced_project':str(project) if project.exists() else None,
        'printer':(stats.get('printer') or [None])[0],'nozzle_mm':(stats.get('nozzle_mm') or [None])[0],
        'layer_mm':(stats.get('layer_mm') or [None])[0],'layers':stats.get('layers'),
        'plates':stats.get('plates',1),'seconds':stats['seconds'],
        'model_seconds':stats.get('model_seconds'),'flush_seconds':extra.get('flush_seconds'),
        'filament_changes':extra.get('changes'),'warnings':extra.get('warnings',[]),
        'filaments':filaments}


def collect_all(models,replace,force_slice,slicer):
    """Estimate every model, resolving the cache first so the bar counts slices alone.

    A cache hit lands in milliseconds and a slice takes minutes, so counting
    both in one bar makes its remaining time a fiction. Resolving first also
    means a fully cached batch prints no bar at all.
    """
    resolved=[]
    for model in unique_models(models):
        digest=sha256(model)
        resolved.append((model,digest)+cached_stats(model,digest,replace,force_slice))
    pending=sum(1 for entry in resolved if entry[2] is None)
    progress=Progress('slicing',pending,unit='models') if pending else None
    entries=[];sliced=0
    for model,digest,found,source in resolved:
        reused=found is not None
        if found is None:
            progress.update(sliced,detail=model.name,force=True)
            stats,extra,project=slice_model(model,digest,slicer)
            found=(stats,extra);source=str(project);sliced+=1
        entries.append(build_entry(model,digest,*found,source,reused))
    if progress:
        progress.update(sliced,force=True)
        progress.close()
    return entries


def gram(value):
    return '     -' if value is None else f'{value:6.1f}'


def report(entries):
    """Print one table per model plus a combined shopping total."""
    for entry in entries:
        head=[Path(entry['model']).name]
        if entry['printer']:head.append(entry['printer'])
        if entry['nozzle_mm']:head.append(f"{entry['nozzle_mm']} mm nozzle")
        if entry['layer_mm']:head.append(f"{entry['layer_mm']} mm layers")
        if entry['layers']:head.append(f"{entry['layers']} layers")
        print('\n'+' | '.join(head))
        print(f"  source: {entry['source']}"+(' (reused)' if entry['reused_slice'] else ''))
        print(f"  {'#':<2} {'colour':<9} {'role':<10} {'model':>6} {'purge':>6} {'total':>6} {'m':>7} {'cost':>7}")
        for filament in entry['filaments']:
            length='      -' if filament['length_m'] is None else f"{filament['length_m']:7.1f}"
            cost='      -' if filament['cost_usd'] is None else f"${filament['cost_usd']:6.2f}"
            print(f"  {filament['extruder']:<2} {filament['colour'] or '?':<9} {filament['label'][:10]:<10} "
                f"{gram(filament['model_g'])} {gram(filament['purge_g'])} {gram(filament['total_g'])} {length} {cost}")
        totals=[sum(f[key] for f in entry['filaments'] if f[key] is not None)
            for key in ('model_g','purge_g','total_g')]
        cost=sum(f['cost_usd'] or 0 for f in entry['filaments'])
        print(f"  {'':<2} {'':<9} {'total':<10} {gram(totals[0])} {gram(totals[1])} {gram(totals[2])} "
            f"{'':>7} ${cost:6.2f}")
        time_note=f"  print time: {format_duration(entry['seconds'])}"
        if entry['model_seconds']:time_note+=f" (model {format_duration(entry['model_seconds'])})"
        if entry['flush_seconds']:
            time_note+=f", {format_duration(entry['flush_seconds'])} purging"
        if entry['filament_changes']:time_note+=f", {entry['filament_changes']} filament changes"
        print(time_note)
        for warning in entry['warnings']:print(f'  slicer warning: {warning}')
    if len(entries)>1:
        weight=sum(f['total_g'] for e in entries for f in e['filaments'])
        seconds=sum(e['seconds'] for e in entries)
        per_colour={}
        for entry in entries:
            for filament in entry['filaments']:
                key=(filament['extruder'],filament['label'],filament['colour'])
                per_colour[key]=per_colour.get(key,0.)+filament['total_g']
        print(f'\n{len(entries)} models | {format_duration(seconds)} | {weight:.0f} g '
            f'({weight/SPOOL_G:.2f} spools of 1 kg)')
        for (index,label,colour),grams in sorted(per_colour.items()):
            print(f'  {index:<2} {colour or "?":<9} {label[:10]:<10} {grams:7.1f} g  '
                f'{grams/SPOOL_G:.2f} spools')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('models',nargs='+',type=Path,help='3MF files to estimate')
    parser.add_argument('--replace',action='store_true',
        help='Reslice even when a sliced project beside the model matches')
    parser.add_argument('--force-slice',action='store_true',
        help='Slice locally even if the 3MF already carries slice metadata')
    parser.add_argument('--slicer',type=Path,default=SLICER)
    parser.add_argument('--json',type=Path,help='Also write the estimate as JSON')
    args=parser.parse_args()
    if not args.slicer.exists():
        raise SystemExit(f'Bambu Studio executable is missing: {args.slicer}')
    entries=collect_all(args.models,args.replace,args.force_slice,args.slicer)
    report(entries)
    if args.json:args.json.write_text(json.dumps(entries,indent=2)+'\n')


if __name__=='__main__':main()
