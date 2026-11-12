#!/bin/zsh
set -e
cd $YD_WORK/gen
KC=/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
python3.12 mk_local.py
$KC sym upgrade --force yd_local.kicad_sym >/dev/null 2>&1
cp yd_local.kicad_sym ../proj/yd_local.kicad_sym
python3.12 gen_sch.py ../proj/yd-esp32-s3.kicad_sch
$KC sch upgrade --force ../proj/yd-esp32-s3.kicad_sch >/dev/null 2>&1
