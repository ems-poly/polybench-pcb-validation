"""Minimal KiCad s-expression reader/writer."""
import re
_tok=re.compile(r'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+)')
class Q(str):
    """quoted string"""
def parse(text):
    pos=0; stack=[[]]
    for m in _tok.finditer(text):
        t=m.group(1)
        if t=='(':
            stack.append([])
        elif t==')':
            x=stack.pop(); stack[-1].append(x)
        elif t[0]=='"':
            stack[-1].append(Q(t[1:-1].replace('\\"','"').replace('\\\\','\\')))
        else:
            stack[-1].append(t)
    return stack[0][0] if len(stack[0])==1 else stack[0]
def find(node,name):
    return [c for c in node if isinstance(c,list) and c and c[0]==name]
def dump(node,indent=0,inline=False):
    pad='\t'*indent
    if isinstance(node,Q):
        return '"'+str(node).replace('\\','\\\\').replace('"','\\"')+'"'
    if isinstance(node,str): return node
    # list
    simple=all(not isinstance(c,list) for c in node)
    if simple or inline:
        return '('+' '.join(dump(c,0,True) for c in node)+')'
    out='('+(dump(node[0]) if not isinstance(node[0],list) else '')
    parts=[]
    for c in node[1:]:
        if isinstance(c,list) and not all(not isinstance(x,list) for x in c):
            out+='\n'+'\t'*(indent+1)+dump(c,indent+1)
        else:
            out+=' '+dump(c,0,True)
    out+='\n'+pad+')' if any(isinstance(c,list) and not all(not isinstance(x,list) for x in c) for c in node[1:]) else ')'
    return out
