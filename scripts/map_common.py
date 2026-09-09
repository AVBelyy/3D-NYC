"""Shared map/model coordinate helpers. Model coordinates are millimetres."""
import atexit,json,os,tempfile
from pathlib import Path
import numpy as np,geopandas as gpd,shapely,rasterio
from affine import Affine
from rasterio.warp import reproject,Resampling
from shapely.geometry import Point,box,shape
from download_data import ROOT

DATA_DIR=Path(os.environ.get('NYC_DATA_DIR',ROOT/'data')).resolve()
RAW=Path(os.environ.get('NYC_RAW_DIR',DATA_DIR/'raw')).resolve()
CACHE_DIR=Path(os.environ.get('NYC_CACHE_DIR',DATA_DIR/'cache')).resolve()
DOWNLOAD_MANIFESTS=Path(os.environ.get('NYC_DOWNLOAD_MANIFEST_DIR',RAW)).resolve()
CONFIG_PATH=Path(os.environ.get('MAP_CONFIG',ROOT/'scripts/map_config.example.json')).resolve()
CFG=json.loads(CONFIG_PATH.read_text())
OUTPUT_DIR=Path(os.environ.get('NYC_OUTPUT_DIR',ROOT/'output')).resolve()
_SCRATCH=None

def _scratch():
    """One throwaway directory per process, removed at exit.

    A run inside a job is handed every directory it may write to. A run without
    one is an experiment, so its output belongs outside the repository and is
    not worth keeping; pass the environment variables below to keep it.
    """
    global _SCRATCH
    if _SCRATCH is None:
        _SCRATCH=tempfile.TemporaryDirectory(prefix='3d-nyc-')
        atexit.register(_SCRATCH.cleanup)
    return Path(_SCRATCH.name)

def _job_dir(variable,name):
    value=os.environ.get(variable)
    if value:return Path(value).resolve()  # a job owns and creates its own directories
    path=_scratch()/name;path.mkdir(parents=True,exist_ok=True);return path

OUT=_job_dir('MAP_WORK_DIR','work')
VALID=_job_dir('NYC_VALID_DIR','validation')
PROCESSED=_job_dir('NYC_PROCESSED_DIR','processed')
ANALYSIS=_job_dir('NYC_ANALYSIS_DIR','analysis')
FT=.3048006096012192
K=FT*1000/CFG['scale_denominator']
W,H=CFG['size_mm'];STEP=CFG['grid_step_mm']
NX,NY=int(round(W/STEP)),int(round(H/STEP));SHAPE=(NY,NX)
if 'aoi_wgs84' in CFG:
    AOI=gpd.GeoSeries([shape(CFG['aoi_wgs84'])],crs=4326).to_crs(2263).iloc[0]
else:
    CENTER=gpd.GeoSeries([Point(*CFG['center_wgs84'])],crs=4326).to_crs(2263).iloc[0]
    legacy_bounds=(CENTER.x-W/2/K,CENTER.y-H/2/K,CENTER.x+W/2/K,CENTER.y+H/2/K)
    AOI=box(*legacy_bounds)
frame=CFG.get('frame_epsg2263')
if frame:
    ORIGIN=np.asarray(frame['origin_ft'],dtype=float)
    X_AXIS=np.asarray(frame['x_axis'],dtype=float)
    Y_AXIS=np.asarray(frame['y_axis'],dtype=float)
else:
    ORIGIN=np.asarray(AOI.bounds[:2],dtype=float)
    X_AXIS=np.asarray([1.,0.]);Y_AXIS=np.asarray([0.,1.])
BOUNDS=AOI.bounds
TOP_LEFT=ORIGIN+Y_AXIS*H/K
TRANSFORM=Affine(X_AXIS[0]*STEP/K,-Y_AXIS[0]*STEP/K,TOP_LEFT[0],
                 X_AXIS[1]*STEP/K,-Y_AXIS[1]*STEP/K,TOP_LEFT[1])

def read(name):
    g=gpd.read_parquet(PROCESSED/f'{name}_aoi.parquet')
    return g[g.intersects(AOI)].copy()

def local(geom):
    return shapely.affinity.affine_transform(geom,[
        K*X_AXIS[0],K*X_AXIS[1],K*Y_AXIS[0],K*Y_AXIS[1],
        -K*float(ORIGIN@X_AXIS),-K*float(ORIGIN@Y_AXIS),
    ])

MODEL_AOI=local(AOI)

def sample_raster(name,resampling=Resampling.bilinear):
    path=PROCESSED/f'rasters/{name}.tif'
    with rasterio.open(path) as src:
        out=np.full(SHAPE,np.nan,np.float32)
        reproject(rasterio.band(src,1),out,src_transform=src.transform,src_crs=src.crs,
            dst_transform=TRANSFORM,dst_crs=2263,resampling=resampling,dst_nodata=np.nan)
    return out

def cells_for_points(x,y):
    dx=np.asarray(x)-ORIGIN[0];dy=np.asarray(y)-ORIGIN[1]
    model_x=K*(dx*X_AXIS[0]+dy*X_AXIS[1])
    model_y=K*(dx*Y_AXIS[0]+dy*Y_AXIS[1])
    return ((H-model_y)/STEP).astype(int),(model_x/STEP).astype(int)

def world_for_cells(rows,cols):
    cols=np.asarray(cols)+.5;rows=np.asarray(rows)+.5
    return (TRANSFORM.a*cols+TRANSFORM.b*rows+TRANSFORM.c,
            TRANSFORM.d*cols+TRANSFORM.e*rows+TRANSFORM.f)

def write_json(path,data):
    clean=json.loads(json.dumps(data,default=lambda x:int(x) if isinstance(x,np.integer) else float(x) if isinstance(x,np.floating) else str(x)),parse_constant=lambda x:None)
    path.write_text(json.dumps(clean,indent=2,allow_nan=False))

def save_grid(name,a):
    dtype='uint8' if a.dtype==np.uint8 else 'float32'
    with rasterio.open(OUT/(name+'.tif'),'w',driver='GTiff',height=NY,width=NX,count=1,dtype=dtype,
        crs=2263,transform=TRANSFORM,compress='deflate',tiled=True,nodata=255 if dtype=='uint8' else np.nan) as dst:
        dst.write(a.astype(dtype),1)
