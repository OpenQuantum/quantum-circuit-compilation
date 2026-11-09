"""Conservative, basis-independent commuting cancellation and XOR factoring.

Every applied identity is valid on arbitrary inputs. Measurements and control-stack
changes are barriers. No sampling, seed manipulation, or verifier changes.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import kickmix as km

XOR = {'X': 0, 'CX': 1, 'CCX': 2}
DIAG = {'Z': 1, 'CZ': 2, 'CCZ': 3}

def parse(line):
    t = line.split()
    if not t or t[0] not in set(XOR) | set(DIAG):
        return None
    op = t[0]
    n = XOR[op]+1 if op in XOR else DIAG[op]
    qs = tuple(t[1:n+1]); suffix = tuple(t[n+1:])
    if len(qs) != n or len(set(qs)) != n or not all(q.startswith('q') for q in qs):
        return None
    if suffix and (len(suffix) != 2 or suffix[0] != 'if' or not suffix[1].startswith('b')):
        return None
    return op, qs, suffix

def commute(a, b):
    if a is None or b is None:
        return False
    oa, qa, _ = a; ob, qb, _ = b
    if oa in DIAG and ob in DIAG:
        return True
    if oa in XOR and ob in XOR:
        return qa[-1] not in qb[:-1] and qb[-1] not in qa[:-1]
    if oa in DIAG:
        return qb[-1] not in qa
    return qa[-1] not in qb

def signature(g):
    op, qs, suffix = g
    return (op, tuple(sorted(qs[:-1])) + qs[-1:] if op in XOR else tuple(sorted(qs)), suffix)

def factor(a, b):
    oa, qa, sa = a; ob, qb, sb = b
    if oa != ob or sa != sb or oa not in {'CCX', 'CCZ'}:
        return None
    if oa == 'CCX':
        if qa[-1] != qb[-1]: return None
        common = set(qa[:-1]) & set(qb[:-1])
        if len(common) != 1: return None
        u = next(iter(common)); v = next(q for q in qa[:-1] if q != u); w = next(q for q in qb[:-1] if q != u)
        middle = f'CCX {u} {v} {qa[-1]}'
    else:
        common = set(qa) & set(qb)
        if len(common) != 2: return None
        u, z = sorted(common); v = next(q for q in qa if q not in common); w = next(q for q in qb if q not in common)
        middle = f'CCZ {u} {z} {v}'
    suffix = (' ' + ' '.join(sa)) if sa else ''
    return [f'CX {w} {v}' + suffix, middle + suffix, f'CX {w} {v}' + suffix]

def optimize(lines, window=256):
    stats = Counter()
    for iteration in range(6):
        changed = False
        parsed = [parse(s) for s in lines]
        for i, a in enumerate(parsed):
            if a is None or lines[i] is None: continue
            for j in range(i+1, min(len(lines), i+window+1)):
                if lines[j] is None: continue
                b = parsed[j]
                if b is None: break
                if signature(a) == signature(b):
                    lines[i] = lines[j] = None
                    stats['cancel_' + a[0]] += 1; changed = True; break
                replacement = factor(a, b)
                if replacement:
                    lines[i] = None; lines[j] = replacement
                    parsed[j] = None
                    stats['factor_' + a[0]] += 1; changed = True; break
                if not commute(a, b): break
        lines = [part for line in lines if line is not None for part in (line if isinstance(line,list) else [line])]
        if not changed: break
    return lines, dict(stats)

def main():
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('output');a=p.parse_args()
    source=Path(a.source);output=Path(a.output)
    circuit=km.Circuit.from_file(source, format=source.suffix[1:])
    lines=str(circuit).splitlines()
    result, stats=optimize(lines)
    rewritten=km.Circuit('\n'.join(result)+'\n')
    output.parent.mkdir(parents=True,exist_ok=True);rewritten.to_file(output,format='kmb')
    receipt={'source':str(source),'output':str(output),'rewrites':stats,'before':str(circuit.operation_statistics()),'after':str(rewritten.operation_statistics())}
    output.with_suffix('.rewrite.json').write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt))

if __name__=='__main__': main()
