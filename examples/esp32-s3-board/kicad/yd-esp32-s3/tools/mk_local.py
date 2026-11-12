def pin(t,x,y,a,name,num,hide=False):
    return f'\t\t\t(pin {t} line\n\t\t\t\t(at {x} {y} {a})\n\t\t\t\t(length 2.54)\n\t\t\t\t(name "{name}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t\t(number "{num}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t)\n'
def prop(n,v,x,y,hide=False):
    h='\n\t\t\t\t(hide yes)' if hide else ''
    return f'\t\t(property "{n}" "{v}"\n\t\t\t(at {x} {y} 0)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t){h}\n\t\t\t)\n\t\t)\n'
def sym(name,ref,value,fp,rect,pins):
    x0,y0,x1,y1=rect
    s=f'\t(symbol "{name}"\n\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n'
    s+=prop('Reference',ref,x0,y1+1.27)+prop('Value',value,x0,y0-1.27)+prop('Footprint',fp,0,0,True)+prop('Datasheet','',0,0,True)+prop('Description','Project-local symbol drawn from the maker schematic (YD-ESP32-S3 SCH V1.4)',0,0,True)
    s+=f'\t\t(symbol "{name}_0_1"\n\t\t\t(rectangle\n\t\t\t\t(start {x0} {y1})\n\t\t\t\t(end {x1} {y0})\n\t\t\t\t(stroke\n\t\t\t\t\t(width 0.254)\n\t\t\t\t\t(type default)\n\t\t\t\t)\n\t\t\t\t(fill\n\t\t\t\t\t(type background)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
    s+=f'\t\t(symbol "{name}_1_1"\n'+''.join(pins)+'\t\t)\n\t\t(embedded_fonts no)\n\t)\n'
    return s
left=[('VIO','1','power_in'),('GND','2','power_in'),('VDD5','3','power_in'),('TXD','4','output'),('RXD','5','input'),('V3','6','passive'),('UD+','7','bidirectional'),('UD-','8','bidirectional')]
right=[('RI','16','input'),('CTS','15','input'),('DSR','14','input'),('RTS','13','output'),('DTR','12','output'),('DCD','11','input'),('ACT#','10','output'),('VBUS','9','input')]
pins=[]
for i,(n,num,t) in enumerate(left): pins.append(pin(t,-12.7,round(10.16-2.54*i,2),0,n,num))
for i,(n,num,t) in enumerate(right): pins.append(pin(t,12.7,round(10.16-2.54*i,2),180,n,num))
pins.append(pin('power_in',0,15.24,270,'EP','17'))
ch=sym('CH343P','U','CH343P','Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm',(-10.16,-10.16,10.16,12.7),pins)
ws=sym('XL-5050RGBC-WS2812B','U','XL-5050RGBC-WS2812B','LED_SMD:LED_WS2812B_PLCC4_5.0x5.0mm_P3.2mm',(-5.08,-5.08,5.08,5.08),
  [pin('power_in',-7.62,2.54,0,'VDD','1'),pin('output',-7.62,-2.54,0,'DO','2'),pin('input',7.62,2.54,180,'DI','4'),pin('power_in',7.62,-2.54,180,'GND','3')])
reg=sym('CJ6107A33GW','U','CJ6107A33GW','Package_TO_SOT_SMD:SOT-223-3_TabPin2',(-7.62,-5.08,7.62,5.08),
  [pin('power_in',-10.16,0,0,'IN','3'),pin('power_out',10.16,0,180,'OUT','2'),pin('power_in',0,-7.62,90,'GND','1')])
open('yd_local.kicad_sym','w').write('(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "kicad_symbol_editor")\n\t(generator_version "10.0")\n'+ch+ws+reg+')\n')
