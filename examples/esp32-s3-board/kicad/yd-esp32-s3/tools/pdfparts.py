"""Extract per-part pad centres from the maker's layout drawing (ESP32-S3-Metric.pdf), fit footprints. Output board mm in photo orientation."""
import os
HERE=os.path.dirname(os.path.abspath(__file__))
WORK=os.environ.get('YD_WORK',os.path.join(HERE,'work'))
REPO=os.environ.get('YD_REPO',os.path.abspath(os.path.join(HERE,'..','..','..','..','..')))
import pymupdf, re, math, json, sys
import numpy as np
from fpl import *
PDF=WORK+"/src/vcc/5-public-YD-ESP32-S3-Hardware info/ESP32-S3-Metric.pdf"
S=8.4996; X0=323.3133; Y0=58.5895; VMAX=57.15
def to_board(X,Y):
    u=(X-X0)/S; v=(Y-Y0)/S
    return (VMAX-v, u)
d=pymupdf.open(PDF); p=d[0]
red=[x['rect'] for x in p.get_drawings() if x.get('fill')==(1.0,0.0,0.0)]
refs=['BOOT','RST','IN0OUT','RGB','PWR','RX1','TX1','USB1','USB2']+[f'C{i}' for i in range(1,10)]+[f'D{i}' for i in (1,2,3)]+['J1','J2']+[f'Q{i}' for i in (1,2)]+[f'R{i}' for i in range(1,11)]+[f'U{i}' for i in range(1,5)]
refs.sort(key=len,reverse=True)
texts=[(w[4],((w[0]+w[2])/2,(w[1]+w[3])/2)) for w in p.get_text("words") if w[4].startswith('PA')]
cos={w[4][2:]:((w[0]+w[2])/2,(w[1]+w[3])/2) for w in p.get_text("words") if w[4].startswith('CO')}
parts={}
for t,c in texts:
    body=t[2:]
    for r in refs:
        if body.startswith(r):
            rest=body[len(r):]
            if re.fullmatch(r'0\d+',rest) or (r.startswith('USB') and rest.startswith('0') ) :
                pad=rest[1:] if not r.startswith('USB') else rest[1:]
                parts.setdefault(r,[]).append((pad,c)); break
            if r.startswith('USB'):
                pass
def rectc(c,maxd=9):
    best=None
    for r in red:
        rc=((r.x0+r.x1)/2,(r.y0+r.y1)/2); dd=math.hypot(rc[0]-c[0],rc[1]-c[1])
        if best is None or dd<best[0]: best=(dd,rc,r)
    return best if best[0]<maxd else None
out={}
for r,lst in parts.items():
    pts={}
    for pad,c in lst:
        b=rectc(c)
        pts.setdefault(pad,[]).append(to_board(*(b[1] if b else c)))
    out[r]=pts
json.dump(out,open(HERE+'/pdf_pads.json','w'),indent=0)
for r,pts in out.items(): print(r,len(pts),list(pts.items())[:3])
