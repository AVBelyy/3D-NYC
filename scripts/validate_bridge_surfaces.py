"""Regression check for bridge decks overwritten by terrain or crossing voids."""
import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
import trimesh
from shapely.ops import substring

from map_common import H, OUT, STEP, local, write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mesh-dir',type=Path,default=OUT/'mesh')
    parser.add_argument('--fields-only',action='store_true')
    parser.add_argument('--height-tolerance-mm',type=float,default=.20)
    parser.add_argument('--report',type=Path,default=OUT/'bridge_surface_validation.json')
    args=parser.parse_args()
    fields=np.load(OUT/'map_fields.npz');material=fields['material'];height=fields['height_mm']
    bridge_path=OUT/'bridges.parquet'
    bridges=gpd.read_parquet(bridge_path) if bridge_path.exists() else gpd.GeoDataFrame()
    segments=[]
    for _,row in bridges.iterrows():
        line=local(row.geometry)
        if line.geom_type!='LineString':continue
        segments.append((line,{'osm_id':int(row.osm_id),'kind':'tagged bridge'}))
    # An ordinary surface road over an explicitly mapped tunnel need not carry
    # bridge=yes. These include the West Drive gaps in the user's screenshots.
    osm_path=OUT/'osm_detail.parquet';tunnel_path=OUT/'tunnels.parquet'
    upper_crossings=0
    if osm_path.exists() and tunnel_path.exists():
        osm=gpd.read_parquet(osm_path)
        surface=osm[osm.highway.notna()&osm.geom_type.eq('LineString')]
        for _,tunnel in gpd.read_parquet(tunnel_path).iterrows():
            lower=local(tunnel.geometry)
            if lower.geom_type!='LineString' or lower.length<=.1:continue
            interior=substring(lower,.05,lower.length-.05)
            for _,upper in surface[surface.intersects(tunnel.geometry)].iterrows():
                tags=json.loads(upper.tags) if isinstance(upper.tags,str) else {}
                if tags.get('tunnel') not in [None,'no']:continue
                line=local(upper.geometry)
                crossing=line.intersection(interior)
                if crossing.is_empty or not any(p.geom_type=='Point' for p in shapely.get_parts(crossing)):continue
                selected=line.intersection(lower.buffer(float(tunnel.width_mm)/2+.10))
                for part in shapely.get_parts(selected):
                    if part.geom_type!='LineString' or part.length<.1:continue
                    segments.append((part,{'osm_id':int(upper.osm_id),'kind':'surface route over tunnel',
                        'lower_tunnel_osm_id':int(tunnel.osm_id),'name':str(upper['name'])}))
                    upper_crossings+=1
    points=[];expected=[];ids=[];colors=[]
    for segment_id,(line,metadata) in enumerate(segments):
        inset=min(.125,line.length*.1)
        for distance in np.linspace(inset,line.length-inset,max(3,int(line.length/.125))):
            point=line.interpolate(distance);r=int((H-point.y)/STEP);c=int(point.x/STEP)
            if not(0<=r<material.shape[0] and 0<=c<material.shape[1]):continue
            points.append([point.x,point.y,float(height.max())+1])
            expected.append(float(height[r,c]));ids.append(segment_id);colors.append(int(material[r,c]))
    ids=np.asarray(ids);colors=np.asarray(colors);expected=np.asarray(expected)
    deficit=np.zeros(len(points))
    if points and not args.fields_only:
        roads=trimesh.util.concatenate([trimesh.load(args.mesh_dir/f'material_{i}.ply',process=False)
            for i in [0,3] if (args.mesh_dir/f'material_{i}.ply').exists()])
        directions=np.tile([0.,0.,-1.],(len(points),1))
        locations,rays,_=roads.ray.intersects_location(np.asarray(points),directions,multiple_hits=True)
        actual=np.full(len(points),-np.inf);np.maximum.at(actual,rays,locations[:,2])
        deficit=expected-actual
    records=[]
    for segment_id in sorted(set(ids)):
        selected=ids==segment_id
        records.append({**segments[segment_id][1],'samples':int(selected.sum()),
            'non_road_field_samples':int((~np.isin(colors[selected],[0,3])).sum()),
            'missing_upper_surface_samples':int((deficit[selected]>args.height_tolerance_mm).sum()),
            'maximum_surface_deficit_mm':float(max(0.,deficit[selected].max()))})
    failed=[r for r in records if r['non_road_field_samples'] or r['missing_upper_surface_samples']]
    report={'bridge_segments':len(bridges),'surface_over_tunnel_segments':upper_crossings,
        'checked_segments':len(records),'samples':len(points),'fields_only':args.fields_only,
        'height_tolerance_mm':args.height_tolerance_mm,'bridges':records,
        'result':'failed' if failed else 'passed'}
    write_json(args.report,report)
    print(f"Bridge surface continuity: {report['result']}; {len(records)} segments, {len(points)} samples")
    if failed:
        for item in failed:print(item)
        raise SystemExit(1)


if __name__=='__main__':main()
