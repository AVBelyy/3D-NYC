"""Aggregate filament use and print time across a set of finished 3MF chunks."""
import argparse,json,statistics
from pathlib import Path
from slice_3mf import SLICER
from estimate_print_stats import collect_all,format_duration,SPOOL_G


def aggregate(entries):
    """Fold per-model estimates into per-extruder totals and a print-time spread."""
    filaments={}
    unattributed={'grams':0.,'entries':0,'models':set()}
    for entry in entries:
        for filament in entry['filaments']:
            slot=filaments.setdefault(filament['extruder'],
                {'extruder':filament['extruder'],'label':filament['label'],
                'colour':filament['colour'],'total_g':0.,'model_g':0.,'purge_g':0.,'cost_usd':0.})
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
        'models':len(entries),'seconds':sum(times),
        'chunk_seconds':{'min':min(times),'mean':statistics.fmean(times),'max':max(times)} if times else {}}


def report(summary):
    """Print the per-filament table and the print-time spread."""
    print(f"\n{summary['models']} chunks | {format_duration(summary['seconds'])} total print time")
    print(f"  {'#':<2} {'colour':<9} {'role':<10} {'model':>8} {'purge':>8} {'total':>8} {'cost':>8}")
    for filament in summary['filaments']:
        print(f"  {filament['extruder']:<2} {filament['colour'] or '?':<9} {filament['label'][:10]:<10} "
            f"{filament['model_g']:8.1f} {filament['purge_g']:8.1f} {filament['total_g']:8.1f} "
            f"${filament['cost_usd']:7.2f}")
    keys=('model_g','purge_g','total_g')
    totals=[sum(f[key] for f in summary['filaments']) for key in keys]
    cost=sum(f['cost_usd'] for f in summary['filaments'])
    print(f"  {'':<2} {'':<9} {'total':<10} {totals[0]:8.1f} {totals[1]:8.1f} {totals[2]:8.1f} ${cost:7.2f}")
    if totals[2]:
        print(f"  {'':<2} {'':<9} {'purge share':<10} {totals[1]/totals[2]*100:7.1f}%"
            f"  ({totals[2]/SPOOL_G:.2f} spools of 1 kg extruded)")
    spread=summary['chunk_seconds']
    if spread:
        print(f"  per-chunk time: min {format_duration(spread['min'])} | "
            f"mean {format_duration(spread['mean'])} | max {format_duration(spread['max'])}")
    loose=summary['unattributed']
    if loose['entries']:
        plural='entry lacks' if loose['entries']==1 else 'entries lack'
        print(f"  unattributed: {loose['grams']:.1f} g in {loose['entries']} filament {plural} "
            f"a model/purge split ({', '.join(loose['models'])})")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('models',nargs='+',type=Path,help='3MF files to summarize')
    parser.add_argument('--replace',action='store_true',
        help='Reslice even when a sliced project beside the model matches')
    parser.add_argument('--force-slice',action='store_true',
        help='Slice locally even if the 3MF already carries slice metadata')
    parser.add_argument('--slicer',type=Path,default=SLICER)
    parser.add_argument('--json',type=Path,help='Also write the summary as JSON')
    args=parser.parse_args()
    if not args.slicer.exists():
        raise SystemExit(f'Bambu Studio executable is missing: {args.slicer}')
    entries=collect_all(args.models,args.replace,args.force_slice,args.slicer)
    summary=aggregate(entries)
    report(summary)
    if args.json:args.json.write_text(json.dumps(summary,indent=2)+'\n')


if __name__=='__main__':main()
