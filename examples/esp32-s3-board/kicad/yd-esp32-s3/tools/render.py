
import os
HERE=os.path.dirname(os.path.abspath(__file__))
WORK=os.environ.get('YD_WORK',os.path.join(HERE,'work'))
REPO=os.environ.get('YD_REPO',os.path.abspath(os.path.join(HERE,'..','..','..','..','..')))
import sys, json, re, math, html, base64
from playwright.sync_api import sync_playwright
OX,OY=110.0,90.0
SVG=open(WORK+'/proj/board.svg').read()
SVG=re.sub(r'<\?xml.*?\?>|<!DOCTYPE.*?>','',SVG,flags=re.S)
D={f['ref']:f for f in json.load(open(HERE+'/pcbdump.json'))}
NETPADS={}
for r,f in D.items():
    for p in f['pads']:
        if p['net']: NETPADS.setdefault(p['net'],[]).append((r,p))
def svg_inner(vb,w,h,extra='',opacity=1.0,bg='#ffffff'):
    s=SVG.replace('width="297.0022mm" height="210.0072mm" viewBox="0.0000 0.0000 297.0022 210.0072"',f'width="{w}" height="{h}" viewBox="{vb}" preserveAspectRatio="xMidYMid meet"')
    s=s.replace('</svg>',extra+'</svg>')
    return s
def pad_of(ref_pin):
    ref,pin=ref_pin.rsplit('.',1); f=D[ref]
    return [p for p in f['pads'] if p['n']==pin]
def overlay_for(test):
    o=[]
    sc=1.0
    def circ(x,y,r,col,w=0.25,fill='none',op=1): return f'<circle cx="{OX+x:.3f}" cy="{OY+y:.3f}" r="{r}" fill="{fill}" stroke="{col}" stroke-width="{w}" opacity="{op}"/>'
    def txt(x,y,t,col,size=1.5,anchor='start'): return f'<text x="{OX+x:.3f}" y="{OY+y:.3f}" font-family="Helvetica,Arial,sans-serif" font-size="{size}" font-weight="bold" fill="{col}" text-anchor="{anchor}" stroke="#000" stroke-width="0.12" paint-order="stroke">{html.escape(t)}</text>'
    # nets
    for n in test['nets']:
        pads=NETPADS.get(n,[])
        pts=[(p['x'],p['y']) for r,p in pads]
        for (x,y) in pts: o.append(circ(x,y,0.95,'#00e5ff',0.2,'#00e5ff',0.35))
        if 1<len(pts)<=9:  # ratsnest by nearest-neighbour chain
            left=pts[:]; cur=left.pop(0); chain=[cur]
            while left:
                nxt=min(left,key=lambda q:(q[0]-cur[0])**2+(q[1]-cur[1])**2); left.remove(nxt); o.append(f'<line x1="{OX+cur[0]:.3f}" y1="{OY+cur[1]:.3f}" x2="{OX+nxt[0]:.3f}" y2="{OY+nxt[1]:.3f}" stroke="#00e5ff" stroke-width="0.18" stroke-dasharray="0.5 0.35"/>'); cur=nxt
    # parts
    roles={}
    for key,lab in (('probe_p',' (red lead +)'),('probe_m',' (black lead -)')):
        v=test.get(key)
        if v: roles[v]=lab
    k=0
    for d in test['highlight']:
        if '.' in d:
            for p in pad_of(d):
                o.append(circ(p['x'],p['y'],1.6,'#ff2d2d',0.35))
                up=p['y']>14
                ly=p['y']-2.6-(k%2)*1.9 if up else p['y']+3.2+(k%2)*1.9
                o.append(f'<line x1="{OX+p["x"]:.3f}" y1="{OY+p["y"]+(-1.6 if up else 1.6):.3f}" x2="{OX+p["x"]:.3f}" y2="{OY+ly+(0.4 if up else -1.2):.3f}" stroke="#ff6b6b" stroke-width="0.15"/>')
                o.append(txt(p['x'],ly,d+roles.get(d,''),'#ff9a9a',1.5,'middle'))
            k+=1
        else:
            f=D[d]; x,y,w,h=f['bbox']; pad=0.5
            if w*h>1200:
                xs=[q['x'] for q in f['pads']]; ys=[q['y'] for q in f['pads']]; x,y,w,h=min(xs)-1,min(ys)-1,max(xs)-min(xs)+2,max(ys)-min(ys)+2
            o.append(f'<rect x="{OX+x-pad:.3f}" y="{OY+y-pad:.3f}" width="{w+2*pad:.3f}" height="{h+2*pad:.3f}" rx="0.6" fill="#ff00ff" fill-opacity="0.16" stroke="#ff00ff" stroke-width="0.3"/>')
            o.append(txt(x+w/2,y-1.0,d,'#ff8cff',1.6,'middle'))
    return ''.join(o)
def parse_plan(path):
    tests=[]
    for ln in open(path):
        m=re.match(r'\|\s*([UPF]\d+)\s*\|(.*)\|\s*$',ln)
        if not m: continue
        cells=[c.strip() for c in ('|'+ln.strip()).split('|')[1:-1]] if False else [c.strip() for c in ln.strip().strip('|').split('|')]
        if len(cells)<15 or cells[0] in ('ID',): continue
        tid,title,pp,pm,nets,hl=cells[:6]
        sp=lambda s:[] if s in ('—','') else [x.strip() for x in s.split(',')]
        tests.append(dict(id=tid,title=title,nets=sp(nets),highlight=sp(hl),probe_p=None if pp=='—' else pp,probe_m=None if pm=='—' else pm))
    return tests
def shoot(html_doc,out,w,h):
    with sync_playwright() as p:
        b=p.webkit.launch(); pg=b.new_page(viewport={'width':w,'height':h}); pg.set_content(html_doc); pg.wait_for_timeout(300); pg.screenshot(path=out); b.close()
if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='highlights':
        outdir=sys.argv[3]; tests=[t for t in parse_plan(sys.argv[2]) if t['nets'] or t['highlight']]
        W,H=1640,860
        for t in tests:
            vb=f'{OX-8} {OY-6} 82 43'
            sv=svg_inner(vb,W,H,overlay_for(t))
            cap=f'{t["id"]}  {t["title"]}   |   parts (magenta): {", ".join([h for h in t["highlight"] if "." not in h]) or "none"}   pads (red ring): {", ".join([h for h in t["highlight"] if "." in h]) or "none"}   nets (cyan): {", ".join(t["nets"]) or "none"}'
            doc=f'<html><body style="margin:0;background:#0b0b0b"><div style="color:#ddd;font:14px Helvetica,Arial;padding:6px 10px;height:22px">{html.escape(cap)}</div>{sv}</body></html>'
            shoot(doc,f'{outdir}/{t["id"]}_highlight.png',W,H+36); print('wrote',t['id'])
    elif mode=='photo':
        S=24; vb=f'{OX-8} {OY-5} 75 38'; W,H=75*S,38*S
        sv=svg_inner(vb,W,H)
        doc=f'<html><body style="margin:0;background:#000;position:relative;width:{W}px;height:{H}px"><img src="data:image/png;base64,{base64.b64encode(open("$YD_WORK/rect.png","rb").read()).decode()}" style="position:absolute;left:0;top:0;width:{W}px;height:{H}px"><div style="position:absolute;left:0;top:0;opacity:{sys.argv[3] if len(sys.argv)>3 else 0.8}">{sv}</div></body></html>'
        shoot(doc,sys.argv[2],W,H)
    elif mode=='board':
        W,H=1640,860; vb=f'{OX-8} {OY-6} 82 43'
        doc=f'<html><body style="margin:0;background:#0b0b0b">{svg_inner(vb,W,H)}</body></html>'
        shoot(doc,sys.argv[2],W,H)
