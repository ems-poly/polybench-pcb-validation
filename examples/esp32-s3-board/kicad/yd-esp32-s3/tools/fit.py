import json, math, itertools
import numpy as np
from fpl import *
P=json.load(open('pdf_pads.json'))
CAND={
 'BOOT':[],'RST':[],
}
def fit(ref,fpname,padmap=None):
    fp=load_fp(fpname); lp={}
    for n,x,y,w,h in pads(fp): lp.setdefault(n,[]).append((x,y))
    A=[];B=[]
    for pn,pts in P[ref].items():
        names=padmap(pn) if padmap else [pn]
        if not names: continue
        # average lib positions of the names
        pos=[q for nm in names for q in lp.get(nm,[])]
        if not pos: continue
        lx=sum(q[0] for q in pos)/len(pos); ly=sum(q[1] for q in pos)/len(pos)
        mx=sum(q[0] for q in pts)/len(pts); my=sum(q[1] for q in pts)/len(pts)
        A.append((lx,ly)); B.append((mx,my))
    A=np.array(A);B=np.array(B)
    best=None
    for rot in (0,90,180,270):
        t=math.radians(rot); c,s=math.cos(t),math.sin(t)
        R=np.array([[c,s],[-s,c]])   # x'=x c + y s ; y'=-x s + y c
        Ar=np.dot(A,R.T)
        off=(B-Ar).mean(0)
        err=np.linalg.norm(B-(Ar+off),axis=1)
        e=(err.max(),err.mean())
        if best is None or e[0]<best[0][0]: best=(e,rot,off)
    return best
if __name__=='__main__':
    import sys
    tests=[('R1','Resistor_SMD:R_0402_1005Metric'),('C3','Capacitor_SMD:C_1206_3216Metric'),('D1','Diode_SMD:D_SOD-123F'),('D1','Diode_SMD:D_SMA'),('U2','Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm'),('U3','Package_TO_SOT_SMD:SOT-223-3_TabPin2'),('Q1','Package_TO_SOT_SMD:SOT-23'),('U1','LED_SMD:LED_WS2812B_PLCC4_5.0x5.0mm_P3.2mm'),('TX1','LED_SMD:LED_0603_1608Metric'),('U4','RF_Module:ESP32-S3-WROOM-1'),('IN0OUT','Jumper:SolderJumper-2_P1.3mm_Open_TrianglePad1.0x1.5mm')]
    for r,f in tests:
        try: print(r,f.split(':')[1],fit(r,f))
        except Exception as e: print(r,f,'ERR',e)
