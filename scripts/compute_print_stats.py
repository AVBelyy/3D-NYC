"""Report per-filament use and print time for finished 3MFs, slicing any that are not sliced."""
import argparse,hashlib,json,re,shutil,statistics,tempfile,zipfile
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
PREVIEW_PAD=10.8
PREVIEW_FONTS='DejaVu Sans, Helvetica, Arial, Apple Color Emoji, Segoe UI Emoji, sans-serif'
SVG_OPEN=re.compile(r'<svg\b[^>]*>')
SVG_BAND=re.compile(r'<g id="print-stats" data-svg-height="([^"]*)" data-svg-view="([^"]*)">.*?</g>'
    r'\s*<g id="print-stats-shift"[^>]*>\n?',re.S)
SVG_CHUNKS=re.compile(r'</g>\s*(?:<g id="print-stats-chunks"[^>]*>.*?</g>\s*)?</svg>\s*\Z',re.S)
SVG_BOXES=re.compile(r'<desc id="print-stats-boxes">(.*?)</desc>',re.S)
SVG_BOX=re.compile(r'<rect data-print-stats="([^"]*)"[^>]*/>')
SVG_LABEL=re.compile(r'<g id="text_\d+">')
# Matplotlib emits a text group at scale = font size / 100, so a label's own
# scale is its font size: matching it keeps a caption the size of the label it
# hangs from, at whatever canvas the preview was rendered at.
GLYPH_EM=100.
CHUNK_FONT=0.75
# Where the stats line sits below the label box's old edge, and how much room is
# left under it, both in caption ems: the gap matches the label's own line
# spacing, so the line reads as part of the card rather than crowding its size.
CHUNK_GAP=0.8
CHUNK_DROP=0.55
CHUNK_LABEL=re.compile(r'_([A-Z]+\d+(?:\.\d+)?)$')
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


def aggregate(entries):
    """Fold per-model estimates into per-extruder totals and a print-time spread.

    Grams the slicer never split into model and purge are counted apart rather
    than folded into either side, so one model read from a Studio export does
    not silently flatten the split the rest of the batch does have. An extruder
    keeps the first real name and colour it is seen with, because a model whose
    parts carry no material role would otherwise label the whole batch.
    """
    filaments={}
    unattributed={'grams':0.,'entries':0,'models':set()}
    for entry in entries:
        for filament in entry['filaments']:
            slot=filaments.setdefault(filament['extruder'],
                {'extruder':filament['extruder'],'label':filament['label'],
                'colour':filament['colour'],'total_g':0.,'model_g':0.,'purge_g':0.,'cost_usd':0.})
            if slot['label'].startswith('Filament '):slot['label']=filament['label']
            slot['colour']=slot['colour'] or filament['colour']
            slot['total_g']+=filament['total_g']
            slot['cost_usd']+=filament['cost_usd'] or 0.
            if filament['model_g'] is None:
                unattributed['grams']+=filament['total_g']
                unattributed['entries']+=1
                unattributed['models'].add(Path(entry['model']).name)
            else:
                slot['model_g']+=filament['model_g']
                slot['purge_g']+=filament['purge_g']
    times=[entry['seconds'] for entry in entries]
    return {'filaments':[filaments[key] for key in sorted(filaments)],
        'unattributed':{**unattributed,'models':sorted(unattributed['models'])},
        'models':len(entries),'plates':sum(entry.get('plates') or 1 for entry in entries),
        'seconds':sum(times),
        'model_seconds':{'min':min(times),'mean':statistics.fmean(times),'max':max(times)}
            if times else {}}


def escape(text):
    return text.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')


def unescape(text):
    return text.replace('&gt;','>').replace('&lt;','<').replace('&amp;','&')


def band_lines(summary):
    """Time, then weight split into model and purge, then one colour chip per filament.

    Character references keep this file ASCII while the caption still reads as a
    stopwatch, a spool and a swatch of the filament the grams were extruded from.
    """
    filaments=summary['filaments']
    total=sum(filament['total_g'] for filament in filaments)
    model=sum(filament['model_g'] for filament in filaments)
    purge=sum(filament['purge_g'] for filament in filaments)
    loose=summary['unattributed']['grams']
    head=f"&#9201; {format_duration(summary['seconds'])} &#183; &#129525; {total:.0f} g"
    if model or purge:
        head+=f' = {model:.0f} g model + {purge:.0f} g purge'
        if loose>=0.5:head+=f' + {loose:.0f} g unsplit'
    # A hairline outline keeps the ivory swatch visible against the white band.
    chips=' &#160; '.join(f'<tspan fill="{filament["colour"] or "#888888"}" stroke="#999999" '
        f'stroke-width="0.3">&#9632;</tspan> {escape(filament["label"])} '
        f'{filament["total_g"]:.0f} g' for filament in filaments)
    return [head,chips] if chips else [head]


def chunk_caption(entry):
    """One plate's own line: what it takes to print and what it extrudes.

    The model and purge split stays out of it. It is the whole plan's number to
    weigh, the band carries it, and a plate's card is read for what that plate
    costs. Returned with a character count, because the box behind the line has
    to be sized before anything has measured a glyph.
    """
    total=sum(filament['total_g'] for filament in entry['filaments'])
    line=f"{format_duration(entry['seconds'])} X {total:.0f} g"
    return '&#9201; '+line.replace(' X ',' &#183; &#129525; '),len(line)+4


def chunk_label(entry):
    """The plate label a model's name ends in, which is what the preview draws."""
    found=CHUNK_LABEL.search(Path(entry['model']).stem)
    return found.group(1) if found else None


def box_shape(xs,ys):
    """The rounded rectangle matplotlib drew, as edges and a corner radius.

    The radius is the gap between an outer edge and the straight run beside it,
    which is what the box's own path records.
    """
    def inset(values):
        below=[value for value in values if value<max(values)]
        return max(below) if len(set(values))>2 and below else max(values)
    return min(xs),min(ys),max(xs),max(ys),max(xs)-inset(xs)


def label_boxes(text,labels):
    """Find each plate label's own box in the preview, so its card can carry the stats.

    The box is matplotlib's: its geometry, corner radius and style are read back
    from the path it drew rather than guessed, which is what lets the card grow
    without looking like a second annotation that happens to sit nearby.
    """
    boxes={}
    for label in labels:
        mark=text.find(f'<!-- {label} -->')
        if mark<0:continue
        opens=[found.end() for found in SVG_LABEL.finditer(text,0,mark)]
        path=(re.search(r'<path\s+d="([^"]*)"\s+style="([^"]*)"\s*/>',text[opens[-1]:mark])
            if opens else None)
        scale=re.search(r'scale\(([\d.]+)',text[mark:mark+400])
        if not path or not scale:continue
        numbers=[float(value) for value in re.findall(r'-?\d+(?:\.\d+)?',path.group(1))]
        boxes[label]={'element':path.group(0),'style':path.group(2),
            'font':float(scale.group(1))*GLYPH_EM,
            'shape':box_shape(numbers[0::2],numbers[1::2])}
    return boxes


def chunk_markup(text,entries,band):
    """Grow every labelled plate's card to hold its own print stats.

    The card is matplotlib's own box, made taller in place so the stats read as
    the last lines of the label rather than as a second box below it. The box it
    replaces is carried along verbatim, which is what lets a rerun put the
    preview back exactly as the planner drew it before captioning it again.
    """
    captions={label:entry for entry in entries if (label:=chunk_label(entry))}
    unnamed=[Path(entry['model']).name for entry in entries if not chunk_label(entry)]
    boxes=label_boxes(text,captions)
    rows='';originals={}
    for label,box in boxes.items():
        caption,length=chunk_caption(captions[label])
        # Smaller than the label, so the plate's name still leads its card.
        font=box['font']*CHUNK_FONT
        left,top,right,bottom,radius=box['shape']
        grown=font*(CHUNK_GAP+CHUNK_DROP)
        width=length*font*0.55+font*0.8
        centre=(left+right)/2
        edge=max(0.,(width-(right-left))/2)
        replacement=(f'<rect data-print-stats="{label}" x="{left-edge:.6f}" y="{top:.6f}" '
            f'width="{right-left+2*edge:.6f}" height="{bottom-top+grown:.6f}" '
            f'rx="{radius:.6f}" style="{box["style"]}"/>')
        originals[label]=box['element']
        text=text.replace(box['element'],replacement,1)
        rows+=(f'<text x="{centre:.2f}" y="{bottom+font*CHUNK_GAP:.2f}" font-size="{font:.2f}" '
            f'font-family="{PREVIEW_FONTS}" fill="#1A1A1A" text-anchor="middle">{caption}</text>')
    stored=escape(json.dumps(originals))
    return (text,f'<g id="print-stats-chunks" transform="translate(0 {band:.2f})">'
        f'<desc id="print-stats-boxes">{stored}</desc>{rows}</g>\n',
        sorted(boxes),sorted(set(captions)-set(boxes))+unnamed)


def restore_boxes(text):
    """Put every grown card back to the box matplotlib drew, before a band is stripped."""
    stored=SVG_BOXES.search(text)
    if not stored:return text
    originals=json.loads(unescape(stored.group(1)))
    def revert(match):
        return originals.get(match.group(1),match.group(0))
    return SVG_BOX.sub(revert,text)


def svg_size(text):
    """The root's own height and viewBox, which a band has to grow together."""
    match=SVG_OPEN.search(text)
    if not match:raise RuntimeError('Preview is not an SVG')
    height=re.search(r'\sheight="([^"]*)"',match.group(0))
    view=re.search(r'\sviewBox="([^"]*)"',match.group(0))
    if not height or not view:raise RuntimeError('Preview SVG has no height or viewBox')
    return height.group(1),view.group(1)


def set_svg_size(text,height,view):
    match=SVG_OPEN.search(text)
    tag=re.sub(r'\sheight="[^"]*"',f' height="{height}"',match.group(0))
    tag=re.sub(r'\sviewBox="[^"]*"',f' viewBox="{view}"',tag)
    return text[:match.start()]+tag+text[match.end():]


def strip_band(text):
    """Undo an earlier caption, so a rerun replaces it instead of stacking a second."""
    match=SVG_BAND.search(text)
    if not match:return text
    text=restore_boxes(text)
    match=SVG_BAND.search(text)
    text=set_svg_size(text[:match.start()]+text[match.end():],match.group(1),match.group(2))
    return SVG_CHUNKS.sub('</svg>\n',text)


def update_preview(path,entries,summary):
    """Caption the planner's preview with what its plates actually cost to print.

    The band is a strip grown above matplotlib's canvas rather than an overlay, so
    it covers no map, and it carries the untouched root size, which is what lets a
    rerun replace the caption rather than add a second one.
    """
    text=strip_band(path.read_text(encoding='utf-8'))
    height,view=svg_size(text)
    box=view.split()
    lines=band_lines(summary)
    font=max(7.,float(box[2])/80)
    band=PREVIEW_PAD+font*1.5*len(lines)
    rows=''.join(f'<text x="{PREVIEW_PAD:g}" y="{PREVIEW_PAD/2+font*1.5*(index+1):.2f}" '
        f'font-size="{font:.2f}" font-family="{PREVIEW_FONTS}" fill="#1A1A1A">{line}</text>'
        for index,line in enumerate(lines))
    markup=(f'<g id="print-stats" data-svg-height="{height}" data-svg-view="{view}">'
        f'<rect x="0" y="0" width="{box[2]}" height="{band:.2f}" fill="#FFFFFF"/>{rows}</g>\n'
        f'<g id="print-stats-shift" transform="translate(0 {band:.2f})">\n')
    text,chunks,captioned,missing=chunk_markup(text,entries,band)
    start=text.find('<g id="figure_1">')
    if start<0:start=SVG_OPEN.search(text).end()
    close=text.rindex('</svg>')
    text=text[:start]+markup+text[start:close]+'</g>\n'+chunks+text[close:]
    grown=re.sub(r'^-?[\d.]+',lambda m:f'{float(m.group(0))+band:.6f}',height)
    view=' '.join(box[:3]+[f'{float(box[3])+band:.6f}'])
    path.write_text(set_svg_size(text,grown,view),encoding='utf-8')
    return captioned,missing


def gram(value,width=6):
    return f"{'-':>{width}}" if value is None else f'{value:{width}.1f}'


def filament_table(filaments,width,lengths):
    """The per-filament block both reports print, one plate's or a whole batch's."""
    head=(f"  {'#':<2} {'colour':<9} {'role':<10} {'model':>{width}} {'purge':>{width}} "
        f"{'total':>{width}}")
    print(f"{head} {'m':>7} {'cost':>7}" if lengths else f"{head} {'cost':>7}")
    for filament in filaments:
        row=(f"  {filament['extruder']:<2} {filament['colour'] or '?':<9} "
            f"{filament['label'][:10]:<10} {gram(filament['model_g'],width)} "
            f"{gram(filament['purge_g'],width)} {gram(filament['total_g'],width)}")
        if lengths:
            length=filament['length_m']
            row+='       -' if length is None else f' {length:7.1f}'
        cost=filament['cost_usd']
        print(row+('      -' if cost is None else f' ${cost:6.2f}'))
    totals=[sum(filament[key] or 0. for filament in filaments)
        for key in ('model_g','purge_g','total_g')]
    row=(f"  {'':<2} {'':<9} {'total':<10} {gram(totals[0],width)} {gram(totals[1],width)} "
        f"{gram(totals[2],width)}")
    if lengths:row+=f" {'':>7}"
    print(f"{row} ${sum(filament['cost_usd'] or 0. for filament in filaments):6.2f}")
    return totals


def report_models(entries):
    """One table per model, with the machine and the time its own slice reported."""
    for entry in entries:
        head=[Path(entry['model']).name]
        if entry['printer']:head.append(entry['printer'])
        if entry['nozzle_mm']:head.append(f"{entry['nozzle_mm']} mm nozzle")
        if entry['layer_mm']:head.append(f"{entry['layer_mm']} mm layers")
        if entry['layers']:head.append(f"{entry['layers']} layers")
        print('\n'+' | '.join(head))
        print(f"  source: {entry['source']}"+(' (reused)' if entry['reused_slice'] else ''))
        filament_table(entry['filaments'],6,True)
        time_note=f"  print time: {format_duration(entry['seconds'])}"
        if entry['model_seconds']:time_note+=f" (model {format_duration(entry['model_seconds'])})"
        if entry['flush_seconds']:
            time_note+=f", {format_duration(entry['flush_seconds'])} purging"
        if entry['filament_changes']:time_note+=f", {entry['filament_changes']} filament changes"
        print(time_note)
        for warning in entry['warnings']:print(f'  slicer warning: {warning}')


def report_summary(summary):
    """The batch as one print: per-filament totals, the purge share and the time spread."""
    plates='' if summary['plates']==summary['models'] else f" on {summary['plates']} plates"
    print(f"\n{summary['models']} models{plates} | "
        f"{format_duration(summary['seconds'])} total print time")
    totals=filament_table(summary['filaments'],8,False)
    if totals[2]:
        print(f"  {'':<2} {'':<9} {'purge share':<10} {totals[1]/totals[2]*100:8.1f}%"
            f" ({totals[2]/SPOOL_G:.2f} spools of 1 kg extruded)")
    spread=summary['model_seconds']
    if spread and summary['models']>1:
        print(f"  per-model time: min {format_duration(spread['min'])} | "
            f"mean {format_duration(spread['mean'])} | max {format_duration(spread['max'])}")
    loose=summary['unattributed']
    if loose['entries']:
        plural='entry lacks' if loose['entries']==1 else 'entries lack'
        print(f"  unattributed: {loose['grams']:.1f} g in {loose['entries']} filament {plural} "
            f"a model/purge split ({', '.join(loose['models'])})")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('models',nargs='+',type=Path,help='3MF files to estimate')
    parser.add_argument('--replace',action='store_true',
        help='Reslice even when a sliced project beside the model matches')
    parser.add_argument('--force-slice',action='store_true',
        help='Slice locally even if the 3MF already carries slice metadata')
    parser.add_argument('--slicer',type=Path,default=SLICER)
    parser.add_argument('--summary-only',action='store_true',
        help='Print the batch totals alone, without a table per model')
    parser.add_argument('--json',type=Path,help='Also write the estimate as JSON')
    parser.add_argument('--update-preview',type=Path,
        help="Caption a planner preview SVG with this batch's print time and filament weight")
    args=parser.parse_args()
    if not args.slicer.exists():
        raise SystemExit(f'Bambu Studio executable is missing: {args.slicer}')
    if args.update_preview and not args.update_preview.exists():
        raise SystemExit(f'Preview is missing: {args.update_preview}')
    entries=collect_all(args.models,args.replace,args.force_slice,args.slicer)
    summary=aggregate(entries)
    if not args.summary_only:report_models(entries)
    if args.summary_only or len(entries)>1:report_summary(summary)
    if args.json:
        args.json.write_text(json.dumps({'entries':entries,'summary':summary},indent=2)+'\n')
    if args.update_preview:
        captioned,missing=update_preview(args.update_preview,entries,summary)
        print(f'\npreview captioned: {args.update_preview} '
            f'({len(captioned)} of {len(entries)} models on a labelled plate)')
        if missing:print(f"  no plate labelled: {', '.join(missing)}")


if __name__=='__main__':main()
