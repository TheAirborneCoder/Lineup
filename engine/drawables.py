# SPDX-License-Identifier: GPL-3.0-or-later
"""Recover named LOD0 drawables, preserving native meshlet topology and UV0."""
import json
import re
import struct
import zlib

import numpy as np

from script_tree import _name_hash

def word(buf, at):
    return struct.unpack_from("<I",buf,at)[0]

def pointer(buf, at):
    addr = struct.unpack_from("<Q",buf,at)[0] - 0x50000000
    if not 0 <= addr < len(buf):
        raise ValueError("pointer outside system page")
    return addr

def read_stream(source, pos):
    decoder = zlib.decompressobj()
    result = bytearray()
    while not decoder.eof:
        chunk = source[pos:pos+65536]
        if not chunk or len(result)>64<<20:
            raise ValueError("incomplete resource stream")
        result.extend(decoder.decompress(chunk))
        pos += len(chunk)-len(decoder.unused_data)
    return result,(pos+15)&-16

def read_system(source, member):
    sysdata, pos = read_stream(source,member+16)
    if len(sysdata)<184 or sysdata[:8]!=bytes(8):
        raise ValueError("unknown root")
    required=max(struct.unpack_from("<Q",sysdata,a)[0] for a in (8,0x50,0xa8))-0x50000000
    if required<0 or required>=32<<20:
        raise ValueError("no drawable fields")
    while required>=len(sysdata):
        extra,pos=read_stream(source,pos)
        sysdata.extend(extra)
    name = re.match(rb"([a-z0-9_-]+)\.#dr\0",sysdata[pointer(sysdata,0xa8):])
    if not name:
        raise ValueError("unknown drawable name")
    return name[1].decode("ascii"),sysdata,pos

def model_geometry(system,root=0):
    lod=pointer(system,root+0x50)
    model_list=pointer(system,lod)
    count,cap=struct.unpack_from("<HH",system,lod+8)
    if not 0<count<=cap<=4096:
        raise ValueError("invalid model array")
    parts=[]
    for i in range(count):
        model=pointer(system,model_list+8*i)+8
        entries=pointer(system,model)
        count_g,cap_g=struct.unpack_from("<HH",system,model+8)
        if not 0<count_g<=cap_g<=4096:
            raise ValueError("invalid geometry array")
        materials=pointer(system,model+24)
        for j in range(count_g):
            geo=pointer(system,entries+8*j)
            vert=pointer(system,geo+8)
            index=pointer(system,pointer(system,geo+64))
            shader=struct.unpack_from("<H",system,materials+2*j)[0]
            def descriptor(at,flag):
                length=word(system,at+8)*4
                ptr=struct.unpack_from("<Q",system,at+24)[0]-0x60000000
                if word(system,at+16)!=flag or ptr<0 or ptr+length>128<<20 or not length:
                    raise ValueError("invalid GPU buffer descriptor")
                return ptr,length
            parts.append((i,j,shader,descriptor(vert,0x1601),descriptor(index,0x1380)))
    return parts

def vectors(buf,offset,code,count,width):
    return np.frombuffer(buf,dtype=code,offset=offset,count=count*width).reshape(count,width)

def decode_part(vertex,indices):
    header=np.frombuffer(vertex,dtype="<u4",count=23)
    first=header[0]
    if first not in (0x4042,0x3063):
        raise ValueError("unsupported vertex format")
    starts=(header>>8)*2
    p=int(starts[0])
    stride=8 if first==0x4042 else 12
    size=int(min(starts[starts>p]))-p
    if size%stride or not size:
        raise ValueError("invalid vertex array")
    count=size//stride
    if first==0x4042:
        q=vectors(vertex,p,"<u2",count,4)[:,1:].astype("f4")
        q[:,:2]*=256
        base=np.frombuffer(vertex,"<f4",3,0x60)
        step=np.frombuffer(vertex,"<f4",3,0x70)
        pos=q*step+base
        mode=word(vertex,0x6c)
        if mode not in (0,1,2):
            raise ValueError("unknown quantizer axis")
        pos=pos[:,[(2,1,0),(1,2,0),(0,1,2)][mode]]
    else:
        pos=vectors(vertex,p,"<f4",count,3)
    if not np.isfinite(pos).all():
        raise ValueError("nonfinite vertices")
    n,_,tri_count,_,r,_=struct.unpack_from("<6H",indices)
    tri_pos=12+16*n
    remap_pos=tri_pos+tri_count*4
    if not n or remap_pos+r*2>len(indices):
        raise ValueError("invalid index array")
    bases=vectors(indices,12,"<u2",n,8)[:,2].astype("u4")
    tri=np.frombuffer(indices,"<u4",tri_count,tri_pos)
    remap=np.frombuffer(indices,"<u2",r,remap_pos)
    polys=remap[bases[tri>>21,None]+((tri[:,None]>>np.array([0,7,14],"u4"))&127)]
    if polys.max()>=count:
        raise ValueError("triangle exceeds vertex count")
    a,b,c=polys.T
    polys=polys[(a!=b)&(b!=c)&(a!=c)]
    if not len(polys):
        raise ValueError("empty drawable geometry")
    normals=None
    if header[2]&255==0x21:
        raw=np.frombuffer(vertex,"<u4",count,int(starts[2]))
        xy=np.stack((raw&1023,(raw>>10)&1023),axis=1).astype("f4")*(2/1023)-1
        z=1-np.abs(xy).sum(axis=1)
        xy[z<0]=(1-np.abs(xy[z<0,::-1]))*np.where(xy[z<0]<0,-1,1)
        normals=np.column_stack((xy,z))
        normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-9)
    if normals is not None:
        a,b,c=polys.T
        cross=np.cross(pos[b]-pos[a],pos[c]-pos[a])
        flip=np.sum(cross*(normals[a]+normals[b]+normals[c]),axis=1)<0
        polys[flip]=polys[flip][:,[0,2,1]]
    uv=None
    fmt=header[3]&255
    if fmt==0x28:
        uv=vectors(vertex,int(starts[3]),"<i2",count,2).astype("f4")/8192
    if fmt==0x42:
        uv=vectors(vertex,int(starts[3]),"<f4",count,2)
    return pos,normals,uv,polys

class Glb:
    def __init__(self):
        self.data=bytearray()
        self.doc={"asset":{"version":"2.0","generator":"Local levels.rpf drawable extractor"},
                  "scene":0,"scenes":[{"nodes":[]}],"nodes":[],"meshes":[],
                  "buffers":[],"bufferViews":[],"accessors":[]}

    def array(self,array,typ,component,bounds=False):
        array=np.ascontiguousarray(array,dtype="<f4" if component==5126 else "<u2")
        self.data.extend(bytes((-len(self.data))%4))
        view={"buffer":0,"byteOffset":len(self.data),"byteLength":array.nbytes}
        idx=len(self.doc["bufferViews"])
        self.doc["bufferViews"].append(view)
        self.data.extend(array.tobytes())
        acc={"bufferView":idx,"componentType":component,"count":len(array),"type":typ}
        if bounds:
            acc.update(min=array.min(0).tolist(),max=array.max(0).tolist())
        self.doc["accessors"].append(acc)
        return len(self.doc["accessors"])-1

    def write(self,path):
        self.data.extend(bytes((-len(self.data))%4))
        self.doc["buffers"]=[{"byteLength":len(self.data)}]
        head=json.dumps(self.doc,separators=(",",":"),allow_nan=False).encode()
        head+=b" "*((-len(head))%4)
        with path.open("wb") as f:
            f.write(struct.pack("<4sIIII",b"glTF",2,28+len(head)+len(self.data),len(head),0x4e4f534a))
            f.write(head)
            f.write(struct.pack("<II",len(self.data),0x004e4942))
            f.write(self.data)

def yup(a):
    result=a[:,[0,2,1]].copy()
    result[:,2]*=-1
    return result

def extract(source,offset,out):
    name,system,p=read_system(source,offset)
    return export_system(source,offset,out,name,system,p)

def export_system(source,offset,out,name,system,p,root=0,archive_name="levels.rpf",*,fragment_archetype_bounds=None):
    parts=model_geometry(system,root)
    required=max(at+size for *_,vert,ind in parts for at,size in (vert,ind))
    gpu=bytearray()
    while len(gpu)<required:
        if source[p:p+1]!=b"x":
            raise ValueError("unresolved graphics stream")
        chunk,p=read_stream(source,p)
        gpu.extend(chunk)
    glb=Glb()
    info={"name":name,"member":hex(offset),"archetypeHash":_name_hash(name),"vertices":0,"triangles":0,
          "parts":len(parts)}
    mins,maxs=[],[]
    prims=[]
    for model,geometry,shader,(v,nv),(i,ni) in parts:
        pos,norm,uv,faces=decode_part(memoryview(gpu)[v:v+nv],memoryview(gpu)[i:i+ni])
        mins.append(pos.min(0))
        maxs.append(pos.max(0))
        info["vertices"]+=len(pos)
        info["triangles"]+=len(faces)
        attr={"POSITION":glb.array(yup(pos),"VEC3",5126,True)}
        if norm is not None:
            attr["NORMAL"]=glb.array(yup(norm),"VEC3",5126)
        if uv is not None and np.isfinite(uv).all():
            attr["TEXCOORD_0"]=glb.array(uv,"VEC2",5126)
        prims.append({"attributes":attr,"indices":glb.array(faces.ravel(),"SCALAR",5123),
                      "extras":{"model":model,"geometry":geometry,"shaderIndex":shader}})
    bounds=np.vstack((np.min(mins,axis=0),np.max(maxs,axis=0)))
    root_bounds=np.array([struct.unpack_from("<3f",system,root+b) for b in (48,64)])
    excess=max(np.max(root_bounds[0]-bounds[0]),np.max(bounds[1]-root_bounds[1]),0)
    if excess>max(0.3,np.linalg.norm(root_bounds[1]-root_bounds[0])*0.05):
        if fragment_archetype_bounds is None or not root or pointer(system,0x18)!=root:
            raise ValueError("geometry exceeds stored bounds")
        # Some intact fragment drawables retain a translated reference box.
        # Independently require the named archetype bounds and the drawable's dimensions.
        name_at=pointer(system,root+0xa8)
        fragment_name=name.encode("ascii").lower()+b".#ft\0"
        if bytes(system[name_at:name_at+len(fragment_name)]).lower()!=fragment_name:
            raise ValueError("translated bounds require a verified fragment drawable")
        refs=np.asarray(fragment_archetype_bounds,dtype=np.float64).reshape(-1,2,3)
        if not len(refs) or not np.isfinite(refs).all() or not np.isfinite(root_bounds).all():
            raise ValueError("missing finite fragment reference bounds")
        archetype_error=float(np.abs(refs-bounds).max())
        dimensions_error=float(np.abs(np.diff(root_bounds,axis=0)-np.diff(bounds,axis=0)).max())
        if archetype_error>0.03 or dimensions_error>0.03:
            raise ValueError("fragment bounds disagree with archetype or drawable dimensions")
        info["fragmentBoundsCheck"]={
            "maxArchetypeExtremaErrorM":archetype_error,
            "maxDrawableDimensionsErrorM":dimensions_error,
            "drawableCenterMinusMeshCenterM":(root_bounds.mean(0)-bounds.astype(np.float64).mean(0)).tolist(),
        }
    info["geometryBounds"]=bounds.tolist()
    info["drawableBounds"]=root_bounds.tolist()
    glb.doc["meshes"].append({"name":name,"primitives":prims})
    glb.doc["nodes"].append({"name":name,"mesh":0})
    glb.doc["scenes"][0]["nodes"]=[0]
    glb.doc["extras"]={"archive":archive_name,"member":hex(offset),"nameHash":hex(info["archetypeHash"])}
    if "fragmentBoundsCheck" in info:
        glb.doc["extras"]["fragmentBoundsCheck"]=info["fragmentBoundsCheck"]
    out.mkdir(exist_ok=True)
    info["file"]=f"{name}.{offset:x}.glb"
    glb.write(out/info["file"])
    return info
