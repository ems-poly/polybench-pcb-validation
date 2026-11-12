"""Resolve every Highlight / Nets / Probe cell of the YD plan against the redrawn board with the plug-in's own resolver (polybench.kicad.select), no KiCad running."""
import sys, re
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from polybench.kicad import pcbfile
from polybench.kicad.select import plan_targets
from render import parse_plan
board,plan=sys.argv[1],sys.argv[2]
snap=pcbfile.load(board)
rows=['| Test | Parts | Pads (REF.PIN) | Nets (pads) | Missing |','|---|---|---|---|---|']; bad=0
for t in parse_plan(plan):
    hl=t['highlight']+[x for x in (t['probe_p'],t['probe_m']) if x and x not in t['highlight']]
    parts=[h for h in hl if '.' not in h]; pins=[h for h in hl if '.' in h]
    if not (parts or pins or t['nets']): continue
    p=plan_targets(snap,parts,pins,t['nets'])
    miss=p.parts_missing+p.pins_missing+p.nets_missing
    bad+=bool(miss)
    rows.append(f"| {t['id']} | {', '.join(p.parts) or '-'} | {', '.join(p.pins) or '-'} | {', '.join(f'{n} ({p.net_pad_count.get(n,0)})' for n in p.nets) or '-'} | {', '.join(miss) or 'none'} |")
print('\n'.join(rows)); print('\nunresolved:',bad)
