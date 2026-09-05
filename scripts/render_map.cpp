// Dependency-free orthographic triangle renderer with a directional shadow map.
// Input is a uint64 triangle count followed by 12 little-endian float32 values per triangle.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>
struct V { float x,y,z; V operator+(V a)const{return{x+a.x,y+a.y,z+a.z};} V operator-(V a)const{return{x-a.x,y-a.y,z-a.z};} V operator*(float s)const{return{x*s,y*s,z*s};} };
float dot(V a,V b){return a.x*b.x+a.y*b.y+a.z*b.z;}
V cross(V a,V b){return{a.y*b.z-a.z*b.y,a.z*b.x-a.x*b.z,a.x*b.y-a.y*b.x};}
V unit(V a){return a*(1.f/std::max(1e-12f,std::sqrt(dot(a,a))));}
struct Tri { V p[3], color; };
struct Camera {V right,up,look,center;float scale;int w,h; V project(V p)const{p=p-center;return{dot(p,right)*scale+w*.5f,h*.5f-dot(p,up)*scale,dot(p,look)};}};
Camera camera(float az,float el,V center,float span,int w,int h){az*=M_PI/180;el*=M_PI/180;V look{std::cos(el)*std::cos(az),std::cos(el)*std::sin(az),std::sin(el)};V right=unit(cross({0,0,1},look));return{right,cross(look,right),look,center,w/span,w,h};}
float edge(V a,V b,float x,float y){return (b.x-a.x)*(y-a.y)-(b.y-a.y)*(x-a.x);}
void draw(const Tri&t,const Camera&c,std::vector<float>&depth,std::vector<unsigned char>*image,const Camera*lc,const std::vector<float>*shadow){
 V a=c.project(t.p[0]),b=c.project(t.p[1]),d=c.project(t.p[2]);float area=edge(a,b,d.x,d.y);if(std::abs(area)<1e-8)return;
 int x0=std::max(0,(int)std::floor(std::min({a.x,b.x,d.x}))),x1=std::min(c.w-1,(int)std::ceil(std::max({a.x,b.x,d.x})));
 int y0=std::max(0,(int)std::floor(std::min({a.y,b.y,d.y}))),y1=std::min(c.h-1,(int)std::ceil(std::max({a.y,b.y,d.y})));
 V normal=unit(cross(t.p[1]-t.p[0],t.p[2]-t.p[0]));V light=unit({-.45f,-.65f,1});
 float illumination=.54f+.48f*std::max(0.f,dot(normal,light));
 for(int y=y0;y<=y1;y++)for(int x=x0;x<=x1;x++){
  float u=edge(b,d,x+.5f,y+.5f)/area,v=edge(d,a,x+.5f,y+.5f)/area,w=1-u-v;if(u<-1e-5||v<-1e-5||w<-1e-5)continue;
  float z=u*a.z+v*b.z+w*d.z;size_t id=(size_t)y*c.w+x;if(z<=depth[id])continue;depth[id]=z;if(!image)continue;
  V p=t.p[0]*u+t.p[1]*v+t.p[2]*w;float shade=1;
  if(lc&&shadow){V lp=lc->project(p);int ix=(int)lp.x,iy=(int)lp.y;float blocked=0,samples=0;
   float facing=dot(normal,lc->look);
   for(int dy=-1;dy<=1;dy++)for(int dx=-1;dx<=1;dx++){int xx=ix+dx,yy=iy+dy;if(xx<0||yy<0||xx>=lc->w||yy>=lc->h)continue;samples++;
    float expected=lp.z;if(std::abs(facing)>.1f)expected-=(dot(normal,lc->right)*(xx+.5f-lp.x)-dot(normal,lc->up)*(yy+.5f-lp.y))/(lc->scale*facing);
    if((*shadow)[(size_t)yy*lc->w+xx]>expected+.06f)blocked++;}
   if(samples)shade=1-.32f*blocked/samples;
  }
  float rgb[3]={t.color.x,t.color.y,t.color.z};for(int k=0;k<3;k++){float val=rgb[k]*illumination*shade;(*image)[id*3+k]=(unsigned char)(std::clamp(val,0.f,1.f)*255+.5f);}
 }
}
int main(int argc,char**argv){
 if(argc!=11){std::cerr<<"mesh.bin out.ppm width height azimuth elevation center_x center_y center_z span\n";return 2;}
 std::ifstream in(argv[1],std::ios::binary);uint64_t n;in.read((char*)&n,sizeof(n));std::vector<Tri> tris(n);in.read((char*)tris.data(),n*sizeof(Tri));if(!in){std::cerr<<"Truncated triangle stream\n";return 3;}
 int w=std::stoi(argv[3]),h=std::stoi(argv[4]);V center{std::stof(argv[7]),std::stof(argv[8]),std::stof(argv[9])};
 Camera c=camera(std::stof(argv[5]),std::stof(argv[6]),center,std::stof(argv[10]),w,h);
 Camera light=camera(-124.7,51.7,{100,100,8},310,3200,3200);std::vector<float> shadows(light.w*light.h,-1e30f);
 for(const auto&t:tris)draw(t,light,shadows,nullptr,nullptr,nullptr);
 std::vector<float> depth((size_t)w*h,-1e30f);std::vector<unsigned char> image((size_t)w*h*3,244);
 V cream{.97f,.965f,.945f};Tri floor1{{{-400,-400,-.025f},{600,-400,-.025f},{600,600,-.025f}},cream};Tri floor2{{{-400,-400,-.025f},{600,600,-.025f},{-400,600,-.025f}},cream};
 draw(floor1,c,depth,&image,&light,&shadows);draw(floor2,c,depth,&image,&light,&shadows);
 for(const auto&t:tris)draw(t,c,depth,&image,&light,&shadows);
 std::ofstream out(argv[2],std::ios::binary);out<<"P6\n"<<w<<" "<<h<<"\n255\n";out.write((char*)image.data(),image.size());std::cout<<n<<" triangles rendered\n";
}
