import cv2, numpy as np, json, re
IM=''+REPO+'/examples/esp32-s3-board/aoi/D01_top.jpg'
im=cv2.imread(IM)
hsv=cv2.cvtColor(im,cv2.COLOR_BGR2HSV)
mask=((hsv[...,0]>75)&(hsv[...,0]<105)&(hsv[...,1]>60)&(hsv[...,2]>70)).astype(np.uint8)
det={}
for name,(ya,yb) in {'J1':(476,520),'J2':(1228,1276)}.items():
    strip=np.zeros_like(mask); strip[ya:yb,60:1740]=mask[ya:yb,60:1740]
    strip=cv2.morphologyEx(strip,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
    n,lab,st,cen=cv2.connectedComponentsWithStats(strip)
    comps=sorted((float(cen[i][0]),float(cen[i][1])) for i in range(1,n) if 120<st[i][4]<800)
    assert len(comps)==22,(name,len(comps))
    # sorted by x ascending: photo x ascending = pin 22..1
    for k,c in enumerate(comps): det[f'{name}.{22-k}']=c
def board(pin):
    j,n=pin.split('.'); n=int(n)
    return (57.15-(1.91+2.54*(n-1)), 1.27 if j=='J1' else 26.67)
FIT=['J1.1','J1.22','J2.1','J2.22']
def homog(fit):
    src=np.array([board(p) for p in fit],np.float32); dst=np.array([det[p] for p in fit],np.float32)
    H,_=cv2.findHomography(src,dst,0)   # board mm -> photo px
    return H
def proj(H,pts):
    p=np.array(pts,np.float64).reshape(-1,1,2); return cv2.perspectiveTransform(p,H).reshape(-1,2)
if __name__=='__main__':
    H=homog(FIT); Hi=np.linalg.inv(H)
    test=[p for p in det if p not in FIT]
    px=np.array([det[p] for p in test]); mm=proj(Hi,px); ref=np.array([board(p) for p in test])
    err=np.linalg.norm(mm-ref,axis=1)
    print('fit pads',FIT); print('held-out',len(test),'mean %.3f rms %.3f max %.3f mm'%(err.mean(),np.sqrt((err**2).mean()),err.max()))
    for p,e in sorted(zip(test,err),key=lambda t:-t[1])[:5]: print(p,round(e,3))
    json.dump({'H':H.tolist(),'det':det,'fit':FIT,'err_mean':float(err.mean()),'err_rms':float(np.sqrt((err**2).mean())),'err_max':float(err.max()),'n_test':len(test)},open('gen/homog.json','w'))
    # rectified image: board x in [-8,66], y in [-5,33], 24 px/mm
    S=24; x0,y0,x1,y1=-8,-5,67,33
    A=np.array([[S,0,-x0*S],[0,S,-y0*S],[0,0,1]])
    out=cv2.warpPerspective(im,A@Hi,(int((x1-x0)*S),int((y1-y0)*S)),flags=cv2.INTER_CUBIC)
    cv2.imwrite('rect.png',out); print(out.shape)
