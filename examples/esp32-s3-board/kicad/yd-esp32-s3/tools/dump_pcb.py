# KiCad python: dump pads/footprints of a board as JSON (board-relative mm)
import sys, json, pcbnew
OX,OY=110.0,90.0
b=pcbnew.LoadBoard(sys.argv[1]); mm=pcbnew.ToMM
out=[]
for fp in b.GetFootprints():
    p=fp.GetPosition(); bb=fp.GetBoundingBox(False,False)
    pads=[]
    for pad in fp.Pads():
        c=pad.GetPosition(); s=pad.GetSize(); 
        pads.append(dict(n=pad.GetNumber(),net=pad.GetNetname(),x=mm(c.x)-OX,y=mm(c.y)-OY,w=mm(s.x),h=mm(s.y),a=pad.GetOrientationDegrees(),shape=int(pad.GetShape()),th=pad.GetDrillSize().x>0,attr=int(pad.GetAttribute())))
    out.append(dict(ref=fp.GetReference(),x=mm(p.x)-OX,y=mm(p.y)-OY,rot=fp.GetOrientationDegrees(),side='bottom' if fp.IsFlipped() else 'top',
       bbox=[mm(bb.GetX())-OX,mm(bb.GetY())-OY,mm(bb.GetWidth()),mm(bb.GetHeight())],fpid=str(fp.GetFPID().GetUniStringLibId()),pads=pads,
       refpos=[mm(fp.Reference().GetPosition().x)-OX,mm(fp.Reference().GetPosition().y)-OY]))
json.dump(out,open(sys.argv[2],'w'))
print('dumped',len(out))
