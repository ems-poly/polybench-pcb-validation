
import os
HERE=os.path.dirname(os.path.abspath(__file__))
WORK=os.environ.get('YD_WORK',os.path.join(HERE,'work'))
REPO=os.environ.get('YD_REPO',os.path.abspath(os.path.join(HERE,'..','..','..','..','..')))
import re, os
from sexp import *
from fpl import KI
_cache={}
def sym_block(libid):
    lib,name=libid.split(':')
    if lib=='yd_local': path=HERE+'/yd_local.kicad_sym'
    else: path=f'{KI}/symbols/{lib}.kicad_sym'
    if path not in _cache: _cache[path]=open(path).read()
    t=_cache[path]
    key=f'\n\t(symbol "{name}"\n'
    i=t.find(key)
    if i<0: raise KeyError(libid)
    j=t.find('\n\t)\n',i)
    return t[i+1:j+3]
def flat_block(libid):
    lib,name=libid.split(':')
    b=sym_block(libid)
    m=re.search(r'\(extends "([^"]+)"\)',b[:800])
    if not m: return b
    base=m.group(1)
    bb=sym_block(f'{lib}:{base}')
    val=re.search(r'\(property "Value" "([^"]*)"',b).group(1)
    bb=bb.replace(f'(symbol "{base}"',f'(symbol "{name}"',1).replace(f'"{base}_',f'"{name}_')
    bb=re.sub(r'\(property "Value" "[^"]*"',f'(property "Value" "{val}"',bb,1)
    return bb
def sym_parsed(libid):
    return parse(flat_block(libid))
def pins_of(libid):
    s=sym_parsed(libid); out=[]
    for sub in find(s,'symbol'):
        for p in find(sub,'pin'):
            at=find(p,'at')[0]; ln=find(p,'length')[0]
            out.append(dict(num=str(find(p,'number')[0][1]),name=str(find(p,'name')[0][1]),type=p[1],x=float(at[1]),y=float(at[2]),ang=float(at[3]),len=float(ln[1])))
    return out
def props_of(libid):
    s=sym_parsed(libid); d={}
    for p in find(s,'property'):
        at=find(p,'at')[0]; d[str(p[1])]=(float(at[1]),float(at[2]))
    ext=find(s,'extends')
    return d,ext
if __name__=='__main__':
    for l in ['Device:R','Device:C','Device:LED','Device:D','Connector:USB_C_Receptacle_USB2.0_16P','Transistor_BJT:Q_NPN_BEC','Regulator_Linear:AMS1117-3.3','Switch:SW_SPST','Connector_Generic:Conn_01x22','RF_Module:ESP32-S3-WROOM-1','power:GND','power:+5V','power:+3V3','power:PWR_FLAG']:
        b=sym_block(l); print(l,len(b),'extends' in b[:400]); 
    for l in ['Device:D','Transistor_BJT:Q_NPN_BEC','Regulator_Linear:AMS1117-3.3','RF_Module:ESP32-S3-WROOM-1']:
        print(l,[(p['num'],p['name'],p['type'],p['x'],p['y'],p['ang']) for p in pins_of(l)][:45])
