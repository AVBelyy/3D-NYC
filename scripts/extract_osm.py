"""Extract OSM detail layers, preferring the citywide spatial cache."""
import json
import os
import time
from pathlib import Path

import geopandas as gpd
import osmium
import shapely
from shapely.geometry import Point
from map_common import RAW,OUT,AOI,CACHE_DIR,write_json
from cache_common import read_tiled_geoparquet


KEYS = ('highway','building','building:part','natural','leisure','landuse',
        'amenity','barrier','man_made','historic','railway')
AREA_KEYS = frozenset(('building','building:part','natural','leisure','landuse',
                       'amenity','man_made','historic'))
LINE_KEYS = frozenset(('highway','barrier','railway'))


def main():
    cache = CACHE_DIR / "new_york_osm"
    manifest_path = cache / "manifest.json"
    if manifest_path.is_file() and (cache / "catalog.geojson").is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("status") != "complete" or manifest.get("production_ready") is False:
            raise RuntimeError(f"OSM cache is not production-ready: {manifest_path}")
        g = read_tiled_geoparquet(
            cache, tuple(AOI.bounds),
            deduplicate_by=["source_order"], source_order=["source_order"],
        )
        g.to_parquet(OUT / "osm_detail.parquet")
        write_json(OUT / "osm_detail_profile.json", {
            "source": "cache",
            "cache": str(cache),
            "rows": len(g),
            "types": g.geom_type.value_counts().to_dict(),
            "errors": [],
            "leisure": g.leisure.value_counts().to_dict() if "leisure" in g else {},
            "amenity": g.amenity.value_counts().to_dict() if "amenity" in g else {},
        })
        print(f"PROGRESS completed source=cache retained_features={len(g)}", flush=True)
        return
    aoi=gpd.GeoSeries([AOI],crs=2263).to_crs(4326).iloc[0]
    xmin,ymin,xmax,ymax=aoi.bounds
    source=RAW/'new_york_osm/new-york-latest.osm.pbf'
    if not source.exists():raise FileNotFoundError(f'OSM source is missing: {source}')
    proc=osmium.FileProcessor(source).with_locations().with_areas().with_filter(osmium.filter.KeyFilter(*KEYS))
    f=osmium.geom.WKBFactory()
    prepared_aoi=shapely.prepared.prep(aoi)
    rows=[]
    errors=[]
    seen=0
    checkpoint=time.monotonic()
    for obj in proc:
        seen+=1
        # Keep the progress check out of the per-object hot path. At the
        # observed scan rate this adds well below a second of reporting lag.
        if seen & 8191 == 0 and time.monotonic()-checkpoint>=30:
            print(f'PROGRESS scanned_objects={seen} retained_features={len(rows)} geometry_errors={len(errors)}',flush=True)
            checkpoint=time.monotonic()
        try:
            if obj.is_node():
                lon,lat=obj.location.lon,obj.location.lat
                if not (xmin<=lon<=xmax and ymin<=lat<=ymax):continue
                geom=Point(lon,lat)
                if not prepared_aoi.contains(geom):continue
                typ='node';oid=obj.id
            elif obj.is_area():
                if not any(k in obj.tags for k in AREA_KEYS):continue
                geom=shapely.from_wkb(f.create_multipolygon(obj));typ='way' if obj.from_way() else 'relation';oid=obj.orig_id()
            elif obj.is_way():
                if not any(k in obj.tags for k in LINE_KEYS) and obj.tags.get('natural')!='tree_row':continue
                geom=shapely.from_wkb(f.create_linestring(obj));typ='way';oid=obj.id
            else:continue
            if not prepared_aoi.intersects(geom):continue
            # Most objects passing KeyFilter are discarded by the type-specific
            # checks above. Delay this allocation until the feature is retained.
            tags=dict(obj.tags)
            rows.append({'osm_type':typ,'osm_id':oid,'tags':json.dumps(tags),'geometry':geom,
                **{k:tags.get(k) for k in KEYS+('name','width','bridge','tunnel','layer','surface','sport','area')}})
        except (RuntimeError,ValueError) as e:
            errors.append({'id':obj.id,'type':type(obj).__name__,'error':str(e)})
    columns=['osm_type','osm_id','tags',*KEYS,'name','width','bridge','tunnel','layer','surface','sport','area']
    if rows:g=gpd.GeoDataFrame(rows,crs=4326).to_crs(2263)
    else:
        data={column:[] for column in columns}
        g=gpd.GeoDataFrame(data,geometry=gpd.GeoSeries([],crs=4326),crs=4326).to_crs(2263)
    g.to_parquet(OUT/'osm_detail.parquet')
    write_json(OUT/'osm_detail_profile.json',{'rows':len(g),'types':g.geom_type.value_counts().to_dict(),'errors':errors,
        'leisure':g.leisure.value_counts().to_dict(),'amenity':g.amenity.value_counts().to_dict()})
    print(f'PROGRESS completed scanned_objects={seen} retained_features={len(g)} geometry_errors={len(errors)}',flush=True)

if __name__=='__main__':main()
