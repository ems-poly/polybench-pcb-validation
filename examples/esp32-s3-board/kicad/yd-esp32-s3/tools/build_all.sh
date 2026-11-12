#!/bin/zsh
set -e
cd $YD_WORK/gen
KC=/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
KP=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
python3 -W ignore placement.py
./build_sch.sh
$KP -u build_pcb.py ../proj/yd-esp32-s3.kicad_pcb 2>&1 | grep -v stdpbase
cd ../proj
$KC sch erc -o erc.json --format json --severity-all yd-esp32-s3.kicad_sch 2>&1 | grep Found
$KC pcb drc --schematic-parity --severity-all --format json -o drc.json yd-esp32-s3.kicad_pcb 2>&1 | grep Found
python3.12 ../gen/netcheck.py yd-esp32-s3.kicad_sch | tail -1
python3.12 - <<'P'
import json,collections
d=json.load(open('drc.json'))
for k in ('violations','unconnected_items','schematic_parity'):
    c=collections.Counter(v['type'] for v in d.get(k,[])); print(k,sum(c.values()),dict(c))
P
