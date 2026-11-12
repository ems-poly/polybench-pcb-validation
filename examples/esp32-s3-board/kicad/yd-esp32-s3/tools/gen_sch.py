import uuid, math, re, sys, json
from libx import *
from design import *
NS=uuid.UUID('6d3f2a10-0000-4000-8000-000000000001')
def U(*k): return str(uuid.uuid5(NS,'/'.join(map(str,k))))
ROOT=U('root')
PROJ='yd-esp32-s3'
LAYOUT={  # ref: (x,y,rot) ; rot 90 puts 2-pin parts horizontal
 'USB1':(40,60,0),'USB2':(40,140,0),'U2':(125,70,0),'U1':(125,125,0),'U3':(125,170,0),'Q1':(200,60,0),'Q2':(200,100,0),
 'U4':(285,115,0),'J1':(375,65,0),'J2':(375,150,0)}
SMALL=[r for r in PARTS if r not in LAYOUT]
order=['R2','R3','R9','R10','D1','D2','D3','USB-OTG','IN-OUT','R5','R7','R8','R11','USB-JTAG','C1','C2','C7','C8','C9','C10','C3','C4','C5','C6',
       'R1','R4','R6','TX','RX','PWR','RGB','BOOT','RST']
assert sorted(order)==sorted(SMALL),(set(SMALL)^set(order))
cols=[40,100,160,220,280,340]; 
for i,r in enumerate(order):
    c=i%6; row=i//6
    LAYOUT[r]=(cols[c],205+row*12.7,90)
def rotpt(x,y,rot):
    t=math.radians(rot); c,s=round(math.cos(t),6),round(math.sin(t),6)
    return (x*c-y*s, x*s+y*c)
def pin_sch(p,X,Y,rot):
    rx,ry=rotpt(p['x'],p['y'],rot); return (round(X+rx,3),round(Y-ry,3))
def outward(p,rot):
    a=(p['ang']+180+rot)%360; return a
DIR={0:(1,0),90:(0,-1),180:(-1,0),270:(0,1)}   # sch coords (y down)
out=[]
syms_used=set()
body=[]
def add(s): body.append(s)
def eff(just=None,hide=False,size=1.27):
    j=f'\n\t\t\t(justify {just})' if just else ''
    h='\n\t\t\t(hide yes)' if hide else ''
    return f'(effects\n\t\t\t(font\n\t\t\t\t(size {size} {size})\n\t\t\t){j}{h}\n\t\t)'
cnt={'pwr':0}
def wire(a,b):
    add(f'\t(wire\n\t\t(pts\n\t\t\t(xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})\n\t\t)\n\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid "{U("wire",a,b,len(body))}")\n\t)\n')
def label(name,pt,ang):
    just={0:'left',90:'left',180:'right',270:'right'}[ang]
    add(f'\t(global_label "{name}"\n\t\t(shape bidirectional)\n\t\t(at {pt[0]} {pt[1]} {ang})\n\t\t(fields_autoplaced yes)\n\t\t{eff(just)}\n\t\t(uuid "{U("label",name,pt)}")\n\t\t(property "Intersheet References" "${{INTERSHEET_REFS}}"\n\t\t\t(at {pt[0]} {pt[1]} 0)\n\t\t\t{eff(None,True)}\n\t\t)\n\t)\n')
def noconn(pt):
    add(f'\t(no_connect\n\t\t(at {pt[0]} {pt[1]})\n\t\t(uuid "{U("nc",pt)}")\n\t)\n')
def place_sym(libid,ref,value,fp,X,Y,rot,extra_props=None,power=False,pins_nets=None,unitpins=None):
    syms_used.add(libid)
    props,ext=props_of(libid)
    def tp(name):
        x,y=props.get(name,(0,0)); rx,ry=rotpt(x,y,rot); return (round(X+rx,3),round(Y-ry,3))
    s=f'\t(symbol\n\t\t(lib_id "{libid}")\n\t\t(at {X} {Y} {rot})\n\t\t(unit 1)\n\t\t(exclude_from_sim no)\n\t\t(in_bom {"no" if power else "yes"})\n\t\t(on_board {"no" if power else "yes"})\n\t\t(dnp no)\n\t\t(uuid "{U("sym",ref)}")\n'
    rp=tp('Reference'); vp=tp('Value')
    if power:
        s+=f'\t\t(property "Reference" "{ref}"\n\t\t\t(at {X} {Y} 0)\n\t\t\t{eff(None,True)}\n\t\t)\n'
        s+=f'\t\t(property "Value" "{value}"\n\t\t\t(at {vp[0]} {vp[1]} 0)\n\t\t\t{eff()}\n\t\t)\n'
    else:
        s+=f'\t\t(property "Reference" "{ref}"\n\t\t\t(at {rp[0]} {rp[1]} 0)\n\t\t\t{eff("left")}\n\t\t)\n'
        s+=f'\t\t(property "Value" "{value}"\n\t\t\t(at {vp[0]} {vp[1]+2.54} 0)\n\t\t\t{eff("left")}\n\t\t)\n'
    s+=f'\t\t(property "Footprint" "{fp}"\n\t\t\t(at {X} {Y} 0)\n\t\t\t{eff(None,True)}\n\t\t)\n'
    s+=f'\t\t(property "Datasheet" "~"\n\t\t\t(at {X} {Y} 0)\n\t\t\t{eff(None,True)}\n\t\t)\n'
    for k,v in (extra_props or {}).items():
        s+=f'\t\t(property "{k}" "{v}"\n\t\t\t(at {X} {Y} 0)\n\t\t\t{eff(None,True)}\n\t\t)\n'
    pins=pins_of(libid)
    seen=set()
    for p in pins:
        if p['num'] in seen: continue
        seen.add(p['num'])
        s+=f'\t\t(pin "{p["num"]}"\n\t\t\t(uuid "{U("pin",ref,p["num"])}")\n\t\t)\n'
    s+=f'\t\t(instances\n\t\t\t(project "{PROJ}"\n\t\t\t\t(path "/{ROOT}"\n\t\t\t\t\t(reference "{ref}")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n'
    add(s)
    return pins
POWER={'GND':('power:GND',270),'5V':('power:+5V',90),'VDD33':('power:+3V3',90)}   # natural body-out direction
def connect(pins,ref,X,Y,rot,nets):
    done={}
    for p in pins:
        net=nets[p['num']]
        pt=pin_sch(p,X,Y,rot); a=int(outward(p,rot))%360
        key=(pt)
        if key in done:
            assert done[key]==net,(ref,p['num'],net,done[key]); continue
        done[key]=net
        if net=='NC':
            noconn(pt); continue
        dx,dy=DIR[a]; end=(round(pt[0]+2.54*dx,3),round(pt[1]+2.54*dy,3))
        wire(pt,end)
        if net in POWER:
            lid,nat=POWER[net]; cnt['pwr']+=1
            r=(a-nat)%360
            # rot convention: sch rot positive = CCW on screen (same as lib y-up)
            place_sym(lid,f'#PWR{cnt["pwr"]:03d}',net,'',end[0],end[1],r,power=True)
        else:
            label(net,end,a)
def snap(v): return round(round(v/2.54)*2.54,3)
for ref,(X,Y,rot) in LAYOUT.items():
    X,Y=snap(X),snap(Y)
    d=PARTS[ref]
    pins=place_sym(d['sym'],ref,d['value'],d['fp'],X,Y,rot)
    connect(pins,ref,X,Y,rot,d['pins'])
# PWR_FLAGs
for i,net in enumerate(['GND','5V','VBUSB']):
    X=30.48+i*25.4; Y=266.7
    place_sym('power:PWR_FLAG',f'#FLG{i+1:02d}','PWR_FLAG','',X,Y,0,power=True)
    wire((X,Y),(X,round(Y+2.54,3)))
    if net in POWER:
        cnt['pwr']+=1; lid,nat=POWER[net]; place_sym(lid,f'#PWR{cnt["pwr"]:03d}',net,'',X,round(Y+2.54,3),(270-nat)%360,power=True)
    else: label(net,(X,round(Y+2.54,3)),270)
# text notes
def text(s,x,y,size=1.8):
    add(f'\t(text "{s}"\n\t\t(exclude_from_sim no)\n\t\t(at {x} {y} 0)\n\t\t{eff("left",False,size)}\n\t\t(uuid "{U("text",s)}")\n\t)\n')
text('YD-ESP32-S3 COREBOARD V1.4: redrawn for the Polybench example (not VCC-GND design files; not for manufacturing). See PROVENANCE.md.',15,12,2.5)
text('Designators and values follow the maker PDF (SCH V1.4, 2022-09-23). Nets are wired by label; nets with names ending _A, _B, _CC1, _CC2, _V3, _PU, VBUS2, 5V_HDR are named by us (the PDF leaves them unnamed).',15,17,1.5)
text('USB-C symbol: KiCad standard (A1/B12 etc. split); the PDF symbol merges A1B12, A4B9, B1A12, B4A9. Electrically identical.',15,21,1.5)
text('Two-pin jumpers RGB, IN-OUT, USB-OTG, USB-JTAG are 0 ohm in the PDF; the layout drawing shows solder-jumper pads (footprint chosen to match).',15,25,1.5)
# lib symbols
libs=''
for lid in sorted(syms_used):
    b=flat_block(lid)
    lib,name=lid.split(':')
    b=b.replace(f'(symbol "{name}"',f'(symbol "{lid}"',1)
    libs+=b+'\n' if not b.endswith('\n') else b
hdr=f'(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n\t(generator_version "9.0")\n\t(uuid "{ROOT}")\n\t(paper "A3")\n\t(title_block\n\t\t(title "YD-ESP32-S3 COREBOARD V1.4 (redraw)")\n\t\t(date "2026-11-06")\n\t\t(rev "r1")\n\t\t(company "Polystage, for the Polybench example")\n\t\t(comment 1 "Redrawn from VCC-GND SCH V1.4. Not VCC-GND design files. Not for manufacturing.")\n\t)\n\t(lib_symbols\n'
txt=hdr+libs+'\t)\n'+''.join(body)+f'\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n\t(embedded_fonts no)\n)\n'
import os
dst=sys.argv[1]
os.makedirs(os.path.dirname(dst),exist_ok=True)
open(dst,'w').write(txt)
print('wrote',dst,len(txt))
