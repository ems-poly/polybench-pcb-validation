#!/bin/zsh
set -e
cd $YD_WORK/proj
KC=/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
KP=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
V=python3
EV=$REPO/examples/esp32-s3-board/kicad/yd-esp32-s3/evidence
mkdir -p $EV
$KC pcb export svg --mode-single -l "B.Cu,B.Silkscreen,F.Cu,F.Silkscreen,Edge.Cuts" --page-size-mode 1 --exclude-drawing-sheet --theme "KiCad Default" -o board.svg yd-esp32-s3.kicad_pcb >/dev/null 2>&1
$KP ../gen/dump_pcb.py yd-esp32-s3.kicad_pcb ../gen/pcbdump.json 2>&1 | grep -v stdpbase
$V -W ignore ../gen/render.py board $EV/board_render.png
$V -W ignore ../gen/render.py photo ../overlay_full.png 0.75
$V -W ignore ../gen/render.py highlights $REPO/examples/esp32-s3-board/YD_ESP32_S3_TEST_PLAN.md $EV
$V - <<'P'
from PIL import Image
im=Image.open(WORK+'/overlay_full.png'); im=im.resize((im.width*2//3,im.height*2//3),Image.LANCZOS); im.save('$REPO/examples/esp32-s3-board/kicad/yd-esp32-s3/evidence/overlay_pcb_on_rectified_photo.png',optimize=True)
r=Image.open(WORK+'/rect.png'); r=r.resize((r.width*2//3,r.height*2//3),Image.LANCZOS); r.save('$REPO/examples/esp32-s3-board/kicad/yd-esp32-s3/evidence/rectified_D01_top.png',optimize=True)
P
ls -la $EV
