"""The YD-ESP32-S3 COREBOARD V1.4 schematic, as read from the maker's PDF (see PROVENANCE.md).
PARTS: ref -> dict(sym=lib id, value, fp, pins={pin number: net}).  'NC' = deliberately unconnected.
Nets named only by us (not in the PDF) are listed in OUR_NAMES."""
R4='Resistor_SMD:R_0402_1005Metric'; C4_='Capacitor_SMD:C_0402_1005Metric'; C6_='Capacitor_SMD:C_0603_1608Metric'
C12='Capacitor_SMD:C_1206_3216Metric'; LED6='LED_SMD:LED_0603_1608Metric'; SJ='Jumper:SolderJumper-2_P1.3mm_Open_TrianglePad1.0x1.5mm'
OUR_NAMES={'USB1_CC1':'USB1 CC1 to R2','USB1_CC2':'USB1 CC2 to R3','USB2_CC1':'USB2 CC1 to R9','USB2_CC2':'USB2 CC2 to R10',
 'CH343_V3':'U2 V3 to C2','VBUS2':'USB2 VBUS, before D2 / USB-OTG','5V_HDR':'J1.21 and D3 anode / IN-OUT (header side of D3)',
 'GPIO3_PU':'R11 to USB-JTAG','LED_TX_A':'R1 to LED TX anode','LED_RX_A':'R4 to LED RX anode','LED_PWR_A':'R6 to LED PWR anode',
 'Q1_B':'R5 to Q1 base','Q2_B':'R7 to Q2 base'}
def usb(vbus,cc1,cc2,dp,dn):
    p={'A1':'GND','A12':'GND','B1':'GND','B12':'GND','SH':'GND','A4':vbus,'A9':vbus,'B4':vbus,'B9':vbus,'A5':cc1,'B5':cc2,
       'A6':dp,'B6':dp,'A7':dn,'B7':dn,'A8':'NC','B8':'NC'}
    return p
def hdr(names): return {str(i+1):n for i,n in enumerate(names)}
J1=['VDD33','VDD33','CHIP_PU','GPIO4','GPIO5','GPIO6','GPIO7','GPIO15','GPIO16','GPIO17','GPIO18','GPIO8','GPIO3','GPIO46','GPIO9','GPIO10','GPIO11','GPIO12','GPIO13','GPIO14','5V_HDR','GND']
J2=['GND','U0TXD','U0RXD','GPIO1','GPIO2','GPIO42','GPIO41','GPIO40','GPIO39','GPIO38','GPIO37','GPIO36','GPIO35','GPIO0','GPIO45','GPIO48','GPIO47','GPIO21','GPIO20','GPIO19','GND','GND']
PARTS={
 'USB1':dict(sym='Connector:USB_C_Receptacle_USB2.0_16P',value='TYPEC-304-BCP16',fp='Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12',pins=usb('VBUSB','USB1_CC1','USB1_CC2','D+','D-')),
 'USB2':dict(sym='Connector:USB_C_Receptacle_USB2.0_16P',value='TYPEC-304-BCP16',fp='Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12',pins=usb('VBUS2','USB2_CC1','USB2_CC2','GPIO20','GPIO19')),
 'D1':dict(sym='Device:D',value='D26',fp='Diode_SMD:D_SOD-123F',pins={'2':'VBUSB','1':'5V'}),   # D pin1=K pin2=A
 'D2':dict(sym='Device:D',value='D26',fp='Diode_SMD:D_SOD-123F',pins={'2':'VBUS2','1':'5V'}),
 'D3':dict(sym='Device:D',value='D26',fp='Diode_SMD:D_SOD-123F',pins={'2':'5V_HDR','1':'5V'}),
 'USB-OTG':dict(sym='Device:R',value='0',fp=SJ,pins={'1':'VBUS2','2':'5V'}),
 'IN-OUT':dict(sym='Device:R',value='0',fp=SJ,pins={'1':'5V_HDR','2':'5V'}),
 'RGB':dict(sym='Device:R',value='0',fp=SJ,pins={'1':'GPIO48','2':'RGB_CTRL'}),
 'USB-JTAG':dict(sym='Device:R',value='0',fp=SJ,pins={'1':'GPIO3_PU','2':'GPIO3'}),
 'R1':dict(sym='Device:R',value='1k',fp=R4,pins={'1':'LED_TX_A','2':'VDD33'}),
 'R4':dict(sym='Device:R',value='1k',fp=R4,pins={'1':'LED_RX_A','2':'VDD33'}),
 'R6':dict(sym='Device:R',value='1k',fp=R4,pins={'1':'VDD33','2':'LED_PWR_A'}),
 'R2':dict(sym='Device:R',value='5.1k',fp=R4,pins={'1':'USB1_CC1','2':'GND'}),
 'R3':dict(sym='Device:R',value='5.1k',fp=R4,pins={'1':'USB1_CC2','2':'GND'}),
 'R9':dict(sym='Device:R',value='5.1k',fp=R4,pins={'1':'USB2_CC1','2':'GND'}),
 'R10':dict(sym='Device:R',value='5.1k',fp=R4,pins={'1':'USB2_CC2','2':'GND'}),
 'R5':dict(sym='Device:R',value='10k',fp=R4,pins={'1':'DTR','2':'Q1_B'}),
 'R7':dict(sym='Device:R',value='10k',fp=R4,pins={'1':'RTS','2':'Q2_B'}),
 'R8':dict(sym='Device:R',value='10k',fp=R4,pins={'1':'VDD33','2':'CHIP_PU'}),
 'R11':dict(sym='Device:R',value='10k',fp=R4,pins={'1':'VDD33','2':'GPIO3_PU'}),
 'C1':dict(sym='Device:C',value='1uF',fp=C4_,pins={'1':'VBUSB','2':'GND'}),
 'C2':dict(sym='Device:C',value='100nF',fp=C4_,pins={'1':'CH343_V3','2':'GND'}),
 'C3':dict(sym='Device:C',value='10uF',fp=C12,pins={'1':'VDD33','2':'GND'}),
 'C4':dict(sym='Device:C',value='10uF',fp=C12,pins={'1':'5V','2':'GND'}),
 'C5':dict(sym='Device:C',value='10uF',fp=C4_,pins={'1':'VDD33','2':'GND'}),
 'C6':dict(sym='Device:C',value='100nF',fp=C4_,pins={'1':'VDD33','2':'GND'}),
 'C7':dict(sym='Device:C',value='1uF',fp=C4_,pins={'1':'CHIP_PU','2':'GND'}),
 'C8':dict(sym='Device:C',value='100nF',fp=C4_,pins={'1':'GPIO0','2':'GND'}),
 'C9':dict(sym='Device:C',value='100nF',fp=C4_,pins={'1':'CHIP_PU','2':'GND'}),
 'C10':dict(sym='Device:C',value='1uF',fp=C4_,pins={'1':'VDD33','2':'GND'}),
 'TX':dict(sym='Device:LED',value='LED2',fp=LED6,pins={'1':'U0TXD','2':'LED_TX_A'}),   # LED pin1=K pin2=A
 'RX':dict(sym='Device:LED',value='LED2',fp=LED6,pins={'1':'U0RXD','2':'LED_RX_A'}),
 'PWR':dict(sym='Device:LED',value='LED-RED',fp=LED6,pins={'1':'GND','2':'LED_PWR_A'}),
 'BOOT':dict(sym='Switch:SW_SPST',value='ST-1185S',fp='Button_Switch_SMD:SW_SPST_TS-1088-xR020',pins={'1':'GND','2':'GPIO0'}),
 'RST':dict(sym='Switch:SW_SPST',value='ST-1185S',fp='Button_Switch_SMD:SW_SPST_TS-1088-xR020',pins={'1':'GND','2':'CHIP_PU'}),
 'Q1':dict(sym='Transistor_BJT:Q_NPN_BEC',value='SS8050-G',fp='Package_TO_SOT_SMD:SOT-23',pins={'1':'Q1_B','2':'RTS','3':'CHIP_PU'}),
 'Q2':dict(sym='Transistor_BJT:Q_NPN_BEC',value='SS8050-G',fp='Package_TO_SOT_SMD:SOT-23',pins={'1':'Q2_B','2':'DTR','3':'GPIO0'}),
 'U1':dict(sym='yd_local:XL-5050RGBC-WS2812B',value='XL-5050RGBC-WS2812B',fp='LED_SMD:LED_WS2812B_PLCC4_5.0x5.0mm_P3.2mm',pins={'1':'5V','2':'NC','3':'GND','4':'RGB_CTRL'}),
 'U2':dict(sym='yd_local:CH343P',value='CH343P',fp='Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm',
   pins={'1':'VDD33','2':'GND','3':'VBUSB','4':'U0RXD','5':'U0TXD','6':'CH343_V3','7':'D+','8':'D-','9':'VBUSB','10':'NC','11':'NC','12':'DTR','13':'RTS','14':'NC','15':'NC','16':'NC','17':'GND'}),
 'U3':dict(sym='yd_local:CJ6107A33GW',value='CJ6107A33GW',fp='Package_TO_SOT_SMD:SOT-223-3_TabPin2',pins={'1':'GND','2':'VDD33','3':'5V'}),
 'U4':dict(sym='RF_Module:ESP32-S3-WROOM-1',value='ESP32-S3-WROOM-1',fp='RF_Module:ESP32-S3-WROOM-1',
   pins={'1':'GND','40':'GND','41':'GND','2':'VDD33','3':'CHIP_PU','4':'GPIO4','5':'GPIO5','6':'GPIO6','7':'GPIO7','8':'GPIO15','9':'GPIO16','10':'GPIO17','11':'GPIO18','12':'GPIO8','13':'GPIO19','14':'GPIO20',
         '15':'GPIO3','16':'GPIO46','17':'GPIO9','18':'GPIO10','19':'GPIO11','20':'GPIO12','21':'GPIO13','22':'GPIO14','23':'GPIO21','24':'GPIO47','25':'GPIO48','26':'GPIO45',
         '27':'GPIO0','28':'GPIO35','29':'GPIO36','30':'GPIO37','31':'GPIO38','32':'GPIO39','33':'GPIO40','34':'GPIO41','35':'GPIO42','36':'U0RXD','37':'U0TXD','38':'GPIO2','39':'GPIO1'}),
 'J1':dict(sym='Connector_Generic:Conn_01x22',value='Header 22',fp='Connector_PinHeader_2.54mm:PinHeader_1x22_P2.54mm_Vertical',pins=hdr(J1)),
 'J2':dict(sym='Connector_Generic:Conn_01x22',value='Header 22',fp='Connector_PinHeader_2.54mm:PinHeader_1x22_P2.54mm_Vertical',pins=hdr(J2)),
}
# parts that are not on the top-side drawing of the maker's layout: bottom side, position unknown (TBR)
BOTTOM_TBR=['R11','USB-OTG','USB-JTAG']
