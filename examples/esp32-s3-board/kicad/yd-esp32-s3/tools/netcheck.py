"""Net-first check: expected nets read from SCH V1.4 PDF (typed by net) vs KiCad's own netlist export of the redrawn schematic."""
import sys, subprocess, re, json
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from sexp import *
KC='/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli'
def usbp(u,*names): return [f'{u}.{n}' for n in names]
GNDUSB=lambda u:[f'{u}.{n}' for n in ('A1','A12','B1','B12','SH')]
VBUS=lambda u:[f'{u}.{n}' for n in ('A4','A9','B4','B9')]
# header GPIO map as read from the PDF: net -> (module pin, J pin)
HDR={'GPIO1':('U4.39','J2.4'),'GPIO2':('U4.38','J2.5'),'GPIO3':('U4.15','J1.13'),'GPIO4':('U4.4','J1.4'),'GPIO5':('U4.5','J1.5'),'GPIO6':('U4.6','J1.6'),'GPIO7':('U4.7','J1.7'),
 'GPIO8':('U4.12','J1.12'),'GPIO9':('U4.17','J1.15'),'GPIO10':('U4.18','J1.16'),'GPIO11':('U4.19','J1.17'),'GPIO12':('U4.20','J1.18'),'GPIO13':('U4.21','J1.19'),'GPIO14':('U4.22','J1.20'),
 'GPIO15':('U4.8','J1.8'),'GPIO16':('U4.9','J1.9'),'GPIO17':('U4.10','J1.10'),'GPIO18':('U4.11','J1.11'),'GPIO21':('U4.23','J2.18'),'GPIO35':('U4.28','J2.13'),'GPIO36':('U4.29','J2.12'),
 'GPIO37':('U4.30','J2.11'),'GPIO38':('U4.31','J2.10'),'GPIO39':('U4.32','J2.9'),'GPIO40':('U4.33','J2.8'),'GPIO41':('U4.34','J2.7'),'GPIO42':('U4.35','J2.6'),'GPIO45':('U4.26','J2.15'),
 'GPIO46':('U4.16','J1.14'),'GPIO47':('U4.24','J2.17')}
EXP={
 'GND':GNDUSB('USB1')+GNDUSB('USB2')+['R2.2','R3.2','R9.2','R10.2']+[f'C{i}.2' for i in range(1,11)]+['U1.3','U2.2','U2.17','U3.1','U4.1','U4.40','U4.41','PWR.1','BOOT.1','RST.1','J1.22','J2.1','J2.21','J2.22'],
 '5V':['D1.1','D2.1','D3.1','U1.1','U3.3','C4.1','USB-OTG.2','IN-OUT.2'],
 'VDD33':['U2.1','C10.1','U3.2','C3.1','R6.1','R1.2','R4.2','R8.1','R11.1','C5.1','C6.1','U4.2','J1.1','J1.2'],
 'VBUSB':VBUS('USB1')+['D1.2','C1.1','U2.3','U2.9'],
 'D+':['USB1.A6','USB1.B6','U2.7'],'D-':['USB1.A7','USB1.B7','U2.8'],
 'GPIO20':['USB2.A6','USB2.B6','U4.14','J2.19'],'GPIO19':['USB2.A7','USB2.B7','U4.13','J2.20'],
 'U0TXD':['U2.5','TX.1','U4.37','J2.2'],'U0RXD':['U2.4','RX.1','U4.36','J2.3'],
 'RTS':['U2.13','Q1.2','R7.1'],'DTR':['U2.12','R5.1','Q2.2'],
 'CHIP_PU':['Q1.3','U4.3','R8.2','C7.1','C9.1','RST.2','J1.3'],
 'GPIO0':['Q2.3','U4.27','BOOT.2','C8.1','J2.14'],
 'RGB_CTRL':['RGB.2','U1.4'],'GPIO48':['RGB.1','U4.25','J2.16'],
 # nets the PDF leaves unnamed (names are ours)
 'USB1_CC1':['USB1.A5','R2.1'],'USB1_CC2':['USB1.B5','R3.1'],'USB2_CC1':['USB2.A5','R9.1'],'USB2_CC2':['USB2.B5','R10.1'],
 'CH343_V3':['U2.6','C2.1'],'VBUS2':VBUS('USB2')+['D2.2','USB-OTG.1'],'5V_HDR':['J1.21','D3.2','IN-OUT.1'],'GPIO3_PU':['R11.2','USB-JTAG.1'],
 'LED_TX_A':['R1.1','TX.2'],'LED_RX_A':['R4.1','RX.2'],'LED_PWR_A':['R6.2','PWR.2'],'Q1_B':['R5.2','Q1.1'],'Q2_B':['R7.2','Q2.1'],
}
for n,(m,j) in HDR.items(): EXP[n]=[m,j]
EXP['GPIO3']=['U4.15','J1.13','USB-JTAG.2']
NC={'USB1.A8','USB1.B8','USB2.A8','USB2.B8','U1.2','U2.10','U2.11','U2.14','U2.15','U2.16'}
UNNAMED_IN_PDF={'USB1_CC1','USB1_CC2','USB2_CC1','USB2_CC2','CH343_V3','VBUS2','5V_HDR','GPIO3_PU','LED_TX_A','LED_RX_A','LED_PWR_A','Q1_B','Q2_B'}
def kicad_nets(sch):
    out=subprocess.run([KC,'sch','export','netlist','--format','kicadsexpr','-o','/tmp/_yd.net',sch],capture_output=True,text=True)
    t=parse(open('/tmp/_yd.net').read())
    nets={}
    for n in find(find(t,'nets')[0],'net'):
        name=str(find(n,'name')[0][1]).lstrip('/')
        nets[name]=sorted(f"{find(x,'ref')[0][1]}.{find(x,'pin')[0][1]}" for x in find(n,'node'))
    parts=[str(find(c,'ref')[0][1]) for c in find(find(t,'components')[0],'comp')]
    return nets,parts
def main(sch):
    nets,parts=kicad_nets(sch)
    rows=[];bad=0
    for name in sorted(EXP,key=lambda s:(s in UNNAMED_IN_PDF,s)):
        e=sorted(set(EXP[name])); k=sorted(nets.get(name,[]))
        ok=e==k
        if not ok: bad+=1
        rows.append((name,len(e),ok,'' if ok else f'missing {sorted(set(e)-set(k))} extra {sorted(set(k)-set(e))}'))
    extra=[n for n in nets if n not in EXP and not n.startswith('unconnected-')]
    nc_ok=all(any(p in v for v in [[]]) or True for p in NC)
    unconn=sorted(p for n,v in nets.items() if n.startswith('unconnected-') for p in v)
    return rows,bad,extra,unconn,len(parts),nets
if __name__=='__main__':
    rows,bad,extra,unconn,np_,nets=main(sys.argv[1])
    md=['| Net | Pins | Source | Result |','|---|---|---|---|']
    for n,c,ok,msg in rows: md.append(f"| {n} | {c} | {'PDF label' if n not in UNNAMED_IN_PDF else 'unnamed in PDF, name ours'} | {'match' if ok else 'MISMATCH '+msg} |")
    print('\n'.join(md)); print('\nparts in netlist',np_,'| nets checked',len(rows),'| mismatches',bad,'| extra nets in KiCad',extra,'| unconnected pins',unconn, 'expected NC',sorted(NC)==unconn)
