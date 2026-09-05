"""Render the actual final triangle meshes; no generated or retouched geography."""
import argparse,subprocess,struct,json
from pathlib import Path
import numpy as np,trimesh
from PIL import Image
from map_common import *

def main():
    p=argparse.ArgumentParser();p.add_argument('--mesh-dir',type=Path,required=True);p.add_argument('--view',choices=['overview','detail','tunnel','top'],default='overview');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    binary=OUT/'render_map';source=Path(__file__).with_suffix('.cpp')
    if not binary.exists() or source.stat().st_mtime>binary.stat().st_mtime:
        sdk=subprocess.check_output(['xcrun','--show-sdk-path'],text=True).strip()
        subprocess.run(['clang++','-O3','-std=c++17','-isysroot',sdk,'-isystem',str(Path(sdk)/'usr/include/c++/v1'),str(source),'-o',str(binary)],check=True)
    stream=a.mesh_dir/'render_triangles.bin'
    mesh_paths=[a.mesh_dir/f'material_{i}.ply' for i in range(4)]
    existing=[path for path in mesh_paths if path.exists()]
    if not existing:raise RuntimeError(f'No material meshes found in {a.mesh_dir}')
    if not stream.exists() or any(path.stat().st_mtime>stream.stat().st_mtime for path in existing):
        count=0
        with stream.open('wb') as f:
            f.write(struct.pack('<Q',0))
            for i,color in enumerate(CFG['colors']):
                path=a.mesh_dir/f'material_{i}.ply'
                if not path.exists():continue
                m=trimesh.load(path,process=False)
                rgb=np.array([int(color[j:j+2],16)/255 for j in [1,3,5]],np.float32)
                for start in range(0,len(m.faces),100000):
                    faces=m.faces[start:start+100000];data=np.empty((len(faces),12),'<f4');data[:,:9]=m.vertices[faces].reshape(-1,9);data[:,9:]=rgb;f.write(data.tobytes());count+=len(faces)
            f.seek(0);f.write(struct.pack('<Q',count))
    extent=max(W,H)
    views={'overview':(2400,1900,-105,55,W/2,H/2,6,extent*1.35),
           'top':(2200,2200,-90,89.99,W/2,H/2,4,extent*1.09)}
    if a.view=='detail':
        view=(2000,1600,-105,57,W/2,H/2,8,min(75,extent*.75))
    elif a.view=='tunnel':
        g=gpd.read_parquet(OUT/'tunnels.parquet')
        point=local(g.geometry.iloc[0]).centroid if len(g) else Point(W/2,H/2)
        view=(1800,1300,-41,65,point.x,point.y,4.2,min(24,extent*.5))
    else:view=views[a.view]
    ppm=a.mesh_dir/f'{a.view}.ppm'
    supersampled=(view[0]*2,view[1]*2,*view[2:])
    subprocess.run([str(binary),str(stream),str(ppm),*[str(v) for v in supersampled]],check=True)
    target=a.output
    target.parent.mkdir(parents=True,exist_ok=True)
    Image.open(ppm).resize((view[0],view[1]),Image.Resampling.LANCZOS).save(target)
    print(target)

if __name__=='__main__':main()
