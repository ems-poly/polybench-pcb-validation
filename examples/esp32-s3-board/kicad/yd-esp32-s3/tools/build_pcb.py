# run with KiCad's python:  .../Python.framework/Versions/Current/bin/python3 build_pcb.py OUT.kicad_pcb
import os
HERE=os.path.dirname(os.path.abspath(__file__))
WORK=os.environ.get('YD_WORK',os.path.join(HERE,'work'))
REPO=os.environ.get('YD_REPO',os.path.abspath(os.path.join(HERE,'..','..','..','..','..')))
import sys, json, uuid
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import pcbnew
from pcbnew import FromMM, VECTOR2I
from design import PARTS
from libx import pins_of
KI='/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints'
NS=uuid.UUID('6d3f2a10-0000-4000-8000-000000000001')
ROOT=str(uuid.uuid5(NS,'root'))
def U(*k): return str(uuid.uuid5(NS,'/'.join(map(str,k))))
OX,OY=110.0,90.0       # board origin on the sheet
LABEL={'R9':(-2.4,0),'R10':(-2.5,0),'R2':(-2.4,0),'R3':(-2.4,0),'R1':(-2.1,0),'R4':(-2.1,0),'R6':(-2.1,0),'TX':(-2.3,0),'RX':(-2.3,0),'PWR':(-2.5,0),
 'R5':(2.2,0),'R8':(2.2,0),'C7':(2.2,0),'R7':(2.2,0),'C5':(2.2,0),'C6':(2.2,0),'C8':(0,-1.1),'C9':(0,-1.1),'D1':(0,-1.45),'D2':(0,-1.45),'D3':(0,-1.45),'IN-OUT':(0,-1.5),'RGB':(0,-1.5),
 'C1':(0,-1.0),'C2':(0,-1.0),'C10':(-1.6,0),'Q1':(2.5,-2.4),'Q2':(2.5,-2.4),'USB-OTG':(0,-1.5),'R11':(0,-1.5),'USB-JTAG':(0,-1.5)}
PL=json.load(open(HERE+'/placement.json'))
b=pcbnew.BOARD()
nets={}
def net(name):
    if name not in nets:
        n=pcbnew.NETINFO_ITEM(b,name); b.Add(n); nets[name]=n
    return nets[name]
for ref,d in PARTS.items():
    p=PL[ref]; lib,name=p['fp'].split(':')
    fp=pcbnew.FootprintLoad(f'{KI}/{lib}.pretty',name)
    fp.SetFPID(pcbnew.LIB_ID(lib,name)); fp.SetReference(ref); fp.SetValue(d['value'])
    if 'SolderJumper' in name: fp.SetExcludedFromBOM(False)
    pos=VECTOR2I(FromMM(OX+p['x']),FromMM(OY+p['y']))
    fp.SetPosition(pos)
    b.Add(fp)
    if p['side']=='bottom':
        fp.Flip(pos,pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    fp.SetOrientationDegrees(p['rot'])
    for pad in fp.Pads():
        nm=pad.GetNumber()
        n=d['pins'].get(nm)
        if n and n!='NC': pad.SetNet(net(n))
        elif n=='NC':
            pn=[q['name'] for q in pins_of(d['sym']) if q['num']==nm][0]
            pad.SetNet(net(f'unconnected-({ref}-{pn}-Pad{nm})'))
    fp.SetPath(pcbnew.KIID_PATH('/'+ROOT+'/'+U('sym',ref)))
    # reference text: silkscreen, readable
    r=fp.Reference(); sz=0.8 if d['fp'].split(':')[1].startswith(('USB_C','ESP32','PinHeader','SW_','QFN','SOT-223','LED_WS')) else 0.6
    r.SetTextSize(pcbnew.VECTOR2I(FromMM(sz),FromMM(sz))); r.SetTextThickness(FromMM(0.1 if sz<0.8 else 0.12)); r.SetVisible(True)
    if ref in LABEL:
        dx,dy=LABEL[ref]; r.SetPosition(VECTOR2I(FromMM(OX+p['x']+dx),FromMM(OY+p['y']+dy))); r.SetTextAngleDegrees(0)
    v=fp.Value(); v.SetVisible(False)
# outline 57.15 x 27.94
for (x0,y0,x1,y1) in [(0,0,57.15,0),(57.15,0,57.15,27.94),(57.15,27.94,0,27.94),(0,27.94,0,0)]:
    s=pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(FromMM(0.1))
    s.SetStart(VECTOR2I(FromMM(OX+x0),FromMM(OY+y0))); s.SetEnd(VECTOR2I(FromMM(OX+x1),FromMM(OY+y1))); b.Add(s)
# note text
t=pcbnew.PCB_TEXT(b); t.SetText('YD-ESP32-S3 (redraw for Polybench example) - NOT FOR MANUFACTURING - no copper routing'); t.SetLayer(pcbnew.Cmts_User)
t.SetPosition(VECTOR2I(FromMM(OX+28),FromMM(OY+34))); t.SetTextSize(VECTOR2I(FromMM(1.2),FromMM(1.2))); b.Add(t)
ts=b.GetTitleBlock(); ts.SetTitle('YD-ESP32-S3 COREBOARD V1.4 (redraw)'); ts.SetRevision('r1'); ts.SetDate('2026-11-06'); ts.SetCompany('Polystage, for the Polybench example'); ts.SetComment(0,'Redrawn from VCC-GND SCH V1.4 and a photo. Not VCC-GND design files. Not for manufacturing.')
pcbnew.SaveBoard(sys.argv[1],b)
print('saved',sys.argv[1],len(list(b.GetFootprints())),'footprints',len(nets),'nets')
