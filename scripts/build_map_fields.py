"""Fuse measured map layers into a printable relief and explicit material regions.

Rasterization at 0.125 mm is a manufacturing discretization, not source precision.
Separate underpass solids are constructed later; their footprints are kept here.
"""
import json,math
import numpy as np,pandas as pd,geopandas as gpd,shapely
import rasterio
from affine import Affine
from shapely.geometry import box,Polygon,Point
from rasterio.features import geometry_mask,geometry_window,rasterize
from rasterio.warp import Resampling
from scipy.ndimage import distance_transform_edt,gaussian_filter,binary_dilation
import mapbox_earcut
from map_common import *
from _canopy_relief import measured_canopy_relief
from crossings import carried_structures,fit_linear_elevation_profile,minimum_crossing_length_mm,tunnel_surface_masks
from _material_layers import (FIRST_LAYER_HEIGHT_MM,MATERIAL_NAMES,drawn_line_relief_mm,
    printable_surface_mm,surface_color_depth_mm,white_substrate_top)
from road_symbols import TRAIL_HIGHWAYS,constructs_bridge_deck,drawn_route_classification,parse_tags,trail_width_mm
from _surface_styles import LAND_COVER_BARE_SOIL,apply_street_palette,paint_bridge_decks,paint_trail_ribbons,stair_tread_mask,vegetated_ground_mask
from terrain_relief import absolute_elevation_to_mm,choose_terrain_relief

def burn(shapes,dtype='float32',fill=0):
    shapes=[(shapely.make_valid(g.intersection(AOI)),v) for g,v in shapes if not g.is_empty and g.intersects(AOI)]
    return rasterize(shapes,out_shape=SHAPE,transform=TRANSFORM,fill=fill,dtype=dtype) if shapes else np.full(SHAPE,fill,dtype=dtype)

def geom_mask(g):return burn([(g,1)],dtype='uint8').astype(bool)

def semantic_number(row,key,default=0.):
    if row is None:return float(default)
    value=pd.to_numeric(row.get(key,np.nan),errors='coerce')
    return float(value) if np.isfinite(value) else float(default)

def native_raster_values(name,geom):
    """Read finite source-resolution raster cells inside one small geometry."""
    path=PROCESSED/f'rasters/{name}.tif'
    with rasterio.open(path) as src:
        try:window=geometry_window(src,[geom],pad_x=1,pad_y=1)
        except rasterio.errors.WindowError:return np.asarray([],dtype=np.float32)
        values=src.read(1,window=window)
        mask=geometry_mask([geom],out_shape=values.shape,transform=src.window_transform(window),
            invert=True,all_touched=True)
    result=values[mask]
    return result[np.isfinite(result)]

def sample_raster_halo(name,padding_cells,resampling=Resampling.bilinear):
    """Sample beyond the print frame so neighborhood filters agree at shared seams."""
    padding_cells=int(padding_cells)
    if padding_cells<1:raise ValueError('Raster halo must contain at least one cell')
    path=PROCESSED/f'rasters/{name}.tif'
    halo_shape=(NY+2*padding_cells,NX+2*padding_cells)
    halo_transform=TRANSFORM*Affine.translation(-padding_cells,-padding_cells)
    with rasterio.open(path) as src:
        out=np.full(halo_shape,np.nan,np.float32)
        reproject(rasterio.band(src,1),out,src_transform=src.transform,src_crs=src.crs,
            dst_transform=halo_transform,dst_crs=2263,resampling=resampling,dst_nodata=np.nan)
    return out

# Open space the city neither owns as a park nor resurveys often: institutional
# lawns and quadrangles.  The 2017 land cover calls many of them impervious, and
# they carry no planimetric PARK polygon, so without a semantic fallback they
# reach the ivory grid default and print as blank slabs at ground level.
GREEN_FALLBACK_LEISURE=['garden','dog_park','park']
GREEN_FALLBACK_LANDUSE=['grass','recreation_ground']

def tag_values(tags,key):
    """Split one OSM tag into its semicolon-separated alternative values."""
    value=tags.get(key)
    return [part.strip() for part in value.split(';') if part.strip()] if isinstance(value,str) else []

def recreation_material(tags):
    """Return the semantic material for an explicitly mapped recreation area."""
    for leisure in tag_values(tags,'leisure'):
        if leisure in ['playground','pitch','track']:return 1
    return None

def green_fallback_kind(tags):
    """Identify mapped green/open space that supplements stale land cover."""
    for leisure in tag_values(tags,'leisure'):
        if leisure in GREEN_FALLBACK_LEISURE:return leisure
    for landuse in tag_values(tags,'landuse'):
        if landuse in GREEN_FALLBACK_LANDUSE:return landuse
    return None

def is_surface_parking(tags):
    """Select open parking polygons while excluding structured/covered parking."""
    if tags.get('amenity')!='parking' or tags.get('building'):return False
    if tags.get('parking') in ['underground','multi-storey','rooftop','garage_boxes']:return False
    if tags.get('covered')=='yes' or tags.get('location') in ['underground','indoor','rooftop']:return False
    return True

def near_values(geom,array,buffer_m=3):
    coords=shapely.get_coordinates(geom.boundary.segmentize(buffer_m/FT))
    rr,cc=cells_for_points(coords[:,0],coords[:,1]);good=(rr>=0)&(rr<NY)&(cc>=0)&(cc<NX)
    a=array[rr[good],cc[good]];return a[np.isfinite(a)]

def small_mask(geom):
    x0,y0,x1,y1=local(geom).bounds
    c0=max(0,int(x0/STEP)-1);c1=min(NX,int(x1/STEP)+2)
    r0=max(0,int((H-y1)/STEP)-1);r1=min(NY,int((H-y0)/STEP)+2)
    if r1<=r0 or c1<=c0:return None
    t=TRANSFORM*Affine.translation(c0,r0)
    a=rasterize([(shapely.make_valid(geom),1)],out_shape=(r1-r0,c1-c0),transform=t,dtype='uint8').astype(bool)
    return (slice(r0,r1),slice(c0,c1)),a

def triangles_3d(poly):
    for part in shapely.get_parts(poly):
        if part.geom_type!='Polygon':continue
        rings=[np.asarray(part.exterior.coords)[:-1]]+[np.asarray(r.coords)[:-1] for r in part.interiors]
        rings=[r for r in rings if len(r)>=3]
        if not rings:continue
        v=np.concatenate(rings);ends=np.cumsum([len(r) for r in rings]).astype(np.uint32)
        try:f=mapbox_earcut.triangulate_float64(np.ascontiguousarray(v[:,:2]),ends).reshape(-1,3)
        except ValueError:continue
        for tri in v[f]:yield tri

def main():
    vertical=CFG.get('vertical_exaggeration',1.)
    mm_to_source=lambda value:value*CFG['scale_denominator']/(1000*vertical)
    source_to_mm=lambda value:value*1000/CFG['scale_denominator']*vertical
    # A pavement pad and a drawn line are different surfaces. Sidewalks, roadbeds,
    # plazas and parking sit on the pad; carriageway and trail lines stand above it
    # so a road reads as one continuous raised line through every junction.
    pad_relief=float(CFG['path_relief_mm']);line_relief=drawn_line_relief_mm(CFG)
    report={'config':CFG,'bounds_epsg2263_ft':BOUNDS,'grid_shape':SHAPE,'layers':{},'inferences':[]}
    aoi_mask=geom_mask(AOI)
    if not aoi_mask.any():raise RuntimeError('Bounding polygon does not cover any manufacturing-grid cells')
    b=read('buildings_projected');b['geometry']=shapely.make_valid(b.geometry)
    if 'material_override' not in b:b['material_override']=-1
    building_materials=pd.to_numeric(b.material_override,errors='coerce').fillna(-1).astype(int).to_numpy()
    if not np.isin(building_materials,[-1,0,1,2,3]).all():
        raise RuntimeError('Building material overrides must be -1 or one of the four configured material indices')
    city=read('citygml_buildings');roofs=read('citygml_surfaces');roofs=roofs[roofs.kind.eq('roof')].copy()
    osm=gpd.read_parquet(OUT/'osm_detail.parquet')
    ground_sigma=.65/(STEP*CFG['scale_denominator']/1000)
    canopy_sigma=CFG.get('canopy_smoothing_m',1.)/(STEP*CFG['scale_denominator']/1000)
    canopy_gap_cells=int(math.ceil(CFG.get('canopy_maximum_closed_gap_mm',.75)/(2*STEP)))+2
    ground_halo_cells=max(2,int(math.ceil(4*ground_sigma)),int(math.ceil(4*canopy_sigma)),canopy_gap_cells)
    ground_halo=sample_raster_halo('ground_m',ground_halo_cells)
    upper_halo=sample_raster_halo('upper_surface_m',ground_halo_cells)
    lc_halo=sample_raster_halo('landcover',ground_halo_cells,Resampling.nearest)
    valid_halo=np.isfinite(ground_halo)
    if not valid_halo.any():raise RuntimeError('Padded terrain sample contains no finite ground elevations')
    idx=distance_transform_edt(~valid_halo,return_distances=False,return_indices=True)
    ground_halo[~valid_halo]=ground_halo[tuple(idx[:,~valid_halo])];del idx
    ground_halo=gaussian_filter(ground_halo,ground_sigma)
    core=(slice(ground_halo_cells,ground_halo_cells+NY),slice(ground_halo_cells,ground_halo_cells+NX))
    ground=ground_halo[core].copy()
    upper=upper_halo[core].copy()
    lc=lc_halo[core].copy()
    valid=valid_halo[core]
    del valid_halo
    # No-data under buildings/water must be filled to create a solid. Source observation distances remain available.
    report['inferences'].append({'kind':'ground_fill','cells':int((~valid).sum()),'method':'nearest finite terrain for solid interior; water levels overridden separately'})
    report['inferences'].append({'kind':'ground_smoothing_halo','cells':ground_halo_cells,
        'source_metres':ground_halo_cells*STEP*CFG['scale_denominator']/1000,
        'method':'four-sigma padded sampling keeps adjacent shared-grid filters consistent'})
    parks=read('planimetrics_PARK');park_mask=burn([(g,1) for g in parks.geometry],dtype='uint8')>0
    material=np.zeros(SHAPE,np.uint8)
    # Classify on the padded sample so a field straddling the tile edge is
    # judged on the same evidence in both neighbouring chunks.
    green_ground=vegetated_ground_mask(lc_halo)[core]
    material[aoi_mask&(park_mask|green_ground)]=1
    report['inferences'].append({'kind':'bare_soil_in_vegetated_ground',
        'cells':int((aoi_mask&green_ground&(lc==LAND_COVER_BARE_SOIL)).sum()),
        'method':'unvegetated ground continuous with a mostly vegetated land-cover region is the same field'})
    top=ground.copy()
    # Most surface treatments are ground-relative and must retain their fixed
    # printable height. Bridges and stairs instead follow an absolute surveyed
    # elevation profile, so record those cells for the terrain-only transform.
    absolute_terrain_surface_mask=np.zeros(SHAPE,dtype=bool)
    elev=read('planimetrics_ELEVATION');water_elev=elev[elev.SUB_FEATURE_CODE.eq(301000)]
    road_spot_elev=elev[elev.SUB_FEATURE_CODE.eq(300000)]
    bridge_spot_elev=elev[elev.SUB_FEATURE_CODE.eq(300020)]
    water=[]
    # Current detailed small water features supplement planimetric lake and pond outlines.
    for _,r in osm[osm.natural.eq('water') & osm.geom_type.isin(['Polygon','MultiPolygon'])].iterrows():
        water.append((r.geometry,str(r['name']),'OSM'))
    for _,r in read('planimetrics_HYDROGRAPHY').iterrows():water.append((r.geometry,str(r.NAME),'Planimetrics'))
    water_records=[];water_mask=np.zeros(SHAPE,dtype=bool)
    for geom,name,source in water:
        mask=geom_mask(geom)
        water_mask|=mask
        points=water_elev[water_elev.within(geom.buffer(3/FT))]
        if len(points):level=float(points.ELEVATION.median()*FT);method='planimetric water elevation'
        else:
            edge=near_values(geom,ground);level=float(np.percentile(edge,15)) if len(edge) else float(np.nanmedian(ground[mask]));method='inferred from nearby terrain'
        material[mask]=2;ground[mask]=level;top[mask]=level
        water_records.append({'name':name,'source':source,'level_m_navd88':level,'method':method,'cells':int(mask.sum())})
    report['layers']['water']=water_records
    print('Ground and water',flush=True)
    # Explicit gardens, dog runs and grass polygons supplement older land-cover
    # data, but remain below mapped paths and pavement in the styling priority.
    # Playgrounds, pitches and tracks are semantic recreation green regardless
    # of surface; collect them for a higher-priority pass after road/path styling.
    recreation=[];green_fallback=[];osm_parking=[]
    for _,r in osm[osm.geom_type.isin(['Polygon','MultiPolygon'])].iterrows():
        tags=json.loads(r.tags)
        selected=recreation_material(tags)
        if selected is not None:recreation.append((r,tags,selected))
        fallback=green_fallback_kind(tags)
        if fallback is not None:green_fallback.append((r,tags,fallback))
        if is_surface_parking(tags):osm_parking.append((r,tags))
    fallback_green_mask=np.zeros(SHAPE,dtype=bool);fallback_records=[]
    for r,tags,kind in green_fallback:
        mask=geom_mask(r.geometry);fallback_green_mask|=mask
        fallback_records.append({'osm_id':int(r.osm_id),'kind':kind,'cells':int(mask.sum()),
            'method':'OSM semantic green fallback below paths and paved areas'})
    material[fallback_green_mask&~water_mask]=1
    report['layers']['semantic_green_fallbacks']=fallback_records
    recreation_records=[];recreation_green_mask=np.zeros(SHAPE,dtype=bool)
    for r,tags,selected in recreation:
        mask=geom_mask(r.geometry)
        recreation_green_mask|=mask
        recreation_records.append({'osm_id':int(r.osm_id),'leisure':tags.get('leisure'),
            'surface':tags.get('surface'),'material':int(selected),'cells':int(mask.sum()),
            'method':'OSM semantic recreation green regardless of surface'})
    report['layers']['recreation_areas']=recreation_records
    roadbed=read('planimetrics_ROADBED')
    roadbed=roadbed[roadbed.SUB_FEATURE_CODE.isin([350000,350010,350030])]
    roadbed_mask=burn([(geom,1) for geom in roadbed.geometry],dtype='uint8')>0
    sidewalks=read('planimetrics_SIDEWALK')
    sidewalk_mask=burn([(geom,1) for geom in sidewalks.geometry],dtype='uint8')>0
    tan_pavement=[]
    for name in ['PLAZA','PARKING_LOT']:
        for geom in read('planimetrics_'+name).geometry:tan_pavement.append((geom,1))
    tan_pavement_mask=burn(tan_pavement,dtype='uint8')>0
    city_paved_mask=roadbed_mask|sidewalk_mask|tan_pavement_mask
    osm_parking_mask=burn([(r.geometry,1) for r,_ in osm_parking],dtype='uint8')>0
    osm_parking_fallback_mask=osm_parking_mask&~city_paved_mask
    # Ivory is a fixed-width cartographic centerline, not the full roadbed.
    # Outside parks, the remaining measured roadbed shares the tan sidewalk
    # field; inside parks it retains green terrain, matching the reference map.
    road_surface_path=PROCESSED/'ivory_road_surface.parquet'
    symbol_road_mask=np.zeros(SHAPE,dtype=bool)
    if road_surface_path.exists() and CFG.get('ivory_carriageways',False):
        road_surfaces=gpd.read_parquet(road_surface_path)
        if len(road_surfaces):symbol_road_mask=burn([(g,1) for g in road_surfaces.geometry],dtype='uint8')>0
        symbol_road_mask&=aoi_mask&~water_mask
    road_surface_mask=symbol_road_mask&aoi_mask&~water_mask
    sidewalk_mask&=aoi_mask&~water_mask
    tan_roadbed_mask=roadbed_mask&~park_mask
    tan_pavement_mask=(tan_pavement_mask|osm_parking_fallback_mask|tan_roadbed_mask)&aoi_mask&~water_mask
    street_report=apply_street_palette(material,top,ground,
        sidewalk_mask=sidewalk_mask,tan_pavement_mask=tan_pavement_mask,
        road_mask=road_surface_mask,relief_source=mm_to_source(pad_relief),
        line_relief_source=mm_to_source(line_relief))
    roadmask=roadbed_mask|road_surface_mask|sidewalk_mask|tan_pavement_mask
    report['layers']['street_palette']={**street_report,
        'roadbed_polygons':len(roadbed),'sidewalk_polygons':len(sidewalks),
        'ivory_ribbon_cells':int(road_surface_mask.sum()),
        'tan_roadbed_cells':int(tan_roadbed_mask.sum()),
        'pavement_relief_mm':pad_relief,'drawn_line_relief_mm':line_relief,
        'style':'raised ivory road lines; tan urban roadbeds and sidewalks; green park shoulders'}
    report['layers']['osm_surface_parking']={'polygons':len(osm_parking),
        'fallback_cells':int(osm_parking_fallback_mask.sum()),
        'method':'tan open OSM parking only outside NYC Planimetrics paved coverage'}
    route_path=PROCESSED/'road_symbol_routes.parquet'
    routes=gpd.read_parquet(route_path) if route_path.exists() else gpd.GeoDataFrame()
    route_by_id={int(r.osm_id):r for _,r in routes.iterrows()}
    paths=[];tunnels=[];bridges=[];stairs=[];ivory_motor_bridges=[];ivory_deck_records=[]
    bridge_surface_mask=np.zeros(SHAPE,dtype=bool)
    highway=osm[osm.highway.notna() & osm.geom_type.eq('LineString')]
    for _,r in highway.iterrows():
        tags=parse_tags(r.tags);is_tunnel=tags.get('tunnel') in ['yes','building_passage']
        semantic=route_by_id.get(int(r.osm_id))
        classification=drawn_route_classification(semantic,r.highway)
        ivory_eligible=bool(semantic.ivory_eligible) if semantic is not None else False
        if classification=='trail':
            width_mm=trail_width_mm(r.highway,tags,CFG)
        else:
            width_mm=semantic_number(semantic,'surface_width_mm',CFG.get('road_line_width_mm',.5))
        geom=r.geometry.intersection(AOI)
        if is_tunnel and ivory_eligible and geom.length*K>=minimum_crossing_length_mm(CFG):
            tunnels.append({'geometry':geom,'osm_id':int(r.osm_id),'width_mm':width_mm,'name':str(r['name']),
                'ivory_eligible':True,
                'layer':str(tags.get('layer','-1'))})
            continue
        if is_tunnel:continue
        if classification=='trail':
            shape=geom.buffer(width_mm/2/K,quad_segs=4)
            paths.append((shape,1))
        if constructs_bridge_deck(tags,classification,geom.length*K,CFG):
            bridges.append({'geometry':geom,'osm_id':int(r.osm_id),'width_mm':width_mm,
                'name':str(r['name']),'ivory_eligible':ivory_eligible,
                'layer':str(tags.get('layer','1')),'bridge_tag':str(tags.get('bridge','yes'))})
        if r.highway=='steps':stairs.append({'geometry':geom,'width_mm':width_mm,'osm_id':int(r.osm_id)})
    # Use city trails only away from OSM path coverage, avoiding doubled parallel tracks.
    existing=shapely.union_all([g for g,_ in paths]) if paths else Polygon()
    trail_add=[]
    for geom in read('trails').geometry:
        part=geom.intersection(AOI).difference(existing.buffer(.1/K))
        if part.length*K>.6:trail_add.append((part.buffer(CFG['minimum_path_width_mm']/2/K,quad_segs=4),1))
    pathmask=burn(paths+trail_add,dtype='uint8')>0
    trail_report=paint_trail_ribbons(material,top,ground,trail_mask=pathmask,
        road_mask=road_surface_mask,line_relief_source=mm_to_source(line_relief))
    transport=read('planimetrics_TRANSPORT_STRUCTURE')

    def endpoint_road_elevation(point):
        """Prefer a surveyed road spot near a portal, checked against LiDAR ground."""
        rr,cc=cells_for_points([point.x],[point.y]);r=int(np.clip(rr[0],0,NY-1));c=int(np.clip(cc[0],0,NX-1))
        fallback=float(ground[r,c]);method='LiDAR ground at mapped portal'
        nearby=road_spot_elev[road_spot_elev.distance(point)<=12/FT].copy()
        if len(nearby):
            nearby['_distance']=nearby.distance(point)
            nearby['_elevation_m']=pd.to_numeric(nearby.ELEVATION,errors='coerce')*FT
            nearby=nearby[np.isfinite(nearby._elevation_m)&(np.abs(nearby._elevation_m-fallback)<=3.)]
            if len(nearby):
                value=float(nearby.nsmallest(3,'_distance')._elevation_m.median())
                return value,'planimetric road spot elevation checked against LiDAR ground'
        return fallback,method

    # Retain the source anchors and portal evidence with each hidden segment;
    # mesh construction may later add a separately reported print-only offset.
    for tunnel in tunnels:
        line=tunnel['geometry']
        if line.geom_type!='LineString' or line.is_empty:continue
        start,start_method=endpoint_road_elevation(Point(line.coords[0]))
        end,end_method=endpoint_road_elevation(Point(line.coords[-1]))
        portals=transport[transport.SUB_FEATURE_CODE.eq(231000)&transport.intersects(line.buffer(3/FT))]
        portal_z=shapely.get_coordinates(portals.geometry,include_z=True)[:,2]*FT if len(portals) else np.asarray([])
        tunnel.update({'road_start_elevation_m':start,'road_end_elevation_m':end,
            'road_profile_method':start_method if start_method==end_method else start_method+'; '+end_method,
            'portal_structure_count':int(len(portals)),
            'portal_roof_elevation_m':float(np.nanmedian(portal_z)) if len(portal_z) else np.nan})

    # A surveyed structure is claimed by proximity, so every way on a viaduct
    # matches the same deck polygon. Judge trail claims against the carriageway
    # bridges that could be carried by the same structure.
    carriageway_bridge_routes=routes[routes.ivory_eligible&routes.bridge] if len(routes) else routes
    carriageway_bridge_geometry=(shapely.union_all(carriageway_bridge_routes.geometry.values)
        if len(carriageway_bridge_routes) else Polygon())
    deck_paint=[]
    for bridge in bridges:
        candidates=transport[transport.SUB_FEATURE_CODE.isin([230000,233000,235000]) & transport.intersects(bridge['geometry'].buffer(2/FT))]
        # OSM bridge tagging can extend slightly beyond the surveyed
        # transport-structure polygon. Include the complete printable road
        # ribbon so neither approach loses its surface at a raster cell.
        approach_ribbon=bridge['geometry'].buffer(bridge['width_mm']/2/K,quad_segs=4).intersection(AOI)
        carried=carried_structures(candidates.geometry,bridge['geometry'],
            carriageway_bridge_geometry,is_carriageway=bridge['ivory_eligible'])
        if len(candidates):
            # Selection uses intersection, so a matched city feature may extend
            # far beyond this tile (long bridge decks are the common case).
            # Rasterization clips implicitly, but deck_geometry is also consumed
            # later as an explicit 3-D solid and therefore must be clipped here.
            structure_shape=shapely.union_all(shapely.make_valid(candidates.geometry)).intersection(AOI)
            # Every structure the way runs on is elevation evidence, but only
            # the ones it carries are its surface. A viaduct sidewalk sits at
            # the measured deck height while the roadway keeps the deck itself.
            claimed=(shapely.union_all(shapely.make_valid(candidates.geometry[carried])).intersection(AOI)
                if carried.any() else Polygon())
            shape=claimed.union(approach_ribbon)
            xyz=shapely.get_coordinates(candidates.geometry,include_z=True)
            measured=float(np.nanmedian(xyz[:,2])*FT)
            support=bridge_spot_elev[bridge_spot_elev.intersects(structure_shape.union(approach_ribbon).buffer(3/FT))]
            if len(support)>=2:
                profile=fit_linear_elevation_profile(bridge['geometry'],shapely.get_coordinates(support.geometry),
                    pd.to_numeric(support.ELEVATION,errors='coerce').to_numpy()*FT,measured,
                    'planimetric bridge elevation points')
            else:
                profile=fit_linear_elevation_profile(bridge['geometry'],xyz[:,:2],xyz[:,2]*FT,measured,
                    'planimetric transport-structure polygon Z')
            method=profile.method
        else:
            shape=approach_ribbon
            samples=near_values(shape,ground);measured=float(np.percentile(samples,80)) if len(samples) else float(np.median(ground))
            profile=fit_linear_elevation_profile(bridge['geometry'],[],[],measured,'inferred from approach terrain')
            method='inferred from approach terrain'
        mask=geom_mask(shape)
        if not mask.any():continue
        bridge_surface_mask|=mask
        deck_start=float(profile.start)
        deck_end=float(profile.end)
        rows,cols=np.where(mask);xx,yy=world_for_cells(rows,cols)
        along=shapely.line_locate_point(bridge['geometry'],shapely.points(xx,yy),normalized=True)
        deck_source_values=np.interp(np.asarray(along,dtype=float),[0.,1.],[deck_start,deck_end])
        deck=float(np.median(deck_source_values));beneath=float(np.percentile(ground[mask],10))
        deck_paint.append({'rows':rows,'cols':cols,
            'values':deck_source_values,'ivory':bridge['ivory_eligible']})
        absolute_terrain_surface_mask[mask]=True
        bridge.update({'deck_geometry':shape.wkt,'deck_elevation_m':deck,
            'deck_start_elevation_m':deck_start,'deck_end_elevation_m':deck_end,
            'deck_profile_samples':int(profile.samples),'deck_profile_rejected_outliers':int(profile.rejected_outliers),
            'beneath_elevation_m':beneath,'height_method':method,
            'transport_structure_subtypes':json.dumps(sorted(map(int,candidates.SUB_FEATURE_CODE.unique()))) if len(candidates) else '[]',
            'carried_structure_count':int(carried.sum()),
            'approach_ribbon_included':bool(carried.any())})
        if bridge['ivory_eligible']:
            ivory_motor_bridges.append({'osm_id':bridge['osm_id'],'name':bridge['name'],
                'width_mm':bridge['width_mm'],'deck_cells':int(mask.sum()),
                'method':'full measured bridge deck is ivory, matching the roadbed'})
            ivory_deck_records.append((ivory_motor_bridges[-1],deck_paint[-1]))
    deck_report=paint_bridge_decks(material,top,deck_paint,line_relief_source=mm_to_source(line_relief))
    # Report the ivory a deck actually keeps, not the cells it asked for: a
    # claim measured before the other decks are painted cannot detect a loss.
    for record,entry in ivory_deck_records:
        record['cells']=int((material[entry['rows'],entry['cols']]==0).sum())
    upper_tunnel_surface_mask=np.zeros(SHAPE,dtype=bool)
    for t in tunnels:
        mask=geom_mask(t['geometry'].buffer((t['width_mm']/2+.10)/K,quad_segs=4))
        # Remove a lower tunnel's accidental Planimetric roadbed from the park
        # surface, but never repaint an already mapped upper bridge deck. The
        # height reset must use exactly the same protected selection as the
        # material reset or it silently flattens a surviving upper road.
        restore,protected=tunnel_surface_masks(mask,park_mask,bridge_surface_mask,
            symbol_road_mask|pathmask)
        upper_tunnel_surface_mask|=protected
        material[restore]=1;top[restore]=ground[restore]
    protected_transport_surface=bridge_surface_mask|upper_tunnel_surface_mask
    hard_trail_eligible=int(routes[routes.highway.isin(TRAIL_HIGHWAYS)&routes.ivory_eligible].shape[0]) if len(routes) else 0
    if hard_trail_eligible:raise RuntimeError('A hard trail class was assigned an ivory road surface')
    report['layers']['ivory_carriageways']={'cells':int(road_surface_mask.sum()),
        'roadbed_cells':int(roadbed_mask.sum()),'centerline_symbol_cells':int(symbol_road_mask.sum()),
        'relief_above_surroundings_mm':line_relief-pad_relief,
        'relief_above_ground_mm':line_relief,
        'style':'fixed-width ivory cartographic road line raised above the pavement beside it',
        'categorical_trails_eligible':hard_trail_eligible}
    report['layers']['paths']={'osm_path_segments':len(paths),'supplementary_trail_segments':len(trail_add),'stairs':len(stairs),
        'mapped_bridge_segments':len(bridges),'mapped_road_tunnels':len(tunnels),'roadbed_polygons':len(roadbed),
        'ivory_motor_bridges':ivory_motor_bridges,'bridge_deck_painting':deck_report,
        'trail_ribbon_painting':trail_report,
        'protected_surface_cells_over_tunnels':int(upper_tunnel_surface_mask.sum())}
    print('Paths and surface regions',flush=True)
    # Roof heights are absolute elevations, not ground-relative scalar heights.
    bids=burn([(geom,i+1) for i,geom in enumerate(b.geometry)],dtype='int32')
    bmask=bids>0
    recreation_green=recreation_green_mask&~bmask&~water_mask&~protected_transport_surface
    material[recreation_green]=1
    report['layers']['recreation_green_cells']=int(recreation_green.sum())
    roof_grid=np.full(SHAPE,np.nan,np.float32)
    flat=roofs[(roofs.z_max_ft-roofs.z_min_ft)<.05].sort_values('z_max_ft')
    roof_grid=burn([(r.geometry,float(r.z_max_ft*FT)) for _,r in flat.iterrows()],fill=np.nan)
    ntri=0
    for _,r in roofs[(roofs.z_max_ft-roofs.z_min_ft)>=.05].iterrows():
        for tri in triangles_3d(r.geometry):
            xy=tri[:,:2];normal=np.cross(tri[1]-tri[0],tri[2]-tri[0])
            if abs(normal[2])<1e-6:continue
            poly=Polygon(xy)
            if poly.area<.05 or not poly.intersects(AOI):continue
            found=small_mask(poly)
            if found is None:continue
            sl,mask=found;rr,cc=np.mgrid[sl[0],sl[1]]
            x,y=world_for_cells(rr,cc)
            z=(tri[0,2]-(normal[0]*(x-tri[0,0])+normal[1]*(y-tri[0,1]))/normal[2])*FT
            window=roof_grid[sl];window[mask]=np.fmax(window[mask],z[mask]);ntri+=1
    # Retain old detailed roofs where the current footprint still covers them; fill new parts only.
    roof_valid=np.isfinite(roof_grid)&bmask
    material[bmask]=0
    park_height_fills=0
    parks_path=PROCESSED/'parks_structures.parquet'
    if parks_path.exists():
        parks=gpd.read_parquet(parks_path)
        lookup=(parks.assign(_id=parks.doitt_id.astype(str).str.replace(r'\.0$','',regex=True),
                    _height=pd.to_numeric(parks.height_roof,errors='coerce'))
                .dropna(subset=['_height']).drop_duplicates('_id').set_index('_id')._height)
        missing=pd.to_numeric(b.height_roof,errors='coerce').fillna(0)<=0
        supplement=b.doitt_id.astype(str).map(lookup)
        use=missing&supplement.notna()&(supplement>0)
        b.loc[use,'height_roof']=supplement[use];park_height_fills=int(use.sum())
    height_lookup=np.r_[0,pd.to_numeric(b.height_roof,errors='coerce').fillna(0).to_numpy()*FT]
    inferred_height_ids=[]
    for i,r in b.iterrows():
        if not np.isfinite(r.height_roof) or r.height_roof<=0:inferred_height_ids.append(str(r.doitt_id))
    fallback_height=height_lookup[bids];fallback_height=np.where(fallback_height>0,fallback_height,6.)
    top[bmask]=ground[bmask]+fallback_height[bmask]
    top[roof_valid]=np.maximum(roof_grid[roof_valid],ground[roof_valid]+1)
    # Restore physically continuous legacy roof coverage across sub-metre footprint-boundary discrepancies.
    old_near=np.isfinite(roof_grid)&binary_dilation(bmask,iterations=1)&~np.isin(material,[2,3])
    top[old_near]=np.maximum(roof_grid[old_near],ground[old_near]+1);material[old_near]=0;bmask|=old_near
    material_lookup=np.r_[-1,building_materials]
    building_material_grid=material_lookup[bids]
    legacy_only=old_near&(bids==0)
    if legacy_only.any() and (building_materials>=0).any():
        nearest=distance_transform_edt(bids==0,return_distances=False,return_indices=True)
        building_material_grid[legacy_only]=material_lookup[bids[tuple(nearest[:,legacy_only])]]
        del nearest
    custom_building_mask=bmask&(building_material_grid>=0)
    custom_counts=[int((custom_building_mask&(building_material_grid==color)).sum()) for color in range(4)]
    report['layers']['buildings']={'current_footprints':len(b),'historic_objects':len(city),'roof_polygons':len(roofs),
        'nonflat_roof_triangles':ntri,'cells_with_historic_roof':int(roof_valid.sum()),'fallback_cells':int((bmask&~np.isfinite(roof_grid)).sum()),
        'missing_height_building_ids':inferred_height_ids,'parks_structure_height_fills':park_height_fills,
        'custom_material_footprints':int((building_materials>=0).sum()),'custom_material_cells':custom_counts}
    print('Roof surfaces',len(roofs),'sloping triangles',ntri,flush=True)
    # Do not infer monument identity or geometry from OSM and sparse LiDAR.
    # Monument augmentation stays disabled until a source supplies both
    # authoritative coordinates and a precise, automatically joinable 3D asset.
    report['layers']['landmarks']=[]
    print('Monument augmentation disabled',flush=True)
    # Restore the varied measured canopy, while closing only narrow source gaps
    # and keeping a print-scaled clear shoulder around mapped trails. Applying
    # trail exclusion after gap closing prevents the closing operation from
    # swallowing real paths.
    ch=upper-ground
    ordinary_canopy_surface=~roadmask&~np.isin(material,[2,3])
    parking_canopy_surface=osm_parking_fallback_mask&(material==3)
    canopy_surface=(ordinary_canopy_surface|recreation_green|parking_canopy_surface)&~protected_transport_surface
    trail_setback=float(CFG.get('canopy_trail_setback_mm',.30))
    trail_clearance=(distance_transform_edt(~pathmask)*STEP<=trail_setback) if pathmask.any() else pathmask
    canopy_eligible=aoi_mask&~bmask&canopy_surface&~trail_clearance
    canopy_height,canopy_mask,canopy_report=measured_canopy_relief(
        upper_halo-ground_halo,lc_halo==1,canopy_eligible,
        grid_step_mm=STEP,scale_denominator=CFG['scale_denominator'],
        smoothing_m=CFG.get('canopy_smoothing_m',1.),
        maximum_gap_mm=CFG.get('canopy_maximum_closed_gap_mm',.75),
        edge_roll_mm=CFG.get('canopy_edge_roll_mm',.50),
        minimum_source_height_m=CFG.get('canopy_minimum_source_height_m',1.),
        maximum_source_height_m=CFG['canopy_max_height_m'],core_slices=core)
    top[canopy_mask]=ground[canopy_mask]+canopy_height[canopy_mask];material[canopy_mask]=1
    report['layers']['canopy']={**canopy_report,
        'area_m2':float(canopy_mask.sum()*(STEP*CFG['scale_denominator']/1000)**2),
        'osm_parking_canopy_cells':int((canopy_mask&osm_parking_fallback_mask).sum()),
        'trail_canopy_setback_mm':trail_setback,
        'trail_clearance_cells':int(trail_clearance.sum()),
        'source_role':'LiDAR upper-surface heights define the varied printed canopy relief'}
    del ground_halo,upper_halo,lc_halo
    print('Smoothed measured canopy',canopy_report['printable_canopy_cells'],flush=True)
    # Measured rooftop tanks; cooling heights inferred only when later outlines lack usable LiDAR evidence.
    fixtures=[]
    for name in ['WATER_TANK','COOLING_TOWERS']:
        for _,r in read('planimetrics_'+name).iterrows():
            geom=r.geometry
            equivalent_diameter=2*np.sqrt(geom.area/np.pi)*K
            if equivalent_diameter<CFG['minimum_fixture_width_mm']:
                geom=geom.buffer((CFG['minimum_fixture_width_mm']-equivalent_diameter)/2/K,quad_segs=6)
            found=small_mask(geom)
            if found is None:continue
            sl,mask=found;mask&=bmask[sl]
            if not mask.any():continue
            roof_z=float(np.percentile(top[sl][mask],30))
            if name=='WATER_TANK':
                height=float(r.HEIGHT*FT);target=float(r.TOP_ELEVATION*FT)
                if target<roof_z-1:
                    fixtures.append({'layer':name,'bin':str(r.BIN),'method':'skipped: tank top below existing roof, mixed-date conflict'})
                    continue
                method='measured top elevation; existing higher roof detail is retained'
            else:
                points=upper[sl][mask];points=points[np.isfinite(points)]
                observed=float(np.percentile(points,70)) if len(points) else roof_z
                h=np.clip(observed-roof_z,0,4.)
                minimum=mm_to_source(CFG['inferred_cooling_height_mm'])
                target=roof_z+max(float(h),minimum);method='LiDAR difference' if h>=minimum else 'inferred minimum fixture height'
            top[sl][mask]=np.maximum(top[sl][mask],target);material[sl][mask]=0
            fixtures.append({'layer':name,'bin':str(r.BIN),'height_above_roof_m':target-roof_z,'method':method})
    report['layers']['fixtures']=fixtures
    # Reapply per-building colors after roof fixtures so tanks and cooling
    # towers remain part of the selected building's material rather than ivory.
    material[custom_building_mask]=building_material_grid[custom_building_mask]
    # Preserve the measured rise but quantize mapped stair runs to printable treads.
    stair_records=[];stair_yielded=0
    for r in stairs:
        line=r['geometry']
        if line.geom_type!='LineString' or line.length*K<.9:continue
        ends=np.array([line.coords[0],line.coords[-1]])[:,:2]
        rr,cc=cells_for_points(ends[:,0],ends[:,1]);rr=np.clip(rr,0,NY-1);cc=np.clip(cc,0,NX-1)
        z0,z1=ground[rr,cc];rise_mm=source_to_mm(abs(float(z1-z0)))
        count=min(int(line.length*K/.4),int(rise_mm/.08))
        if count<2:continue
        found=small_mask(line.buffer(r['width_mm']/2/K,quad_segs=3))
        if found is None:continue
        sl,mask=found
        mask,yielded=stair_tread_mask(mask,building_mask=bmask[sl],water_mask=material[sl]==2,
            road_mask=road_surface_mask[sl],protected_transport_mask=protected_transport_surface[sl])
        stair_yielded+=yielded
        if not mask.any():continue
        rows,cols=np.where(mask);xx,yy=world_for_cells(rows+sl[0].start,cols+sl[1].start)
        t=shapely.line_locate_point(line,shapely.points(xx,yy),normalized=True)
        stepped=z0+(z1-z0)*np.round(t*count)/count+mm_to_source(line_relief)
        top[sl][mask]=stepped;material[sl][mask]=3
        absolute_window=absolute_terrain_surface_mask[sl];absolute_window[mask]=True
        stair_records.append({'osm_id':r['osm_id'],'print_treads':count,'measured_rise_mm':rise_mm,
            'tread_cells':int(mask.sum())})
    report['layers']['print_scaled_stairs']={'runs':stair_records,
        'tread_cells_yielded_to_roads_and_decks':stair_yielded,
        'priority':'a stair tread yields to the drawn carriageway and to any mapped deck it would otherwise cut'}
    # Selected mapped wall/portal caps are strengthened to one printable line. No invented railings or ornament.
    wall_count=0
    for geom in read('planimetrics_RETAININGWALL').geometry:
        found=small_mask(geom.buffer(.20/K,quad_segs=2))
        if found is None:continue
        sl,mask=found;mask&=~bmask[sl]&~(material[sl]==2)
        top[sl][mask]=np.maximum(top[sl][mask],ground[sl][mask]+mm_to_source(.20))
        material[sl][mask]=0;wall_count+=1
    structures=read('planimetrics_TRANSPORT_STRUCTURE')
    portal_count=0
    for _,r in structures.iterrows():
        if int(r.SUB_FEATURE_CODE)!=231000:continue
        found=small_mask(r.geometry.buffer(.08/K,quad_segs=2))
        if found is None:continue
        sl,mask=found;mask&=~bmask[sl]&~(material[sl]==2)
        top[sl][mask]=np.maximum(top[sl][mask],ground[sl][mask]+mm_to_source(.20))
        material[sl][mask]=0;portal_count+=1
    report['layers']['wall_caps']=wall_count;report['layers']['portal_caps']=portal_count
    # Printable location markers for mapped subway entrances.  These are tiny
    # ivory caps on the local ground, not invented entrance architecture.
    entrance_path=PROCESSED/'mta_subway_entrances.parquet'
    entrance_records=[]
    if entrance_path.exists() and CFG.get('subway_entrances',False):
        entrances=gpd.read_parquet(entrance_path)
        for _,r in entrances.iterrows():
            if 'Passage' in str(r.entrance_type):continue
            found=small_mask(r.geometry.buffer(CFG['minimum_entrance_width_mm']/2/K,quad_segs=4))
            if found is None:continue
            sl,mask=found;mask&=~bmask[sl]&~(material[sl]==2)
            if not mask.any():continue
            top[sl][mask]=np.maximum(top[sl][mask],ground[sl][mask]+mm_to_source(CFG['entrance_relief_mm']))
            material[sl][mask]=0
            entrance_records.append({'stop_name':str(r.stop_name),'type':str(r.entrance_type),'cells':int(mask.sum())})
    report['layers']['subway_entrances']=entrance_records
    # Cells outside an arbitrary crop polygon are absent model space, not a
    # fifth printable material. Keep the sentinel through topology cleanup and
    # let mesh generation ignore it.
    material[~aoi_mask]=255
    # Clean one-pixel islands and diagonal-only contacts that cannot be manufactured at this nozzle size.
    cleanups=0
    for _ in range(12):
        a=material[:-1,:-1];bb=material[:-1,1:];c=material[1:,:-1];d=material[1:,1:]
        printable=(a<4)&(bb<4)&(c<4)&(d<4)
        bad=(a==d)&(a!=bb)&(a!=c)&printable
        bad2=(bb==c)&(bb!=a)&(bb!=d)&printable
        # Crossing validation relies on mapped upper routes remaining intact.
        # Their cells were deliberately protected above, so topology cleanup
        # may reshape neighboring material but must not replace those cells.
        bad&=~protected_transport_surface[1:,1:]
        bad2&=~protected_transport_surface[1:,:-1]
        if not bad.any() and not bad2.any():break
        ii,jj=np.where(bad);ii2,jj2=np.where(bad2)
        old=material.copy();old_absolute=absolute_terrain_surface_mask.copy()
        material[ii+1,jj+1]=old[ii,jj+1];top[ii+1,jj+1]=top[ii,jj+1]
        material[ii2+1,jj2]=old[ii2,jj2];top[ii2+1,jj2]=top[ii2,jj2]
        absolute_terrain_surface_mask[ii+1,jj+1]=old_absolute[ii,jj+1]
        absolute_terrain_surface_mask[ii2+1,jj2]=old_absolute[ii2,jj2]
        cleanups+=len(ii)+len(ii2)
    # Remove cells connected to their own material only at a corner.  Such
    # zero-width junctions collapse when the closed mesh is serialized as
    # float32, while being far below one extrusion width.
    isolated_cleanups=0
    for _ in range(4):
        changed=0;old=material.copy();old_top=top.copy();old_absolute=absolute_terrain_surface_mask.copy()
        for color in range(4):
            same=np.zeros(SHAPE,np.uint8)
            same[1:]+=(old[1:]==color)&(old[:-1]==color);same[:-1]+=(old[:-1]==color)&(old[1:]==color)
            same[:,1:]+=(old[:,1:]==color)&(old[:,:-1]==color);same[:,:-1]+=(old[:,:-1]==color)&(old[:,1:]==color)
            rr,cc=np.where((old==color)&(same==0)&~protected_transport_surface&
                (np.indices(SHAPE)[0]>0)&(np.indices(SHAPE)[0]<NY-1)&
                (np.indices(SHAPE)[1]>0)&(np.indices(SHAPE)[1]<NX-1))
            for r,c in zip(rr,cc):
                values=[value for value in [old[r-1,c],old[r+1,c],old[r,c-1],old[r,c+1]] if value<4]
                if not values:continue
                replacement=max(set(values),key=values.count)
                source=next((p for p in [(r-1,c),(r+1,c),(r,c-1),(r,c+1)] if old[p]==replacement),(r,c))
                material[r,c]=replacement;top[r,c]=old_top[source]
                absolute_terrain_surface_mask[r,c]=old_absolute[source];changed+=1
        isolated_cleanups+=changed
        if not changed:break
    local_origin=float(np.min(ground[aoi_mask]))
    origin=float(CFG.get('terrain_origin_m')) if CFG.get('terrain_origin_m') is not None else local_origin
    terrain_values=ground[aoi_mask&~water_mask]
    if not len(terrain_values):terrain_values=ground[aoi_mask]
    terrain_relief=choose_terrain_relief(terrain_values,
        scale_denominator=CFG['scale_denominator'],vertical_exaggeration=vertical,
        layer_height_mm=CFG['layer_height_mm'],
        minimum_levels=CFG.get('minimum_terrain_relief_levels',6.),
        maximum_factor=CFG.get('maximum_terrain_relief_factor',3.),
        minimum_source_span_m=CFG.get('minimum_terrain_source_span_m',.5),
        requested_factor=CFG.get('terrain_relief_factor'))
    gz=absolute_elevation_to_mm(ground,origin_m=origin,scale_denominator=CFG['scale_denominator'],
        vertical_exaggeration=vertical,terrain_factor=terrain_relief.factor,
        minimum_terrain_mm=CFG['minimum_terrain_mm'])
    # Buildings, canopy, roads, wall caps and other surface treatments retain
    # their ground-relative height. Only surveyed bridge/stair elevations use
    # the absolute terrain transform, with their print-only line lift restored.
    # Every cell it selects is a drawn line -- a bridge deck or a stair run --
    # so the lift stripped here is the one those painters applied.
    z=gz+source_to_mm(top-ground)
    absolute_terrain_surface_mask&=aoi_mask&~bmask&np.isin(material,[0,3])
    geographic_surface=top[absolute_terrain_surface_mask]-mm_to_source(line_relief)
    z[absolute_terrain_surface_mask]=absolute_elevation_to_mm(geographic_surface,
        origin_m=origin,scale_denominator=CFG['scale_denominator'],vertical_exaggeration=vertical,
        terrain_factor=terrain_relief.factor,minimum_terrain_mm=CFG['minimum_terrain_mm'])+line_relief
    # Every visible surface is now placed on the slicer's own layer planes, and
    # any step rounding invented across ground flatter than a layer is settled.
    # This is the last point at which the map's surfaces are all in one array,
    # and it has to run before the substrate is seated beneath them, so the
    # skin the substrate carries keeps its exact colour depth.
    first_layer=float(CFG.get('first_layer_height_mm',FIRST_LAYER_HEIGHT_MM))
    settled_ground=printable_surface_mm(gz,aoi_mask,layer_height_mm=CFG['layer_height_mm'],
        first_layer_height_mm=first_layer)
    settled_surface=printable_surface_mm(z,aoi_mask,layer_height_mm=CFG['layer_height_mm'],
        first_layer_height_mm=first_layer)
    # Ground and visible surface are quantized against their own neighbourhoods,
    # so on ground the map draws nothing on they can settle onto different
    # planes. Only an inversion the quantization introduced is corrected; where
    # the source already put the surface below the ground it is left alone.
    inverted=aoi_mask&(gz<=z)&(settled_ground>settled_surface)
    settled_ground[inverted]=settled_surface[inverted]
    report['printable_surfaces']={
        'first_layer_height_mm':first_layer,'layer_height_mm':float(CFG['layer_height_mm']),
        'maximum_ground_shift_mm':float(np.abs(settled_ground-gz)[aoi_mask].max()),
        'maximum_surface_shift_mm':float(np.abs(settled_surface-z)[aoi_mask].max()),
        'mean_surface_shift_mm':float(np.abs(settled_surface-z)[aoi_mask].mean()),
        'inverted_cells_corrected':int(inverted.sum()),
        'rule':'visible surfaces are placed on layer planes; a step is kept only where a region rises a whole layer'}
    gz,z=settled_ground,settled_surface
    if not np.isfinite(z[aoi_mask]).all() or z[aoi_mask].min()<=CFG['base_mm']:
        raise RuntimeError(
            f"Terrain origin {origin:g} m puts the model at or below its {CFG['base_mm']:g} mm base; "
            f"local minimum is {local_origin:g} m")
    color_depth=surface_color_depth_mm(CFG['layer_height_mm'],CFG.get('minimum_surface_color_depth_mm',.24))
    configured_depth=CFG.get('surface_color_depth_mm')
    if configured_depth is not None and not math.isclose(float(configured_depth),color_depth,abs_tol=1e-9):
        raise RuntimeError('Configured surface color depth is not aligned with the active layer height')
    substrate_reference=np.minimum(gz,z)
    substrate_top=white_substrate_top(substrate_reference,aoi_mask,
        base_mm=CFG['base_mm'],color_depth_mm=color_depth)
    if np.any(substrate_top[aoi_mask]>z[aoi_mask]+1e-6):
        raise RuntimeError('White substrate rises above the visible map surface')
    report.update({'vertical_origin_m_navd88':origin,'local_minimum_elevation_m_navd88':local_origin,
        'z_range_mm':[float(z[aoi_mask].min()),float(z[aoi_mask].max())],
        'terrain_relief':terrain_relief.__dict__,
        'material_layers':{'substrate_material':int(CFG.get('foundation_material',0)),
            'substrate_color':MATERIAL_NAMES[int(CFG.get('foundation_material',0))],
            'surface_color_depth_mm':color_depth,
            'contract':'one continuous substrate below all visible surface materials'},
        'material_cell_counts':[int((material==color).sum()) for color in range(4)],'diagonal_contact_cleanups':cleanups,
        'isolated_cell_cleanups':isolated_cleanups})
    np.savez_compressed(OUT/'map_fields.npz',height_mm=z.astype(np.float32),ground_mm=gz.astype(np.float32),material=material,
        substrate_top_mm=substrate_top,canopy_height_m=canopy_height.astype(np.float32),building_mask=bmask,aoi_mask=aoi_mask,
        upper_tunnel_surface_mask=upper_tunnel_surface_mask)
    save_grid('map_top_mm',z);save_grid('map_material',material);save_grid('map_ground_mm',gz)
    for name,rows in [('tunnels',tunnels),('bridges',bridges),('stairs',stairs)]:
        if rows:gpd.GeoDataFrame(rows,crs=2263).to_parquet(OUT/(name+'.parquet'))
    write_json(OUT/'field_build_report.json',report)
    gpd.GeoDataFrame({'name':[CFG['name']],'scale':[CFG['scale_denominator']]},geometry=[AOI],crs=2263).to_crs(4326).to_file(OUT/'map_area.geojson',driver='GeoJSON')
    print('Saved fused fields',report['z_range_mm'],flush=True)

if __name__=='__main__':main()
