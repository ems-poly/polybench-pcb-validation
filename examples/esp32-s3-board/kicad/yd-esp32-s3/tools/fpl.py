import os
from sexp import *
KI='/Applications/KiCad/KiCad.app/Contents/SharedSupport'
def load_fp(lib_name):
    lib,name=lib_name.split(':')
    return parse(open(f'{KI}/footprints/{lib}.pretty/{name}.kicad_mod').read())
def pads(fp):
    out=[]
    for p in find(fp,'pad'):
        at=find(p,'at')[0]; sz=find(p,'size')[0]
        out.append((str(p[1]),float(at[1]),float(at[2]),float(sz[1]),float(sz[2])))
    return out
