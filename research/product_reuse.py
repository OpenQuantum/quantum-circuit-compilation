"""Reuse quantum wires with symbolically identical Boolean products.

Hash-consed XOR/AND expressions prove identities; expression growth is capped by
forgetting correlations (fresh symbols), never by assuming a value is zero.
Measurement/reset phase instructions are retained without modification.
"""
from collections import Counter, defaultdict
import argparse,json,re
from pathlib import Path
import kickmix as km

def rewrite(lines):
    nodes=[('const',0),('const',1)];intern={};q={};b={};holders=defaultdict(set);stack=[];stats=Counter();out=[]
    def fresh(): nodes.append(('var',len(nodes)));return len(nodes)-1
    def operation(kind,args):
        if kind=='xor':
            terms=set()
            for x in args:
                for t in (nodes[x][1] if nodes[x][0]=='xor' else (() if x==0 else (x,))):
                    if t in terms:terms.remove(t)
                    else:terms.add(t)
            if not terms:return 0
            if len(terms)==1:return next(iter(terms))
        else:
            terms=set()
            for x in args:
                if x==0:return 0
                if x==1:continue
                terms.update(nodes[x][1] if nodes[x][0]=='and' else (x,))
            if not terms:return 1
            if len(terms)==1:return next(iter(terms))
        if len(terms)>96:return fresh()
        key=(kind,tuple(sorted(terms)))
        if key not in intern:intern[key]=len(nodes);nodes.append(key)
        return intern[key]
    def get(w):
        table=q if w.startswith('q') else b
        if w not in table:
            table[w]=fresh()
            if w.startswith('q'):holders[table[w]].add(w)
        return table[w]
    def put(w,value):
        table=q if w.startswith('q') else b
        if w in table and w.startswith('q'):holders[table[w]].discard(w)
        table[w]=value
        if w.startswith('q'):holders[value].add(w)
    for lineno,line in enumerate(lines):
        t=line.split()
        if not t:out.append(line);continue
        op=t[0]
        if op in {'APPEND_TO_REGISTER','REGISTER','INFO_TAG','DEBUG_PRINT'}:out.append(line);continue
        suffix=[]
        if 'if' in t:
            ix=t.index('if');suffix=t[ix:];t=t[:ix]
        condition=operation('and',stack+([get(suffix[1])] if suffix else []))
        if op=='PUSH_CONDITION':stack.append(get(suffix[1] if suffix else t[1]));out.append(line);continue
        if op=='POP_CONDITION':stack.pop();out.append(line);continue
        wires=t[1:]
        if op in {'X','CX','CCX'}:
            controls=wires[:-1];target=wires[-1]
            product=operation('and',[get(w) for w in controls])
            replacement=None
            if op=='CCX':
                if product==0:replacement=[]
                elif product==1:replacement=['X '+target]
                else:
                    available=holders[product]-{target}
                    if available:replacement=['CX '+sorted(available)[0]+' '+target]
                if replacement is not None:
                    stats['removed_CCX']+=1
                    line='\n'.join(s+(' '+' '.join(suffix) if suffix else '') for s in replacement)
            put(target,operation('xor',[get(target),operation('and',[condition,product])]))
        elif op in {'R','HMR'}:
            target=wires[0]
            put(target,operation('xor',[get(target),operation('and',[condition,get(target)])])) if condition!=1 else put(target,0)
            if op=='HMR':put(wires[1],fresh())
        elif op=='CCZ':
            replacement=None
            full=operation('and',[get(w) for w in wires])
            if full==0:replacement=''
            elif full==1:replacement='NEG'
            elif holders[full]:replacement='Z '+sorted(holders[full])[0]
            else:
                for i,j,k in [(0,1,2),(0,2,1),(1,2,0)]:
                    pair=operation('and',[get(wires[i]),get(wires[j])])
                    if holders[pair]:
                        v=sorted(holders[pair])[0]
                        replacement=('Z '+v) if v==wires[k] else ('CZ '+v+' '+wires[k])
                        break
            if replacement is not None:
                stats['removed_CCZ']+=1
                line=replacement+(' '+' '.join(suffix) if suffix and replacement else '')
        elif op in {'Z','CZ','Z_POW','NEG'}:pass
        elif op in {'BIT_STORE0','BIT_STORE1','BIT_INVERT'}:
            w=wires[0];old=get(w)
            new=1 if op=='BIT_STORE1' else 0
            if op=='BIT_INVERT':put(w,operation('xor',[old,condition]))
            else:put(w,operation('xor',[old,operation('and',[condition,operation('xor',[old,new])])]))
        elif op in {'SWAP','CSWAP'}:
            c=condition if op=='SWAP' else operation('and',[condition,get(wires[0])])
            a,z=wires[-2:];x,y=get(a),get(z);delta=operation('and',[c,operation('xor',[x,y])])
            put(a,operation('xor',[x,delta]));put(z,operation('xor',[y,delta]))
        else:
            for w in wires:
                if re.fullmatch('[qb][0-9]+',w):put(w,fresh())
        if line:out.extend(line.splitlines())
    assert not stack
    return out,dict(stats)

def main():
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('output');a=p.parse_args()
    c=km.Circuit.from_file(a.source,format=Path(a.source).suffix[1:]);lines,stats=rewrite(str(c).splitlines())
    d=km.Circuit('\n'.join(lines)+'\n');out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);d.to_file(out,format='kmb')
    record={'source':a.source,'output':a.output,'rewrites':stats,'before':str(c.operation_statistics()),'after':str(d.operation_statistics())}
    out.with_suffix('.rewrite.json').write_text(json.dumps(record,indent=2));print(json.dumps(record))
if __name__=='__main__':main()
