"""Part positions (board mm, photo orientation: x right = antenna, y down) seeded from the maker's layout drawing and checked on the rectified photo."""
import json, sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from fit import fit, P
from design import PARTS, BOTTOM_TBR
ALIAS={'IN-OUT':'IN0OUT','RX':'RX1','TX':'TX1'}
USBM={'A1B12':['A1','B12'],'B1A12':['B1','A12'],'A4B9':['A4','B9'],'B4A9':['B4','A9'],'A5':['A5'],'A6':['A6'],'A7':['A7'],'A8':['A8'],'B5':['B5'],'B6':['B6'],'B7':['B7'],'B8':['B8']}
FLIP180=['D1','D2','D3','TX','RX','PWR']   # lib pad 1 is the cathode; maker's pad 1 taken as anode (TBR for the LEDs)
out={}
for ref,d in PARTS.items():
    if ref in BOTTOM_TBR or ref in ('J1','J2','U2','U3','C10'): continue
    pr=ALIAS.get(ref,ref)
    pm=None
    if ref.startswith('USB') and ref in ('USB1','USB2'): pm=lambda n: USBM.get(n,[])
    if ref=='U1': pm=lambda n: {'1':['1'],'2':['2'],'3':['4'],'4':['3']}.get(n,[])
    if ref in FLIP180: pm=lambda n: {'1':['2'],'2':['1']}.get(n,[])
    (emax,emean),rot,off=fit(pr,d['fp'],pm)
    out[ref]=dict(fp=d['fp'],x=round(float(off[0]),3),y=round(float(off[1]),3),rot=rot,side='top',src='layout drawing fit, err max %.2f mm'%emax)
# manual
out['J1']=dict(fp=PARTS['J1']['fp'],x=55.24,y=1.27,rot=270,side='top',src='header grid 2.54 mm, pin 1 at 1.91 mm from the antenna-end edge')
out['J2']=dict(fp=PARTS['J2']['fp'],x=55.24,y=26.67,rot=270,side='top',src='header grid 2.54 mm')
out['U3']=dict(fp=PARTS['U3']['fp'],x=28.59,y=9.37,rot=270,side='top',src='layout drawing, pads 1-3 and tab')
out['U2']=dict(fp=PARTS['U2']['fp'],x=14.25,y=21.22,rot=270,side='top',src='photo: QFN-16, silk dot at pin 1 (top-right)')
# bottom, best guess (TBR)
out['C10']=dict(fp=PARTS['C10']['fp'],x=11.1,y=20.45,rot=90,side='top',src='photo: lone 0402-size capacitor beside U2; not in the maker layout drawing; designator is a TBR guess')
out['C1']=dict(fp=PARTS['C1']['fp'],x=12.79,y=18.0,rot=0,side='top',src='photo: 0402-size capacitor above U2; C1/C2 assignment is a TBR guess')
out['C2']=dict(fp=PARTS['C2']['fp'],x=15.53,y=18.0,rot=0,side='top',src='photo: 0402-size capacitor above U2; C1/C2 assignment is a TBR guess')
out['R11']=dict(fp=PARTS['R11']['fp'],x=33.0,y=21.5,rot=0,side='bottom',src='TBR guess: near module pad 15')
out['USB-JTAG']=dict(fp=PARTS['USB-JTAG']['fp'],x=33.0,y=24.0,rot=0,side='bottom',src='TBR guess: next to R11')
out['USB-OTG']=dict(fp=PARTS['USB-OTG']['fp'],x=10.5,y=6.5,rot=0,side='bottom',src='TBR guess: behind D2')
assert set(out)==set(PARTS),set(PARTS)^set(out)
json.dump(out,open('placement.json','w'),indent=1)
for k,v in out.items(): print(k,v['x'],v['y'],v['rot'],v['src'])
