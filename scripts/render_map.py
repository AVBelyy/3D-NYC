"""Render the actual final triangle meshes; no generated or retouched geography."""
import argparse
from pathlib import Path
import numpy as np,trimesh
from numba import njit,prange
from PIL import Image
from map_common import *

F=np.float32
JIT=dict(cache=True,nogil=True)
BAND=64                                 # scanlines per band; a band is owned by one thread
BACKGROUND=244
PREVIEW_SCALE=2                         # the saved preview, as a multiple of the view size
SUPERSAMPLE=2                           # rasterised larger again, then resampled down
LIGHT=(-.45,-.65,1.)
CREAM=(.97,.965,.945)
FLOOR=((-400.,-400.,-.025),(600.,-400.,-.025),(600.,600.,-.025),(-400.,600.,-.025))
# The shadow camera frames the original 200 mm plate rather than deriving from W
# and H, so an off-square map is lit a little off centre.  Kept as it was.
SHADOW_VIEW=(-124.7,51.7,(100.,100.,8.),310.,3200)


def unit(v):
    v=np.asarray(v,F);return (v/np.sqrt((v*v).sum())).astype(F)


def camera(az,el,center,span,w,h):
    """Orthographic camera as a basis whose rows are right, up, look, and centre."""
    az,el=np.radians(az),np.radians(el)
    look=unit((np.cos(el)*np.cos(az),np.cos(el)*np.sin(az),np.sin(el)))
    right=unit(np.cross((0,0,1),look))
    return np.array([right,unit(np.cross(look,right)),look,center],F),F(w/span),int(w),int(h)


def bands(P,cam,band=BAND):
    """(triangle, band) pairs grouped by band, so no two bands share a scanline."""
    basis,scale,w,h=cam
    q=P.reshape(-1,3)-basis[3]
    sy=(F(h*.5)-(q@basis[1])*scale).reshape(-1,3)
    first=np.clip(np.floor(sy.min(1)),0,h-1).astype(np.int32)//band
    last=np.clip(np.ceil(sy.max(1)),0,h-1).astype(np.int32)//band
    reach=(last-first+1).astype(np.int64)
    tri=np.repeat(np.arange(len(first),dtype=np.int32),reach)
    step=np.arange(len(tri),dtype=np.int64)-np.repeat(np.cumsum(reach)-reach,reach)
    index=first[tri]+step.astype(np.int32)
    order=np.argsort(index,kind='stable');count=(h+band-1)//band
    return tri[order],np.searchsorted(index[order],np.arange(count+1)),count


@njit(**JIT,inline='always')
def _project(x,y,z,basis,scale,w,h):
    qx=x-basis[3,0];qy=y-basis[3,1];qz=z-basis[3,2]
    return (F((qx*basis[0,0]+qy*basis[0,1]+qz*basis[0,2])*scale+w*.5),
        F(h*.5-(qx*basis[1,0]+qy*basis[1,1]+qz*basis[1,2])*scale),
        F(qx*basis[2,0]+qy*basis[2,1]+qz*basis[2,2]))


@njit(**JIT,inline='always')
def _edge(ax,ay,bx,by,x,y):
    return (bx-ax)*(y-ay)-(by-ay)*(x-ax)


@njit(**JIT,inline='always')
def _normal(P,t):
    ux=P[t,1,0]-P[t,0,0];uy=P[t,1,1]-P[t,0,1];uz=P[t,1,2]-P[t,0,2]
    vx=P[t,2,0]-P[t,0,0];vy=P[t,2,1]-P[t,0,1];vz=P[t,2,2]-P[t,0,2]
    nx=uy*vz-uz*vy;ny=uz*vx-ux*vz;nz=ux*vy-uy*vx
    scale=max(F(1e-12),np.sqrt(nx*nx+ny*ny+nz*nz))
    return F(nx/scale),F(ny/scale),F(nz/scale)


@njit(**JIT,inline='always')
def _quantize(value):
    return np.uint8(min(F(1),max(F(0),value))*255+.5)


@njit(**JIT,parallel=True)
def _depth_pass(P,basis,scale,w,h,depth,tri_of_band,edges,count,band):
    """Fill a depth buffer only; used for the shadow map."""
    for b in prange(count):
        top=b*band;bottom=min((b+1)*band,h)
        for k in range(edges[b],edges[b+1]):
            t=tri_of_band[k]
            ax,ay,az=_project(P[t,0,0],P[t,0,1],P[t,0,2],basis,scale,w,h)
            bx,by,bz=_project(P[t,1,0],P[t,1,1],P[t,1,2],basis,scale,w,h)
            cx,cy,cz=_project(P[t,2,0],P[t,2,1],P[t,2,2],basis,scale,w,h)
            area=_edge(ax,ay,bx,by,cx,cy)
            if abs(area)<1e-8:continue
            for y in range(max(top,int(np.floor(min(ay,by,cy)))),
                    min(bottom-1,int(np.ceil(max(ay,by,cy))))+1):
                for x in range(max(0,int(np.floor(min(ax,bx,cx)))),
                        min(w-1,int(np.ceil(max(ax,bx,cx))))+1):
                    fx=x+F(.5);fy=y+F(.5)
                    u=_edge(bx,by,cx,cy,fx,fy)/area;v=_edge(cx,cy,ax,ay,fx,fy)/area;s=1-u-v
                    if u<-1e-5 or v<-1e-5 or s<-1e-5:continue
                    z=u*az+v*bz+s*cz;i=y*w+x
                    if z>depth[i]:depth[i]=z


@njit(**JIT,parallel=True)
def _color_pass(P,colors,basis,scale,w,h,depth,image,shadow,lbasis,lscale,lw,lh,
        tri_of_band,edges,count,band,light):
    """Rasterise, shade, and shadow every triangle into the output image."""
    for b in prange(count):
        top=b*band;bottom=min((b+1)*band,h)
        for k in range(edges[b],edges[b+1]):
            t=tri_of_band[k]
            ax,ay,az=_project(P[t,0,0],P[t,0,1],P[t,0,2],basis,scale,w,h)
            bx,by,bz=_project(P[t,1,0],P[t,1,1],P[t,1,2],basis,scale,w,h)
            cx,cy,cz=_project(P[t,2,0],P[t,2,1],P[t,2,2],basis,scale,w,h)
            area=_edge(ax,ay,bx,by,cx,cy)
            if abs(area)<1e-8:continue
            nx,ny,nz=_normal(P,t)
            illumination=F(.54)+F(.48)*max(F(0),nx*light[0]+ny*light[1]+nz*light[2])
            # Slope-scaled bias needs the normal in the light camera's own frame.
            facing=nx*lbasis[2,0]+ny*lbasis[2,1]+nz*lbasis[2,2]
            along=nx*lbasis[0,0]+ny*lbasis[0,1]+nz*lbasis[0,2]
            across=nx*lbasis[1,0]+ny*lbasis[1,1]+nz*lbasis[1,2]
            for y in range(max(top,int(np.floor(min(ay,by,cy)))),
                    min(bottom-1,int(np.ceil(max(ay,by,cy))))+1):
                for x in range(max(0,int(np.floor(min(ax,bx,cx)))),
                        min(w-1,int(np.ceil(max(ax,bx,cx))))+1):
                    fx=x+F(.5);fy=y+F(.5)
                    u=_edge(bx,by,cx,cy,fx,fy)/area;v=_edge(cx,cy,ax,ay,fx,fy)/area;s=1-u-v
                    if u<-1e-5 or v<-1e-5 or s<-1e-5:continue
                    z=u*az+v*bz+s*cz;i=y*w+x
                    if z<=depth[i]:continue
                    depth[i]=z
                    lx,ly,lz=_project(P[t,0,0]*u+P[t,1,0]*v+P[t,2,0]*s,
                        P[t,0,1]*u+P[t,1,1]*v+P[t,2,1]*s,
                        P[t,0,2]*u+P[t,1,2]*v+P[t,2,2]*s,lbasis,lscale,lw,lh)
                    ix=int(lx);iy=int(ly);blocked=F(0);samples=F(0)
                    for dy in range(-1,2):
                        for dx in range(-1,2):
                            sx=ix+dx;sy=iy+dy
                            if sx<0 or sy<0 or sx>=lw or sy>=lh:continue
                            samples+=1;expected=lz
                            if abs(facing)>.1:
                                expected-=(along*(sx+F(.5)-lx)-across*(sy+F(.5)-ly))/(lscale*facing)
                            if shadow[sy*lw+sx]>expected+F(.06):blocked+=1
                    shade=F(1)-F(.32)*blocked/samples if samples>0 else F(1)
                    image[i*3]=_quantize(colors[t,0]*illumination*shade)
                    image[i*3+1]=_quantize(colors[t,1]*illumination*shade)
                    image[i*3+2]=_quantize(colors[t,2]*illumination*shade)


def load(mesh_dir):
    """Every material solid as one triangle soup carrying its filament colour."""
    tris=[];tints=[]
    for i,color in enumerate(CFG['colors']):
        path=mesh_dir/f'material_{i}.ply'
        if not path.exists():continue
        m=trimesh.load(path,process=False)
        tris.append(np.asarray(m.vertices,F)[m.faces])
        rgb=np.array([int(color[j:j+2],16)/255 for j in (1,3,5)],F)
        tints.append(np.broadcast_to(rgb,(len(m.faces),3)))
    if not tris:raise RuntimeError(f'No material meshes found in {mesh_dir}')
    return (np.ascontiguousarray(np.concatenate(tris)),
        np.ascontiguousarray(np.concatenate(tints)))


def render(P,colors,cam,band=BAND):
    """Shadow map first, then the shaded image; the floor casts no shadow."""
    lit=camera(*SHADOW_VIEW[:4],SHADOW_VIEW[4],SHADOW_VIEW[4])
    shadow=np.full(lit[2]*lit[3],-1e30,F)
    tri_of_band,edges,count=bands(P,lit,band)
    _depth_pass(P,*lit,shadow,tri_of_band,edges,count,band)

    floor=np.array([[FLOOR[0],FLOOR[1],FLOOR[2]],[FLOOR[0],FLOOR[2],FLOOR[3]]],F)
    P=np.concatenate([floor,P])
    colors=np.concatenate([np.broadcast_to(np.array(CREAM,F),(2,3)),colors])
    depth=np.full(cam[2]*cam[3],-1e30,F)
    image=np.full(cam[2]*cam[3]*3,BACKGROUND,np.uint8)
    tri_of_band,edges,count=bands(P,cam,band)
    _color_pass(P,colors,*cam,depth,image,shadow,*lit,tri_of_band,edges,count,band,unit(LIGHT))
    return image.reshape(cam[3],cam[2],3)


def main():
    p=argparse.ArgumentParser();p.add_argument('--mesh-dir',type=Path,required=True);p.add_argument('--view',choices=['overview','detail','tunnel','top'],default='overview');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
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
    width,height,az,el,cx,cy,cz,span=view
    width*=PREVIEW_SCALE;height*=PREVIEW_SCALE
    P,colors=load(a.mesh_dir)
    # Rasterised larger again and resampled down; the mesh has no normals of its
    # own, so supersampling is the only antialiasing there is.
    image=render(P,colors,camera(az,el,(cx,cy,cz),span,width*SUPERSAMPLE,height*SUPERSAMPLE))
    target=a.output
    target.parent.mkdir(parents=True,exist_ok=True)
    Image.fromarray(image).resize((width,height),Image.Resampling.LANCZOS).save(target)
    print(target)

if __name__=='__main__':main()
